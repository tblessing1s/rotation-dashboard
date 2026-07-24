# Application Summary

A one-read orientation to the whole system. For strategy rationale and setup see
[`README.md`](../README.md); for the dense, phase-by-phase internals see
[`docs/OVERVIEW.md`](OVERVIEW.md). This doc sits above both — enough to build an
accurate mental model in a few minutes, then point you at the depth.

## What it is

A single-user, single-strategy **options-income dashboard** for the "Cash Flow
Machine" (CFM) — a poor-man's-covered-call diagonal: buy a deep-ITM LEAP call
(~0.90 delta, ~180 DTE) as the deployed capital, sell weekly ITM short calls
against it, roll weekly, and track extrinsic payback until each position is
income-positive. It is full-stack — a **Python/Flask API** (`backend/`) plus a
**React + Vite + Tailwind SPA** (`frontend/`) — served as one process and
deployed as a single Fly.io machine.

## The core idea (two invariants)

- **`state.json` is the single source of truth.** The execution log is
  append-only and immutable; every position, ledger, meter, and summary is
  *derived* from it by `logging_handler.recompute_derived()`, which runs after
  each append and is byte-stable on a full rebuild. You fix derivation, not
  state. Single-writer: exactly one machine, one volume at `/data`.
- **Paper by default.** Executions are always captured against live prices and
  logged; `CFM_LIVE_TRADING=1` is what gates real Schwab order *transmission*.
  The state updates identically either way (the honest paper path).

## The workflow spine: scan → gate → execute → track

This is literally the app's tagline and its structure.

- **Scan** — compute market regime, sector strength, a stock filter, and a
  per-symbol scorecard over cached daily bars. The Scan tab runs a detached,
  memoized full-universe sweep and surfaces a "Ready to Enter" shortlist.
  *(`screening.py`, `metrics/scorecard.py`, `indicators.py`; Scan tab.)*
