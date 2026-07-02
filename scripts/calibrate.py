#!/usr/bin/env python3
"""Offline threshold-calibration harness (thin CLI wrapper).

Replays the scorecard over the cached OHLCV history and emits a markdown
report of forward 4-8 week returns per verdict bucket and per candidate
threshold. See backend/calibration.py for the logic.

    python scripts/calibrate.py                       # all holdings
    python scripts/calibrate.py --tickers NVDA,AMD    # subset
    python scripts/calibrate.py --out report.md
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from calibration import main  # noqa: E402

if __name__ == "__main__":
    main()
