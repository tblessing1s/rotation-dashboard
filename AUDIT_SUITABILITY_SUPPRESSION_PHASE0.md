# Phase 0 audit — suitability suppression tiers (prompt 2 of 2)

Date: 2026-08-24 · `TRAVIS_EXTENSION` · **No implementation. HARD STOP pending approval.**

Every claim carries a `file:line` citation. §8 lists the decisions that need an
answer before Phase 1 can be written.

---

## Summary

**The safety invariant already holds, and there is nothing to fix.** Every
position-management path derives its working set from `state["positions"]`, and
not one of them reads scan membership, the scan cache, `bench`, or `suitability`
(§3). Prompt 2's §1.6 — "if any position-management path derived from scan
membership, fix that coupling here" — has no work in it. That is the single most
important finding and it is a clean result.

**The prerequisite is half-met.** The capacity metric now exists in code exactly
as prompt 1 specified, but it has **not been deployed and has accrued zero
observations**, so the human review the prompt gates on cannot have happened
(§1). Enforcement cannot ship. Shadow-mode classification can be built and is
useful — but this is the operational gate, and it is currently closed.

**Three things prompt 2 assumes are not true of this codebase:**

1. There is no *single* visibility choke point — the sweep feeds nine consumers
   and the existing affordability filter is applied at **two** API boundaries
   independently (§2). The pattern to copy is real; the singular is not.
2. `suitability` is not a display label. It **gates the recommendation pool, the
   entry queue and the intraday hot-refresh set** (§7). Absorbing it into the
   tier system would silently change all three.
3. Skipping a name in the scan loop does not just save work — it removes that
   name's row from the day's cached sweep, which makes it permanently "missing"
   and re-triggers the incremental recompute path every request (§4). The naive
   `next_recheck_date` check at loop top is a performance regression, not a
   saving, unless the cache is taught about it.

---

## 1. Prerequisite verification (0.1)

### What exists — as specified

| Prompt 1 requirement | Where |
|---|---|
| Observation emission | `backend/maintenance.py:229-243` — one per name per scan day, off rows the sweep already computed |
| `juice_capacity_wk_pct(symbol)` | `backend/juice_capacity.py:322` |
| `CAPACITY_WINDOW_DAYS = 252`, `CAPACITY_MIN_OBS = 20` | `backend/config.py:565`, `:571` |
| INSUFFICIENT_HISTORY guard | `backend/juice_capacity.py:100` (sentinel), enforced `:366-368` |
| No-authority status | `backend/juice_capacity.py:3-8`; `capacity_detail` returns `shadow: True` / `blocking: False` as literals (`:386-387`); pinned by `test_juice_capacity.py` (AST import check + byte-identical scan output) |
| Source tagging | `SOURCE_LIVE` / `SOURCE_SEED` / `SOURCE_BACKFILL` — `backend/juice_capacity.py:102-104` |
| Display | `frontend/src/components/Scorecard.jsx:425` (`CapacityShadow`), rendered `:633` |

### What does not exist — the operational gate

**Observation count for every name: zero.** `DATA_DIR/juice_capacity_log.json`
does not exist. The metric was committed today (`eefefba`) and has not been
deployed; the nightly sweep that emits observations
(`backend/maintenance.py:229-243`) has never run against it.

**Mechanism to query counts**, once deployed:

```python
juice_capacity.capacity_detail("ET")   # -> {capacity, obs, obs_days, by_source, ...}
juice_capacity.series("ET")            # -> every stored observation
```

and per-row in the scan payload at `row["juice_capacity"]`
(`backend/metrics/scorecard.py:556-566`), rendered on every expanded card.

**Bootstrap has not been run.** `scripts/backfill_juice_capacity.py --seed
--backfill` recovers ~180 seeded days from `scan_rejection_log` plus ~254
replayed days per name, in ~4-5 minutes for the full universe. Until it runs,
every name reads `INSUFFICIENT_HISTORY` — under prompt 2's own rule
(§1.1 case 1) that means **every name is unsuppressible and the whole feature is
a no-op**.

