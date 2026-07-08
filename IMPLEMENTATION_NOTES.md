# IMPLEMENTATION_NOTES.md — Tiered Market-Data Scheduler

Companion to `AUDIT.md` (Phase 0). This records what shipped, the assumptions made,
which `PROPOSED_DEFAULT` constants most need calibration, and how the audit changed
the plan. Scope was **data fetching only** — no order/roll/exit path was touched, and
`state.json`'s schema and role are unchanged.

## Test status

`backend/` suite: **459 passed** (397 pre-existing + 62 new), offline, mocked clock +
mocked providers throughout — no live API calls, no `time.sleep`, no wall-clock in
tests. Frontend builds clean (`vite build`). Run: `cd backend && python -m pytest -q`.

---

## What changed (new modules)

| Module | Role | Purity |
|---|---|---|
| `backend/market_scheduler.py` | Pure decision layer: `assign_tiers`, `fetch_due`, `max_age_seconds`, `EscalationTracker` (defense + market), `is_market_open`. Tiers as an `IntEnum`. | **Pure** — deterministic given inputs + clock; no I/O. |
| `backend/queue_state.py` | Adapter: builds `PortfolioState` / `QueueState` from open positions + the cached scorecard + universe. Provider-free. | Impure (reads state), no provider calls. |
| `backend/data_cache.py` | Staleness store: `fetched_at`+`provider`+tier per datum, `get_with_staleness`, `stale_blocks_go` (the STALE_BLOCKS_GO rule), panel/summary surfacing. | Impure (in-process store), no state.json. |
| `backend/data_transport.py` | Transport/routing: ONE batched Schwab quote per cycle, per-tier failover (Schwab→AV→cache), 429/`Retry-After` exponential backoff, budget logging, staleness recording, defense-level derivation from bars. | Impure (providers) — the ONLY place tier logic touches a client. |
| `backend/data_budget.py` | Per-provider/per-tier/day call counter persisted to `DATA_DIR/data_budget.json` (**not** state.json); shed ladder T3→T2→T1-cadence, T0 never; `/api/data-budget` snapshot. | Impure (small JSON file). |
| `backend/tier_poll.py` | Runtime orchestrator: one polling cycle wiring all of the above; called from the existing daemon tick. | Impure glue; best-effort, never breaks the tick. |

**Wiring/edits:** `config.py` (new `# Tiered market-data scheduler` constant block,
each tagged `HARD_CFM_RULE` / `PROPOSED_DEFAULT`); `alert_scheduler._tick` gains
`_maybe_tier_poll` (guarded by `CFM_TIER_POLL`, market-hours only, its own per-symbol
cadence gates inside `run_cycle`); `app.py` adds `/api/data-budget` and extends
`/api/data-health` with `data_budget` / `staleness` / `tier_poll`; `/api/scan/ready`
enforces STALE_BLOCKS_GO. Frontend: `StaleBadge` primitive (`ui.jsx`), `api.dataBudget`,
a "Tiered scheduler" section in `DataHealth.jsx` (budget/shed/staleness/escalations),
and `stale_blocked` GO candidates surfaced in `ReadyToEnter.jsx`. `.gitignore` ignores
`backend/data_budget.json`.

**New tests:** `test_market_scheduler.py` (27), `test_data_cache.py` (12),
`test_data_transport.py` (14), `test_tier_poll.py` (9) — covering all 8 required cases
plus the XLK regression fixture.

---

## How the design maps to the required test cases

1. **Tier transitions** (`test_market_scheduler`): gates pass T3→T2; slot opens T2→T1;
   entry →T0; exit →T3.
2. **`fetch_due` cadence**: per-tier quote intervals, market open vs closed (quotes
   zero off-hours), EOD bar batch fires exactly once/day.
3. **Defense escalation**: price crossing the short strike promotes cadence
   (`POLL_ESCALATED_SECONDS`) + emits an alert event; decays after
   `ESCALATION_DECAY_MINUTES`; edge-triggered (no per-tick spam).
4. **Market escalation**: SPY/held-sector move ≥ `ESCALATION_INDEX_MOVE_PCT` sets the
   global refresh flag (all T0/T1 read escalated).
5. **Staleness blocks GO** (`test_data_cache`): a stale input blocks the GO emit;
   staleness flag surfaces in the API + `stale_blocked` list.
6. **Batching** (`test_data_transport`): N Tier 0/1 symbols → exactly one Schwab request.
7. **Shed order**: T3 sheds before T2 before T1-cadence; T0 untouched; transitions logged.
8. **Provider failover**: Schwab failure on a Tier 0 name routes to fallback and sets a
   `tier0_degraded` flag (logged, never silent).
+ **XLK regression fixture**: open position, elevated ATR, price at the consolidation
   low → defense escalation fires.

---

## Assumptions & decisions (confirmed with the operator before building)

1. **No yfinance in this codebase.** Providers are Schwab + Alpha Vantage only; the
   **parquet daily-bar cache is the "cheap EOD" layer** the spec attributed to
   yfinance. Tier 2/3 ride that cache (refreshed by the EOD batch via Schwab, AV
   fallback). Provider routing is per-tier and swappable — a yfinance client could be
   dropped into `data_transport` later without touching any scheduler code.
2. **No ranked entry queue exists.** `QueueState` is adapted from `/api/scan/ready`:
   `rank` = juice-desc order, `gates_passed` = scorecard verdict "GO". Nothing forecasts
   *when* a position will close, so **`slot_opens_within_days` = 0 when a book slot is
   free now (`MAX_CFM_POSITIONS − open > 0`), else +inf** — on-deck (Tier 1) activates
   only when a slot is actually available (the operator chose "free slot = now"). The
   field is honoured as-is, so real horizon data drops in later with no code change.
