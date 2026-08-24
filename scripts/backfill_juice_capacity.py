#!/usr/bin/env python3
"""Bootstrap the juice-capacity observation history (thin CLI wrapper).

A ONE-TIME operator action, deliberately not wired into the nightly sweep: a
full-universe replay is ~4-5 minutes of CPU, which does not belong on a job that
otherwise finishes in seconds. After this runs, the nightly sweep keeps the
series current at one observation per name per scan day.

Two sources, both offline — neither makes a provider call:

  --seed      recover real readings from scan_rejection_log, which has been
              persisting combined_weekly_yield_pct per candidate since schema
              v21. These carry a genuine dividend leg.
  --backfill  replay the juice computation over cached daily bars. Exact rather
              than approximate (the number is computed entirely from bars — see
              backend/juice_capacity.backfill), but JUICE-ONLY: no dividend
              history exists to replay.

Run both, seed first: seeded days are real readings and a backfill never
overwrites an existing observation, so the richer source wins where they
overlap.

    python scripts/backfill_juice_capacity.py --seed --backfill
    python scripts/backfill_juice_capacity.py --backfill --tickers ET,XLE
    python scripts/backfill_juice_capacity.py --backfill --force   # re-replay

The metric this feeds is SHADOW: display and telemetry only, zero authority.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import juice_capacity  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", action="store_true",
                    help="recover observations from the scan rejection log")
    ap.add_argument("--backfill", action="store_true",
                    help="replay observations from cached daily bars")
    ap.add_argument("--tickers", help="comma-separated subset (default: the universe)")
    ap.add_argument("--force", action="store_true",
                    help="re-replay symbols that already have history")
    args = ap.parse_args()

    if not (args.seed or args.backfill):
        ap.error("pick at least one of --seed / --backfill")

    tickers = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
               if args.tickers else None)

    # Seed first: a backfill gap-fills and never overwrites, so real readings
    # already on disk survive the replay that follows.
    if args.seed:
        out = juice_capacity.seed_from_scan_rejection_log(tickers)
        print(f"seed:     {out}")
    if args.backfill:
        out = juice_capacity.backfill(tickers, force=args.force)
        print(f"backfill: recorded={out.get('recorded')} "
              f"symbols={out.get('symbols')} skipped={len(out.get('skipped') or [])}")
        if not out.get("ok"):
            print(f"          error: {out.get('error')}", file=sys.stderr)
            return 1

    for t in (tickers or [])[:20]:
        d = juice_capacity.capacity_detail(t)
        print(f"  {t:<6} capacity={d['capacity']}  obs={d['obs']}  {d['by_source']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