### Verdict on the prerequisite

Prompt 2's opening says: "If the capacity metric is absent or still unreviewed,
STOP and report." It is present but unreviewed. I am not treating that as a full
stop, because you asked for the audit and the audit does not depend on the
review — but the **enforcement flip cannot happen**, and I would go further than
prompt 2 does: the shadow period should not start counting from the day the code
ships. It should start from the day the capacity numbers are reviewed against
real names, because until then there is nothing to review the classifications
*against*. See §8.1.

---

## 2. Scan membership and visibility architecture (0.2)

### How a name reaches the scan table

| Stage | Location |
|---|---|
| Intake — the operative universe | `backend/sector_data.py:305-317` (`all_tickers`: constituents + sector ETFs) |
| Requested by the sweep | `backend/metrics/scorecard.py:935` |
| Bar prefetch (all names, one batch) | `backend/metrics/scorecard.py:857` |
| **Per-name evaluation loop** | `backend/metrics/scorecard.py:877-892` |
| Per-name row build | `backend/metrics/scorecard.py:428` (`score_ticker`) |
| Canonical verdict | `backend/metrics/scorecard.py:647` |
| Triggers / path-to-ready / bench | `:650`, `:651`, `:653` |
| Day cache (per trading-day epoch) | `backend/scan_cache.py:219` (`store`), `:162` (`reusable`) |
| API — the scan table | `backend/app.py:126-153` (`/api/scan/scorecard`) |
| API — Ready-to-Enter | `backend/app.py:156-215` (`/api/scan/ready`) |
| Frontend rows | `frontend/src/components/Scorecard.jsx:723-726` → filter `:785-790` → render `:794-797` |
| Ordering | `sortRows` / `sortBench` (`:648`) — client-side |

`candidate_universe` (`backend/candidate_universe.py:11-14`) is a **shadow**
intake artifact; the sector universe stays operative unless
`config.UNIVERSE_SCREEN_ENABLED`. Not a suppression surface.

### The choke point — there isn't one, there are two, and that is fine

Nine call sites consume the sweep:

| Consumer | Purpose | Should suppression apply? |
|---|---|---|
| `app.py:143` `/api/scan/scorecard` | the scan table | **yes** |
| `app.py:183` `/api/scan/ready` | Ready-to-Enter shortlist | **yes** |
| `recommendation_runner.py:129` | ENTER candidates | **yes** (entry) |
| `refresh_policy.py:159` | on-demand single-name refresh | no — explicit request |
| `entry_context.py:166` | entry snapshot at trade time | no — single explicit name |
| `maintenance.py:217` | nightly telemetry + observation emission | **no — must see everything** |
| `universe_health.py:41` | diagnostics | no |
| `screening.py:88` | cache warm | no |
| `metrics/scorecard.py:935` | the sweep itself | no |

**The precedent to copy already exists.** Affordability — structurally the same
problem, "a name that is in the universe but should not be shown as actionable" —
is implemented as `split_by_affordability` (`backend/metrics/scorecard.py:810`),
a shared annotate-then-partition function applied at the API boundary, *not*
inside the sweep. `app.py:135-137` states the reasoning: the filter is applied
there "so the memoized market scan stays account-free and shared across
requests". It is called from both `app.py:146` and `app.py:199`.

**Recommendation:** a `split_by_suitability(rows)` mirroring it exactly, called
at the same two API boundaries plus `recommendation_runner._entry_candidates`.
Three call sites, one shared function, zero changes inside the sweep. The sweep
keeps computing every name — which is what `maintenance` needs for the
observation feed (§4) and what keeps the cache coherent (§4).

Rows must be **annotated in place** with their tier before partitioning, exactly
as affordability annotates `affordable` / `lot_cost_over_by`
(`backend/metrics/scorecard.py:813-816`), so a suppressed row can explain itself
wherever it is shown.

