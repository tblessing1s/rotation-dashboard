"""Recommendation runner — the IMPURE shell around the pure engine.

This module owns everything the engine is forbidden to touch: provider reads,
the real clock, state.json, and notification delivery. It freezes one market
snapshot per pass, hands it to recommendation_engine.evaluate() (the exact code
path a future automation switch would call), appends whatever the engine
emitted, and pushes actionable recommendations through the existing notifier.

Scheduled by alert_scheduler at the same ET slots as the alert pass (including
the 16:15 post-close slot, which is what makes the kill switch's
confirmed-close rule evaluable same-day); manual trigger via
POST /api/recommendations/run. Every pass either emits new records or confirms
the open ones still stand — per-position silence is impossible by construction
(the engine emits explicit ALL_CLEAR records).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

import config
import data_handler
import indicators
import kill_switch
import logging_handler as log
import recommendation_engine as engine
import sector_data
import strike_policy
import trust_derive
from rec_types import ActionType

logger = logging.getLogger("cfm.recommendations")

_run_lock = threading.Lock()
_last_run: dict | None = None

_SEVERITY = {
    ActionType.EXIT: "CRITICAL",
    ActionType.DEFEND: "HIGH",
    ActionType.ROLL_OUT: "MEDIUM",
    ActionType.ROLL_DOWN: "MEDIUM",
    ActionType.ENTER: "MEDIUM",
}


def _ticker_snapshot(ticker: str, position: dict | None, q_pair, price, bars,
                     spy_bars) -> dict:
    """Freeze one ticker's evaluation inputs. Best-effort per field — a missing
    provider value becomes None (the engine treats None as 'cannot assess'),
    never an exception out of the pass."""
    import dividends
    import earnings as earnings_mod
    tk: dict = {"bars": bars, "spy_bars": spy_bars}
    try:
        tk["last_close"] = indicators.last(bars)
        tk["atr"] = indicators.atr(bars)
        tk["hist_vol"] = indicators.hist_vol(bars)
        tk["pct_above_ma21"] = indicators.pct_from_ma(bars, 21)
    except Exception:  # noqa: BLE001
        pass
    tk["price"] = price
    try:
        rs_spy, rs_sector = q_pair if q_pair is not None else kill_switch._rs_pair(ticker)
        tk["rs3m_vs_spy"], tk["rs3m_vs_sector"] = rs_spy, rs_sector
    except Exception:  # noqa: BLE001
        tk["rs3m_vs_spy"] = tk["rs3m_vs_sector"] = None
    try:
        etf = sector_data.sector_for(ticker)
        if etf and etf.upper() != ticker.upper():
            tk["sector_bars"] = data_handler.get_daily(etf)
    except Exception:  # noqa: BLE001
        pass
    try:
        tk["q"], tk["q_source"] = dividends.q_with_source(ticker)
    except Exception:  # noqa: BLE001
        tk["q"], tk["q_source"] = 0.0, "none"
    try:
        tk["earnings"] = earnings_mod.cached_earnings(ticker)
    except Exception:  # noqa: BLE001
        tk["earnings"] = None
    try:
        import iv_history
        tk["iv_rank"] = (iv_history.iv_rank(ticker) or {}).get("iv_rank")
    except Exception:  # noqa: BLE001
        tk["iv_rank"] = None
    if position is not None:
        try:
            import dividends
            import leap_policy
            lh = leap_policy.leap_health(position, df=bars, stock_price=price,
                                         q=tk.get("q") or 0.0)
            tk["juice"] = {
                "inadequate": lh.get("juice_adequate") is False,
                "yield_pct": lh.get("juice_yield_pct"),
                "target_pct": lh.get("juice_target_pct"),
                "maintenance_status": lh.get("maintenance_status"),
            }
        except Exception:  # noqa: BLE001
            tk["juice"] = None
    return tk


def _live_price(ticker: str) -> float | None:
    try:
        return data_handler.live_price(ticker)
    except Exception:  # noqa: BLE001
        try:
            quote = data_handler.latest_quote(ticker)
            return quote["price"] if quote else None
        except Exception:  # noqa: BLE001
            return None


def _entry_candidates(market: dict, spy_bars) -> list[dict]:
    """The frozen ENTER candidate list: the Scorecard's own worst-signal GO
    subset + Level 5, exactly what /api/scan/ready composes. Uses the memoized
    sweep — a cold cache computes once and is shared with the Scan tab."""
    try:
        import account_gate
        from metrics import scorecard as scorecard_metrics
        sc = scorecard_metrics.scorecard(None)
        go_rows = [r for r in sc.get("results", []) if r.get("verdict") == "GO"]
        if not go_rows:
            return []
        level5 = account_gate.evaluate_many([r["ticker"] for r in go_rows])
        out = []
        for r in go_rows:
            t = r["ticker"]
            bars = data_handler.get_daily(t)
            market["tickers"].setdefault(t.upper(), _ticker_snapshot(
                t, None, None, _live_price(t), bars, spy_bars))
            out.append({
                "ticker": t, "verdict": r.get("verdict"),
                "level5": level5.get(t),
                "juice_weekly_pct": r.get("juice_weekly_pct"),
                "blockers": [],
            })
        return out
    except Exception:  # noqa: BLE001 — a failed sweep never blocks position recs
        logger.exception("entry-candidate sweep failed; pass continues without ENTER")
        return []


def build_market_snapshot(state: dict, include_entry: bool = True) -> dict:
    """Gather every impure input the engine needs into one frozen snapshot."""
    import screening
    try:
        regime = screening.regime()
        regime_view = {"status": regime.get("published_regime") or regime.get("status"),
                       "lights": regime.get("lights")}
    except Exception:  # noqa: BLE001
        regime_view = {"status": None, "lights": None}
    market: dict = {
        "as_of": log.utcnow(),
        "regime": regime_view,
        "posture": strike_policy.get_posture(state),
        "tickers": {},
    }
    spy_bars = None
    try:
        spy_bars = data_handler.get_daily(config.BENCHMARK)
    except Exception:  # noqa: BLE001
        pass
    for p in state.get("positions", []):
        if p.get("status") == "closed":
            continue
        t = p.get("ticker", "")
        if not t:
            continue
        try:
            bars = data_handler.get_daily(t)
        except Exception:  # noqa: BLE001
            bars = None
        market["tickers"][t.upper()] = _ticker_snapshot(
            t, p, None, _live_price(t), bars, spy_bars)
    if include_entry:
        market["entry_candidates"] = _entry_candidates(market, spy_bars)
    return market


def _notify(new_recs: list[dict], state: dict, dry_run: bool | None) -> None:
    """Push newly emitted ACTIONABLE recommendations through the existing
    notifier channels. Dedup is inherent: the engine emits a given claim once
    per validity window, so a repeat pass re-sends nothing."""
    import notifier
    from urllib.parse import quote
    actionable = [r for r in new_recs if r.get("action_type") != ActionType.NO_ACTION]
    if not actionable:
        return
    settings = (state.get("alerts") or {}).get("settings") or {}
    if dry_run is None:
        dry_run = bool(settings.get("dry_run", config.alerts_dry_run_default()))
    batch = []
    for r in actionable:
        t = r.get("ticker") or ""
        ticket = r.get("proposed_ticket") or {}
        net = (ticket.get("estimates") or {}).get("net_per_share")
        batch.append({
            "type": "RECOMMENDATION",
            "severity": _SEVERITY.get(r.get("action_type"), "MEDIUM"),
            "rule": f"trigger {r.get('trigger_rule')}",
            "ticker": t,
            "message": (f"{t}: {r.get('action_type')} recommended "
                        f"({r.get('trigger_rule')})"
                        + (f", est net {net:+.2f}/sh" if isinstance(net, (int, float)) else "")),
            "action": (f"Open the {t} position card to execute or dismiss "
                       f"(rec {r.get('rec_id')}, valid until {r.get('valid_until')})."),
            "data": {"rec_id": r.get("rec_id"), "action_type": r.get("action_type"),
                     "trigger_rule": r.get("trigger_rule")},
            "fingerprint": f"RECOMMENDATION|{t}|{r.get('rec_id')}",
            "action_url": f"/?action=focus&ticker={quote(t)}" if t else None,
        })
    try:
        notifier.dispatch(batch, settings, dry_run=dry_run)
    except Exception:  # noqa: BLE001 — delivery failure never fails the pass
        logger.exception("recommendation notification dispatch failed")


def run(notify: bool = True, include_entry: bool = True,
        dry_run: bool | None = None) -> dict:
    """One scheduled/manual evaluation pass. Returns a summary; the emitted
    records live in state.recommendations (append-only)."""
    global _last_run
    with _run_lock:
        now = datetime.now(timezone.utc)
        state = log.load_state()
        market = build_market_snapshot(state, include_entry=include_entry)
        open_recs = trust_derive.open_recommendations(state, now)
        new_recs = engine.evaluate(market, state, now, open_recs)
        stored = log.append_recommendations(new_recs)
        if notify and stored:
            _notify(stored, state, dry_run)
        _last_run = {
            "at": log.utcnow(),
            "positions_evaluated": sum(1 for p in state.get("positions", [])
                                       if p.get("status") != "closed"),
            "entry_candidates": len(market.get("entry_candidates") or []),
            "open_before": len(open_recs),
            "emitted": len(stored),
            "emitted_ids": [r.get("rec_id") for r in stored],
        }
        logger.info("recommendation pass: %s", _last_run)
        return _last_run


def last_run() -> dict | None:
    return _last_run
