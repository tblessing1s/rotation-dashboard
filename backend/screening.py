"""The CFM scan: market regime, sector strength, stock filter, and the 4-level
entry gate. All read cached/fetched daily bars via data_handler and compute with
indicators — no provider calls beyond what data_handler caches.
"""
from __future__ import annotations

import threading
import time

import config
import data_handler
import indicators
import sector_data

# Short-TTL memoization so the expensive full-universe scans run once and are
# reused by repeated polls and by the entry gate, instead of recomputing on
# every concurrent request. Per-key locks collapse a thundering herd (many
# parallel callers on a cold cache) into a single computation.
_RESULT_TTL = int(__import__("os").environ.get("SCAN_CACHE_TTL", "300"))
_results: dict[str, tuple[float, object]] = {}
_result_locks: dict[str, threading.Lock] = {}
_results_guard = threading.Lock()


def _cached(key: str, fn, ttl: int = _RESULT_TTL):
    hit = _results.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    with _results_guard:
        lock = _result_locks.setdefault(key, threading.Lock())
    with lock:
        hit = _results.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        _results[key] = (time.time(), val)
        return val


# ---------------------------------------------------------------------------
# Level 1 — market regime
# ---------------------------------------------------------------------------
def regime() -> dict:
    return _cached("regime", _compute_regime)


def _compute_regime() -> dict:
    # One parallel batch warms breadth universe + VIX + SPY, then compute.
    data_handler.prefetch(config.BREADTH_SYMBOLS + [config.VIX_SYMBOL, config.BENCHMARK])
    frames = data_handler.get_many(config.BREADTH_SYMBOLS)
    breadth = indicators.breadth(frames)
    vix_df = data_handler.get_daily(config.VIX_SYMBOL)
    vix = indicators.last(vix_df)
    spy_df = data_handler.get_daily(config.BENCHMARK)
    spy_dist = indicators.pct_from_ma(spy_df) if spy_df is not None else None
    spy_trend = "up" if (spy_dist or 0) > 0 else "down" if spy_dist is not None else "unknown"

    status = "yellow"
    if breadth is not None and vix is not None:
        green = breadth >= config.REGIME_BREADTH_GREEN and vix < config.VIX_CALM and spy_trend == "up"
        red = breadth <= config.REGIME_BREADTH_RED or vix > config.VIX_ELEVATED
        status = "green" if green else "red" if red else "yellow"
    return {
        "status": status,
        "breadth": breadth,
        "vix": round(vix, 2) if vix is not None else None,
        "spy_trend": spy_trend,
        "spy_dist_ma21": spy_dist,
    }


# ---------------------------------------------------------------------------
# Level 2 — sector strength
# ---------------------------------------------------------------------------
def _sector_breadth(etf: str) -> float | None:
    frames = data_handler.get_many(sector_data.constituents(etf))
    return indicators.breadth(frames)


def sectors() -> dict:
    return _cached("sectors", _compute_sectors)


def _compute_sectors() -> dict:
    # Warm SPY + every sector ETF + every constituent in one parallel batch, so
    # the per-sector breadth loop below reads from cache instead of fetching
    # 500 symbols one at a time.
    data_handler.prefetch([config.BENCHMARK] + sector_data.sector_etfs() + sector_data.all_tickers())
    spy = data_handler.get_daily(config.BENCHMARK)
    out = {}
    for etf in sector_data.sector_etfs():
        df = data_handler.get_daily(etf)
        rs = indicators.rs3m(df, spy) if df is not None else None
        bdth = _sector_breadth(etf)
        expanding = indicators.atr_expanding(df) if df is not None else None
        strong = (rs is not None and rs >= config.SECTOR_RS3M_MIN
                  and bdth is not None and bdth >= config.SECTOR_BREADTH_MIN)
        status = "green" if strong else "red" if (rs is not None and rs < 0) else "yellow"
        out[etf] = {
            "name": sector_data.sectors()[etf].name,
            "rs3m": rs,
            "breadth": bdth,
            "atr_expanding": expanding,
            "status": status,
        }
    return out


# ---------------------------------------------------------------------------
# Levels 3 & 4 — stock filter
# ---------------------------------------------------------------------------
def _stock_row(ticker: str, spy, sector_rs_vs_spy: float | None, sector_etf: str) -> dict:
    df = data_handler.get_daily(ticker)
    rs_vs_spy = indicators.rs3m(df, spy) if df is not None else None
    rs_vs_sector = None
    if rs_vs_spy is not None and sector_rs_vs_spy is not None:
        rs_vs_sector = round(rs_vs_spy - sector_rs_vs_spy, 2)
    atrp = indicators.atr_pct(df) if df is not None else None
    cons = indicators.consolidating(df) if df is not None else None

    beats = (rs_vs_spy is not None and rs_vs_spy > config.STOCK_RS_VS_SPY_MIN
             and rs_vs_sector is not None and rs_vs_sector > config.STOCK_RS_VS_SECTOR_MIN)
    if beats and cons:
        status = "ready"
    elif rs_vs_sector is not None and rs_vs_sector < 0:
        status = "no"
    else:
        status = "wait"
    return {
        "ticker": ticker,
        "sector": sector_etf,
        "rs3m_vs_spy": rs_vs_spy,
        "rs3m_vs_sector": rs_vs_sector,
        "atr_pct": atrp,
        "consolidating": cons,
        "status": status,
    }


