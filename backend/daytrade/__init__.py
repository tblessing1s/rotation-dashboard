"""Day-trade sleeve (TRAVIS_EXTENSION) — a rules-based intraday strategy run
alongside CFM for capital too small to fit a CFM position. See the package's
modules for the pieces:

  store.py      side-channel persistence (DATA_DIR/daytrade_log/), the same
                zero-authority pattern csp_dry_powder.py uses
  universe.py   Rule 1 — nightly screener + prior-day levels
  bars.py       5-min bar ingestion during the Rule 2 signal window
  scheduler.py  in-process thread wiring the two jobs together

Phase 1 (this change) is schema + scheduled ingestion only: no signal engine,
no paper/live execution, no UI. It shares Schwab auth, quotes, and the account
layer with CFM (data_handler, schwab_api, accounts) but the rotation regime
gate does not feed into it, and it never touches state.json or `positions` —
nothing here is a real position until a later phase's execution adapter exists.
"""
