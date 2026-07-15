# CLAUDE.md

Guidance for Claude Code working in this repo. Keep it short; update it when the
facts below change.

## What this is

A full-stack "CFM" options-strategy dashboard (scan → gate → execute → track):

- **`backend/`** — Python 3.10+ / Flask API. Flat module layout (modules import
  each other by bare name: `import logging_handler`). Entry point: `app.py`.
- **`frontend/`** — React + Vite + Tailwind SPA. Entry: `src/index.jsx`.
- **`scripts/`**, root `*.py` — operational helpers (calibration, VAPID keys, etc.).

`state.json` (on the Fly volume at `$DATA_DIR/state.json`, `backend/` locally) is
the **single source of truth**. The execution log is append-only and immutable;
positions and the theta/payback ledgers are **derived** from it by
`logging_handler.recompute_derived()`. Prefer fixing derivation over editing state.

## Commands

```bash
# Tests (deps are installed by the SessionStart hook)
python -m pytest backend -q                 # full suite
python -m pytest backend/test_payouts.py -q # one file

# Run locally
./start.sh                                   # backend + frontend (start.bat on Windows)
cd backend && python app.py                  # backend only
cd frontend && npm install && npm run build  # build the UI
```

There is **no configured linter/formatter** (no ruff/flake8/black/eslint config).
Match the surrounding style; don't introduce a linter unless asked.

## Conventions worth knowing

- **Units:** LEAP prices and extrinsic are stored **per-contract** but displayed
  **per-share** (÷100). Short premiums/extrinsic are per-share. When editing either
  layer, keep both consistent (see `executor._apply_txn_edit`, `HistoryTab.jsx`).
- **Period bucketing:** bucket executions by date→expiration via
  `logging_handler.bucket_datetime()` — both the theta ledger and the Payouts view
  key off it so they can't disagree. Never re-derive week/month with a bespoke
  parser.
- **Optional deps:** `pywebpush` (web-push) and `boto3` (S3 backup) are lazily
  imported and not required for the test suite. `pywebpush`'s `http-ece` sub-dep
  may fail to build in some containers; that's expected and non-fatal.
- Tests use `backend/conftest.py`'s scriptable `MockSchwabClient`; no live Schwab
  calls are ever made.

## Session startup

`.claude/hooks/session-start.sh` (registered in `.claude/settings.json`) installs
backend Python deps + `pytest` and frontend npm deps on remote (web) sessions, and
puts `backend/` on `PYTHONPATH`. It's idempotent and runs synchronously.
