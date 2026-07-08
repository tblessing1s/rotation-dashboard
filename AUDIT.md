# AUDIT.md — Market-Data Fetch Layer (Phase 0)

**Task:** Tiered market-data scheduler. **Scope of this document:** map the existing
fetch layer completely before any code is written, and flag where the codebase
contradicts the assumptions in the implementation prompt.

Every reference is `file:line` relative to `backend/` unless noted. This audit was
produced by reading the code, not from memory.

---

## 0. Executive summary — what's really there

- **There is no ad-hoc *chain* polling to remove.** The prompt's "suspected largest
  waste — option chains polled on a schedule" is **REFUTED** (§3). Chains are
  on-demand per HTTP request, plus one nightly IV snapshot for *held* names only.
- The real intraday cost today is: (a) a **pre-open full-universe warm scan** that
  fetches ~500 symbols' daily bars, and (b) a **flat 15-minute "hot refresh"** that
  re-fetches *daily bars* (not quotes) for up to 40 "hot" names during market hours
  (`refresh_policy.py` + `alert_scheduler.py`). This flat-cadence, bars-based hot
  refresh **is** the ad-hoc/uniform system the task replaces.
- **yfinance is not in the codebase at all** (§C1) — a direct contradiction of the
  prompt. Providers are Schwab (primary) + Alpha Vantage (fallback) only; the
  **parquet daily-bar cache is the "cheap EOD" layer** the prompt attributes to
  yfinance.
- **There is no ranked entry queue** with rank / slot-horizon (§C2) — another
  contradiction. `/api/scan/ready` computes a flat, juice-sorted candidate list on
  demand. We drive Tier 1/2 from that behind the minimal `QueueState` interface the
  prompt allows.
- **No 429 handling, no Schwab backoff, no API-call counter** exist (§4). Alpha
  Vantage is the only provider with a retry loop.
- **Cached daily bars carry no `fetched_at` and no provider tag** — freshness is
  inferred from parquet file mtime only (§2). By contrast the earnings and dividend
  caches already carry `fetched_at` + `source`; that is the provenance pattern to
  mirror for the new staleness cache.
- Architecture constraint that shapes everything: **single-writer.** state.json
  lives on one Fly volume attached to one machine, so all scheduling runs inside
  **one in-process daemon thread** (`alert_scheduler`). The tiered scheduler must
  hook into that existing tick — **not** spawn a second scheduler.

---

## 1. Every provider fetch call path

**Providers:** Schwab Trader API (`schwab_api.py`), Alpha Vantage (`alpha_vantage.py`).
**yfinance: absent** (no import anywhere; not in `requirements.txt`).

### Low-level primitives

| Primitive | Location | Data | Batched? |
|---|---|---|---|
| `SchwabClient.get_daily_bars(symbol, start)` | `schwab_api.py:238` | bars | no |
| `SchwabClient.get_quotes(symbols) -> dict` | `schwab_api.py:267` | quotes | **YES — multi-symbol, one request** (`params={"symbols": ",".join(...)}` at :274) |
| `SchwabClient.get_quote(symbol)` | `schwab_api.py:290` | quote | wrapper over `get_quotes([symbol])` |
| `SchwabClient.get_option_chain(symbol, …)` | `schwab_api.py:296` | chain | no |
| `SchwabClient.get_instrument_fundamental(symbol)` | `schwab_api.py:325` | div/earnings | no |
| `alpha_vantage.daily_bars / global_quote / overview / earnings_calendar` | `alpha_vantage.py:66/133/127/114` | fallback bars/quote/fundamentals/earnings | no |

### Call-site inventory (what data, what cadence, what trigger)

**Daily bars** — all route through `data_handler._fetch` (`:140` Schwab → `:147` AV fallback),
wrapped by `get_daily`/`get_many`/`prefetch`:
- `screening.warm_scan_cache` (`screening.py:51`) — SPY + all sector ETFs + all
  constituents. **Scheduled** (warm-scan slots + boot) and on-demand background scan.
- `refresh_policy.refresh_hot` (`refresh_policy.py:123`) → `prefetch(force=True)` —
  **scheduled**, 15-min cadence, market hours. *(the flat hot refresh)*
- `refresh_policy.refresh_tickers` (`:152`) — on-demand (`/api/refresh/ticker`, `/api/refresh/sector`).
- Scan/scorecard sweeps, `option_chain`, `position_manager`, `/api/diagnostics/vix` — per-request.

**Quotes:**
- `data_handler.latest_quote` (`:224` Schwab → `:235` AV) — single quote, on-demand
  (execution price capture, `position_manager._stock_price`, chain spot anchor).
- `data_handler.live_prices` (`:283` **batched** Schwab → `:298` AV per-symbol) —
  used by `refresh_policy.refresh_tickers`.
