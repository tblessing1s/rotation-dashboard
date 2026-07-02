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
| 3 | per-position `circuit_breaker` + `dividend` snapshot — Phase 1 |
| 4 | `roll_ledger` (derived from paired roll executions) — Phase 2 |
| 5 | `cycles` (derived closed-cycle records) — Phase 3 |

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

## Level 5 entry gate — Account & Juice (Phase 1)

The 4-level gate answers "right stock, right tape"; Level 5
(`backend/account_gate.py`) answers "is the ACCOUNT ready and does the TRADE
pay". It is enforced server-side inside `executor.execute` for every
`buy_leap`: a blocking failure rejects the entry (HTTP 400) unless the payload
carries a typed `override_reason`, which is logged on the immutable execution
record together with the checks it overrode.

Blocking checks:

- **cash_reserve** — post-trade free cash ≥ Σ `RESERVE_ATR_MULT`(2)×ATR$×contracts×100
  across all open positions incl. the proposed one (PROPOSED_DEFAULT formula;
  the computed number is shown).
- **position_limit** — ≤ `MAX_CFM_POSITIONS` (2, HARD_CFM_RULE).
- **capital_limit** — deployed + proposed ≤ `MAX_DEPLOYED_CAPITAL`
  ($38K, PROPOSED_DEFAULT in the $35–40K band).
- **sector_concentration** — < `MAX_POSITIONS_PER_SECTOR` (1, PROPOSED_DEFAULT)
  already open in the same sector (the filters funnel into the hottest sector).
- **juice_adequacy** — weekly extrinsic ÷ LEAP cost ≥
  `CYCLE_RETURN_MIN/CYCLE_WEEKS_MAX` (~1.88%/week; 15–25% over 4–8 weeks is
  HARD_CFM_RULE). Uses real chain numbers when the Execute flow has them,
  else a Black–Scholes estimate at the ticker's trailing realized vol.

Warnings (non-blocking): **juice_rich** (premium > `JUICE_RICH_FACTOR`(1.75)×
history-implied — risk pricing), **earnings_in_cycle** (report inside the
8-week cycle window).

Additionally, every entry **stores a circuit breaker** (line in the sand):
the operator's price or the default `max(MA50, entry − 2×ATR)`
(`CIRCUIT_BREAKER_ATR_MULT`, PROPOSED_DEFAULT) — this feeds the
`CIRCUIT_BREAKER` alert — and a **dividend snapshot**
(`dividends.next_dividend`: next ex-date + per-payment amount, day-cached,
Schwab fundamentals → Alpha Vantage OVERVIEW → manual override
`dividend_event_overrides` in metadata) — this feeds `ASSIGNMENT_RISK`.

**Scorecard** gains two columns: `Juice/wk` (history-implied weekly extrinsic ÷
LEAP cost vs the target, red when inadequate) and `Earnings` (days to the next
report, cache/override-only — the scorecard never triggers a provider fetch
storm). **API**: `GET /api/account-gate?ticker=&contracts=&leap_cost=&weekly_extrinsic=`.

## Position management mechanics (Phase 2)

- **75% buyback rule** (HARD_CFM_RULE): every open short shows `% decayed`
  (sale premium vs current value, both derived from stored execution data);
  ≥75% decayed with >2 DTE shows a ROLL NOW badge on the Positions tab (and
  fires the `BUYBACK_75` alert). Clicking it stages the roll with reason
  `75%-rule`.
- **Defend / roll-down engine**: when the underlying closes below a short
  strike, `GET /api/defend?ticker=` returns the defensive roll: new strike =
  price − 1.5×ATR (GREEN) or 2.0×ATR (YELLOW), est. net credit/debit, the new
  short's extrinsic, and the cost-basis effect. The Positions tab shows the
  recommendation with one-click staging into the roll modal (reason `defend`).
  Estimates come from trailing vol; the staged roll re-prices from the live
  chain.
