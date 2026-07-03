#!/usr/bin/env python3
"""Report which tickers in the CFM universe are dead or CFM-unusable.

Run against LIVE data (needs Schwab / Alpha Vantage configured — it fetches
OHLCV for every name). In demo mode the sweep is skipped (synthetic store).

    python scripts/check_universe.py              # data availability only
    python scripts/check_universe.py --weeklies   # also probe weekly options (slow)

`no_data` names are renamed / delisted / typo'd tickers that silently show "—"
everywhere and never scan — fix them in tickers_by_sector.txt. `no_weeklies`
names have data but can't run CFM (no weekly short to sell).
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")

import universe_health  # noqa: E402


def _group(rows):
    by_sector: dict[str, list] = {}
    for r in rows:
        by_sector.setdefault(r["sector"] or "?", []).append(r["ticker"])
    return by_sector


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weeklies", action="store_true",
                    help="also probe weekly options (one chain call per ticker — slow)")
    args = ap.parse_args(argv)

    rep = universe_health.check(check_weeklies=args.weeklies)
    if rep.get("skipped"):
        print(rep["skipped"])
        return 0

    print(f"Universe: {rep['total']} tickers · {rep['with_data']} returned data · "
          f"{len(rep['no_data'])} dead")
    if rep["no_data"]:
        print("\nNO DATA (renamed / delisted / typo — fix in tickers_by_sector.txt):")
        for sector, ts in sorted(_group(rep["no_data"]).items()):
            print(f"  {sector:5} {', '.join(ts)}")
    else:
        print("\nEvery ticker returned data — no dead names.")

    if rep.get("checked_weeklies"):
        print(f"\nCFM-ready (data + weeklies): {rep['cfm_ready']}")
        if rep["no_weeklies"]:
            print("NO WEEKLIES (has data, but can't run CFM — no weekly short):")
            for sector, ts in sorted(_group(rep["no_weeklies"]).items()):
                print(f"  {sector:5} {', '.join(ts)}")
    else:
        print("\n(Run with --weeklies to also flag names that lack weekly options.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