- `position_manager._live_short_marks` (`position_manager.py:35`) — **batched** quote
  for all open short-call legs, per Positions/Overview request.
- `alerts.run` DEFEND leg (`alerts.py:223`, `live_price`) — **scheduled** (alert slots).
- `screening._compute_regime` VIX (`screening.py:182`) — per-scan.

**Option chains** (§3): `option_chain._fetch_chain` (`option_chain.py:61`, `strike_count=100`),
5-min TTL, reached only via `/api/option-chain/<t>`, `/api/roll-options`, `/api/coverage`
(request/modal driven). Tiny 2-strike weeklies-detection chain (`weeklies.py:87`),
week-cached. Nightly held-name IV snapshot `maintenance.snapshot_iv` → `option_chain()`.

**Fundamentals / earnings / dividends:** `dividends.py:77/161`, `earnings.py:102/77`
(Schwab primary, AV fallback) — day-cached, lazy + nightly.

### The one scheduler (background timer)

`alert_scheduler` — a single daemon thread started at `app.py:1036`
(`alert_scheduler.start_once()`), loop `while not _stop.wait(30)` (`alert_scheduler.py:217`).
Per tick it runs: heartbeat ping; nightly maintenance at `MAINTENANCE_ET`; **intraday
hot refresh** (`_maybe_hot_refresh`, market hours, `:85`); post-close refresh; morning
reconcile; the alert evaluation pass at `config.ALERT_SCHEDULE_ET`; and a warm scan.
No APScheduler, no cron, no other fetch loop. Worker parallelism is bounded pools
(`data_handler._executor` 8 workers `:53`, `weeklies._pool`), not schedulers.

**Implication:** the tiered scheduler's pure functions get called from *this* tick.
Tier polling = extend `_tick`; we do not add threads (single-writer invariant).

---

## 2. Existing caching + staleness

| Cache | Location | Store | TTL | `fetched_at`? | provider tag? |
|---|---|---|---|---|---|
| Daily bars | `data_handler.py:24,82` | parquet + mem dict | 12h (**file mtime**) | **No** | **No** (only global `_last_success`) |
| Option chain | `option_chain.py:32` | mem dict | 300s | epoch in tuple | No (Schwab-only) |
| Weeklies | `weeklies.py:44` | mem dict | 7d | epoch in tuple | No |
| **Earnings** | `earnings.py:30` | JSON disk | 24h | **Yes** | **Yes** (+conflict) |
| **Dividends** | `dividends.py:27` | JSON disk | 24h | **Yes** | **Yes** |
| IV history | `iv_history.py:27` | JSON disk | 260-pt ring | date only | No |
| Accounts | `schwab_api.py:54` | mem tuple | 60s | epoch | No |
| Scan memo | `screening.py:21` | mem dict | 300s | epoch | No |

**Key facts for the staleness layer:**
- Bars freshness = parquet mtime vs 12h (`data_handler._is_fresh :87`); cache age is
  `cache_age_hours` (`:316`). No per-datum timestamp, no provider identity on the frame.
- Live quotes return a transient `source` field (schwab/alphavantage/cache/demo) but
  it is **never persisted**.
- **Earnings/dividends already do it right** (`{... "fetched_at": time.time(), "source": ...}`,
  `earnings.py:126`, `dividends.py:112`). The new `get_with_staleness` cache should
  copy this shape.
- `refresh_policy` is **not** a staleness engine — it's a single-global cadence gate
  (`_last_refresh`, resets on restart). It force-refreshes *bars*; it never measures
  per-quote staleness.

---

## 3. Are chains polled on a schedule? — **REFUTED (with one bounded exception)**

No fixed-cadence intraday chain polling exists. The interactive chain is on-demand
per Flask route (5-min TTL). The scheduler's intraday hot refresh fetches **bars only**
(`refresh_policy.py:117-124`), never chains. The only scheduled chain traffic:
1. **Nightly** IV snapshot for **held tickers only, once/night** (`maintenance.py:70-80,127`).
2. Cache-cold weeklies detection during a warm scan (week-cached, so rarely a real fetch).

→ The prompt's premise that chains are the largest waste does not hold. The design's
"chains on-demand only" rule is already the de-facto behaviour; our job is to make it
explicit and keep the nightly held-name snapshot as the Tier 0/1 "once-daily" path.

---

## 4. Rate-limit / failure handling today

- **429: not handled anywhere.** No `Retry-After`, no status-code branching. Schwab
  raises `SchwabError` on any non-200 (`schwab_api.py:248,280,321`); only 403+"Access
  Denied" (Akamai edge) is special-cased.