### Bench admission

`row["bench"] = scan_triggers.is_bench(rv["verdict"], rv["triggers"])` —
`backend/metrics/scorecard.py:653`, function at `backend/scan_triggers.py:584-605`.

`is_bench` is **pure over (verdict, triggers)** and takes no symbol. Two options:

- **(a) Inside `is_bench`** — needs a new `suppressed` argument threaded from
  `score_ticker`. Keeps one definition of "benchable", but puts a
  capacity-derived input inside a pure gate-composition function, which is
  exactly the boundary the shadow discipline exists to protect.
- **(b) In `split_by_suitability`** — clear `bench` on suppressed rows at the
  same choke point that hides them, next to the tier annotation.

**Recommend (b).** It keeps suppression entirely outside `scan_triggers`, so
`is_bench` stays a pure function of the gate and the "suppression is a visibility
concern, not a gate concern" boundary is structural rather than conventional.
The ET/XLE test in prompt 2 §1.7 ("`is_bench` false regardless of L4 status")
passes either way.

---

## 3. Position-management isolation (0.3) — clean, nothing to fix

Every path, and what its working set derives from:

| Path | Working set | Citation |
|---|---|---|
| Kill switch | `state["positions"]` | `backend/kill_switch.py:93-96` (`evaluate_all`) |
| Defend / roll recommendations | `state["positions"]` | `backend/recommendation_runner.py:177-188` |
| Reconciliation | `state["positions"]` + the broker's own position list | `backend/reconcile.py:242`, `:755`, `:794`; broker side `:192` |
| Assignment handling | `state["positions"]` | `backend/position_manager.py` — no scan import |
| Portfolio risk / reserves | `state["positions"]` | `backend/portfolio_risk.py:145`, `:211`, `:235` |
| Spread monitor | position legs | `backend/spread_monitor.py` — no scan import |
| Order lifecycle | `state["pending_orders"]` / `order_events` | `backend/order_lifecycle.py` — no scan import |
| Accrual | the executions ledger | `backend/accrual.py` — no scan import |
| Intraday bar refresh | **Tier 1 = open positions, "never dropped"** | `backend/refresh_policy.py:101-104`, cap at `:116` leaves room for every position |

A grep for `scorecard` / `scan_cache` / `suitability` / `bench` / `scan_triggers`
across `kill_switch.py`, `position_manager.py`, `reconcile.py`,
`spread_monitor.py`, `portfolio_risk.py`, `order_lifecycle.py` and `accrual.py`
returns **nothing**. The two modules that do reference the scan reference it for
entry only:

- `recommendation_runner._entry_candidates` (`:118-153`) is explicitly
  entry-scoped, and its own error handler says so: *"a failed sweep never blocks
  position recs"* (`:152`). Position snapshots are built separately at `:177-188`.
- `recommendation_engine.py:521-529` is `_entry_blocked` — the entry gate.

**Conclusion: hiding a name from the scan cannot remove it from any
position-management path.** The invariant is structural today, not merely
observed. What Phase 1 owes is not a fix but a **regression test that pins it** —
prompt 2 §1.7's byte-identical open-position test is exactly right, and it is
cheap because it is asserting something already true.

One thing to be careful about, which is *not* a violation but is adjacent: the
hot-refresh set's Tier 2/3 (`backend/refresh_policy.py:106-114`) derive from scan
rows. A suppressed name loses intraday refresh priority. That is correct — it is
not an entry candidate — and Tier 1 guarantees any name with an open position
keeps it regardless of tier. Worth a test, not a change.

---

## 4. Scheduling machinery (0.4)

### Current cadence — everything, every sweep, once per trading day

