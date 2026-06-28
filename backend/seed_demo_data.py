"""Seed the dashboard with realistic *demo* data so the UI can be previewed
without live Schwab / Alpha Vantage keys.

It writes the demo store — kept *separate* from the live store (state.demo.json +
cache_demo/, vs state.json + cache/) so toggling demo mode never touches real
positions — both inside DATA_DIR (gitignored, so this never touches tracked files):

  1. A synthetic daily-OHLCV parquet cache for SPY, the VIX, every sector ETF and
     their constituents. In demo mode data_handler reads only this cache, so the
     Scan (regime / sectors / stock filter), Kill-Switch and Positions tabs all
     render real computed numbers instead of blanks.
  2. A populated state.demo.json — a small book of CFM positions with a multi-week
     log of executions. The theta ledger and extrinsic-payback meters are then
     *derived* from those executions exactly as in production (nothing is
     hand-maintained), so what you see is what the app produces from real use.

Demo mode is toggled from the navbar (Live/Demo switch) — which calls seed() on
first use — or from the CLI:

    python backend/seed_demo_data.py            # build demo store, switch ON
    python backend/seed_demo_data.py --clear    # remove demo store, switch OFF
"""
from __future__ import annotations

import argparse
import os
import shutil
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import config
import data_handler
import executor
import logging_handler as log
import sector_data

RNG = np.random.default_rng(20260628)
HISTORY = 260  # business days of synthetic history per symbol

# Per sector-group daily drift relative to SPY. Growth runs hot (strong RS3M,
# green sectors); defensives lag (negative RS3M, red). This is what colours the
# Scan tab the way a real risk-on tape would.
SPY_DRIFT = 0.0008
GROUP_ALPHA = {
    "growth": 0.0024,    # XLK, XLY, XLC -> strong, green
    "cyclical": 0.0011,  # XLI, XLF      -> firm, yellow/borderline
    "inflation": 0.0004, # XLE, XLB
    "rates": -0.0004,    # XLRE          -> red
    "defensive": -0.0007,# XLV, XLP, XLU -> red
    "": 0.0006,
}


# ---------------------------------------------------------------------------
# Synthetic market-data cache
# ---------------------------------------------------------------------------
def _frame(base: float, mu: float, sigma: float, consolidate_tail: int = 0,
           anchor: float | None = None) -> pd.DataFrame:
    """A plausible daily OHLCV frame: a trending close with intraday range.

    consolidate_tail flattens the last N bars (drift ~0) so a name reads as
    "consolidating near its MA" — low ATR%, close hugging MA21 — while its 3-month
    relative strength stays positive from the earlier trend. `anchor` rescales the
    whole series so the last close lands on a chosen price; a constant scale leaves
    RS3M, ATR% and MA-distance untouched, so it only fixes the absolute level.
    """
    drift = np.full(HISTORY, mu)
    if consolidate_tail:
        drift[-consolidate_tail:] = 0.0002
    shocks = RNG.normal(0.0, sigma, HISTORY)
    close = base * np.exp(np.cumsum(drift + shocks))
    if anchor is not None and close[-1]:
        close = close * (anchor / close[-1])
    rng_frac = np.abs(RNG.normal(0.0, sigma, HISTORY)) + 0.002
    high = close * (1 + rng_frac)
    low = close * (1 - rng_frac)
    open_ = np.concatenate([[close[0]], close[:-1]])
    vol = RNG.integers(2_000_000, 40_000_000, HISTORY)
    idx = pd.bdate_range(end=datetime.utcnow().date(), periods=HISTORY)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