- **Schwab: zero retry/backoff.** One attempt → fall through.
- **Alpha Vantage: the only retry** — 3 attempts, linear 2/4/6s
  (`alpha_vantage.py:43-63`); AV delivers rate limits as HTTP-200 `Note`/`Information`.
- **Fallback chain: Schwab → AV → last-good cache**, and it **never raises** — degrades
  to cached close labelled `source:"cache"` (`data_handler.py:157,186,258`). Consumers
  degrade too (chain→"unknown", weeklies→None, earnings/div→None).
- **No call counter / budget / quota** — only `_fallback_events` (int) and
  `_last_success`/`_last_error`, surfaced via `data_handler.health()` (`:45`).
- Only implicit throttles: 8-worker pool cap + cache TTLs + AV backoff.

→ We add: 429/`Retry-After`-aware exponential backoff **on the Schwab path**, a
per-provider/per-tier/per-day call counter (persisted outside state.json), and the
shed ladder. Tier 0 degradations (Schwab→AV failover, stale-beyond-tolerance) must be
surfaced, not silent.

---

## 5. State, positions, and the drivers of tier assignment

**state.json** is loaded by `logging_handler.load_state` (`:89`); `recompute_derived`
(`:311`) rebuilds ledgers but **not** per-position risk fields (ATR/stops are computed
live in the view layer). Position schema created in `executor._ensure_position` (`:104`).

Fields available to drive **defense escalation**:

| Level the prompt asks for | Status | Where / note |
|---|---|---|
| Short-call strike | ✅ persisted | `short_calls[].strike` (`executor.py:806`) |
| Parent sector ETF | ✅ persisted | `position["sector"]` = `sector_data.sector_for(t)` (`executor.py:110`) |
| Circuit-breaker "line" | ✅ persisted | `position["circuit_breaker"]["price"]` (`executor.py:733`) |
| ATR value | ⚠️ not stored | computed live `indicators.atr(df)` (`executor.py:1517`, `account_gate.py:226`); available from cached bars any time |
| **Trailing stop (1.5×ATR / 1.0×)** | ❌ missing | no `trailing_stop` concept anywhere; nearest is the *static* circuit-breaker line |
| **Recorded consolidation low** | ❌ missing | only a live boolean `indicators.consolidating(df)` (`indicators.py:181`); no numeric low persisted |

→ Two defense levels (trailing stop, consolidation low) are **not currently persisted**.
Both are *derivable from cached daily bars* (ATR → trailing stop = last − mult×ATR;
consolidation low → recent swing low). Proposed adjustment in §7.

**Entry queue / watchlist:** no persisted queue. `/api/scan/ready`
(`app.py:134-171`) computes on demand: full scorecard → keep `verdict=="GO"` → run
Level-5 `account_gate` → split `ready` (L5 pass) vs `near_misses`, **sorted by
`juice_weekly_pct` desc** (`app.py:168`). Each entry: `{ticker, sector,
juice_weekly_pct, earnings_date, level5}`. **No `rank`, no "slot opens within N days",
no persisted ordering.** Hard gates: Levels 1–4 `screening.entry_gate` (`:357`),
Level 5 `account_gate.evaluate` (`:189`, blocking: cash reserve, position limit ≤
`MAX_CFM_POSITIONS`, capital, sector concentration, juice adequacy, earnings-in-cycle).

---

## 6. Frontend refresh + where badges/budget go

Polling primitive: `useApi(fn, deps, interval)` (`components/ui.jsx:113`, on-mount
`load()` + optional `setInterval`). No react-query/SWR.

| Loop | Endpoint | Interval |
|---|---|---|
| `App.jsx:46` | `/api/alerts` | 60s |
| `Overview.jsx:167` | `/api/overview` | 5m |
| `PositionTracker.jsx:405` | `/api/kill-switch` | 5m |
| `DataHealth.jsx:410` | `/api/data-health` | 2m |
| `ScanProgress.jsx:33` | `/api/scan/status` | 2.5s |

**Badge candidates:** Overview, PositionTracker, PortfolioRisk, ReadyToEnter,
Scorecard, DataHealth.

**Budget/shed home:** extend **`GET /api/data-health`** (`app.py:781-804`). It already
returns `providers`, `ohlcv_cache_age_hours`, `hot_refresh`, earnings/dividend cache,
Schwab token — and is already polled every 2m by `DataHealth.jsx`. Add a `data_budget`
block there (or a sibling `/api/data-budget`) and the panel renders with minimal
wiring.

---

## 7. Contradictions with the prompt → proposed adjustments

**Present before implementing — these change the plan. None are silently improvised.**

1. **yfinance does not exist.** *Adjustment:* Tier 2/3 "provider = yfinance/cache"
   becomes **"parquet daily-bar cache, refreshed by the EOD batch via Schwab (AV
   fallback)."** The existing parquet cache already *is* the cheap-EOD layer. No new
   dependency — consistent with "do not add external deps without flagging." I will
   keep provider routing swappable so a yfinance client could be dropped in later.