There is no per-name cadence or staleness logic. `_compute_scorecard`
(`backend/metrics/scorecard.py:877-892`) evaluates every name unconditionally.
What limits cost is the **day cache**: `scan_cache.scan_day`
(`backend/scan_cache.py:103-118`) rolls just after the close, so one sweep serves
the evening, the next pre-market and the following session. A regime flip
changes the fingerprint (`:121-133`) and forces a fresh sweep.

The nearest thing to per-name scheduling that exists is `scan_cache.reusable`
(`:162-204`), which already does per-name row reuse for universe edits.

### The trap in "skip at loop top"

`scan_cache.store` (`:219`) persists the sweep result as the day's cache, and
`reusable` computes `missing` as names with **no stored row** (`:196`). So a
suppressed name skipped in the loop produces no row, lands in `missing` on every
subsequent read, and the incremental path (`backend/metrics/scorecard.py:953-965`)
recomputes it — every request. The naive skip is a **performance regression**.

Three ways out:

- **(a) Carry the row forward.** On a skip, reuse the name's last stored row
  (stamped stale) instead of computing a fresh one. The cache stays complete,
  `missing` stays empty, and the suppressed section has real data to render.
- **(b) Teach `reusable` about suppression.** Adds a capacity-derived input to
  the cache layer — a new coupling in the wrong direction.
- **(c) Don't skip at all.** Compute everything, suppress only at the visibility
  choke point (§2).

**Recommendation: (c) for Phase 1, with (a) as a later optimisation if the sweep
cost ever justifies it.** The honest accounting: the sweep is already bounded to
roughly once per trading day, it is off the request path, and it is what feeds
the capacity observations. Skipping evaluation saves CPU on a job that is not
CPU-constrained, while introducing a cache-coherence hazard, an observation gap
(below), and a staleness question for the suppressed section's own display. The
`next_recheck_date` still gets **computed and stored on the transition event** —
it is what the UI shows and what a later optimisation would key off — but Phase 1
should not yet let it gate evaluation. This is a deliberate departure from prompt
2 §1.3; see §8.2.

### What a skip must not skip — and it is separable

**Daily bar ingestion is already outside the per-name loop.**
`data_handler.prefetch(...)` at `backend/metrics/scorecard.py:857` warms every
name in one parallel batch *before* the loop at `:877`; `prefetch` → `get_many` →
`get_daily` (`backend/data_handler.py:214-217`, `:203-211`, `:174-201`) is what
actually fetches and writes the bar cache. `score_ticker`'s own `get_daily`
(`:458`) then reads the warm cache.

So bars stay warm for a suppressed name under any skip design, as long as the
name remains in the list passed to `_compute_scorecard`. A readmitted name
returns with full history — no INSUFFICIENT_DATA on structure or INST. **Pin
this with a test rather than trusting the ordering to survive refactors.**

### Observation-feed interaction

If suppressed names were skipped, they would stop emitting capacity observations
between rechecks, because emission reads the sweep's rows
(`backend/maintenance.py:229-243`).

Implication, stated precisely: the median goes **stale but valid** — it is a
trailing median over a 252-day window, and dropping to weekly or monthly sampling
does not invalidate it, it just thins it. But two second-order effects deserve
naming:

1. **The guard interacts badly with thinning.** `CAPACITY_MIN_OBS = 20` counts
   distinct days (`backend/juice_capacity.py:366`). A STRUCTURAL name sampled
   every 30 days accrues 20 observations in ~20 months. If it ever ages out of
   the window (`CAPACITY_LOG_DAYS = 315`, `backend/config.py:574`), it flips to
   INSUFFICIENT_HISTORY — which prompt 2 defines as *unsuppressible* — and pops
   back into the scan. A suppression scheme that starves its own input eventually
   un-suppresses everything.
2. **`current` becomes as stale as the sampling.** Tier 3's readmission test
   (`current ≥ floor × 1.00`) reads a number last computed up to 30 days ago.

Under recommendation (c) neither arises: observations continue daily for every
name, the median stays dense, and `current` is always today's. This is a further
argument for (c) beyond the cache hazard.

