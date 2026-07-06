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
| `WHIPSAW_EXIT` | CRITICAL | HARD_CFM_RULE (whipsaw) | cumulative defend guard: ≥`WHIPSAW_DEFEND_ROLLS` defensive rolls in `WHIPSAW_WINDOW_WEEKS`, OR cumulative roll drag > `WHIPSAW_DRAG_PCT` of position capital → exit, not another defend (the slow-grind bleed no single check owns) |
| `ASSIGNMENT_RISK` | HIGH | HARD_CFM_RULE (assignment mechanics) | base: an ITM short whose extrinsic has collapsed below `ASSIGNMENT_EXTRINSIC_FLOOR` (a few cents) — assignable any time, deep-ITM early assignment is an extrinsic problem; escalation: extrinsic < upcoming dividend before ex-div. Note: the short is covered by a LEAP, not stock — assignment creates *short stock* that owes any dividend |
| `TOKEN_EXPIRY` | HIGH | PROPOSED_DEFAULT (`TOKEN_WARN_AGE_DAYS`=5) | Schwab refresh token older than 5 days (dies at ~7) |
| `BUYBACK_75` | MEDIUM | HARD_CFM_RULE (75% buyback) | short lost ≥75% of sale premium with >2 DTE → roll early to capture juice |
| `JUICE_INADEQUATE` | MEDIUM | HARD_CFM_RULE (income target) | trailing weekly juice below the strategy's per-profile income target while the position still self-funds its decay (the band above `CAPITAL_BURN`) → reassess/redeploy while capital is intact |
| `EARNINGS_WINDOW` | MEDIUM | HARD_CFM_RULE (earnings) | earnings within `EARNINGS_WARN_DAYS` for an open position |
| `EXPIRY_FRIDAY` | MEDIUM | HARD_CFM_RULE (weekly roll) | short expiring today/tomorrow not yet rolled |
| `DATA_STALE` | MEDIUM | PROPOSED_DEFAULT (`DATA_STALE_HOURS`=30) | cached OHLCV older than expected on a market day |

`TOKEN_EXPIRY` and `DATA_STALE` are skipped in demo mode (they describe the
real providers, not the demo store).

**Scheduler** (`backend/alert_scheduler.py`): an in-process daemon thread —
the volume attaches to one machine and state.json is single-writer, so a
separate scheduled machine can't share `/data`. Fires at ET slots on weekdays,
once per slot per day; fly.toml pins `min_machines_running = 1` so the machine
is awake. The schedule (`config.ALERT_SCHEDULE_ET`) is the fixed anchors
(`ALERT_SCHEDULE_ANCHORS_ET`: 08:30, 10:00, 12:30, 15:30, **16:15**) merged with
the **post-open gap-cadence** slots (09:40, 09:50):

- **16:15 post-close slot** (`POST_CLOSE_SLOT_ET`) — the kill switch's
  confirmed-close condition and an end-of-day circuit-breaker breach can only be
  evaluated after the 16:00 close, so the 15:30 slot can't see them; without a
  post-close slot their earliest fire is the next morning's 08:30 ("exit
  immediately" → "exit at tomorrow's open"). The scheduler force-refreshes the
  hot set at this slot first so the official close is cached before evaluation.
- **Post-open gap cadence** (`MARKET_OPEN_ET` + `OPEN_GAP_WINDOW_MIN`=30 /
  `OPEN_GAP_CADENCE_MIN`=10) — the open (09:30) to the first fixed slot (10:00)
  was a 30-min blind window: a gap straight through a position's circuit breaker
  at 09:31 wasn't seen until 10:00. CFM deliberately uses alerts, not resting
  stops, so the cadence *is* the only tripwire — it's tightened across the
  high-volatility post-open window.
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
  the computed number is shown). "Free cash" is the **live Schwab account cash
  balance** (`account_gate.resolve_operating_cash` → `schwab_api.cash_balance`,
  `GET /accounts?fields=positions`, `currentBalances.cashAvailableForTrading`,
  60s-cached) whenever Schwab is connected — a read-only account call, so it
  works even with `CFM_LIVE_TRADING` off. It falls back to the manually-entered
  `state.metadata.operating_cash` in demo mode, when Schwab isn't connected, or
  on any fetch error (stale token, network). A successful live read is
  persisted back to `state.metadata.operating_cash` so the Positions Capital
  card, the portfolio risk card, and the daily checklist's reserve check all
  agree on the same fresh number; each surfaces `operating_cash_source`
  (`"schwab"` | `"manual"`) for transparency. Also synced during nightly
  maintenance (`maintenance.py`) so it stays fresh even on a day Execute is
  never opened.
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

