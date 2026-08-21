"""Kill switch — the binary exit rule.

For each open position, compute RS3M vs SPY. The rule has no debate built in:
  - RS3M vs SPY negative -> exit within 1-2 days (confirm on close).
  - Positive             -> green, hold.

REMOVED 2026-08-21 (TRAVIS_EXTENSION): the RS3M-vs-SECTOR exit-now trigger.
A stock's relative strength against its cap-weighted sector ETF is not a peer
comparison — the ETF is dominated by a few mega-caps — so the signal was judged
meaningless and removed system-wide. **This was a deliberate loosening of a
safety mechanism, not an oversight.** The cases it caught and this rule no longer
does (a name lagging its sector while still beating SPY; the earlier of the two
crossings; the sector half of the YELLOW thinning leg) are enumerated in
docs/decision-2026-08-21-remove-sector-rs.md. Do not reinstate a sector-relative
leg here; the planned replacement is a rules-based industry peer basket.
"""
from __future__ import annotations

import config
import data_handler
import earnings
import indicators


def _rs_spy(ticker: str) -> float | None:
    """RS3M vs SPY for a ticker, in percent — the DIRECT ratio over the 63-day
    lookback (``indicators.rs3m``), which is what the rule has always used
    [HARD_CFM_RULE / KILL_SWITCH_RS_SOURCE].

    This is now the kill switch's only relative-strength input. It is fully
    meaningful for a stock and for an ETF alike (both against the broad market),
    so unlike the removed sector leg it needs no self-comparison guard and no
    profile-dependent peer resolution — see the module docstring.
    """
    spy = data_handler.get_daily(config.BENCHMARK)
    stock = data_handler.get_daily(ticker)
    return indicators.rs3m(stock, spy) if stock is not None else None


def classify(ticker: str, rs_vs_spy: float | None) -> dict:
    """The kill-switch rule as a PURE function over the RS3M-vs-SPY input — the
    single decision core shared by the live evaluator below and the
    recommendation engine (which feeds it a frozen snapshot value). No provider,
    clock, or state access; the rule itself has no debate built in.

    The vs-SPY behaviour here is byte-for-byte what it was before the sector leg
    was removed: same threshold, same wording, same precedence relative to
    YELLOW. Removing the sector branch only promoted this one to first — the two
    were mutually exclusive and sector-RED dominated, so every case that reached
    this branch before still reaches it, unchanged.

    The YELLOW leg lost its sector half in the same removal (see the module
    docstring): it is now the vs-SPY thinning check alone. That is a real, and
    deliberately accepted, reduction in warning coverage — it is NOT an
    oversight and must not be "fixed" by inventing a substitute leg.
    """
    status = "green"
    alert = False
    action = "Hold — relative strength intact."
    if rs_vs_spy is not None and rs_vs_spy < 0:
        status = "red"
        alert = True
        action = f"Exit {ticker} within 1-2 days — RS3M vs SPY turned negative (confirm on close)."
    elif rs_vs_spy is not None and rs_vs_spy < config.STOCK_RS_VS_SPY_MIN:
        status = "yellow"
        action = "Watch — relative strength thinning toward the kill line."
    return {
        "ticker": ticker,
        "rs3m_vs_spy": rs_vs_spy,
        "status": status,
        "alert": alert,
        "suggested_action": action,
    }


def evaluate(ticker: str, profile: str | None = None) -> dict:
    """``profile`` is accepted and ignored: it selected the SECTOR leg's peer
    group, and that leg is gone. Kept in the signature so the position-profile
    call sites (``evaluate_all``) stay unchanged and a future peer-basket
    benchmark has somewhere to land."""
    verdict = classify(ticker, _rs_spy(ticker))
    action = verdict["suggested_action"]
    try:
        earn = earnings.next_earnings(ticker)
    except Exception:  # noqa: BLE001
        earn = {"date": None, "days_until": None, "warning": False}
    if earn.get("warning"):
        action = (f"{action}  Earnings in {earn['days_until']}d ({earn['date']}) — "
                  "roll the short deep-ITM or exit before the report.")
    return {**verdict, "suggested_action": action, "earnings": earn}


def evaluate_all(state: dict) -> list[dict]:
    import income_profile
    out = []
    for p in state.get("positions", []):
        if p.get("status") == "closed":
            continue
        # The POSITION's stamped profile, not a re-derivation — an open position's
        # peer group must not move underneath it on a yield print.
        out.append(evaluate(p.get("ticker", ""), profile=income_profile.of(p)))
    return out


def exit_reason_code(evaluation: dict) -> str | None:
    """The coded exit reason (exit_reasons.ExitReason) a kill-switch trip
    implies, or None when the position is not on a red exit. This is the single
    source the close path stamps so the reason is set AT the point the rule
    fires — RS3M vs SPY negative is the confirm-on-close exit. Advisory:
    kill_switch never closes on its own.

    ``KILL_SWITCH_SECTOR`` is no longer emitted here (see the module docstring).
    The constant is RETIRED, not deleted — historical closes carry it and the
    History tab and the CSV export read it back."""
    import exit_reasons
    if evaluation.get("status") != "red":
        return None
    rs_spy = evaluation.get("rs3m_vs_spy")
    if rs_spy is not None and rs_spy < 0:
        return exit_reasons.ExitReason.KILL_SWITCH_SPY
    return None
