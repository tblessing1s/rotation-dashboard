"""Dry-powder cash-secured-put sleeve — SHADOW ONLY (TRAVIS_EXTENSION).

Short-duration premium collection on capital that cannot support a new full
CFM position. This is a SECOND, DISTINCT use of the put instrument from the
CSP ENTRY mechanism (`scan_verdict.route`, `executor._put_opened`):

                          CSP ENTRY                    CSP DRY-POWDER (here)
    purpose         timing delay on an entry    income on capital that can't
                     the strategy already wants  fund a new full position
    assignment       accepted / hoped for        an ACCIDENT — flagged for
                                                  manual review, never sought
    strike anchor    MA21 zone (price-based)      furthest-OTM strike inside a
                                                  delta band that clears a
                                                  yield floor (safety-first)
    sizing           the CFM lot-size rules       leftover cash only, never
                                                  the $13K reserve or the
                                                  $38K deployed cap
    code path        scan_verdict.py /            THIS module. No shared
                     option_chain.py / executor   state, no shared functions,
                                                  by deliberate design — see
                                                  the Phase 0 audit this
                                                  module was built from.

Both trade the same instrument, so keeping them textually separate (rather
than a shared "CSP" abstraction with a mode flag) is the point: a bug fixed in
one must never silently "fix" — or break — the other's opposite risk posture.

SHADOW ONLY: this module never places an order and never touches
`state.json` or `positions`. It logs every scanned candidate and every
would-be trade to its own append-only, per-day side channel (see STORAGE
below) — the same zero-authority pattern `gate_telemetry.py` uses, chosen
over a `state.json` schema bump for the same reason: nothing here is a real
position or a real execution, so it does not belong in the single source of
truth for the real book. A live order-placement phase, if ever built, is out
of scope for this change and would need its own reviewed design (mirroring
how `CSP_ORDER_PLACEMENT_ENABLED` gates the entry mechanism's Stage 3).

STORAGE — one file per scan day, `DATA_DIR/csp_dry_powder_log/YYYY-MM-DD.json`
--------------------------------------------------------------------------
Each day's file holds that day's `candidates` (every ticker the sweep looked
at, whether or not it qualified), that day's `shadow_trades` (the candidates
that cleared the yield floor and had capital to size), and any `outcomes`
resolved that day for PAST shadow trades whose expiration has passed
(`resolve_outcomes`). Outcomes are appended as their own dated record rather
than mutating the original day's file — no in-place mutation, ever, matching
the append-only discipline the rest of the app uses for real executions.

KNOWN PHASE-1 LIMITATION (flagged, not hidden): the hypothetical re-gate
`resolve_outcomes` runs on an assignment uses TODAY's live gate stack
(`screening.entry_gate`), not the gate stack AS IT STOOD on the historical
assignment date — the app has no point-in-time historical gate evaluator
anywhere, and building one is out of scope here. Read its
`hypothetical_gate_verdict` as "would this still look like an entry today",
not as a true backtest.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pandas as pd

import config
import data_handler
import indicators
import logging_handler as log
import market_calendar
import position_manager
import schwab_api
import weeklies

STRATEGY_TAG = "csp_dry_powder"

TIER_QUALITY = "quality"   # extended + not held + has weeklies + ELIGIBLE under
                           # today's live veto registry (scan_verdict.evaluate)
TIER_GENERAL = "general"   # extended + not held + has weeklies, gate status
                           # otherwise ignored (per spec: "regardless of gate
                           # status otherwise")

STORE_DIR = os.path.join(config.DATA_DIR, "csp_dry_powder_log")
SCHEMA_VERSION = 1
_lock = threading.RLock()


# ---------------------------------------------------------------------------
# Eligibility (§1) — cheap, pure given already-fetched inputs.
# ---------------------------------------------------------------------------
def ema_gap_pct(df: pd.DataFrame | None) -> dict:
    """Whether a name is "extended" for THIS sleeve: price above both the fast
    and slow EMA, and their gap (as % of price) at or above
    `config.DRY_POWDER_EMA_GAP_MIN_PCT`. A different "extended" than the CSP
    entry route's ATR-normalized MA21 distance — see module docstring."""
    price = indicators.last(df)
    fast = indicators.ema(df, config.DRY_POWDER_EMA_FAST)
    slow = indicators.ema(df, config.DRY_POWDER_EMA_SLOW)
    if price is None or fast is None or slow is None or price <= 0:
        return {"extended": False, "gap_pct": None, "ema_fast": fast,
                "ema_slow": slow, "price": price}
    gap_pct = (fast - slow) / price * 100
    extended = price > fast and price > slow and gap_pct >= config.DRY_POWDER_EMA_GAP_MIN_PCT
    return {"extended": extended, "gap_pct": round(gap_pct, 4), "ema_fast": round(fast, 4),
            "ema_slow": round(slow, 4), "price": round(price, 4)}