If (a) or a later skip is adopted, the recheck evaluation **must** run the full
`score_ticker` so the observation emits — that is automatic, since emission reads
whatever rows the sweep produced.

---

## 5. Classification-transition events (0.5)

### Two registries, and the tier events belong in the telemetry one

**`state.json`** is the trading record: schema v21 (`backend/migrations.py:332`,
`_v20_to_v21`), holding the append-only executions ledger plus derived positions
and ledgers rebuilt by `logging_handler.recompute_derived`
(`backend/logging_handler.py:790`). Its typed lists are all trade artifacts —
`order_events` (`:470`), `recommendations` (`:504`), `recommendation_overrides`
(`:521`), `order_receipts` (`:384`).

**Standalone append-only stores under `DATA_DIR`** are the derived-telemetry
registry: `scan_rejection_log`, `scan_diff_log`, `iv_history`, `regime_history`,
`symbol_genius_history`, `structure_labels`, `candidate_universe`, and now
`juice_capacity`. The rule is stated at `backend/scan_rejection_log.py:15-20`.

A tier transition is a market-observation classification with no execution behind
it. It belongs in the second registry. Putting it in `state.json` would also mean
a schema migration and a `recompute_derived` question it has no answer to.

### The reference pattern

Prompt 2 names "the industry resolution service scoping" as the reference if
landed. **It has not landed** — `CHANGELOG.md` (v2.12.0) records the industry
peer-basket benchmark as *planned* and explicitly not part of that change, and
there is no such module.

**Nearest analog, and a very close one: `scan_diff_log` + `scan_diff`.** It is
literally a per-symbol state-transition event log:

- Event vocabulary as module constants — `backend/scan_diff.py:28-34`
  (`SCAN_BENCH_READY`, `SCAN_NEW_READY`, `SCAN_DEGRADED`, …), mirroring
  `alerts.ALERT_TYPES` ids.
- Event constructor — `backend/scan_diff.py:39-40`,
  `{type, ticker, message, data}`.
- `diff()` is a **PURE fold over two record maps** (`:1-24`), no I/O, no clock.
- Persistence — `backend/scan_diff_log.py:1-14`: append-only `events` list, with
  a small last-write-wins `snapshot` riding alongside for cross-day comparison.
- Emitted from the nightly sweep at `backend/maintenance.py:246-268`, comparing
  `scan_rejection_log.latest_before()` against today's rows.

**Registration points for tier transitions:** the event-type constants next to
`scan_diff`'s (or in the new module), the store, and the nightly emission site.
Prompt 2's requirement that "current tier is derivable from the event stream" maps
exactly onto `scan_diff_log`'s append-only `events` + derived-`snapshot` split —
the snapshot is the cache, the events are the truth, and the codebase already has
that shape working.

Whether tier transitions should also fan out through `alerts.record_event` the
way scan-diff events do is a real question, and prompt 2 §1.8 says no
("notification/alerting on tier changes" is out of scope). Noting it because the
analog does it by default and the omission should be deliberate.

---

## 6. UI surfaces (0.6)

**Component:** `frontend/src/components/Scorecard.jsx`.

- **Rows pipeline** — `:723-726` (`results`, with per-row refresh overrides),
  filter predicate `:781-784`, applied `:785-790`, sorted `:794-797`.
- **Filter chips** — `const FILTERS = ["ALL", "READY", BENCH, "CAUTION",
  "WATCH", "BLOCKED"]` at `:643`; counts computed `:731-740`. A `SUPPRESSED`
  chip drops in here, and the counts memo is where `Suppressed (N)` comes from.
- **Collapsed-section precedent** — `ShadowFloorLog` (`:1019`): a `▸`-toggle
  button with a `NO AUTHORITY` badge, collapsed by default, rendering a table
  when open. That is the exact shape prompt 2 §1.4 describes.
