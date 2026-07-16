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
from zoneinfo import ZoneInfo

import config
import data_handler
import execution_gate
import indicators
import kill_switch
import logging_handler as log
import recommendation_engine as engine
import recommendation_settle as settle
import sector_data
import session as session_model
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
    """The frozen ENTER candidate list: the Scorecard's own worst-signal
    SUITABILITY (GO) subset + Level 5. The recommendation engine layers its own
    Level-1 regime check on top (_entry_blocked), so this keys off the
    regime-unaware CFM-suitability signal (`suitability`), not the regime-aware
    scan `verdict`, to avoid double-counting the regime. Uses the memoized sweep —
    a cold cache computes once and is shared with the Scan tab."""
    try:
        import account_gate
        from metrics import scorecard as scorecard_metrics
        sc = scorecard_metrics.scorecard(None)
        go_rows = [r for r in sc.get("results", []) if r.get("suitability") == "GO"]
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
                "ticker": t, "verdict": r.get("suitability"),
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


def _notify(new_recs: list[dict], staged: dict, state: dict, dry_run: bool | None) -> None:
    """Push newly emitted ACTIONABLE recommendations through the existing
    notifier channels. The ALERT ALWAYS FIRES — a settle-deferred rec is not
    suppressed; instead its copy states the window ("Defense staged, executable
    10:00 ET (9:00 CT)"), converting the alert from a fire alarm into a planning
    input (Design §7). Dedup is inherent: the engine emits a given claim once per
    validity window, so a repeat pass re-sends nothing."""
    import notifier
    from urllib.parse import quote
    actionable = [r for r in new_recs if r.get("action_type") != ActionType.NO_ACTION]
    if not actionable:
        return
    staged = staged or {}
    settings = (state.get("alerts") or {}).get("settings") or {}
    if dry_run is None:
        dry_run = bool(settings.get("dry_run", config.alerts_dry_run_default()))
    batch = []
    for r in actionable:
        t = r.get("ticker") or ""
        ticket = r.get("proposed_ticket") or {}
        net = (ticket.get("estimates") or {}).get("net_per_share")
        est = f", est net {net:+.2f}/sh" if isinstance(net, (int, float)) else ""
        sb = staged.get(r.get("rec_id"))
        if sb:
            when = _fmt_dual_tz(settle.parse_ts(sb.get("executable_at")))
            reason = sb.get("reason")
            message = (f"{t}: {r.get('action_type')} staged ({r.get('trigger_rule')}) — "
                       f"executable {when}{est}. {reason} — the opening range is forming.")
            action = (f"Open the {t} position card to pre-approve or dismiss "
                      f"(rec {r.get('rec_id')}, executable {when}).")
        else:
            message = (f"{t}: {r.get('action_type')} recommended ({r.get('trigger_rule')}){est}")
            action = (f"Open the {t} position card to execute or dismiss "
                      f"(rec {r.get('rec_id')}, valid until {r.get('valid_until')}).")
        batch.append({
            "type": "RECOMMENDATION",
            "severity": _SEVERITY.get(r.get("action_type"), "MEDIUM"),
            "rule": f"trigger {r.get('trigger_rule')}",
            "ticker": t,
            "message": message,
            "action": action,
            "data": {"rec_id": r.get("rec_id"), "action_type": r.get("action_type"),
                     "trigger_rule": r.get("trigger_rule"),
                     "settle_status": (sb or {}).get("status"),
                     "executable_at": (sb or {}).get("executable_at")},
            "fingerprint": f"RECOMMENDATION|{t}|{r.get('rec_id')}",
            "action_url": f"/?action=focus&ticker={quote(t)}" if t else None,
        })
    try:
        notifier.dispatch(batch, settings, dry_run=dry_run)
    except Exception:  # noqa: BLE001 — delivery failure never fails the pass
        logger.exception("recommendation notification dispatch failed")


# ---------------------------------------------------------------------------
# Market-settle deferral (PENDING_SETTLE) — staging + release re-validation
# ---------------------------------------------------------------------------
def _coerce_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def _fmt_dual_tz(dt: datetime | None) -> str:
    """"10:00 ET (9:00 CT)" — the settle-window time in Eastern plus the operator's
    local zone, so a phone alert is a planning input, not a fire alarm."""
    if dt is None:
        return "the next session"
    try:
        et = dt.astimezone(session_model.ET)
        op = dt.astimezone(ZoneInfo(config.OPERATOR_TZ))
        op_abbr = op.tzname() or "local"
        return f"{et.strftime('%-H:%M')} ET ({op.strftime('%-H:%M')} {op_abbr})"
    except Exception:  # noqa: BLE001
        return dt.isoformat()


