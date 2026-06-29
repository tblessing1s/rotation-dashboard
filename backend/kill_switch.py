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


def _rs_pair(ticker: str) -> tuple[float | None, float | None]:
    """(RS3M vs SPY, RS3M vs Sector) for a ticker, in percent."""
    spy = data_handler.get_daily(config.BENCHMARK)
    stock = data_handler.get_daily(ticker)
    rs_vs_spy = indicators.rs3m(stock, spy) if stock is not None else None

    sector_etf = sector_data.sector_for(ticker)
    rs_vs_sector = None
    if sector_etf and rs_vs_spy is not None:
        sector_df = data_handler.get_daily(sector_etf)
        sector_rs_vs_spy = indicators.rs3m(sector_df, spy) if sector_df is not None else None
        if sector_rs_vs_spy is not None:
            rs_vs_sector = round(rs_vs_spy - sector_rs_vs_spy, 2)
    return rs_vs_spy, rs_vs_sector


def evaluate(ticker: str) -> dict:
    rs_vs_spy, rs_vs_sector = _rs_pair(ticker)
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
    try:
        earn = earnings.next_earnings(ticker)
    except Exception:  # noqa: BLE001
        earn = {"date": None, "days_until": None, "warning": False}
    if earn.get("warning"):
        action = (f"{action}  Earnings in {earn['days_until']}d ({earn['date']}) — "
                  "roll the short deep-ITM or exit before the report.")
    return {
        "ticker": ticker,
        "rs3m_vs_spy": rs_vs_spy,
        "rs3m_vs_sector": rs_vs_sector,
        "status": status,
        "alert": alert,
        "suggested_action": action,
        "earnings": earn,
    }


def evaluate_all(state: dict) -> list[dict]:
    out = []
    for p in state.get("positions", []):
        if p.get("status") == "closed":
            continue
        out.append(evaluate(p.get("ticker", "")))
    return out