def generate_market_cache(strong_tickers: set[str], anchors: dict[str, float]) -> dict[str, float]:
    """Write the synthetic parquet cache and return each symbol's last close."""
    os.makedirs(config.active_cache_dir(), exist_ok=True)
    last_close: dict[str, float] = {}

    def emit(symbol: str, base: float, mu: float, sigma: float, tail: int = 0,
             anchor: float | None = None) -> None:
        df = _frame(base, mu, sigma, consolidate_tail=tail, anchor=anchor)
        data_handler._write_cache(symbol, df)
        last_close[symbol.upper()] = float(df["Close"].iloc[-1])

    # Benchmark + broad-breadth proxies. Low noise so the trend (not endpoint
    # noise) decides MA position — a clean risk-on tape for the preview.
    emit(config.BENCHMARK, 540.0, 0.0010, 0.0012)  # steady uptrend, close above MA21
    emit("QQQ", 470.0, SPY_DRIFT + 0.0015, 0.003)
    emit("IWM", 220.0, SPY_DRIFT + 0.0004, 0.003)

    # The VIX is an index level, not a trending asset — keep it stationary & calm
    # (a cumulative random walk would wander off to nonsense), so regime reads
    # "VIX < 18, calm".
    vix_close = 14.0 + RNG.normal(0.0, 0.4, HISTORY)
    vidx = pd.bdate_range(end=datetime.utcnow().date(), periods=HISTORY)
    vdf = pd.DataFrame({"Open": vix_close, "High": vix_close + 0.5, "Low": vix_close - 0.5,
                        "Close": vix_close, "Volume": 0}, index=vidx)
    data_handler._write_cache(config.VIX_SYMBOL, vdf)
    last_close[config.VIX_SYMBOL.upper()] = float(vdf["Close"].iloc[-1])

    base_etf = {"XLK": 235, "XLY": 210, "XLC": 105, "XLI": 140, "XLF": 48,
                "XLE": 92, "XLB": 90, "XLV": 150, "XLP": 82, "XLU": 78,
                "XLRE": 42}
    for etf in sector_data.sector_etfs():
        group = config.SECTOR_GROUPS.get(etf, "")
        mu = SPY_DRIFT + GROUP_ALPHA.get(group, 0.0)
        emit(etf, base_etf.get(etf, 100), mu, 0.004)

    # Constituents: group drift + a per-name spread so breadth isn't 0/100.
    for etf in sector_data.sector_etfs():
        group = config.SECTOR_GROUPS.get(etf, "")
        for tkr in sector_data.constituents(etf):
            mu = SPY_DRIFT + GROUP_ALPHA.get(group, 0.0) + RNG.uniform(-0.0010, 0.0014)
            base = float(RNG.uniform(40, 480))
            tail = 0
            anchor = None
            if tkr in strong_tickers:
                # Outpace SPY *and* the sector, then consolidate — so the held
                # names read green on the kill switch and "consolidating" on scan.
                mu = SPY_DRIFT + 0.0048
                tail = 12
                anchor = anchors.get(tkr)
            emit(tkr, base, mu, 0.006, tail=tail, anchor=anchor)

    return last_close


# ---------------------------------------------------------------------------
# Demo book: positions + a multi-week execution log
# ---------------------------------------------------------------------------
def _fridays(n: int, end: str = "2026-06-26") -> list[str]:
    """The `n` most recent Fridays up to and including `end`, oldest first."""
    d = datetime.strptime(end, "%Y-%m-%d")
    out = [d - timedelta(weeks=i) for i in range(n)]
    return [x.strftime("%Y-%m-%d") for x in reversed(out)]


# ticker, sector hint, LEAP strike, entry stock px, extrinsic/contract at entry,
# # of paid-back weeks, weekly (sold, paid) per share, current LEAP time value,
# open short (strike, dte, sold/sh), shares held, share cost basis, LEAP dte.
# `cur_px` is today's (synthetic) stock price the cache is anchored to — kept a
# touch below each open short strike so the weekly call is still OTM and working.
BOOK = [
    dict(ticker="NVDA", strike=90, entry_px=112, cur_px=128, extr_per_contract=480,
         weeks=8, sold=0.95, paid=0.35, leap_tv=1700, short=(132, 5, 1.05),
         shares=300, share_cost=104.0, leap_dte=158),
    dict(ticker="AVGO", strike=140, entry_px=168, cur_px=189, extr_per_contract=520,
         weeks=5, sold=0.90, paid=0.33, leap_tv=2050, short=(196, 4, 1.00),
         shares=100, share_cost=158.0, leap_dte=143),
    dict(ticker="UBER", strike=58, entry_px=72, cur_px=81, extr_per_contract=440,
         weeks=5, sold=0.82, paid=0.30, leap_tv=1500, short=(83, 3, 0.92),
         shares=200, share_cost=68.0, leap_dte=131),
    dict(ticker="AMD", strike=120, entry_px=146, cur_px=164, extr_per_contract=400,
         weeks=2, sold=0.88, paid=0.36, leap_tv=1650, short=(168, 2, 1.10),
         shares=0, share_cost=None, leap_dte=28),
]
CONTRACTS = config.LEAP_CONTRACTS  # 5


