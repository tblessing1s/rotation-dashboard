# Audit: Market-Settle Execution Gate (Time-of-Day Order Discipline) — Phase 0

Written report only. No code is changed by this document. All references are
`file:line` at audit time (branch `claude/market-settle-execution-gate-pivwu8`,
on top of `fb244c0`, app **v2.6.0 / state schema v17**). Paths are under
`backend/` unless noted.

This audit maps every order-submission path, the clock sources that must become
injectable, the (absent) intraday session model, the recommendation/alert state,
and the whipsaw / confirmed-close / order-type / spread subsystems the feature
must **reference, not duplicate**. It ends with the contradictions this codebase
forces on the spec, which the implementation plan must absorb.

---

## Headline findings the implementation must absorb

1. **There is one clean shared chokepoint for placements and a separate one for
   cancels — exactly what the spec wants.** Every order *placement* funnels
   through `executor.execute(payload)` (`executor.py:261`); `CANCEL` has its own
   entry `executor.cancel_order(order_id)` (`executor.py:887`) that deliberately
   does **not** pass through `execute()`. This is a gift: the gate lives in
   `execute()` (covers all placements + the future-automation path) and simply is
   **never added** to `cancel_order`, satisfying `CANCEL_NEVER_GATED` structurally.

2. **The spec's `action_type` taxonomy does not exist in this codebase and must be
   *derived*, not stored.** The gate's `ENTRY | ROLL_SHORT | ROLL_LEAP | DEFENSE |
   EXIT_KILL | CANCEL` maps onto executor action strings + roll/exit reasons, not
   1:1 onto `rec_types.ActionType` (`ENTER/ROLL_OUT/ROLL_DOWN/DEFEND/EXIT/
   NO_ACTION`). In particular **DEFENSE has no order path of its own** — it is a
   `roll_short` with `roll_reason == "defend"` (§1.2). A classifier that reads the
   `execute()` payload is a prerequisite for the gate. See §1.2 for the full map;
   this is the biggest spec-vs-reality gap.

3. **No intraday session model, and no early-close support anywhere.**
   `market_calendar.py` is date-only (full-day holidays; it *explicitly* declines
   to model half-days, `market_calendar.py:11-12`). `market_scheduler.is_market_open`
   (`market_scheduler.py:45-51`) is a naive `"09:30" <= HH:MM < "16:00"` string
   compare that (a) assumes the passed `now` is already ET and (b) has no
   minutes-since-open / minutes-to-close / early-close notion. **A session model
   is a hard prerequisite** (Design §1). The ET convention to reuse is
   `ZoneInfo("America/New_York")`, already imported in `alerts.py:30`,
   `alert_scheduler.py:32`, `refresh_policy.py:32`, `tier_poll.py:36`,
   `payouts.py:52`, `data_budget.py:28`.

4. **The gap-emergency unlock's intraday inputs do not exist in the data layer.**
   Opening-range low, "underlying has printed two-sided quotes for ≥ N minutes",
   and a live "current print" are **not** produced anywhere — the data layer is
   EOD daily-bar parquet + throttled last/mark quotes (no minute bars, no
   bid/ask history, no opening-range tracker). `execution_window()` stays pure by
   taking these as `gap_context` inputs, but **wiring** the emergency path needs
   either new intraday capture or a documented degrade to *gap-size-only* unlock
   (overnight gap in ATR units IS computable from prior close + current print).
   This is the single largest wiring risk. See §3.

5. **No market orders are ever constructed.** All broker orders are `LIMIT`
   (single-leg) or `NET_DEBIT`/`NET_CREDIT` (multi-leg) via three
   `schwab_api.build_*_order` builders. `NO_MARKET_ORDERS_AT_OPEN` therefore
   blocks nothing that exists today — it is a **forward invariant** (a future
   market-order path is refused at the gate), and the emergency-path "execute as
   limit" requirement is *already true by construction*. Implement it as an
   assertion at the gate, not a change to any builder. See §6.

6. **Recommendation records are immutable dicts with a *derived* status — there
   is no stored state field and no `executable_at`.** PENDING_SETTLE must be an
   **additive optional field** (`executable_at`, plus a small pre-approval flag /
   lifecycle-event list), never a mutation of an existing record, and the derived
   "open/resolved" logic in `trust_derive` must learn `now < executable_at =
   staged`. A schema migration to **v18** is warranted for a new settle/lifecycle
   store even though the per-record field is nullable-additive. See §4.

7. **The execution layer breaks the injected-clock convention that the rest of the
   app already follows.** The pure engine, scheduler, and trust-derive layers all
   take an explicit `now: datetime` (UTC-aware) or `today: date`. But
   `recommendation_runner.run()` reads the wall clock once (`recommendation_runner.py:226`)
   and takes no `now` param, and `executor.py` reads the clock *implicitly on every
   write* via `log.utcnow()` with no clock parameter at all. Making the gate
   testable requires threading an injected `now` into `run()` and `execute()`.
   See §2.

---

## 1. Order-submission paths to Schwab (Phase 0 item 1)

### 1.1 The broker boundary and the chokepoints

All real order I/O is in `schwab_api.py::SchwabClient`:

| Function | Location | HTTP |
|---|---|---|
| `place_order(account_hash, order)` | `schwab_api.py:392` | `POST /accounts/{hash}/orders` |
| `cancel_order(account_hash, order_id)` | `schwab_api.py:410` | `DELETE …/orders/{order_id}` |
| `preview_order` | `schwab_api.py:381` | non-transmitting (not an order path) |

Every real placement in the codebase reaches `place_order` from **`executor.py`**
only, at five call sites (a legged roll calls the single-leg placer twice):

| `executor.py` line | Function | Action family |
|---|---|---|
| 653 | `_place_live` | single-leg buy/close (ENTRY legacy, EXIT single-leg, roll legs) |
| 1489 | `_place_live_roll` | atomic short roll |
| 1845 | `_place_live_open` | atomic entry |
| 2044 | `_place_live_exit` | atomic exit |
| 2211 | `_place_live_leap_roll` | LEAP roll |

`cancel_order` reaches `client.cancel_order` at `executor.py:922`.

**Primary gate location — `executor.execute(payload)` (`executor.py:261`).** This
is the single narrowest point common to *all* placement action types, reached in
production from exactly one caller, `POST /api/execute` → `app.py:474`. It already
hosts the sibling gates the time-gate should sit beside:

- reconciliation freeze — `executor.py:272-273` (`_enforce_not_frozen`)
- Level-5 account/juice gate (entry only) — `executor.py:288-289`
- exit-reason validation — `executor.py:308-309`
- `mode = "live" if live_transmit() else "logged"` — `executor.py:313`

The gate slots in around `executor.py:311-315` (after validation, before dispatch).

**Backstop chokepoint — `_assert_transmit_allowed(action)` (`executor.py:88`).**
Called at the top of every `_place_live*` function (`executor.py:639, 1431, 1468,
1816, 2010, 2192`) right before transmission; it already raises `SchwabError` to
enforce the demo-safety invariant. A settle-window market-order assertion mirrors
this idiom. **It is deliberately NOT called by `cancel_order`** — which is exactly
why cancels stay ungated.

**CANCEL path — `executor.cancel_order(order_id)` (`executor.py:887`)**, exposed at
`POST /api/order-cancel` → `app.py:504-511`. Does **not** traverse `execute()`.
Leaving the gate out of this function *is* the implementation of `CANCEL_NEVER_GATED`.

### 1.2 Action-type mapping (the spec taxonomy → this codebase) — **CONTRADICTION TO RESOLVE**

`executor.VALID_ACTIONS` (`executor.py:22-23`) = `{buy_leap, sell_short,
close_short, close_leap, roll_short, roll_leap, open_position_atomic,
close_position_atomic, adjustment}`. The gate must classify each into a spec
`action_type`. Proposed classifier (reads the `execute()` payload):

| Gate `action_type` | executor action(s) | Disambiguator |
|---|---|---|
| **ENTRY** | `open_position_atomic`, `buy_leap` | — (opening LEAP long) |
| **ROLL_SHORT** | `roll_short` | `roll_reason ∈ {scheduled, 75%-rule, earnings}` (routine) |
| **DEFENSE** | `roll_short` | `roll_reason == "defend"` (from `defend_recommendation`, `executor.py:2238`) |
| **ROLL_LEAP** | `roll_leap` | — |
| **EXIT_KILL** | `close_position_atomic`, `close_leap`, `close_short` | any (a close is always exit-side) |
| **CANCEL** | *(cancel_order path — never reaches execute())* | structural |
| *(ungated / N/A)* | `adjustment`, `sell_short` | see notes |

Notes / decisions the plan must make explicit:

- **DEFENSE reuses ROLL_SHORT.** `defend_recommendation` (`executor.py:2238`) only
  *stages* a ticket; the operator executes it as a `roll_short` payload. The gate
  must read `roll_reason == "defend"` (the value is in `executor.ROLL_REASONS`,
  `executor.py:51`) to route DEFENSE (emergency-path-eligible) vs. routine
  ROLL_SHORT (never eligible). If `roll_reason` is absent on the payload, **default
  to the stricter class (ROLL_SHORT)** — a routine roll never gets the emergency
  unlock, which is the safe failure (HARD_CFM_RULE: emergency never for routine rolls).
- **`sell_short` standalone** re-establishes an income leg (not a full entry). It
  adds short premium, not new directional risk, and is in `FROZEN_BLOCKED_ACTIONS`.
  Recommend treating it as **ROLL_SHORT-class** for gating (blocked in settle,
  never emergency) — decision to confirm in the plan.
- **`adjustment`** (`executor.py:277`) is a reconciliation bookkeeping path, not a
  broker order — **no gate**.
- **`close_short` / `close_leap`** can be a leg of a roll *or* a standalone close.
  Standalone closes are exit-side (EXIT_KILL class). A `close_leap` while a short is
  open is already rejected (`executor.py:299-303`).

### 1.3 Recommendation-record flow vs. order flow

The `Recommendation` system (`recommendation_engine.py`, `recommendation_runner.py`)
is **decoupled** from execution: the engine emits advisory records + the runner
fires a push; the operator then manually calls `POST /api/execute`. Neither
`recommendation_engine.py` nor `recommendation_runner.py` imports `executor` or
calls `place_order` (confirmed by grep). The runner's docstring
(`recommendation_runner.py:5-6`) calls itself "the exact code path a future
automation switch would call" — i.e. **automation is not yet wired to execution.**
Because the gate lives in the shared `execute()` path, both supervised approval
(today) and that future automation switch traverse it — satisfying the spec's
"gate in the shared path, not the UI" requirement.

---

## 2. Clock sources in execution paths (Phase 0 item 2) — the injected-clock inventory

### 2.1 The convention that already exists (the target pattern)

`now: datetime`, **timezone-aware UTC** (`datetime.now(timezone.utc)`); date-only
helpers use `today: date`. Fully realized in the pure layers:

- `recommendation_engine.evaluate(market, state, now, open_recs)` — `recommendation_engine.py:555`; every helper takes `now` (`:76, 281, 416, 471, 487`). Docstring purity contract: "No `datetime.now()`" (`:11`).
- `trust_derive.resolve/open_recommendations/recompute(state, now)` — `trust_derive.py:166, 289, 685`.
- `market_scheduler.*` — `is_market_open(now)` `:45`, `assign_tiers(..., clock)` `:159`, `fetch_due(..., clock)` `:248`, `EscalationTracker` methods `(…, now)` `:342, 368, 388`.
- `alert_scheduler.*` — `due_slots(now, …)` `:137`, `maintenance_due(now, last)` `:149`.

Optional-`now`/`today` (falls back to a module wall-clock reader; tests pass explicit): `entry_context.build(*, now=None)` `:74`, `tier_poll.run_cycle(now=None)` `:171`, `refresh_policy.maybe_refresh_hot(now=None)` `:167`, `position_manager.whipsaw_status(…, today=None)` `:268`.

**Tests mock by passing explicit `datetime`/`date`** — the pattern the feature's tests will use.

### 2.2 Direct wall-clock reads in execution/order paths that MUST become injectable

| # | Location | Read | Fix |
|---|---|---|---|
| 1 | `recommendation_runner.py:226` | `datetime.now(timezone.utc)` (the one injection origin; `run()` has no `now` param) | add `now` param to `run()` |
| 2 | `executor.py` — all writes via `log.utcnow()` (`logging_handler.py:35-36`) | `datetime.now(timezone.utc)`; sites incl. `171,195,246,662,862,1034,1042,1192,1231,1239,1326,1500,1613,1620,1853,2052,2219` | thread injected `now` into `execute()`/placement funcs (or let `log.utcnow()` accept an override) |
| 3 | `schwab_api.py:368` | `time.time()` in `cash_balance()` (Level-5 pre-order gate) | not strictly on the *time-gate* path; leave unless the gate reads it |
| 4 | `leap_policy.py:66` | `datetime.now(timezone.utc).date()` in `_leap_dte()` (roll-decision DTE) | add `now`/`today` param |
| 5 | `position_manager.py:176, 277` | `today or date.today()` (whipsaw / assignment) | already accepts `today`; thread it from callers |
| 6 | `option_chain.py:59, 267` | `datetime.now()` / `datetime.utcnow().date()` (chain DTE on entry/roll) | flag if gate consumes chain-derived DTE |

**The gate itself takes `now` as a parameter and reads no clock** (Design §2 "no
I/O, deterministic"). The testability requirement (Testing item 10) is met by (a)
the pure gate and (b) threading `now` into `execute()`; item 10's checklist is the
rows above.

---

## 3. Session model & gap-emergency data availability (Phase 0 item 3 + Design §1/§3)

### 3.1 Session model — **PREREQUISITE, mostly absent**

| Capability the gate needs | Exists today? | Where |
|---|---|---|
| is-market-open (trading day + hours) | Partial | `market_scheduler.is_market_open` `:45-51` (naive string compare, assumes ET) |
| full-day holidays | Yes | `market_calendar.holidays/is_trading_day` `:64-106` |
| **minutes since open** | **No** | — |
| **minutes until close** | **No** | — |
| **early-close (half-day) sessions** | **No** | explicitly declined `market_calendar.py:11-12` |
| DST-correct ET conversion | Convention exists | `ZoneInfo("America/New_York")` (see §Headline 3) |

**Plan:** a new small pure `session.py` (name TBD) computing, for an injected
UTC-or-ET `now`: `is_open`, `minutes_since_open`, `minutes_until_close`,
`is_early_close`, `close_time` — DST-correct via `zoneinfo`, reusing
`market_calendar.is_trading_day` for the holiday/weekend base and adding an
**early-close date table** (the ~3 half-days/yr: July 3 (obs.), Black Friday,
Christmas Eve — close 13:00 ET). The close blackout keys off the *actual* close
(`market_calendar` half-day awareness is the new part). `market_scheduler.is_market_open`
should be refactored to delegate to it (or the session model absorbs it) so there
is one session authority.

### 3.2 Gap-emergency inputs — **DATA GAP (largest wiring risk)**

Design §3 requires: overnight gap vs. position in ATR units; opening-range low
after `OPENING_RANGE_MINUTES`; two-sided prints for ≥ `EMERGENCY_MIN_PRINT_MINUTES`.

| Input | Computable today? | Source / gap |
|---|---|---|
| position ATR | **Yes** | `indicators` 9-day ATR (`config.ATR_WINDOW=9`), on cached daily bars |
| prior close | **Yes** | daily-bar parquet cache (`data_handler`) |
| current print | **Yes** (coarse) | throttled last/mark quote `data_handler.live_price` `:213-312` |
| overnight gap in ATR units | **Yes** | derive: (current_print − prior_close) / ATR, signed vs. position |
| **opening-range low (first 15 min)** | **No** | no minute bars / intraday OHLC tracker anywhere |
| **two-sided-quote duration** | **No** | quotes are last/mark only; no bid/ask history, no "since" tracking |

**Consequence:** `execution_window()` remains pure by accepting a `gap_context`
struct, but the *release/unlock evaluation* can only supply the ATR-gap leg today.
The plan must either (a) add a lightweight intraday opening-range + first-two-sided-
print tracker (new, but can piggyback the existing quote cadence — no new polling),
or (b) ship the emergency path as **gap-size-only** with opening-range/print-duration
gaps recorded as "unavailable → not satisfied" (fail-closed: no unlock without the
confirmations). Recommend (b) for the first cut, with the tracker as a follow-up —
fail-closed keeps the HARD rule intact (a filling gap must not unlock).

---

## 4. Recommendation state & PENDING_SETTLE (Phase 0 item 4 + Design §6)

- **Records are immutable plain dicts**, built by `recommendation_engine._build_action_rec/_all_clear_rec/_enter_rec` (`recommendation_engine.py:457-507`); persisted append-only to `state["recommendations"]` with a `rec_id` by `logging_handler.append_recommendations` (`logging_handler.py:366-385`). **state.json is the store.**
- **No `status` field, no `executable_at`.** Lifecycle is *derived*: a rec is "open" per `trust_derive.open_recommendations` (`trust_derive.py:289-302`) or terminal per one of five derived `Resolution` values (`rec_types.py:91-106`, computed by `trust_derive.resolve`). The matchable window is `emitted <= exec_at <= valid_until` (`trust_derive.py:191-194`) — there is **no lower "not before" bound**.
- **Smallest correct change:**
  1. Add optional `executable_at` (ISO) + `pending_settle` marker + a small
     append-only `settle_lifecycle` event list on the record's builders — additive,
     nullable, old records read fine.
  2. Add a **derived** `RecStatus.PENDING_SETTLE` classification (not stored on the
     immutable record — mirrors the `Resolution` pattern) surfaced where
     `open_recommendations` is computed, and teach `_matchable` to treat
     `now < executable_at` as staged.
  3. Re-validation at release + self-cancel + pre-approval belong in a new
     **runner-adjacent release pass** (the runner is impure and already the
     orchestration seam, `recommendation_runner.py:220-244`), so the pure engine
     stays pure.
- **Migration:** bump `migrations.CURRENT_VERSION` 17 → **18**, add `_v17_to_v18`
  using `state.setdefault(...)` for any new settle/pending store, register in
  `MIGRATIONS` (`migrations.py:266-283`). Additive-only; the pre-migration snapshot
  machinery (`migrations.py:300-310`) is unchanged. The nullable per-record field
  needs no per-record rewrite.

### 4.1 Approval / execution seam

No "approve" endpoint exists. Supervised flow = engine emits rec + push →
operator opens position card → executes via `POST /api/execute` (`app.py:470-474`)
or dismisses via `POST /api/recommendations/dismiss` (`app.py:833-862`). Execution
links back via optional `source_rec_id` (`trust_derive.py:152-157, 199-203`). The
pre-approve toggle for PENDING_SETTLE is a new small rec-scoped flag + endpoint,
consumed by the release pass.

---

## 5. Whipsaw guard & confirmed-close (Phase 0 item 5) — reference, do not duplicate

- **Whipsaw guard:** `position_manager.whipsaw_status(position, rolls, today)`
  (`position_manager.py:268-313`) — pure; trips on **OR** of a count leg
  (`n_def >= config.WHIPSAW_DEFEND_ROLLS`) or a drag leg
  (`drag_pct >= config.WHIPSAW_DRAG_PCT*100`), scoped to ticker + `entry_date`
  (`:279-282`). Config `config.py:737-749`. Consumed (not forked) by
  `recommendation_engine.py:314-317`, `alerts.py:281-301`, `executor.py:2293-2298`.
  The new feature only *references* the whipsaw rationale in the settle-window
  copy ("a 9:33 defensive roll on a gap is the whipsaw pattern") — no new logic.

- **Confirmed close (the convention close-blackout deferrals must re-validate
  against):** it is a *timing* convention, not a data structure. A short is
  breached only when **BOTH** the settled daily close AND the live price sit below
  the strike — `below = last_close < strike; confirmed = below and (price is None
  or price < strike)`. Verbatim in three mirror places:
  `recommendation_engine.py:343-346`, `alerts.py:238-247`, `executor.py:2261-2270`.
  The kill switch is **advisory only** (`kill_switch.py:105-120`,
  `exit_reasons.py:5-10`); RS-vs-SPY is "confirm on close" (`kill_switch.py:65`).
  These become evaluable same-day at the **16:15 post-close scheduler slot**
  (`config.py:442-449`; rationale: RS-vs-SPY + EOD circuit breaker "can only be
  evaluated AFTER the 16:00 close"). The feature's close-blackout deferral
  ("signal at 3:50 defers, re-validates against confirmed close, executable next
  session at open+settle") must reuse this `below`/`confirmed` two-step and the
  16:15 slot's confirmed-close, **not** invent a parallel notion.

---

## 6. Order type (market vs limit) (Phase 0 item 6) — the block is a forward invariant

- **No market order is ever built.** Three builders, all in `schwab_api.py`:
  `build_single_leg_order` (`:460`, hardcoded `"orderType": "LIMIT"`, `"session":
  "NORMAL"`, `"duration": "DAY"`), `build_net_order` (`:478`, `NET_CREDIT/NET_DEBIT`),
  `build_roll_order` (`:508`, `NET_CREDIT/NET_DEBIT`). Per-share limit from
  `executor._limit_price` (`:624-633`).
- **Therefore:** `NO_MARKET_ORDERS_AT_OPEN` blocks nothing today; implement it as
  a gate assertion that would refuse a (future) market order inside the settle
  window — mirroring `_assert_transmit_allowed` (`executor.py:88`). The Design §3/§4
  requirement that emergencies "execute as limit orders (marketable limits OK)" is
  **already satisfied by construction** — the emergency path just uses the existing
  LIMIT builders. No order-builder change is needed.

---

## 7. Spread-quality check (Design §5) — data location & the missing store

- **Bid/ask already fetched, no new polling:** `option_chain._fetch_chain(ticker)`
  (`option_chain.py:46-67`) caches the Schwab call chain **5 min/ticker**;
  `schwab_api.parse_call_chain` (`:556-588`) normalizes `{strike, dte, bid, ask,
  mark, delta, …}`. Positions expose `current_bid`/`current_ask` from the matched
  row (`option_chain.py:251-252, 609-610, 636-637`).
- **Canonical spread math to reuse:** `burn.exit_slippage_est(...)`
  (`burn.py:51-69`) — `half_spread = (ask - bid) / 2.0`, round-trip = full spread ×
  contracts × 100. This is the codebase's one spread formula; the dollar-slippage
  estimate the acknowledge-UI needs is this same computation.
- **No trailing-spread store exists.** Quote history on disk is daily OHLCV parquet
  + a price/tier staleness cache (`data_transport.py:110-117`) — neither retains
  bid/ask. The plan adds a small trailing per-contract spread accumulator fed from
  the already-cached chain rows (samples arrive ≤ every 5 min), with an explicit
  **"no baseline"** state when fewer than N samples exist (spec: never fabricate an
  average). Positions store `current_bid` but not a paired `current_ask`
  (`position_manager.py:230-242`), so the live chain row — not stored state — is the
  per-sample source.

---

## 8. Config constants — provenance convention

`config.py` already uses the exact `PROPOSED_DEFAULT` / `HARD_CFM_RULE` provenance
tags the spec's constant block wants (e.g. `config.py:169-184, 261-272`). The new
constants (`MARKET_SETTLE_MINUTES`, `ENTRY_EARLIEST_MINUTES`, `CLOSE_BLACKOUT_MINUTES`,
`GAP_EMERGENCY_ATR_MULT`, `OPENING_RANGE_MINUTES`, `EMERGENCY_MIN_PRINT_MINUTES`,
`SPREAD_QUALITY_MULT` as `PROPOSED_DEFAULT`; `NO_MARKET_ORDERS_AT_OPEN`,
`EMERGENCY_NEVER_FOR_ENTRY`, `CANCEL_NEVER_GATED` as `HARD_CFM_RULE`) go in one
section of `config.py`, tagged, alongside the existing `MARKET_OPEN_ET`
(`config.py:459`) / `ALERT_SCHEDULE_ANCHORS_ET` (`config.py:442`) time anchors.

---

## 9. Contradictions with the spec, flagged (not silently improvised)

1. **`action_type` taxonomy is not native** (§1.2) — must be derived from executor
   actions + `roll_reason`/exit-reason; DEFENSE ≠ its own path; CANCEL isn't a
   recommendation type. **Biggest gap.**
2. **Gap-emergency intraday inputs (opening range, two-sided-print duration) are not
   produced by the data layer** (§3.2) — recommend fail-closed gap-size-only
   emergency for the first cut, with an opening-range tracker as follow-up.
3. **No market orders exist** (§6) — `NO_MARKET_ORDERS_AT_OPEN` is a forward
   invariant; "emergencies as limit" is already true. Not a blocker, but the plan
   should say so rather than imply a market path is being disabled.
4. **Early-close sessions unmodeled** (§3.1) — the session model must add a half-day
   table; `market_calendar` intentionally omits it.
5. **`executable_at` / PENDING_SETTLE have no home on immutable records** (§4) —
   additive nullable field + derived status + a runner-adjacent release pass; do not
   mutate records or add a stored status enum.
6. **Executor clock is implicit** (§2.2) — `execute()`/`run()` need an injected
   `now` for the gate to be testable; this is a real (if mechanical) refactor.

---

## 10. Recommended build order (unchanged from spec, with the gates above)

1. **Session model** (`session.py`) + early-close table + refactor
   `market_scheduler.is_market_open` to delegate. Tests: DST, early close.
2. **`execution_window()`** pure gate + `WindowVerdict` + gap-emergency (fail-closed
   on absent intraday inputs) + action-type classifier. Full offline matrix.
3. **Wire into `execute()`** (`executor.py:261`) + backstop assertion in
   `_assert_transmit_allowed` + market-order forward-block; inject `now`. Spread
   accumulator from cached chain rows.
4. **PENDING_SETTLE**: additive record field, derived status, migration v18,
   runner-adjacent release pass with re-validation + self-cancel + pre-approve.
5. **Notifications/UI**: window-aware dual-timezone copy in
   `recommendation_runner._notify` (`recommendation_runner.py:199-213`); countdown /
   pre-approve rendering in the position card.

No files other than this audit are modified. Presenting before implementation per
Phase 0.