def _gap_from_market(rec: dict, market: dict, sess) -> execution_gate.GapContext:
    """Gap-emergency inputs for a DEFENSE/EXIT from the frozen market snapshot (no
    extra reads). Fail-closed: opening-range break unavailable (False), print
    duration proxied by elapsed session time, all CFM orders are LIMIT."""
    tk = (market.get("tickers") or {}).get((rec.get("ticker") or "").upper()) or {}
    prior_close, atr, cur = tk.get("last_close"), tk.get("atr"), tk.get("price")
    adverse = None
    if prior_close is not None and atr and atr > 0 and cur is not None:
        adverse = max(0.0, float(prior_close) - float(cur)) / float(atr)
    return execution_gate.GapContext(
        adverse_gap_atr=adverse, broke_opening_range_low=False,
        two_sided_print_minutes=sess.minutes_since_open, is_limit_order=True)


def _verdict_for_rec(rec: dict, market: dict, now: datetime):
    """The gate verdict for a recommendation at ``now`` (None for non-actionable)."""
    gate_action = settle.gate_action_for(rec)
    if gate_action is None:
        return None
    sess = session_model.session_state(now)
    gap = None
    if (gate_action in (execution_gate.GateAction.DEFENSE, execution_gate.GateAction.EXIT_KILL)
            and sess.is_open
            and (sess.minutes_since_open or 0.0) < config.MARKET_SETTLE_MINUTES):
        gap = _gap_from_market(rec, market, sess)
    return execution_gate.execution_window(gate_action, now, sess, gap)


def _stage_new(stored: list[dict], market: dict, now: datetime) -> dict:
    """Stage each newly-emitted ACTIONABLE rec that falls in a blocked window as
    PENDING_SETTLE (carrying executable_at). Returns {rec_id: settle_block} for the
    ones staged, so the notifier can render window-aware copy."""
    ids = {r.get("rec_id") for r in stored
           if r.get("action_type") != ActionType.NO_ACTION}
    if not ids:
        return {}
    state = log.load_state()
    staged: dict = {}
    for rec in state.get("recommendations", []):
        if rec.get("rec_id") in ids and not rec.get("settle"):
            verdict = _verdict_for_rec(rec, market, now)
            if settle.stage(rec, verdict, now):
                staged[rec["rec_id"]] = rec["settle"]
    if staged:
        log.save_state(state)
    return staged


def _trigger_still_holds(rec: dict, market: dict, state: dict, now: datetime):
    """Re-validate a pending rec's trigger at release. Re-runs the pure engine with
    no open recs (so its dedup can't suppress a still-firing trigger) and checks the
    same action_type re-emerges for the position. Returns True (holds), False
    (cleared — e.g. the gap filled, or the confirmed-close breach recovered), or
    None (could not evaluate → don't self-cancel, but don't auto-submit either)."""
    try:
        fresh = engine.evaluate(market, state, now, open_recs=[])
    except Exception:  # noqa: BLE001
        return None
    action = rec.get("action_type")
    pid, tkr = rec.get("position_id"), (rec.get("ticker") or "").upper()
    for r in fresh:
        same_action = r.get("action_type") == action
        same_pos = (pid and r.get("position_id") == pid) or (r.get("ticker") or "").upper() == tkr
        if same_action and same_pos:
            return True
    return False


def release_pending(now: datetime | None = None, market: dict | None = None,
                    notify: bool = True, dry_run: bool | None = None,
                    submit_fn=None) -> dict:
    """Process PENDING_SETTLE recs whose window has opened. Each is re-validated
    against a fresh snapshot: still-firing -> RELEASED (and, if pre-approved,
    auto-submitted via ``submit_fn``); cleared -> SELF_CANCELED (with a
    notification); stale past validity -> EXPIRED. All transitions append to the
    record. Deterministic given ``now``."""
    now = _coerce_now(now)
    state = log.load_state()
    due = settle.due(state, now)
    summary = {"released": 0, "self_canceled": 0, "expired": 0, "executed": 0}
    if not due:
        return summary
    if market is None:
        market = build_market_snapshot(state, include_entry=False)
    events: list[tuple[dict, str]] = []
    for rec in due:
        if settle.is_expired(rec, now):
            settle.mark(rec, settle.SettleStatus.EXPIRED, now,
                        "validity window elapsed before the settle window opened")
            summary["expired"] += 1
            events.append((rec, settle.SettleStatus.EXPIRED))
            continue
        holds = _trigger_still_holds(rec, market, state, now)
        if holds is False:
            settle.mark(rec, settle.SettleStatus.SELF_CANCELED, now,
                        "trigger no longer valid at release — condition cleared "
                        "(e.g. the gap filled / stock recovered above the strike)")
            summary["self_canceled"] += 1
            events.append((rec, settle.SettleStatus.SELF_CANCELED))
            continue
        note = ("released after the settle window; trigger re-validated"
                if holds else "released; trigger could not be re-validated — confirm manually")
        settle.mark(rec, settle.SettleStatus.RELEASED, now, note)
        summary["released"] += 1
        if holds and rec["settle"].get("pre_approved") and submit_fn is not None:
            try:
                submit_fn(rec, now)
                settle.mark(rec, settle.SettleStatus.EXECUTED, now,
                            "auto-submitted on release (pre-approved, trigger re-validated)")
                summary["executed"] += 1
                events.append((rec, settle.SettleStatus.EXECUTED))
                continue
            except Exception as e:  # noqa: BLE001 — a submit failure never loses the record
                settle.mark(rec, settle.SettleStatus.RELEASED, now,
                            f"pre-approved auto-submit failed ({e}); confirm manually")
        events.append((rec, settle.SettleStatus.RELEASED))
    log.save_state(state)
    if notify and events:
        _notify_settle(events, state, dry_run)
    return summary