- **Expanded-card `Suitability` row** — `:617`, inside the `Readout` grid,
  with `suitability_reasons` beneath (`:620`) and `suitability_notes` (`:628`).
  `CapacityShadow` (`:425`) and `StructureShadow` (`:362`) render below at
  `:633-634`.

The one wrinkle: the bench view has its own sort (`sortBench`, `:648`) and the
counts memo asserts *"a row is never BOTH a WATCH and a BENCH"* (`:733-734`).
Tiers add a second orthogonal axis — a row can be WATCH *and* SUPPRESSED — so
whether the chip row stays mutually exclusive or the suppressed section sits
outside the chips entirely is a genuine design choice. Recommend the latter: a
separate collapsed section below the table, not a seventh chip, so the existing
mutual-exclusivity invariant survives untouched.

---

## 7. Existing `SUITABILITY: AVOID` (0.7)

### What produces it

Two paths, both in `score_ticker`:

1. **Gate short-circuit** — `backend/metrics/scorecard.py:746-752`: a stock-level
   (Level 3/4) gate failure sets `suitability = "AVOID"` with reason
   `"fails entry gate level N"`. This is what ET and XLE show.
2. **The metric rules** — `compute_verdict` (`backend/metrics/scorecard.py:242-277`),
   a GO/CAUTION/AVOID momentum lens. AVOID fires on `below_ma200`
   (`:273-274`) or ATR extension over `T.ATR_EXTENSION_MAX` (`:275-277`).
   CAUTION rules follow; ETFs waive the growth-momentum filters (`:250-256`).

### Its consumers — and this is the finding

`suitability` is **not** a display label. Per the comment at
`backend/metrics/scorecard.py:268-272` — *"`suitability` gates the recommendation
pool, the internal queue and the intraday hot-refresh set"* — and confirmed:

| Consumer | Citation |
|---|---|
| ENTER candidate pool | `backend/recommendation_runner.py:135` (`suitability == "GO"`) |
| Entry queue ranking | `backend/queue_state.py:67` |
| Intraday hot-refresh set, Tier 3 | `backend/refresh_policy.py:112-114` |
| Persisted calibration record | via the row, `backend/scan_rejection_log.py:104-147` |

### Recommendation: keep it, rename the display

Absorbing `suitability` into the tier system would silently change the
recommendation pool, the entry queue and the hot-refresh set — three
behaviours prompt 2 §1.8 puts out of scope. It must stay.

The constraint is a UI one: *two overlapping suitability labels on one card is
not acceptable*. That is satisfiable without touching the field:

- Relabel the existing `Readout` at `:617` from **"Suitability"** to
  **"CFM lens"** (or "Momentum"), which is what it has always measured — a
  stock-momentum verdict, per its own docstring at `:246-247`.
- Give the tier the **"Suitability"** label, since that is what it actually
  answers: is this a name we should be looking at at all.

One label per concept, one card, no behavioural change, and the naming gets more
honest than it is today. The alternative — keeping "Suitability: AVOID" and
adding "Tier: SUPPRESSED_STRUCTURAL" — is the two-label outcome the prompt rules
out.

Worth stating: the two signals answer genuinely different questions and *should*
be able to disagree. A name can be SUITABLE (capacity clears the floor) and AVOID
(below MA200 today), or SUPPRESSED_STRUCTURAL and GO (great momentum, no premium
— precisely the ET/XLE case that motivated this work). Collapsing them would
destroy the distinction the feature exists to draw.

---

## 8. Open decisions

**8.1 The shadow clock should start at review, not at deploy.** Prompt 2 §1.5
sets `SUPPRESSION_SHADOW_DAYS = 14` before enforcement. But until the capacity
backfill runs and the numbers are reviewed (§1), every name reads
INSUFFICIENT_HISTORY and every classification is SUITABLE — 14 days of that
observes nothing. Recommend the shadow period start from the dated review of the
capacity numbers, recorded in the changelog entry, and that the flip check be
against that date. **Concretely: run the backfill, review ET/XLE/a known
compressed name, then start the clock.**