def eligibility(ticker: str, state: dict, df: pd.DataFrame | None) -> dict:
    """One candidate's base eligibility: not currently held (shares or an
    existing CSP), has weeklies, and extended per `ema_gap_pct`. Does NOT
    evaluate the live gate stack — that is layered separately in `scan`
    (only for names that clear this cheap filter first), the same two-pass
    shape `screening.entry_gate` uses for its own account-gate overlay."""
    ticker = ticker.upper()
    held = log.find_position(state, ticker)
    is_held = held is not None and held.get("status") != "closed"
    wk = weeklies.has_weeklies(ticker)
    ema = ema_gap_pct(df)

    reasons = []
    if is_held:
        reasons.append("already_held")
    if wk is False:
        reasons.append("no_weeklies")
    if wk is None:
        reasons.append("weeklies_unknown")
    if not ema["extended"]:
        reasons.append("not_extended")

    return {"ticker": ticker, "eligible": not reasons, "reasons": reasons,
            "is_held": is_held, "has_weeklies": wk, **ema}


# ---------------------------------------------------------------------------
# Pricing / strike selection (§2, §3, §4) — put-specific, delta-band driven.
# ---------------------------------------------------------------------------
def weekly_equivalent_yield_pct(premium_per_share: float | None, strike: float | None,
                                dte: int | None) -> float | None:
    """This contract's yield on collateral (strike x 100), NORMALIZED to a
    1-week basis: a 1-week contract's own yield; a 2-week contract's total
    yield divided by 2. Used both for expiration selection (§3) and, when
    annualized (x 365/7), for the yield-floor check (§4)."""
    if not (premium_per_share and strike and strike > 0 and dte and dte > 0):
        return None
    collateral = strike * config.SHARES_PER_LOT
    total_pct = premium_per_share * config.SHARES_PER_LOT / collateral * 100
    weeks = dte / 7.0
    return round(total_pct / weeks, 4)


def _fetch_put_contracts(ticker: str) -> tuple[float | None, list[dict]]:
    """Raw put chain for this sleeve, via the shared Schwab data layer only —
    deliberately NOT `option_chain.py`'s `put_chain()`/`_fetch_chain`, which
    is the CSP entry mechanism's own (price-anchored) picker. No cache: this
    runs once per scan sweep, not on a live-polling ticket."""
    today = datetime.now()
    to_date = (today + timedelta(days=config.PUT_MAX_DTE + 7)).strftime("%Y-%m-%d")
    payload = data_handler.client().get_option_chain(
        ticker, strike_count=100, from_date=today.strftime("%Y-%m-%d"), to_date=to_date)
    status = (payload or {}).get("status")
    if status and status != "SUCCESS":
        raise schwab_api.SchwabError(f"Schwab returned status '{status}' for {ticker}")
    return schwab_api.parse_put_chain(payload)