def _notify_settle(events: list[tuple[dict, str]], state: dict,
                   dry_run: bool | None) -> None:
    """Push release-pass outcomes (self-cancel / released / expired) — so a defense
    that self-cancels because the gap filled tells the operator so, per Design §6."""
    import notifier
    from urllib.parse import quote
    settings = (state.get("alerts") or {}).get("settings") or {}
    if dry_run is None:
        dry_run = bool(settings.get("dry_run", config.alerts_dry_run_default()))
    _copy = {
        settle.SettleStatus.SELF_CANCELED: ("MEDIUM", "self-canceled — trigger cleared before the window opened"),
        settle.SettleStatus.RELEASED: ("MEDIUM", "released — the settle window has opened; execute or dismiss"),
        settle.SettleStatus.EXECUTED: ("HIGH", "auto-submitted on release (pre-approved)"),
        settle.SettleStatus.EXPIRED: ("LOW", "expired before the settle window opened"),
    }
    batch = []
    for rec, status in events:
        sev, tail = _copy.get(status, ("MEDIUM", status))
        t = rec.get("ticker") or ""
        batch.append({
            "type": "RECOMMENDATION_SETTLE",
            "severity": sev,
            "rule": f"settle {status}",
            "ticker": t,
            "message": f"{t}: {rec.get('action_type')} {tail}.",
            "action": f"Open the {t} position card (rec {rec.get('rec_id')}).",
            "data": {"rec_id": rec.get("rec_id"), "settle_status": status,
                     "action_type": rec.get("action_type")},
            "fingerprint": f"RECOMMENDATION_SETTLE|{t}|{rec.get('rec_id')}|{status}",
            "action_url": f"/?action=focus&ticker={quote(t)}" if t else None,
        })
    try:
        notifier.dispatch(batch, settings, dry_run=dry_run)
    except Exception:  # noqa: BLE001
        logger.exception("settle notification dispatch failed")


def run(notify: bool = True, include_entry: bool = True,
        dry_run: bool | None = None, now: datetime | None = None) -> dict:
    """One scheduled/manual evaluation pass. Returns a summary; the emitted
    records live in state.recommendations (append-only). ``now`` is injectable for
    tests; production defaults to the wall clock."""
    global _last_run
    with _run_lock:
        now = _coerce_now(now)
        # 1) Release any PENDING_SETTLE recs whose window has now opened (re-validates
        #    the trigger against a fresh snapshot; self-cancels a filled gap).
        release_summary = release_pending(now=now, notify=notify, dry_run=dry_run)
        # 2) Evaluate fresh (state may have been mutated by the release pass).
        state = log.load_state()
        # Reconciliation freeze gate (spec §5): while the book diverges from the
        # broker (or holds an unbalanced leg), NO recommendations are generated —
        # acting on unverified state is exactly the failure mode reconciliation
        # exists to prevent. The app surfaces the freeze and waits for a human; it
        # never auto-remediates. Release of already-open PENDING_SETTLE recs above
        # still runs (closing/settling an existing rec is safe).
        import reconcile
        freeze = reconcile.freeze_status(state)
        if freeze["frozen"]:
            _last_run = {
                "at": log.utcnow(),
                "positions_evaluated": 0,
                "emitted": 0, "emitted_ids": [],
                "reconcile_frozen": True,
                "frozen_tickers": freeze["tickers"],
                "freeze_reason": freeze["reason"],
                "released": release_summary,
            }
            logger.warning("recommendation pass SKIPPED — reconciliation freeze: %s",
                           freeze["tickers"])
            return _last_run
        market = build_market_snapshot(state, include_entry=include_entry)
        open_recs = trust_derive.open_recommendations(state, now)
        new_recs = engine.evaluate(market, state, now, open_recs)
        stored = log.append_recommendations(new_recs)
        # 3) Stage newly-emitted recs that land in a blocked window as PENDING_SETTLE.
        staged = _stage_new(stored, market, now)
        # 4) Notify actionable recs, settle-aware (staged ones say "executable …").
        if notify and stored:
            _notify(stored, staged, state, dry_run)
        _last_run = {
            "at": log.utcnow(),
            "positions_evaluated": sum(1 for p in state.get("positions", [])
                                       if p.get("status") != "closed"),
            "entry_candidates": len(market.get("entry_candidates") or []),
            "open_before": len(open_recs),
            "emitted": len(stored),
            "emitted_ids": [r.get("rec_id") for r in stored],
            "staged_pending": len(staged),
            "released": release_summary,
        }
        logger.info("recommendation pass: %s", _last_run)
        return _last_run


def last_run() -> dict | None:
    return _last_run