- **Atomic rolls in live mode**: `roll_short` with `CFM_LIVE_TRADING=1`
  transmits ONE two-leg NET_CREDIT/NET_DEBIT ticket
  (`schwab_api.build_roll_order`: BUY_TO_CLOSE old + SELL_TO_OPEN new) — no
  legging risk — through the same pending → poll → commit/auto-cancel
  lifecycle; the commit overlays the actual per-leg fill prices. Paper mode
  logs both legs immediately at the staged prices.
- **Roll-cost / whipsaw ledger** (`state.roll_ledger`, fully derived): both
  legs of every roll carry `roll_id` + `roll_reason`
  (scheduled | 75%-rule | defend | earnings | kill-switch-exit);
  `recompute_derived` rebuilds per-roll entries (buyback cost, new premium,
  net) and per-ticker aggregates (count, net_total, drag_total = debits paid).
  Positions tab shows cumulative roll drag per position; Theta tab nets rolls
  against juice. This is the dataset that later validates 1.5× vs 2×ATR strike
  placement.
- **Assignment-risk monitor**: each short is checked against the position's
  stored dividend (extrinsic < dividend before ex-div → flag + alert). The
  flag's tooltip explains the PMCC nuance: the short is covered by a LEAP, not
  stock, so assignment creates SHORT STOCK that owes the dividend — roll
  before ex-div.
- **Accumulation vs kill-switch** (`BLOCK_ACCUMULATION_ON_RS_DETERIORATION`,
  HARD_CFM_RULE candidate, OFF by default pending confirmation): when on,
  `can_add_shares` refuses accumulation on any name whose kill switch reads
  red (exit in progress) or yellow (RS3M thinning toward the kill line) — the
  pullback play buys weakness, the kill switch sells it; without the guard the
  two rules can add to a name the strategy is 1–2 days from exiting.

## Exit, history & the learning loop (Phase 3)

- **Closed-cycle records** (`state.cycles`, derived): one immutable summary per
  buy_leap → close_leap window — entry/exit dates, days held, capital deployed
  (LEAP cost), gross juice, roll count/net/drag, LEAP P&L, net result and
  return % vs the 15–25% target, exit reason, and the **scorecard snapshot at
  entry**. The snapshot and exit reason are captured onto the executions at
  trade time (`entry_snapshot` on buy_leap, `exit_reason` on close_leap —
  enum: target hit | trailing stop | kill switch | circuit breaker | earnings |
  discretionary); everything else re-derives deterministically.
- **History tab**: cycle table (expand a row for the at-entry snapshot),
  aggregates (win rate, avg return, avg juice/week, avg roll drag, target hit
  rate), and a weekly net-juice chart against the 1–2%/week-of-deployed target
  band (`WEEKLY_JUICE_TARGET_PCT_MIN/MAX`, HARD_CFM_RULE).
  API: `GET /api/history`.
- **Calibration harness** (`scripts/calibrate.py` → `backend/calibration.py`):
  replays the scorecard over the cached OHLCV history (same metric functions
  as production), pairs each (ticker, as-of) sample with forward 4- and 8-week
  returns, buckets by verdict, and sweeps the ATR-extension cutoff (2.0–4.0)
  and MFI band variants — a markdown report that upgrades PROPOSED_DEFAULT
  thresholds from guess to measured. Offline only; reads the parquet cache.
- **Juice journal export**: `GET /api/export/juice-journal?format=csv|md` —
  weekly juice ledger + roll ledger + closed cycles (the operator's off-system
  record per CFM's juice-journal rule). Buttons on the History tab.
- **Wash-sale flagging** (visibility, not tax software;
  `WASH_SALE_WINDOW_DAYS`=30 PROPOSED_DEFAULT): a loss-closing cycle
  re-entered in the same underlying within 30 days is flagged on the cycle
  AND on the open position; a recent loss with the window still open is
  marked `window_open` so a new entry knows before it happens.
- **Demo**: seeding includes two completed cycles (PLTR target-hit winner,
  COIN kill-switch loser) so History/aggregates/export are populated.