def stock_filter(sector: str | None = None) -> list[dict]:
    key = f"stock_filter:{(sector or 'ALL').upper()}"
    return _cached(key, lambda: _compute_stock_filter(sector))


def _compute_stock_filter(sector: str | None = None) -> list[dict]:
    etfs = [sector.upper()] if sector else sector_data.sector_etfs()
    # Parallel-warm SPY + the sector ETF(s) + their constituents first.
    universe = [config.BENCHMARK] + etfs
    for etf in etfs:
        universe += sector_data.constituents(etf)
    data_handler.prefetch(universe)
    spy = data_handler.get_daily(config.BENCHMARK)
    rows = []
    for etf in etfs:
        sector_df = data_handler.get_daily(etf)
        sector_rs = indicators.rs3m(sector_df, spy) if sector_df is not None else None
        for ticker in sector_data.constituents(etf):
            rows.append(_stock_row(ticker, spy, sector_rs, etf))
    # Sort by RS3M vs Sector descending (best fit first); None last.
    rows.sort(key=lambda r: (r["rs3m_vs_sector"] is None, -(r["rs3m_vs_sector"] or 0)))
    return rows


# ---------------------------------------------------------------------------
# The 4-level entry gate (stop on first fail)
# ---------------------------------------------------------------------------
def entry_gate(ticker: str) -> dict:
    ticker = ticker.upper()
    sector_etf = sector_data.sector_for(ticker)
    levels = []

    # Level 1 — market regime
    reg = regime()
    l1_pass = bool(reg["status"] == "green")
    levels.append({"level": 1, "name": "Market regime green", "pass": l1_pass, "detail": reg})

    # Level 2 — sector strong
    sec = sectors().get(sector_etf, {}) if sector_etf else {}
    l2_pass = bool(sec.get("status") == "green")
    levels.append({"level": 2, "name": "Sector strong", "pass": l2_pass, "detail": {"sector": sector_etf, **sec}})

    # Levels 3 & 4 — stock beating peers + consolidating
    spy = data_handler.get_daily(config.BENCHMARK)
    sector_df = data_handler.get_daily(sector_etf) if sector_etf else None
    sector_rs = indicators.rs3m(sector_df, spy) if sector_df is not None else None
    row = _stock_row(ticker, spy, sector_rs, sector_etf or "")

    l3_pass = bool(row["rs3m_vs_spy"] is not None and row["rs3m_vs_spy"] > config.STOCK_RS_VS_SPY_MIN
                   and row["rs3m_vs_sector"] is not None and row["rs3m_vs_sector"] > config.STOCK_RS_VS_SECTOR_MIN)
    levels.append({"level": 3, "name": "Stock beating peers", "pass": l3_pass, "detail": row})

    l4_pass = bool(row["consolidating"])
    levels.append({"level": 4, "name": "Consolidating, not breaking", "pass": l4_pass,
                   "detail": {"atr_pct": row["atr_pct"], "consolidating": row["consolidating"]}})

    # Stop-on-fail: the cleared level is the highest contiguous pass from 1.
    cleared = 0
    for lv in levels:
        if lv["pass"]:
            cleared = lv["level"]
        else:
            break
    verdict = "READY TO ENTER" if cleared == 4 else "WAIT"
    return {"ticker": ticker, "sector": sector_etf, "levels": levels,
            "cleared_level": cleared, "verdict": verdict}


# ---------------------------------------------------------------------------
# Daily checklist (15-minute routine)
# ---------------------------------------------------------------------------
def daily_checklist(state: dict) -> list[dict]:
    import datetime as _dt
    import kill_switch

    items = []
    reg = regime()
    items.append({"id": "regime", "label": f"Market regime: {reg['status'].upper()}",
                  "ok": reg["status"] == "green", "detail": reg})

    meta = state.get("metadata", {})
    reserve = float(meta.get("reserve_required") or 0)
    operating = float(meta.get("operating_cash") or 0)
    items.append({"id": "reserve", "label": "Reserve funded",
                  "ok": operating >= reserve or reserve == 0,
                  "detail": {"operating_cash": operating, "reserve_required": reserve}})

    # Kill-switch sweep across open positions.
    for ev in kill_switch.evaluate_all(state):
        items.append({"id": f"ks_{ev['ticker']}",
                      "label": f"{ev['ticker']} RS3M intact (vs SPY {ev['rs3m_vs_spy']}, vs Sector {ev['rs3m_vs_sector']})",
                      "ok": ev["status"] != "red", "detail": ev})

    # Short calls expiring + LEAPs nearing roll DTE.
    for p in state.get("positions", []):
        if p.get("status") == "closed":
            continue
        for sc in p.get("short_calls", []):
            dte = sc.get("dte")
            if dte is not None and dte <= 2:
                items.append({"id": f"short_{p['ticker']}_{sc.get('strike')}",
                              "label": f"Roll {p['ticker']} short (DTE {dte})", "ok": False,
                              "detail": sc})
        leap = p.get("leap") or {}
        if leap.get("dte") is not None and leap["dte"] <= config.LEAP_ROLL_DTE:
            items.append({"id": f"leap_{p['ticker']}",
                          "label": f"{p['ticker']} LEAP nearing {config.LEAP_ROLL_DTE} DTE (roll out)",
                          "ok": False, "detail": leap})

    is_friday = _dt.date.today().weekday() == 4
    if is_friday:
        items.append({"id": "friday", "label": "Friday: roll all shorts, log juice, check cap status",
                      "ok": False, "detail": {"weekday": "Friday"}})
    return items