- **Gate** — a 4-level entry gate ("right stock, right tape") plus a server-side
  **Level-5 Account & Juice** gate ("is the account ready and does the trade
  pay") enforced inside `executor.execute()` for every `buy_leap`; a hostile
  open/close window can additionally defer order transmission.
  *(`screening.py`, `account_gate.py`, `execution_gate.py`; ExecuteTab.)*
- **Execute** — capture the live price + premium, run the gates, transmit to
  Schwab only if live (else paper-log), and append the immutable execution;
  live orders flow through a submit → poll → commit/auto-cancel lifecycle.
  *(`executor.py`, `schwab_api.py`, `order_lifecycle.py`; OptionChainModal,
  RollModal.)*
- **Track** — derive positions, the theta/payback ledgers, monthly payouts, and
  guardrails (kill switch, coverage, burn, alerts); reconcile state against the
  broker and surface out-of-band trades as one-click adoptions.
  *(`position_manager.py`, `burn.py`, `payouts.py`, `reconcile.py`,
  `transaction_ingest.py`; Positions / History / Payouts tabs.)*

## Architecture at a glance

```
  Schwab (primary) ─┐
                    ├─►  data_handler  ──►  parquet cache (DATA_DIR/cache)
  Alpha Vantage  ───┘         │
                              ▼
        indicators (RS3M · ATR · MA · RSI · breadth)
                              ▼
     screening (regime · sectors · stock filter · entry gate)
                              ▼
           Flask API  ◄──►  state.json (source of truth)
                              ▼
                     React + Tailwind UI
```

Data sources are **Schwab + Alpha Vantage only**. The backend uses a **flat
module layout** — modules import each other by bare name (`import
logging_handler`) with `backend/` on `PYTHONPATH`. The frontend is a **single
SPA** with no router and no state library: `App.jsx` routes by state across the
tabs below.

## Backend (`backend/`, Flask)

Entry point `app.py` wires ~95 routes grouped by workflow stage and serves the
built frontend. The most important modules:

| Role | Module(s) | Responsibility |
|---|---|---|
| State / persistence | `logging_handler.py` | Atomic single-writer `state.json` I/O; append-only log; `recompute_derived()`. |
| Config | `config.py` | Every threshold, labeled `HARD_CFM_RULE` (strategy) or `PROPOSED_DEFAULT` (tunable). |
| Execution | `executor.py`, `schwab_api.py`, `order_lifecycle.py` | Run gates, capture price/premium, transmit/paper-log, track order state. |
| Gates | `account_gate.py`, `execution_gate.py` | Level-5 Account & Juice; time-of-day order discipline. |
| Scan | `screening.py`, `metrics/scorecard.py`, `indicators.py` | Regime, sectors, entry gate, per-symbol scorecard, TA + Black-Scholes. |
| Data | `data_handler.py`, `alpha_vantage.py`, `option_chain.py` | Daily OHLCV (Schwab → AV → parquet); option chains; strike picking. |
| Tracking | `position_manager.py`, `burn.py`, `payouts.py`, `leap_policy.py` | Position math, theta burn, monthly payout, LEAP lifecycle. |
| Trust layer | `recommendation_engine.py`, `trust_derive.py` | Pure decision engine + coverage/fidelity scoreboard (display-only). |
| Reconciliation | `reconcile.py`, `transaction_ingest.py` | Verify state vs broker; freeze on divergence; ingest transactions as truth. |
| Alerts | `alerts.py`, `alert_scheduler.py`, `notifier.py`, `webpush.py` | Rule engine, ET-slot scheduler, pluggable delivery channels. |

`state.json` holds the immutable `executions` log plus derived `positions`,
`theta_ledger`, `extrinsic_payback`, `roll_ledger`, `cycles`, `payouts`, order
plumbing, the trust layer, and reconciliation ledgers. See
[`docs/OVERVIEW.md`](OVERVIEW.md) for the full alert/gate/threshold catalog.

## Frontend (`frontend/src/`, React + Tailwind)

`App.jsx` drives six tabs plus one overlay flow:

| Tab / flow | Component | Purpose |
|---|---|---|
| Overview | `Overview.jsx` | Landing digest from one `/api/overview` call: regime, "needs attention", book, income. |
| Scan | `ScanProgress` + `ReadyToEnter` + `Scorecard` | Find an entry; detached universe sweep + shortlist. |
| Positions | `PositionTracker.jsx` | Manage the book: health, juice battery, defend/roll, reconciliation, rec cards. |
| History | `HistoryTab.jsx` | Closed-cycle learning loop: theta ledger, cycle log, editable transactions. |
| Payouts | `PayoutsTab.jsx` | Monthly income withdrawals: in progress → finalizable → finalized → paid. |
| Settings | `SettingsTab.jsx` | Posture/demo toggles, live-trading switch, alerts, trust scoreboard, data health. |
| Execute (overlay) | `ExecuteTab.jsx` | The 5-level entry gate + order ticket (OptionChainModal / RollModal). |

A single thin `src/api.js` fetch client exposes ~90 endpoint methods; components
call `api.*` directly. State is plain React with a core `useApi(fn, deps,
interval, retries)` polling hook; cross-component signaling uses `window`
CustomEvents. Tailwind (dark slate + traffic-light palette) is the only styling
system. The app is an installable **PWA** with service-worker **web-push**.

## Operations

- **Deploy** — `Dockerfile` (two stages: node builds the UI, python runs
  Gunicorn); `fly.toml` pins `min_machines_running = 1` (single writer) with a
  `/data` volume for `state.json`, the parquet cache, and the Schwab token.
- **Scheduler** — the alert engine runs as an in-process daemon thread firing at
  ET slots; an optional `HEALTHCHECK_URL` dead-man's switch pages you if it goes
  quiet.
- **Backups** — nightly under `DATA_DIR/backups/`; optional off-machine to S3/
  Tigris (`boto3`, lazy) or email.
- **Helpers** — `scripts/`: `calibrate.py` (threshold calibration),
  `gen_vapid_keys.py` (web-push keys), `restore_state.py`, `check_universe.py`.
- **CI** — `.github/workflows/`: `fly.yml` (deploy on push to `master`) and
  `backup-setup.yml`. Tests are run locally / in-session, not in CI. A
  `SessionStart` hook (`.claude/`) installs deps for remote web sessions.

## Domain glossary

| Term | Meaning |
|---|---|
| PMCC / LEAP | Poor-man's covered call: a deep-ITM LEAP long is the capital; a weekly short call is sold against it. |
| Juice | Net short premium (sold − buyback), booked per `close_short`. |
| Extrinsic / burn | Only extrinsic consumed during the hold is a true cost. Burn = a two-point Black-Scholes *difference* per week — never straight-line proration. |
| Payback meter | How much of the LEAP's entry extrinsic the collected juice has repaid; cycle-scoped, carries across rolls. |
| Per-share vs per-contract | LEAP prices/extrinsic are stored per-contract, displayed per-share (÷100). Keep both consistent when editing. |
| Period bucketing | Closes bucket by fill date → ISO week / month / year (`logging_handler.bucket_datetime()`); undated fills go to an `UNDATED` bucket. |
| Kill switch | RS3M vs Sector negative → exit immediately; RS3M vs SPY negative (confirmed) → exit within 1–2 days. |
| Trust layer | Grades whether the recommendation engine committed before the operator acted, and whether each order's lifecycle was legal. Display-only; nothing auto-trades. |

## Run / test / deploy

```bash
./start.sh                       # backend + frontend locally → http://localhost:5179
python -m pytest backend -q      # full test suite (offline, no provider keys)
cd frontend && npm install && npm run build   # build the UI
fly deploy && fly scale count 1  # deploy (single writer) — see README for secrets
```

See [`README.md`](../README.md) for full credentials, Schwab OAuth, the login
gate, and push-notification setup.

## Where to go deeper

- [`README.md`](../README.md) — strategy, setup, credentials, deploy, PWA/push.
- [`docs/OVERVIEW.md`](OVERVIEW.md) — the full system spec (every alert, gate,
  threshold, phase).
- [`CLAUDE.md`](../CLAUDE.md) — conventions and gotchas for working in the repo.
- Other `docs/` — [`emergency-exit.md`](emergency-exit.md),
  [`leap-lifecycle.md`](leap-lifecycle.md),
  [`reconciliation.md`](reconciliation.md), [`recovery.md`](recovery.md),
  [`trust-layer.md`](trust-layer.md).

---

This implements a mechanical framework. It is not financial advice; the
GO/WAIT verdicts are checklist outputs, not recommendations.