### Ready-to-enter shortlist (Levels 3 + 4 + 5 combined)

`GET /api/scan/ready` returns tickers that clear the Scorecard's own GO
verdict (Level 3 beats peers + Level 4 consolidating + the scorecard's
CFM-suitability rules) **and** Level 5 (Account & Juice), right now — a
one-glance shortlist instead of checking each ticker in Execute one at a time.
Level 1 (regime) / Level 2 (sector) are deliberately excluded, same rationale
as the Scorecard verdict (market-wide context, not stock-specific merit) — so
the list stays useful even on a yellow/red tape. RED still hard-blocks actual
execution at the entry-gate/executor level regardless of what's listed here.

Only runs Level 5 on the (usually small) GO subset, not the whole universe —
`account_gate.evaluate_many` loads state.json once and reuses it across every
candidate (rather than once per ticker) and juice always uses the
history-implied estimate (no live chain in a bulk sweep). Response:
`{"ready": [...], "near_misses": [...]}` — `ready` is sorted by juice/wk
descending; each `near_misses` entry carries `level5.blocking_failures` so a
GO-verdict ticker's specific Level 5 blocker (cash reserve, position limit,
capital cap, sector cap, thin juice) is visible, not just "no."

The full-universe Scorecard sweep (`metrics/scorecard.py::scorecard()` with no
ticker filter) is memoized with `screening`'s existing short-TTL cache
(`SCAN_CACHE_TTL`, default 300s) — the Scan tab mounts both the Scorecard
panel and this Ready-to-Enter panel, which would otherwise each trigger their
own ~500-ticker sweep concurrently on every page load. An explicit ticker
subset (e.g. one ticker's frozen entry snapshot, `executor._entry_snapshot`)
always computes fresh, never cached. The cache is cleared on every demo/live
mode switch, same as regime/sector data. **UI**: a "Ready to Enter" panel on
the Scan tab, clickable straight into Execute, with a collapsible near-misses
list.

## Position management mechanics (Phase 2)

- **75% buyback rule** (HARD_CFM_RULE): every open short shows `% decayed`
  (sale premium vs current value, both derived from stored execution data);
  ≥75% decayed with >2 DTE shows a ROLL NOW badge on the Positions tab (and
  fires the `BUYBACK_75` alert). Clicking it stages the roll with reason
  `75%-rule`.
- **Defend / roll-down engine**: when the underlying closes below a short
  strike, `GET /api/defend?ticker=` returns the defensive roll: new strike
  from the regime × posture table (see "Weekly short strike selection" below),
  est. net credit/debit, the new short's extrinsic, and the cost-basis effect.
  The Positions tab shows the recommendation with one-click staging into the
  roll modal (reason `defend`). Estimates come from trailing vol; the staged
  roll re-prices from the live chain.
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
- **Whipsaw circuit breaker** (`position_manager.whipsaw_status`, the
  `WHIPSAW_EXIT` alert): the individual defend roll-downs are each correct, but
  the whipsaw — roll-down after roll-down in a slow grind, each locking a lower
  strike — is the strategy's real killer, and no single check owns it (the RS
  kill switch and the price circuit breaker can both stay untripped while defend
  bleeds the position weekly). This cumulative guard reads the roll ledger above:
  it trips when ≥`WHIPSAW_DEFEND_ROLLS` (3) defensive rolls landed in the trailing
  `WHIPSAW_WINDOW_WEEKS` (4), OR cumulative roll drag passed `WHIPSAW_DRAG_PCT`
  (5%) of the position's capital — recommending EXIT, not another defend. Scoped
  to the current cycle (rolls on/after the position's entry). Surfaced on the
  Positions tab, on the defend recommendation itself (`GET /api/defend` — so the
  ticket you open to roll tells you to exit instead), and as the alert. The
  counts/percent are PROPOSED_DEFAULT pending the roll-ledger data that tunes them.
- **Assignment-risk monitor**: assignment is modelled as an *extrinsic* problem,
  not a dividend one. The base trigger is an ITM short whose remaining time value
  has collapsed below `ASSIGNMENT_EXTRINSIC_FLOOR` (a few cents) — assignable any
  time, no ex-date required, because the counterparty forfeits no time value by
  exercising. The stored dividend is an *escalation*: extrinsic below the coming
  dividend before ex-div makes early exercise rational on a specific date. Either
  way the flag's tooltip explains the PMCC nuance: the short is covered by a LEAP,
  not stock, so assignment creates SHORT STOCK (that owes any dividend) — roll to
  re-establish time value, never exercise the LEAP to cover.
- **Accumulation vs kill-switch** (`BLOCK_ACCUMULATION_ON_RS_DETERIORATION`,
  HARD_CFM_RULE candidate, OFF by default pending confirmation): when on,
  `can_add_shares` refuses accumulation on any name whose kill switch reads
  red (exit in progress) or yellow (RS3M thinning toward the kill line) — the
  pullback play buys weakness, the kill switch sells it; without the guard the
  two rules can add to a name the strategy is 1–2 days from exiting.

### Weekly short strike selection: regime × posture table

`backend/strike_policy.py` (HARD_CFM_RULE, "Genius System" reference table)
replaces the old flat/regime-only ATR multiplier with a table keyed by market
regime (green/yellow/red) **and** the operator's risk posture
(aggressive/conservative). Each cell is an `(ATR multiplier, minimum ITM%
floor)` pair (`config.STRIKE_TABLE`):

| Regime | Aggressive | Conservative |
| --- | --- | --- |
| GREEN | 0.0×ATR, 0% ITM | 0.5×ATR, 1% ITM |
| YELLOW | 0.5×ATR, 2% ITM | 1.0×ATR, 3% ITM |
| RED | 1.0×ATR, 4% ITM | 1.5×ATR, 5% ITM |

The strike used is whichever candidate sits **further below spot** (max
protection wins):

```
atr_strike = price − atr_mult × ATR
itm_strike = price × (1 − itm_pct)
strike     = min(atr_strike, itm_strike)      # rounded to $0.50
```

(`indicators.short_strike_from_table`.) GREEN/aggressive collapses to both
candidates equal to price — i.e. sell at the money, maximizing premium when
the tape is calm; RED/conservative is the most protective cell.

**Posture** is an operator-editable, persisted setting (`GET`/`POST
/api/strike-posture`, a navbar toggle next to the demo/live switch) stored in
`state.metadata.strike_posture` — per-store, so live and demo can hold
different postures, defaulting to `conservative`
(`config.DEFAULT_STRIKE_POSTURE`).

**RED still blocks new entries** — the Level 1 regime gate is unchanged. The
RED row only feeds the defend/roll-down strike selector for an
already-open position during a red tape (management-only mode); it is not
reachable from a fresh entry.

Every strike-suggestion surface reads from this table: the entry option chain
(`option_chain.option_chain`), the roll picker (`option_chain.roll_options`,
now RED-aware — previously RED silently fell back to YELLOW's multiplier), the
defend engine (`executor.defend_recommendation`), the standalone roll
suggestion (`executor.roll_suggestion`), and the `DEFEND_POSITION` alert
(`alerts.check_defend_position`).

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

## Portfolio risk & ops hardening (Phase 4)

- **Portfolio risk card** (top of Positions tab; `GET /api/portfolio-risk`,
  `backend/portfolio_risk.py`): per-position and aggregate delta in share
  equivalents and dollars — raw and **SPY-beta-adjusted** (beta regressed from
  the cached daily history, ~250 sessions) — theta/day (net decay the diagonal
  collects), vega ($/vol-point), capital deployed vs `MAX_DEPLOYED_CAPITAL`,
  the 2×ATR defensive-reserve status, and the sector-exposure breakdown.
  Greeks imply vol from each leg's stored mark, so the card works offline and
  in demo mode; partially-priced positions are marked.
- **Earnings & dividends as first-class cached data**
  (`backend/maintenance.py`): a nightly slot (`MAINTENANCE_ET`, 17:30 ET,
  every calendar day) refreshes the earnings + dividend day-caches for every
  held name and syncs each open position's `dividend` snapshot — Phases 0–2
  read from these caches instead of ad-hoc lookups. Manual trigger:
  `POST /api/maintenance/refresh`. Skipped in demo mode.
- **Token lifecycle UX**: the Schwab card shows token age and days remaining
  with the one-click re-auth flow; the Phase 0 `TOKEN_EXPIRY` alert fires at
  day 5 (of the ~7-day token life).
- **Data health panel** (Checklist tab; `GET /api/data-health`):
  last-successful-fetch timestamp per source (Schwab bars/quotes, Alpha
  Vantage fallbacks), count of fallback events, recent per-symbol errors,
  OHLCV cache age for key symbols, and earnings/dividends cache staleness —
  silent data failures become visible instead of quietly serving stale
  frames.

## Sector ETFs as CFM entries

`sector_data.all_tickers()` includes the 11 sector ETFs (XLK, XLE, …)
themselves alongside every constituent — they're liquid, weekly-optionable
tickers and valid CFM candidates in their own right. Since every scan
(Scorecard, Ready-to-Enter, `scripts/calibrate.py`) sweeps this one list, the
ETFs are automatically selectable everywhere without per-caller wiring;
`stock_filter(sector=X)` also lists the sector's own ETF as a row alongside
its constituents (`screening._compute_stock_filter`).

A sector ETF entered as its own candidate has no distinct peer sector to
beat — comparing it to itself is tautologically ~0 every time (the same
cached price frame vs itself). Left unguarded this reads as a real, borderline
number instead of "not applicable" and silently breaks three places:

- **Entry gate Level 3** ("RS3M vs Sector > 0%") would permanently fail for
  an ETF, since exactly 0 is never > 0. `screening._stock_row` / `entry_gate`
  now waive that leg for `ticker == sector_etf` (label shows "N/A — is the
  sector"); Level 3 passes on the RS3M-vs-SPY leg alone.
- **Kill switch** (`kill_switch._rs_pair`) had a worse version of the same
  bug: the YELLOW "thinning" leg (`rs_vs_sector < STOCK_RS_VS_SECTOR_MIN + 2`)
  would fire *permanently* for any ETF position, since 0 is always < 2 —
  every healthy sector-ETF CFM position would show a false "watch, thinning
  toward the kill line." `rs_vs_sector` is now `None` (waived) for a
  ticker-is-its-own-sector position; the kill switch relies solely on RS3M vs
  SPY, a fully meaningful check for an ETF against the broad market.
- **Scorecard** (`metrics/scorecard.py::score_ticker`) nulls
  `rs3m_vs_sector` the same way, so the (now-null) sector leg can't spuriously
  trigger `compute_verdict`'s AVOID rule, and the UI shows "—" instead of a
  misleading 0.00%.

`sector_concentration` (Level 5) needed no change: an existing position's
`sector` field already equals its own ETF for a sector-ETF holding, so
entering the ETF itself correctly counts as one more position in that sector
if any constituent is already held.

**UI**: an "ETF" badge next to the ticker on the Scorecard and Stock Filter
rows (tooltip explains the N/A sector leg); the Execute tab's ticker box
already accepts any symbol, so typing a sector ETF there worked mechanically
once the Level 3 / kill-switch waivers above were in place.