**8.2 Recheck cadence: store the date, don't gate on it yet (§4).** I recommend
computing and persisting `next_recheck_date` on every transition event and
displaying it, but *not* letting it skip evaluation in Phase 1 — the cache
hazard, the observation-starvation feedback loop, and the staleness of `current`
all argue against it, and the CPU it saves is not on a constrained path. This is
a departure from prompt 2 §1.3 and needs your explicit yes or no. If you want the
skip, the design must be option (a) from §4 (carry the row forward), not the loop-top
check as written.

**8.3 Which floor?** Same issue flagged in prompt 1's audit §8.2 and now settled
in code: `scan_triggers.floor_for_profile` (`backend/scan_triggers.py:392-406`)
resolves the profile-aware bar — `SHARES_JUICE_FLOOR_PCT = 0.75` for
JUICE_ENGINE, `COMBINED_YIELD_FLOOR_WK = 0.5` for DIVIDEND_COMPOUNDER. Prompt 2
§1.1 says to reuse "`JUICE_FLOOR_WK_PCT`", which does not exist, and its worked
examples assume a single 0.70 floor. **Confirm the tier classifier reads
`floor_for_profile`** — under which ET, as a DIVIDEND_COMPOUNDER, is judged
against 0.5 and not 0.7. Its ~0.31%/wk combined capacity is 62% of that floor,
which lands it in **SUPPRESSED_CONDITION, not STRUCTURAL** (the 0.60 ratio
boundary). If you want ET structural, either the ratio or ET's floor needs to
move — and that is a calibration decision, not an implementation detail.

**8.4 Backfilled capacity understates dividend payers.** Carried forward from
prompt 1 §8.7, and it now bites: backfilled observations carry no dividend leg
(`backend/juice_capacity.py:56-61`), so a payer's backfilled median is
juice-only — for ET that is 0.14 rather than 0.31, which *would* be structural at
any floor. `capacity_detail` reports `by_source` (`:361-364`) precisely so this
is decidable. **Recommend: a structural suppression requires a minimum count of
live or seeded observations, not backfill alone.** Needs a constant and your
number.

**8.5 `SUPPRESSED_CONDITION` on an INSUFFICIENT_HISTORY capacity is undefined.**
Prompt 2 §1.1 case 1 makes INSUFFICIENT_HISTORY unsuppressible outright, but case
3 (`capacity ≥ floor×0.60 AND current < floor×0.80`) is a *condition* judgement
that does not really need a trustworthy capacity — it needs to know capacity is
not terrible. Recommend keeping case 1 as written (unsuppressible, full stop) for
the simplicity of the invariant, but flagging that it means a genuinely
compressed new name stays visible. That is the right default — visible is the
safe failure — just worth knowing it is a deliberate cost.

**8.6 GDDY Aug 21 still does not exist** (prompt 1 audit §8.5). XLK July 6 is
real and passes. Prompt 2 §1.7 requires both. I will pin XLK and note GDDY's
absence rather than inventing a fixture.

**8.7 Tier events and the alert fan-out.** The `scan_diff` analog routes its
transition events through `alerts.record_event` by default
(`backend/maintenance.py:246-268`); prompt 2 §1.8 puts tier-change notification
out of scope. Confirming the omission is deliberate, not inherited by accident.

---

## Deliverable status

Audit complete. **No implementation code written. HARD STOP** pending approval of
the recommendations in §2 (split-at-the-boundary choke point + bench option b),
§4 (don't gate on recheck dates in Phase 1), §5 (scan_diff_log-shaped event
store), §7 (keep `suitability`, relabel the display), and answers to §8 —
particularly **8.1** (the operational gate) and **8.3** (which floor, and whether
ET is meant to land structural).