def _is_weekly_boundary(exp: str | None) -> bool:
    """Friday, or — on a holiday week — the Thursday before it. Small,
    deliberate duplication of `option_chain._is_weekly_boundary`: see the
    module docstring on why this sleeve does not import the CSP-entry
    module's (private) helpers."""
    if not exp:
        return False
    try:
        d = datetime.strptime(str(exp)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    if d.weekday() == 4:
        return not market_calendar.is_market_holiday(d)
    if d.weekday() == 3:
        return market_calendar.is_market_holiday(d + timedelta(days=1))
    return False


def _weekly_expirations(contracts: list[dict], count: int = 2) -> list[str]:
    dated = [c for c in contracts if c.get("dte") is not None and c["dte"] > 0]
    pool = [c for c in dated if _is_weekly_boundary(c.get("expiration"))] or dated
    by_exp: dict[str, int] = {}
    for c in pool:
        exp = c.get("expiration")
        if exp is not None:
            by_exp[exp] = min(by_exp.get(exp, c["dte"]), c["dte"])
    return sorted(by_exp, key=lambda e: by_exp[e])[:count]


def _put_delta(contract: dict, underlying: float | None) -> float | None:
    """|delta| for one put contract, recomputed via BSM (see
    `indicators.put_greeks` for why this is not Schwab's raw `delta` field)."""
    dte = contract.get("dte")
    strike = contract.get("strike")
    bid, ask = contract.get("bid"), contract.get("ask")
    mark = contract.get("mark")
    if mark is None and bid is not None and ask is not None:
        mark = (bid + ask) / 2
    delta, _iv = indicators.put_greeks(underlying, strike, dte, mark,
                                       reported_iv=contract.get("volatility"))
    return abs(delta) if delta is not None else None


def _evaluate_strikes_in_expiration(contracts: list[dict], underlying: float | None,
                                    dte: int) -> list[dict]:
    """EVERY put contract at this expiration with a computable delta — the
    full §8 telemetry row set: delta-band pass/fail and yield-floor pass/fail
    per strike, not just the eventual winner. Order is source order; callers
    pick the winner separately (`_select_strike_in_expiration`)."""
    rows = []
    for c in contracts:
        if c.get("dte") != dte:
            continue
        strike = c.get("strike")
        bid, ask = c.get("bid"), c.get("ask")
        mark = c.get("mark")
        if mark is None and bid is not None and ask is not None:
            mark = (bid + ask) / 2
        if not mark or not strike:
            continue
        d = _put_delta(c, underlying)
        in_band = d is not None and (config.DRY_POWDER_PUT_DELTA_MIN <= d <= config.DRY_POWDER_PUT_DELTA_MAX)
        weekly_pct = weekly_equivalent_yield_pct(mark, strike, dte)
        annualized_pct = round(weekly_pct * (365.0 / 7.0), 2) if weekly_pct is not None else None
        clears_floor = (annualized_pct is not None
                        and annualized_pct >= config.DRY_POWDER_YIELD_FLOOR_ANNUALIZED_PCT)
        rows.append({"strike": strike, "dte": dte, "expiration": c.get("expiration"),
                    "premium_per_share": mark, "abs_delta": round(d, 4) if d is not None else None,
                    "in_delta_band": in_band,
                    "weekly_equivalent_yield_pct": weekly_pct,
                    "annualized_yield_pct": annualized_pct,
                    "clears_yield_floor": clears_floor,
                    "bid": bid, "ask": ask})
    return rows


def _select_strike_in_expiration(contracts: list[dict], underlying: float | None,
                                 dte: int) -> dict | None:
    """The furthest-OTM (lowest |delta|) strike within the configured delta
    band that still clears the annualized yield floor (§2, §4). None when no
    strike in the band clears the floor — logged upstream as
    "scanned, no qualifying strike" (§2)."""
    evaluated = _evaluate_strikes_in_expiration(contracts, underlying, dte)
    qualifying = [r for r in evaluated if r["in_delta_band"] and r["clears_yield_floor"]]
    if not qualifying:
        return None
    return min(qualifying, key=lambda r: r["abs_delta"])


def evaluate_candidate(ticker: str) -> dict:
    """Full evaluation for one ticker, for both selection and telemetry (§8):
    every strike looked at across the next two weekly expirations
    (`evaluated_by_expiration`), and the chosen `winner` (or None — "scanned,
    no qualifying strike", §2)."""
    underlying, contracts = _fetch_put_contracts(ticker)
    evaluated_by_expiration: dict[str, list[dict]] = {}
    if not contracts:
        return {"underlying": underlying, "evaluated_by_expiration": {}, "winner": None}
    exps = _weekly_expirations(contracts, count=2)
    by_exp_winner: dict[str, dict] = {}
    for exp in exps:
        exp_contracts = [c for c in contracts if c.get("expiration") == exp]
        dte = exp_contracts[0]["dte"] if exp_contracts else None
        if dte is None:
            continue
        evaluated_by_expiration[exp] = _evaluate_strikes_in_expiration(exp_contracts, underlying, dte)
        picked = _select_strike_in_expiration(exp_contracts, underlying, dte)
        if picked is not None:
            by_exp_winner[exp] = picked
    winner = _choose_expiration(by_exp_winner)
    return {"underlying": underlying, "evaluated_by_expiration": evaluated_by_expiration,
            "winner": winner}


def select_strike_and_expiration(ticker: str) -> dict | None:
    """The chosen (would-be) trade for one ticker — the `winner` half of
    `evaluate_candidate`, for callers that don't need the full telemetry."""
    return evaluate_candidate(ticker)["winner"]


def _choose_expiration(by_exp: dict[str, dict]) -> dict | None:
    """Between expirations that both cleared the floor (§3): the higher
    weekly-equivalent yield; within tolerance of each other, the SHORTER
    duration (preserves the weekly reassessment option)."""
    if not by_exp:
        return None
    if len(by_exp) == 1:
        return next(iter(by_exp.values()))
    ranked = sorted(by_exp.values(), key=lambda c: c["dte"])
    shorter, longer = ranked[0], ranked[-1]
    gap = longer["weekly_equivalent_yield_pct"] - shorter["weekly_equivalent_yield_pct"]
    if gap <= config.DRY_POWDER_DURATION_TIE_TOLERANCE_PCT:
        return shorter
    return longer


# ---------------------------------------------------------------------------
# Sizing (§5) — leftover cash only, via the shared capital_summary source of
# truth (never re-derives the reserve/cap formulas).
# ---------------------------------------------------------------------------
def dry_powder_available(state: dict, committed_this_cycle: float = 0.0) -> float:
    """Uncommitted cash for this sleeve: `position_manager.capital_summary`'s
    `deployable` figure (already net of the $13K reserve and the $38K
    deployed-capital cap) minus whatever this SAME scan run has already
    committed to earlier candidates."""
    summary = position_manager.capital_summary(state)
    return max(0.0, summary["deployable"] - committed_this_cycle)


def size_contracts(strike: float | None, available_cash: float) -> int:
    if not strike or strike <= 0 or available_cash <= 0:
        return 0
    return int(available_cash // (strike * config.SHARES_PER_LOT))


# ---------------------------------------------------------------------------
# Orchestration — the daily shadow sweep.
# ---------------------------------------------------------------------------
def scan(tickers: list[str], state: dict | None = None) -> dict:
    """Run the sleeve's shadow sweep over `tickers`. For every ticker: cheap
    eligibility first (§1); only eligible names get the live-gate tier check
    and the (expensive) chain fetch + strike selection (§2-4); only
    qualifying strikes get sized (§5) into a shadow trade. Every ticker is
    logged as a candidate row regardless of outcome (§8); records this run to
    the day's side-channel file and returns it."""
    if state is None:
        state = log.load_state()
    candidates: list[dict] = []
    shadow_trades: list[dict] = []
    committed = 0.0

    for raw_ticker in tickers:
        ticker = raw_ticker.upper()
        df = data_handler.get_daily(ticker)
        row = eligibility(ticker, state, df)
        if not row["eligible"]:
            candidates.append(row)
            continue

        try:
            import screening
            gate = screening.entry_gate(ticker)
            row["tier"] = TIER_QUALITY if gate.get("verdict") == "ELIGIBLE" else TIER_GENERAL
            row["gate_verdict"] = gate.get("verdict")
            row["gate_blocked_by"] = gate.get("blocked_by")
        except Exception as e:  # noqa: BLE001 — a gate-read failure never blocks the sweep
            row["tier"] = TIER_GENERAL
            row["gate_error"] = str(e)

        try:
            evaluation = evaluate_candidate(ticker)
        except Exception as e:  # noqa: BLE001 — an unpriceable name is UNRECORDED-as-trade,
            row["scan_result"] = "chain_unavailable"           # never a false negative
            row["error"] = str(e)
            candidates.append(row)
            continue

        # §8 telemetry: every strike looked at, not just the eventual winner —
        # this is what makes the delta band / yield floor calibratable later.
        row["evaluated_by_expiration"] = evaluation["evaluated_by_expiration"]
        selection = evaluation["winner"]
        if selection is None:
            row["scan_result"] = "no_qualifying_strike"
            candidates.append(row)
            continue

        row["selection"] = selection
        available = dry_powder_available(state, committed)
        n = size_contracts(selection["strike"], available)
        row["available_cash"] = round(available, 2)
        row["sized_contracts"] = n
        row["scan_result"] = "qualifying_strike" if n > 0 else "no_capital"
        candidates.append(row)
        if n <= 0:
            continue

        collateral = round(selection["strike"] * config.SHARES_PER_LOT * n, 2)
        committed += collateral
        shadow_trades.append({
            "strategy_tag": STRATEGY_TAG,
            "ticker": ticker,
            "tier": row["tier"],
            "opened_date": _today(),
            "expiration": selection["expiration"],
            "dte": selection["dte"],
            "strike": selection["strike"],
            "abs_delta": selection["abs_delta"],
            "premium_per_share": selection["premium_per_share"],
            "contracts": n,
            "collateral": collateral,
            "weekly_equivalent_yield_pct": selection["weekly_equivalent_yield_pct"],
            "annualized_yield_pct": selection["annualized_yield_pct"],
            "outcome": None,
        })

    result = {"date": _today(), "schema": SCHEMA_VERSION,
              "candidates": candidates, "shadow_trades": shadow_trades, "outcomes": []}
    _record(result)
    return result


# ---------------------------------------------------------------------------
# Outcome tracking (§6, §8) — hypothetical, on-demand, append-only.
# ---------------------------------------------------------------------------
def _close_on_or_before(df: pd.DataFrame | None, date_str: str) -> float | None:
    if df is None or df.empty:
        return None
    try:
        target = pd.Timestamp(date_str)
    except (TypeError, ValueError):
        return None
    idx = df.index[df.index <= target]
    if len(idx) == 0:
        return None
    return float(df.loc[idx[-1], "Close"])


def resolve_outcomes(state: dict | None = None, as_of: str | None = None) -> list[dict]:
    """For every past shadow trade whose expiration has passed with no
    recorded outcome yet: would it have been assigned (close <= strike at
    expiration) or expired worthless, and — only on assignment — what does
    today's live gate stack say about the resulting share position (§6's
    "entered on weakness, flag for review" vs "would pass standard entry")?
    See the module docstring's KNOWN PHASE-1 LIMITATION on why this is a
    TODAY's-gates read, not a true point-in-time backtest.

    Appends a new `outcomes` record to TODAY's file rather than mutating the
    original day's file — no in-place mutation, ever."""
    if state is None:
        state = log.load_state()
    as_of_date = pd.Timestamp(as_of) if as_of else pd.Timestamp(_today())
    resolved: list[dict] = []

    for day in stored_days():
        data = _load_day(day)
        for trade in data.get("shadow_trades", []):
            if trade.get("outcome") is not None:
                continue
            exp = trade.get("expiration")
            if not exp:
                continue
            try:
                exp_ts = pd.Timestamp(exp)
            except (TypeError, ValueError):
                continue
            if exp_ts > as_of_date:
                continue

            close_at_exp = _close_on_or_before(data_handler.get_daily(trade["ticker"]), exp)
            if close_at_exp is None:
                continue

            assigned = close_at_exp <= trade["strike"]
            outcome = {"assigned": assigned, "close_at_expiration": close_at_exp,
                      "resolved_date": _today()}
            weekly_pct = trade.get("weekly_equivalent_yield_pct")
            outcome["realized_weekly_equivalent_yield_pct"] = (
                weekly_pct if not assigned else None)  # premium is kept either way; an
                                                        # assignment's REALIZED yield needs
                                                        # the share leg's own P&L, out of
                                                        # scope for a shadow-only put record
            if assigned:
                try:
                    import screening
                    gate = screening.entry_gate(trade["ticker"])
                    outcome["hypothetical_gate_verdict"] = gate.get("verdict")
                    outcome["hypothetical_gate_blocked_by"] = gate.get("blocked_by")
                    outcome["classification"] = (
                        "would_pass_standard_entry" if gate.get("verdict") == "ELIGIBLE"
                        else "entered_on_weakness_flag_for_review")
                except Exception as e:  # noqa: BLE001
                    outcome["gate_error"] = str(e)

            resolved.append({"ticker": trade["ticker"], "opened_date": trade["opened_date"],
                             "expiration": exp, "strike": trade["strike"],
                             "contracts": trade["contracts"], "outcome": outcome})

    if resolved:
        _record({"date": _today(), "schema": SCHEMA_VERSION,
                 "candidates": [], "shadow_trades": [], "outcomes": resolved})
    return resolved


# ---------------------------------------------------------------------------
# Storage — one file per day, mirrors gate_telemetry.py's pattern exactly
# (append-only within a day; retention is a file unlink, never a rewrite).
# ---------------------------------------------------------------------------
def _day_path(day: str) -> str:
    return os.path.join(STORE_DIR, f"{day}.json")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_day(day: str) -> dict:
    try:
        with open(_day_path(day), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"date": day, "schema": SCHEMA_VERSION, "candidates": [],
            "shadow_trades": [], "outcomes": []}


def _save_day(day: str, data: dict) -> None:
    tmp = f"{_day_path(day)}.tmp.{os.getpid()}"
    try:
        os.makedirs(STORE_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, _day_path(day))
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def stored_days() -> list[str]:
    try:
        names = os.listdir(STORE_DIR)
    except OSError:
        return []
    days = []
    for n in names:
        if not n.endswith(".json") or ".tmp." in n:
            continue
        stem = n[:-5]
        try:
            datetime.strptime(stem, "%Y-%m-%d")
        except ValueError:
            continue
        days.append(stem)
    return sorted(days)


def prune(max_days: int | None = None) -> int:
    max_days = max_days or config.DRY_POWDER_LOG_RETENTION_DAYS
    days = stored_days()
    if len(days) <= max_days:
        return 0
    removed = 0
    for day in days[:-max_days]:
        try:
            os.remove(_day_path(day))
            removed += 1
        except OSError:
            pass
    return removed


def _record(entry: dict) -> dict:
    """Append this run's candidates/shadow_trades/outcomes to today's file.
    Best-effort by contract, same as `gate_telemetry.record_scan`: a logging
    failure can never alter, block, or fail the sweep that produced it."""
    try:
        with _lock:
            day = entry["date"]
            data = _load_day(day)
            data.setdefault("candidates", []).extend(entry.get("candidates") or [])
            data.setdefault("shadow_trades", []).extend(entry.get("shadow_trades") or [])
            data.setdefault("outcomes", []).extend(entry.get("outcomes") or [])
            _save_day(day, data)
            prune()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
