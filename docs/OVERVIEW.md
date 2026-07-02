# CFM Dashboard — System Overview

A single-strategy options-income dashboard for the Cash Flow Machine (CFM), a
poor-man's-covered-call diagonal: buy deep-ITM LEAP calls (~0.90 delta,
~180 DTE), sell weekly ITM short calls at strike ≈ stock − 1.5×ATR, roll
weekly, track extrinsic payback until income-positive.

Architecture invariants (do not break):

- **state.json is the single source of truth.** Execution records are
  immutable; every ledger/meter/summary is *derived* from them, never
  hand-entered. Single-writer: exactly one Fly.io machine, persistent volume
  at `/data`.
- **Data**: Schwab Trader API primary, Alpha Vantage fallback, daily OHLCV
  cached to parquet. No other providers.
- **Paper by default**: `CFM_LIVE_TRADING=1` gates real order transmission.
- **Config provenance**: every threshold lives in `backend/config.py` (or
  `backend/metrics/thresholds.py`) labeled `HARD_CFM_RULE` (a stated CFM rule
  — changing it changes the strategy) or `PROPOSED_DEFAULT` (a tunable guess
  pending calibration).
- **Demo mode parity**: every feature works against the seeded demo store
  (`state.demo.json` + `cache_demo/`) with no provider keys.

## State schema versioning

`backend/migrations.py` upgrades older `state.json` files on load
(`schema_version`, treated as 1 when absent). Migrations only add structure —
they never rewrite executions.

| version | adds |
| --- | --- |
| 2 | `alerts` (active set, capped log, settings, last_run) — Phase 0 |

## Alerting engine (Phase 0)

The operator works a day job; "exit immediately" rules are only followable if
the app notifies. `backend/alerts.py` evaluates the conditions below,
**dedups** against the active set persisted in `state.alerts` (a condition
fires once when it trips, refreshes `last_seen` while it stays true,
auto-resolves when it clears, and can fire again on a re-trip), then hands
only *new* alerts to the notifier.

| alert | severity | rule source | condition |
| --- | --- | --- | --- |
| `KILL_SWITCH_SECTOR` | CRITICAL | HARD_CFM_RULE (kill switch) | RS3M vs Sector negative for an open position → exit immediately |
| `KILL_SWITCH_SPY` | CRITICAL | HARD_CFM_RULE (kill switch) | RS3M vs SPY negative on confirmed close → exit within 1–2 days |
| `CIRCUIT_BREAKER` | CRITICAL | HARD_CFM_RULE (line in the sand) | last close at/below the position's stored circuit-breaker price |
| `DELTA_UNCOVERED` | HIGH | HARD_CFM_RULE (coverage) | LEAP delta < 0.50 floor, or long delta < a short leg's delta |
| `DEFEND_POSITION` | HIGH | HARD_CFM_RULE (defense) | underlying closed below the short strike; includes suggested roll-down strike (price − 1.5×ATR) |
| `ASSIGNMENT_RISK` | HIGH | HARD_CFM_RULE (dividend/assignment) | short extrinsic < upcoming dividend before ex-div. Note: the short is covered by a LEAP, not stock — assignment creates *short stock* that owes the dividend |
| `TOKEN_EXPIRY` | HIGH | PROPOSED_DEFAULT (`TOKEN_WARN_AGE_DAYS`=5) | Schwab refresh token older than 5 days (dies at ~7) |
| `BUYBACK_75` | MEDIUM | HARD_CFM_RULE (75% buyback) | short lost ≥75% of sale premium with >2 DTE → roll early to capture juice |
| `EARNINGS_WINDOW` | MEDIUM | HARD_CFM_RULE (earnings) | earnings within `EARNINGS_WARN_DAYS` for an open position |
| `EXPIRY_FRIDAY` | MEDIUM | HARD_CFM_RULE (weekly roll) | short expiring today/tomorrow not yet rolled |
| `DATA_STALE` | MEDIUM | PROPOSED_DEFAULT (`DATA_STALE_HOURS`=30) | cached OHLCV older than expected on a market day |

`TOKEN_EXPIRY` and `DATA_STALE` are skipped in demo mode (they describe the
real providers, not the demo store).

**Scheduler** (`backend/alert_scheduler.py`): an in-process daemon thread —
the volume attaches to one machine and state.json is single-writer, so a
separate scheduled machine can't share `/data`. Fires at ET slots
(`ALERT_SCHEDULE_ET`: 08:30, 10:00, 12:30, 15:30) on weekdays, once per slot
per day; fly.toml pins `min_machines_running = 1` so the machine is awake.
Alternative/backup: an external cron can `POST /api/alerts/run` (auto-start
wakes the machine; dedup makes repeat runs no-ops). Disable the thread with
`CFM_ALERTS_SCHEDULER=0`.

**Delivery** (`backend/notifier.py`): pluggable `Notifier` interface —
implement `name`, `configured()`, `send()` and add to `CHANNELS`. Shipped:

- `email` — SMTP; env `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`,
  `SMTP_PASSWORD`, `ALERT_EMAIL_TO`, `ALERT_EMAIL_FROM`.
- `ntfy` — push via ntfy.sh; env `ALERT_NTFY_TOPIC`, optional
  `ALERT_NTFY_SERVER`.
- `log` — dry-run/fallback (used when `CFM_ALERTS_DRY_RUN=1`, the dry-run
  setting is on, or nothing is configured).

**API**: `GET /api/alerts` (active + log + settings), `POST /api/alerts/run`,
`POST /api/alerts/ack {id}`, `POST /api/alerts/settings` (per-type
enable/disable, channel toggles, dry-run).

**UI**: Alerts panel on the Checklist tab (acknowledge, run-now, settings,
history) plus a navbar bell with the active count.

**Demo**: seeding adds a deliberately broken 5th position
(`seed_demo_data.ALERT_DEMO`, PG) that trips every position-based condition in
one evaluator run — kill switch, circuit breaker, both delta flavors, defend,
75% buyback, assignment risk, earnings window, and expiry — exactly 9 alerts.