3. **Defense levels derived from bars, not persisted** (operator chose no schema
   change). `data_transport.defense_levels` computes `trailing_stop = last − mult×ATR`
   and `consolidation_low = recent swing low` from cached bars each cycle;
   `short_strike` and the `circuit_breaker` line come from the persisted position. The
   ATR multiplier defaults to `SHORT_ATR_MULT` (1.5, the CFM default) and is
   per-symbol-overridable via an optional `config.DEFENSE_ATR_MULT_OVERRIDES` dict
   (e.g. `{"APP": 1.0}`) — no ticker is hardcoded.
4. **Chains are not schedule-polled** — the audit refuted the "biggest waste"
   premise. `fetch_due(CHAIN)` always returns False; chains stay on-demand + the
   nightly held-name IV snapshot (untouched). The intraday win is replacing the flat
   15-min *bars* hot-refresh with **batched quotes overlaid on frozen bars**.
5. **Hot-refresh coexists (for now).** The legacy `refresh_policy` hot-refresh still
   keeps daily **bars** current (EOD / warm / post-close); the tiered poll adds intraday
   **quotes** + escalation on top. This is the safe, non-breaking integration. Once the
   tiered poll is validated live, the flat bars hot-refresh can be retired (its cadence
   is superseded); `HOT_REFRESH_MINUTES` / `HOT_TICKERS_MAX` remain until then.
6. **STALE_BLOCKS_GO is gated to the live path.** It only blocks once the scheduler has
   actually populated the staleness store (`data_cache.active()`) and only in a live,
   open-market context — a bulk warm scan legitimately lacks live quotes and behaves as
   before. A record that *exists and is stale* always blocks (live or not). This keeps
   the 397 existing tests green while honouring "unknown-fresh blocks action" where it
   matters.
7. **Single-writer preserved.** Everything runs in the one existing `alert_scheduler`
   daemon thread; no second scheduler, consistent with the /data single-writer invariant.
8. **429 detection is by message match** (`"429" in str(exc)`) because the Schwab client
   folds the status into the error text and does not expose a structured code or the
   `Retry-After` header. The backoff loop *will* honour a `.retry_after` attribute if a
   future structured error carries one. **Limitation:** `Retry-After` is not currently
   readable — see below.

---

## PROPOSED_DEFAULT constants that most need calibration

Ranked by how much a wrong value costs:

1. **`ALPHA_VANTAGE_DAILY_CALL_LIMIT`** (default 500) — the AV free tier is the real
   budget constraint and varies by key (historically 25/day). Set it to the operator's
   actual key limit via env, or the shed ladder mis-fires. **Highest priority.**
2. **`POLL_T0_SECONDS` / `POLL_ESCALATED_SECONDS`** (120 / 30) — Tier 0 freshness vs API
   spend. 30s escalated is aggressive; confirm against the real Schwab rate budget.
3. **`ESCALATION_INDEX_MOVE_PCT`** (1.0%) — a 1% SPY move is common; may be too twitchy
   and keep the book perpetually escalated. Backtest against a few volatile sessions.
4. **`SCHWAB_DAILY_CALL_LIMIT`** (40000) — a placeholder; Schwab publishes ~120 req/min
   but no firm daily cap. Only affects the Schwab shed trigger.
5. **`MAX_AGE_POLL_MULT`** (2.0) — how forgiving staleness is. Too low → spurious GO
   blocks; too high → acting on stale data. Tune once real polling latency is observed.
6. **`REFRESH_KILLSWITCH_PER_DAY`** (3) — intraday RS3M recompute count; more = fresher
   kill-switch inputs at higher bar-fetch cost.
7. **`BUDGET_SOFT_LIMIT_PCT`** (80) and the Tier-2 midpoint shed curve — when shedding
   starts and how steeply it escalates.

`TIER0_NEVER_SHED` and `STALE_BLOCKS_GO` are `HARD_CFM_RULE` and should not be tuned.

---

## Phase 0 findings that altered the plan

- **yfinance absent** → Tier 2/3 provider redefined to the parquet cache (§Assumptions 1).
- **No ranked queue / no close-forecast** → minimal `QueueState` adapter; slot horizon
  reduces to "free slot now" (§Assumptions 2).
- **Trailing stop / consolidation low not persisted** → derived from bars, no schema
  change (§Assumptions 3).
- **Chains never schedule-polled** → no polling to remove; focus shifted to batched
  quotes replacing the flat bars refresh (§Assumptions 4).
- **Earnings/dividends caches already carry `fetched_at`+`source`** → mirrored that
  provenance shape in `data_cache` rather than inventing a new one.
- **Schwab has zero retry/backoff and no 429 handling** → added bounded exponential
  backoff on the Schwab path (the AV path already retries).

## Known limitations / follow-ups

- **`Retry-After` not honoured structurally** (§Assumptions 8) — a small enhancement to
  `schwab_api` to raise a typed rate-limit error carrying the header would let the
  backoff loop obey the server's requested delay instead of pure exponential. Left out
  to avoid broad changes to the provider client in this data-only task.
- **Kill-switch RS3M refresh reuses `refresh_policy.refresh_tickers`**, which refetches
  bars for open names N×/day — heavier than a pure quote overlay, but reuses tested
  code and stays bounded (`REFRESH_KILLSWITCH_PER_DAY`). A lighter quote-only RS3M
  recompute is a possible optimization.
- **Retiring the legacy hot-refresh** once the tiered poll is validated live (§Assumptions 5).
- Enable/disable the tiered poll independently via `CFM_TIER_POLL` (default on).