2. **No ranked entry queue.** *Adjustment:* define the minimal `QueueState` interface
   from the prompt (`[{symbol, rank, gates_passed, slot_opens_within_days}]`) and
   **adapt it from `/api/scan/ready`**: `rank` = index in the existing juice-desc
   order; `gates_passed` = row is in `ready` (L1–5 pass); `slot_opens_within_days`.
   Since the codebase has **no forecast of when an open position will close**, an
   honest "slot opens within N days" reduces to **"is a slot free now"** =
   `MAX_CFM_POSITIONS − active_positions > 0` → horizon 0 when free, ∞ otherwise. I'll
   encode that mapping in the adapter and flag it as the single biggest place real
   queue data would improve Tier 1 targeting. No symbols hardcoded.

3. **Defense levels not all persisted (trailing stop, consolidation low).**
   *Adjustment:* keep the escalation state machine **pure** — it consumes
   pre-computed `defense_levels` + a current price and decides crossings/flags. A
   separate (impure) helper derives those levels from the position + cached daily
   bars: `short_strike` (persisted), `trailing_stop = last − mult×ATR` (ATR from
   bars; mult from config, default from `SHORT_ATR_MULT`=1.5), `consolidation_low`
   (recent swing low from bars), `circuit_breaker.price` (persisted). This needs **no
   state.json schema change** (rule: don't touch the schema). Optionally I can persist
   `atr_at_entry`/`consolidation_low` at open time later, but the derive-from-bars
   path unblocks this task now. Note the prompt's "1.5× for CFM, 1.0× for APP": CFM is
   the strategy default (1.5, matches `SHORT_ATR_MULT`); I'll make the multiplier
   config-driven per-symbol-overridable rather than hardcoding "APP".

4. **"Chains are the largest waste" is false (§3).** *Adjustment:* no chain-polling to
   remove; I preserve on-demand chains + the nightly held-name IV snapshot as the
   Tier 0/1 "once-daily chain" path, and focus the win on replacing the flat 15-min
   *bars* hot-refresh with **batched quotes** overlaid on frozen bars (one batched
   Schwab quote call per interval for all Tier 0/1 names).

5. **Intraday freshness today = re-fetching bars, not quotes.** *Adjustment:* the new
   Tier 0/1 cadence fetches **batched quotes** and overlays them on the frozen daily
   bars (the pattern `refresh_policy.refresh_tickers` already uses at `:153-156`),
   including recomputing RS3M-vs-SPY / vs-Sector intraday for kill-switch inputs
   `REFRESH_KILLSWITCH_PER_DAY` times. Bars stay EOD.

6. **Config constants named for a queue/tier world that's partly absent** are added
   fresh in `config.py` with provenance tags; existing `HOT_REFRESH_MINUTES`/
   `HOT_TICKERS_MAX` are superseded by the tier constants (I'll keep them until the
   new scheduler fully replaces the hot path, then deprecate in `IMPLEMENTATION_NOTES`).

---

## 8. Proposed module layout (for the build phase — not yet built)

- `market_scheduler.py` — **pure**: `assign_tiers`, `fetch_due`, escalation state
  machine, max-age derivation. No I/O. Mocked-clock tests.
- `queue_state.py` — the minimal `QueueState` interface + `/api/scan/ready` adapter (§7.2).
- `data_cache.py` (or extend `data_handler`) — staleness cache: `fetched_at`+`provider`+
  `max_age`, `get_with_staleness`, `STALE_BLOCKS_GO` enforcement hook.
- `data_transport.py` — impure: per-tier provider routing, batched quote fetch,
  429/`Retry-After` backoff + failover, defense-level derivation from bars.
- `data_budget.py` — per-provider/per-tier/day counter (persisted outside state.json,
  e.g. `DATA_DIR/data_budget.json`), soft-limit + shed ladder (T3→T2→T1; **T0 never**).
- Scheduler wiring in `alert_scheduler._tick` (the single daemon); `/api/data-health`
  extension; `DataHealth.jsx` + panel staleness badges.

**Testing fit:** reuse the established pattern — pure functions take an explicit
`now`/`clock` (as `alert_scheduler.due_slots(now, last_run)` and `_market_hours(now)`
already do) and providers are monkeypatched to return fixtures. 381 existing tests
across 30 files; all must stay green.

---

## 9. Guardrails I will honour

No order/roll/exit paths touched · no state.json schema change / no telemetry in it ·
no fixed-schedule chain polling · tier logic decoupled from provider clients · no new
deps without flagging · no silent Tier 0 degradation · no invented queue logic beyond
the `QueueState` interface · all 381 tests still pass.
