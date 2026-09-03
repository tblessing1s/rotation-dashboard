"""roll_readiness — an ADVISORY-ONLY "is it worth rolling this early" signal for
the short-roll picker (``option_chain.roll_options``).

Theta decay is not linear: it accelerates as expiration approaches, so a
contract early in its life decays slowly and a same-strike roll into a fresh
contract usually trades cheap, fast-burning extrinsic for expensive,
slow-burning extrinsic — net LESS extrinsic captured over time. Rolling early
only pays when one of two triggers fires:

  - most of this contract's extrinsic is already banked (nothing meaningful
    left to wait for), or
  - the strike itself no longer offers real downside cushion, independent of
    how much extrinsic has decayed.

This module computes that signal and nothing else. It carries ZERO authority:
it never blocks a roll, never changes which strikes/expirations the picker
offers, and it must never be used to gate execution — see CLAUDE.md "Shadow
mode" for the house convention this follows. PURE: no I/O, callers supply the
already-computed inputs (``position_manager.enrich_short`` is the usual
source for ``extrinsic_captured_pct`` and the ITM buffer).
"""
from __future__ import annotations

import config


def roll_readiness(extrinsic_captured_pct: float | None,
                   itm_buffer_pct: float | None,
                   dte: int | None) -> dict:
    """Is there still real theta to collect, or is this contract effectively
    "done" and safe to roll early?

    ``ready`` is True when either trigger fires:
      - ``extrinsic_captured_pct >= config.ROLL_READY_DECAY_PCT`` (default 80%)
      - ``itm_buffer_pct is not None and itm_buffer_pct < config.ROLL_READY_ITM_FLOOR_PCT``
        (default 3%) — pass None here when the short isn't ITM; a buffer only
        means something once the strike is already breached.

    Both inputs unmeasurable -> ``ready`` is None (unmeasured, not "not
    ready" and not "ready") rather than a false negative.
    """
    reasons: list[str] = []
    if extrinsic_captured_pct is not None and extrinsic_captured_pct >= config.ROLL_READY_DECAY_PCT:
        reasons.append("DECAY_CAPTURED")
    if itm_buffer_pct is not None and itm_buffer_pct < config.ROLL_READY_ITM_FLOOR_PCT:
        reasons.append("ITM_BUFFER_THIN")

    measured = extrinsic_captured_pct is not None or itm_buffer_pct is not None
    ready = None if not measured else bool(reasons)

    return {
        "ready": ready,
        "reasons": reasons,
        "extrinsic_captured_pct": extrinsic_captured_pct,
        "itm_buffer_pct": itm_buffer_pct,
        "decay_threshold_pct": config.ROLL_READY_DECAY_PCT,
        "itm_floor_pct": config.ROLL_READY_ITM_FLOOR_PCT,
        "dte": dte,
        "advisory": True,
    }


# ---------------------------------------------------------------------------
# Phase-1 roll-dialog audit (2026-09) — juice/wk, cushion, week ranking, and the
# roll-up guard. SINGLE-SOURCED here (not reimplemented in the frontend) so the
# number the operator decides on and the number a test pins can never drift;
# ``option_chain.roll_options`` attaches ``juice_per_week_pct``/``cushion_atr``
# to every strike row server-side, and ``frontend/src/components/RollModal.jsx``
# only ever DISPLAYS/RANKS what's already there — it does no price arithmetic
# of its own. All PURE, all advisory/shadow: nothing here blocks anything.
# ---------------------------------------------------------------------------

def juice_per_week(mark: float | None, strike: float | None,
                   spot: float | None, dte: int | None) -> float | None:
    """Extrinsic yield, normalized to a 7-CALENDAR-day week — the SAME day-count
    convention as ``burn.net_juice_per_week`` ([NET_JUICE_TIME_BASE]), so this
    number is directly comparable to it and to config.SHARES_JUICE_FLOOR_PCT.
    Expressed as %/wk (matches SHARES_JUICE_FLOOR_PCT's own 0-100-ish scale, not
    a 0-1 fraction). ``new_extrinsic = mark - max(spot-strike, 0)`` per the
    roll-dialog audit spec — mark-based, not the bid/ask-midpoint
    ``indicators.calculate_extrinsic`` other pickers use; the two are
    deliberately different metrics shown side by side in the roll dialog (see
    the audit's §0.3/§1.7 notes), not a bug.

    None when any input is missing or dte<=0 (a same-day/expired contract has no
    meaningful weekly rate). PURE."""
    if mark is None or strike is None or not spot or not dte or dte <= 0:
        return None
    intrinsic = max(spot - strike, 0.0)
    extrinsic = mark - intrinsic
    return (extrinsic / spot) * (7.0 / dte) * 100.0


