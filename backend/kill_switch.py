"""Kill switch — the binary exit rule.

For each open position, compute RS3M vs SPY and RS3M vs Sector. The rule has no
debate built in:
  - RS3M vs Sector negative  -> hard exit immediately.
  - RS3M vs SPY negative      -> exit within 1-2 days (confirm on close).
Both positive -> green, hold.
"""
from __future__ import annotations

import config
import data_handler
import earnings
import indicators
import sector_data


def _rs_pair(ticker: str, profile: str | None = None) -> tuple[float | None, float | None]:
    """(RS3M vs SPY, RS3M vs Sector) for a ticker, in percent.

    RS3M vs Sector is the DIRECT relative strength of the stock against its
    sector ETF — the same indicators.rs3m over the same 63-day lookback, just
    with the sector ETF as the benchmark instead of SPY. This is the true ratio,
    NOT the vs-SPY difference approximation (rs_vs_spy − sector_rs_vs_spy): the
    kill switch is the critical laggard filter and fires on large moves, exactly
    where the difference approximation diverges most from the true ratio.
    [HARD_CFM_RULE / KILL_SWITCH_RS_SOURCE]

    A sector ETF HELD as its own CFM position has no distinct peer sector to
    compare against — the comparison is tautologically itself, which computes
    to exactly 0 every time. Left unguarded that reads as a REAL number and
    quietly breaks the kill switch for an ETF position: the YELLOW "thinning"
    leg (rs_vs_sector < STOCK_RS_VS_SECTOR_MIN + 2) would fire permanently
    since 0 is always < 2. So rs_vs_sector is None (not applicable) for an
    ETF's own position — the kill switch then relies solely on RS3M vs SPY,
    a fully meaningful check for an ETF against the broad market.

    ``profile`` (schema v21) selects which peer group the SECTOR leg compares
    against: the name's sector ETF for a JUICE_ENGINE position, the dividend-peer
    benchmark for a DIVIDEND_COMPOUNDER. The rule is unchanged — the peer must
    still be beaten — and the vs-SPY leg is NOT touched by this: it retains full,
    unmodified exit authority for both profiles [HARD_CFM_RULE]. The
    self-comparison guard is asked against the ACTIVE benchmark, so a compounder
    whose ticker IS the dividend benchmark is caught by it too.
    """
    import income_profile
    spy = data_handler.get_daily(config.BENCHMARK)
    stock = data_handler.get_daily(ticker)
    rs_vs_spy = indicators.rs3m(stock, spy) if stock is not None else None

    sector_etf = sector_data.sector_for(ticker)
    benchmark = income_profile.benchmark_for(profile, sector_etf)
    is_own_benchmark = income_profile.is_own_benchmark(ticker, profile, sector_etf)
    rs_vs_sector = None
    if benchmark and not is_own_benchmark and stock is not None:
        sector_df = data_handler.get_daily(benchmark)
        rs_vs_sector = indicators.rs3m(stock, sector_df) if sector_df is not None else None
    return rs_vs_spy, rs_vs_sector


def classify(ticker: str, rs_vs_spy: float | None,
             rs_vs_sector: float | None) -> dict:
    """The kill-switch rule as a PURE function over the two RS3M inputs — the
    single decision core shared by the live evaluator below and the
    recommendation engine (which feeds it frozen snapshot values). No provider,
    clock, or state access; the rule itself has no debate built in."""
    status = "green"
    alert = False
    action = "Hold — relative strength intact."
    if rs_vs_sector is not None and rs_vs_sector < 0:
        status = "red"
        alert = True
        action = f"EXIT {ticker} immediately — RS3M vs Sector turned negative."
    elif rs_vs_spy is not None and rs_vs_spy < 0:
        status = "red"
        alert = True
        action = f"Exit {ticker} within 1-2 days — RS3M vs SPY turned negative (confirm on close)."
    elif (rs_vs_sector is not None and rs_vs_sector < config.STOCK_RS_VS_SECTOR_MIN + 2) or \
         (rs_vs_spy is not None and rs_vs_spy < config.STOCK_RS_VS_SPY_MIN):
        status = "yellow"
        action = "Watch — relative strength thinning toward the kill line."
    return {
        "ticker": ticker,
        "rs3m_vs_spy": rs_vs_spy,
        "rs3m_vs_sector": rs_vs_sector,
        "status": status,
        "alert": alert,
        "suggested_action": action,
    }


def evaluate(ticker: str, profile: str | None = None) -> dict:
    rs_vs_spy, rs_vs_sector = _rs_pair(ticker, profile=profile)
    verdict = classify(ticker, rs_vs_spy, rs_vs_sector)
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
    fires — RS3M vs Sector negative dominates (exit now); RS3M vs SPY negative is
    the confirm-on-close exit. Advisory: kill_switch never closes on its own."""
    import exit_reasons
    if evaluation.get("status") != "red":
        return None
    rs_sector = evaluation.get("rs3m_vs_sector")
    if rs_sector is not None and rs_sector < 0:
        return exit_reasons.ExitReason.KILL_SWITCH_SECTOR
    rs_spy = evaluation.get("rs3m_vs_spy")
    if rs_spy is not None and rs_spy < 0:
        return exit_reasons.ExitReason.KILL_SWITCH_SPY
    return None
