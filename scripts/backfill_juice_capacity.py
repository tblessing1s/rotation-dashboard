#!/usr/bin/env python3
"""Bootstrap the trailing juice-capacity history from cached daily bars.

The scan's weekly juice is a PURE function of the cached daily frame (a
Black-Scholes weekly short priced at trailing REALIZED vol — no IV input, no
provider call, no state, no clock). So replaying it over each bar prefix yields
the number the scan WOULD have shown on that date: an exact reconstruction, not
an approximation. That is what makes the capacity median meaningful on day one
instead of a month from now.

Offline and opt-in — this is NOT wired into the nightly sweep or any request
path. It reads the parquet cache and writes DATA_DIR/juice_capacity.json.
Backfilled observations are marked `source: backfill_bar_replay` and stay
distinguishable from live ones forever; they never overwrite a live point.

Two anachronisms are marked on the records rather than hidden — the dividend leg
carries today's yield against a past date (there is no dividend-yield history in
this tree), and the recorded regime is provenance only. See
backend/juice_capacity.py:backfill.

    python scripts/backfill_juice_capacity.py                    # whole universe
    python scripts/backfill_juice_capacity.py --tickers ET,XLE   # a subset
    python scripts/backfill_juice_capacity.py --step 5           # sample every 5th bar
    python scripts/backfill_juice_capacity.py --force            # re-replay covered names
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import juice_capacity  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", help="comma-separated subset (default: the whole universe)")
    ap.add_argument("--step", type=int, default=1,
                    help="trading days between replayed as-of dates (default 1)")
    ap.add_argument("--force", action="store_true",
                    help="re-replay symbols that already carry backfilled history")
    args = ap.parse_args()

    tickers = ([t.strip() for t in args.tickers.split(",") if t.strip()]
               if args.tickers else None)
    out = juice_capacity.backfill(tickers, force=args.force, step=args.step)
    print(json.dumps(out, indent=2))
    if out.get("ok"):
        print(json.dumps(juice_capacity.summary(), indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