def cushion_atr(strike: float | None, spot: float | None, atr_value: float | None) -> float | None:
    """Downside cushion in ATR units: (spot - strike) / ATR. Display-only. PURE."""
    if strike is None or not spot or not atr_value:
        return None
    return (spot - strike) / atr_value


def rank_weeks_by_juice(rows: list[dict], parity_band_pct: float | None = None) -> dict:
    """Pick the "best rate" week from ``rows`` (each ``{"expiration", "dte",
    "juice_per_week_pct"}``, e.g. one strike's row across every candidate
    expiration) — the highest juice/wk, with two weeks within
    ``parity_band_pct`` (default config.ROLL_JUICE_PARITY_BAND_PCT) of each
    other treated as a tie and broken toward the SHORTER DTE (fewer days of gap
    risk, more roll opportunities). Deliberately NOT a net-debit ranking — see
    the roll-dialog audit §1.1: a further-dated contract is ALWAYS cheaper to
    reach for the same strike (more remaining extrinsic to offset the buyback)
    regardless of whether it's actually the better rate.

    Returns ``{"best": <row or None>, "rows": rows}``; rows with no priceable
    juice/wk are ignored for the pick but still returned. PURE."""
    band = parity_band_pct if parity_band_pct is not None else config.ROLL_JUICE_PARITY_BAND_PCT
    priced = [r for r in rows if r.get("juice_per_week_pct") is not None]
    if not priced:
        return {"best": None, "rows": rows}
    best = priced[0]
    for r in priced[1:]:
        within = abs(r["juice_per_week_pct"] - best["juice_per_week_pct"]) <= band
        if within:
            if r["dte"] < best["dte"]:
                best = r
        elif r["juice_per_week_pct"] > best["juice_per_week_pct"]:
            best = r
    return {"best": best, "rows": rows}


def roll_up_guard(*, current_strike: float | None, chosen_strike: float | None,
                  earnings_in_week: bool | None, ex_div_known: bool,
                  ex_div_in_week: bool | None, chosen_juice_per_week_pct: float | None,
                  juice_floor_pct: float | None, operating_cash: float | None,
                  reserve_required: float | None, net_credit: float | None) -> dict | None:
    """SHADOW (TRAVIS_EXTENSION) — rolling to a strike ABOVE the current one is
    economically a fresh entry at that strike; this runs the Level-5-equivalent
    checks (earnings-in-cycle, ex-div-in-cycle, weekly-juice adequacy, cash
    reserve) as PASS/FAIL/UNKNOWN, worst-signal-wins for the summary. ZERO
    blocking authority — a caller must never use this to refuse a roll.

    Returns None when it doesn't apply (chosen_strike <= current_strike, or
    either strike is unknown). PURE."""
    if current_strike is None or chosen_strike is None or chosen_strike <= current_strike:
        return None
    checks = [
        {"id": "earnings", "label": "No earnings inside cycle",
         "status": "FAIL" if earnings_in_week else "PASS"},
        {"id": "ex_div", "label": "Ex-div inside cycle",
         "status": "UNKNOWN" if not ex_div_known else ("FAIL" if ex_div_in_week else "PASS")},
        {"id": "juice", "label": "Weekly-juice adequacy",
         "status": ("UNKNOWN" if chosen_juice_per_week_pct is None or juice_floor_pct is None
                    else ("PASS" if chosen_juice_per_week_pct >= juice_floor_pct else "FAIL"))},
        {"id": "cash_reserve", "label": "Cash reserve",
         "status": ("UNKNOWN" if net_credit is None or operating_cash is None or reserve_required is None
                    else ("PASS" if operating_cash + net_credit >= reserve_required else "FAIL"))},
    ]
    summary = ("FAIL" if any(c["status"] == "FAIL" for c in checks)
              else "UNKNOWN" if any(c["status"] == "UNKNOWN" for c in checks) else "PASS")
    return {"checks": checks, "summary": summary, "advisory": True}