def seed_state(last_close: dict[str, float]) -> None:
    # Start from a clean slate so re-running is idempotent.
    if os.path.exists(config.active_state_path()):
        os.remove(config.active_state_path())
    log.load_state()  # writes the default empty state

    for spec in BOOK:
        t = spec["ticker"]
        # 1) Open the LEAP. extrinsic_at_entry = (exec_price - intrinsic)*contracts.
        intrinsic_pc = (spec["entry_px"] - spec["strike"]) * 100
        exec_price = intrinsic_pc + spec["extr_per_contract"]
        executor.execute({
            "action": "buy_leap", "ticker": t, "strike": spec["strike"],
            "contracts": CONTRACTS, "execution_price": exec_price,
            "stock_price": spec["entry_px"], "dte": spec["leap_dte"],
            "expiration": "2026-12-18",
        })

        # 2) A weekly sell-then-close cycle for each paid-back week. Closing OTM
        #    (stock below the short strike) means the whole close price is time
        #    value paid back, so net juice = (sold - paid) * contracts * 100.
        for _ in range(spec["weeks"]):
            k = spec["strike"] + spec["extr_per_contract"] / 10  # a near-money weekly
            executor.execute({
                "action": "sell_short", "ticker": t, "strike": k,
                "contracts": CONTRACTS, "premium_per_share": spec["sold"],
                "stock_price": k,
            })
            executor.execute({
                "action": "close_short", "ticker": t, "strike": k,
                "contracts": CONTRACTS, "close_price_per_share": spec["paid"],
                "stock_price": k - 1, "extrinsic_sold": spec["sold"],
            })

        # 3) Leave one short open (this week's juice still working).
        sk, sdte, ssold = spec["short"]
        executor.execute({
            "action": "sell_short", "ticker": t, "strike": sk,
            "contracts": CONTRACTS, "premium_per_share": ssold, "stock_price": sk,
            "dte": sdte,
        })

    # Backdate the execution log + open shorts so the ledger spreads across weeks
    # (executor stamps everything "now"), then patch the live-market fields a
    # quote feed would supply (current LEAP bid, shares, DTEs).
    state = log.load_state()
    for spec in BOOK:
        t = spec["ticker"]
        closes = [e for e in state["executions"] if e["ticker"] == t and e["action"] == "close_short"]
        sells = [e for e in state["executions"] if e["ticker"] == t and e["action"] == "sell_short"]
        buys = [e for e in state["executions"] if e["ticker"] == t and e["action"] == "buy_leap"]
        fridays = _fridays(spec["weeks"])
        for e, fri in zip(closes, fridays):
            e["date"] = f"{fri}T20:00:00Z"
        # paired sells sit a few days before their close; the trailing one is the
        # still-open short opened this week.
        for i, e in enumerate(sells):
            if i < len(fridays):
                d = datetime.strptime(fridays[i], "%Y-%m-%d") - timedelta(days=4)
                e["date"] = d.strftime("%Y-%m-%dT15:30:00Z")
            else:
                e["date"] = "2026-06-25T15:30:00Z"
        for e in buys:
            e["date"] = "2026-04-20T15:00:00Z"

        # Patch the position with what a live feed would fill in.
        pos = log.find_position(state, t)
        pos["entry_date"] = "2026-04-20"
        pos["thesis"] = {"fundamentals": f"{t}: sector leader, RS3M intact, accumulating.",
                         "intact": True}
        leap = pos["leap"]
        leap["dte"] = spec["leap_dte"]
        # current LEAP value = today's intrinsic (from the cache price) + time value.
        px = last_close.get(t, spec["entry_px"])
        intrinsic_now = max(px - spec["strike"], 0.0) * CONTRACTS * 100
        leap["current_bid"] = round(intrinsic_now + spec["leap_tv"], 2)
        sh = pos["shares"]
        sh["count"] = spec["shares"]
        sh["cost_basis_per_share"] = spec["share_cost"]
        # The single open short: set its real DTE and a recent open date.
        for sc in pos["short_calls"]:
            sc["open_date"] = "2026-06-25"
            sc["dte"] = spec["short"][1]

    # Portfolio capital + reserve (drives the Positions capital card + milestones).
    state["metadata"].update({
        "capital_deployed": 31200,
        "operating_cash": 14500,
        "reserve_required": config.RESERVE_REQUIRED,
    })

    log.recompute_derived(state)
    log.save_state(state)


def seed() -> int:
    """(Re)build the demo dataset (synthetic cache + sample book). Always targets
    the demo store — never the live one — by switching the process into demo mode
    first, so the handlers write to the demo paths. Returns the position count."""
    config.set_demo_enabled(True)
    strong = {s["ticker"] for s in BOOK}
    anchors = {s["ticker"]: float(s["cur_px"]) for s in BOOK}
    last_close = generate_market_cache(strong, anchors)
    seed_state(last_close)
    return len(BOOK)


def is_seeded() -> bool:
    return (os.path.exists(config.DEMO_STATE_PATH)
            and os.path.isdir(config.DEMO_CACHE_DIR)
            and bool(os.listdir(config.DEMO_CACHE_DIR)))


def ensure_seeded() -> bool:
    """Seed the demo dataset only if it isn't already present. Returns True if it
    (re)built. Cheap no-op when the demo store already exists."""
    if is_seeded():
        return False
    seed()
    return True


def clear() -> None:
    config.set_demo_enabled(False)
    if os.path.exists(config.DEMO_STATE_PATH):
        os.remove(config.DEMO_STATE_PATH)
        print(f"removed {config.DEMO_STATE_PATH}")
    if os.path.isdir(config.DEMO_CACHE_DIR):
        shutil.rmtree(config.DEMO_CACHE_DIR)
        print(f"removed {config.DEMO_CACHE_DIR}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed (or clear) demo data for the CFM dashboard.")
    ap.add_argument("--clear", action="store_true", help="remove the demo store and switch back to live")
    args = ap.parse_args()
    if args.clear:
        clear()
        return

    print(f"generating synthetic market cache under {config.DEMO_CACHE_DIR} …")
    n = seed()
    print(f"  cached the universe and seeded {config.DEMO_STATE_PATH} with {n} positions")
    print("demo mode is now ON. Start the backend (python backend/app.py) and open the dashboard,")
    print("or toggle Live/Demo from the navbar. Run with --clear to remove it.")


if __name__ == "__main__":
    main()
