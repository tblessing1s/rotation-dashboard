# Changelog

## v2.6.0 — Recommendation engine + trust scoreboard + execution fidelity ledger (state schema v17)

The trust layer that must exist before any automated execution is permitted.
The app now (a) commits to specific, actionable recommendations BEFORE the
operator acts, (b) measures agreement between its recommendations and the
operator's actual actions, and (c) grades whether every order lifecycle behaved
exactly as specified. Automation eligibility is a derived, per-action-type,
display-only readout — **no automated order submission exists anywhere in this
version**, and while post-fill reconciliation is `NOT_YET_IMPLEMENTED` no
action type may graduate. Operator doc: `docs/trust-layer.md`.

- **Recommendation records** (`recommendations`, append-only, immutable):
  every scheduled alert slot also runs an evaluation pass emitting, per open
  position, either an actionable recommendation (EXIT / DEFEND / ROLL_OUT,
  with a full proposed ticket: legs, strikes, NET limit, minimum acceptable
  net credit, max slippage vs mid) or an explicit `ALL_CLEAR` — silence is not
  a valid output. Coded trigger rules (`rec_types.TriggerRule`), frozen
  `input_snapshot` (incl. condition-first-true dates for timeliness),
  `valid_until` expiry, and supersession chains.
- **Same-code-path invariant**: the engine
  (`recommendation_engine.evaluate`) is a PURE function over a frozen market
  snapshot + injected clock — the exact function a future automation switch
  would call — and it reuses the existing single sources of truth rather than
  forking them: `strike_policy` for every proposed strike, a newly extracted
  pure `kill_switch.classify` core, `circuit_breaker.evaluate(df=...)`,
  `position_manager.whipsaw_status` / `enrich_short` / a new shared
  `delta_coverage` core (the `DELTA_UNCOVERED` alert now calls the same core).
  The impure shell (`recommendation_runner.py`) owns providers/clock/state.
- **Resolution matching** (derived in `recompute_derived`, never
  hand-entered): executions match the latest open, valid recommendation of the
  same action type on the same position (`source_rec_id` passthrough from the
  UI makes it exact); dismissals carry coded override reasons
  (`DISAGREE_TIMING/STRIKE/ACTION`, `EXTERNAL_INFO`, `DISCIPLINE_LAPSE`,
  `OTHER`+note) as append-only override records; expiries and **coverage
  misses** (an action with no matching recommendation — the loudest failure)
  are synthesized. Pre-activation history (`metadata.trust_layer_since`) and
  out-of-scope mechanics (LEAP rolls, scale-ins, leg repairs, adjustments) are
  excluded by rule.
- **Execution fidelity ledger** (`order_fidelity`, derived + retained past the
  order_events cap): per live ticket — `LIFECYCLE_LEGAL` (replayed against the
  now data-encoded legal transition graph in `order_lifecycle`),
  `SLIPPAGE_IN_BOUND` (reusing `slippage.py`'s exact math against the ticket's
  own bound), `NO_ORPHAN_LEG` (incl. the fill-during-cancel race),
  `CANCEL_CONFIRMED_DEAD` (a cancel that never confirms terminal fails after a
  deadline), and `RECONCILED_CLEAN` = `NOT_YET_IMPLEMENTED` (never a silent
  pass). Paper tickets are graded on what a paper fill can express, flagged
  paper. Failures page via new `ORDER_FIDELITY_FAIL` / `TRUST_COVERAGE_MISS`
  alerts; new actionable recommendations push via the existing notifier.
- **Trust scoreboard** (`trust_scoreboard`, derived; `GET
  /api/trust-scoreboard` + Settings-tab panel): coverage, precision (+ override
  breakdown), timeliness (emission lag + late-after-action flags), fidelity
  pass rate, and per-action-type graduation status with the failing criterion
  named. Criteria: `GRAD_MIN_LIVE_CYCLES`=10, `GRAD_MIN_WEEKS` 8/16/16/26
  (ENTER never eligible), override rate <= 0.10 with zero `DISAGREE_ACTION`
  (PROPOSED_DEFAULT); zero coverage misses, 100% fidelity, reconciliation
  green (HARD, in code).
- **UI**: recommendation cards on each position (proposed ticket, trigger,
  validity countdown, one-tap Execute into the existing flow / Dismiss with a
  forced coded reason), open-recommendation count in the Overview digest, and
  the Trust Scoreboard panel with coverage misses and fidelity failures
  rendered loud.
- **Schema v17** (pre-migration snapshot as always): adds `recommendations`,
  `recommendation_overrides`, `order_fidelity`, `metadata.trust_layer_since`;
  `recommendation_resolutions` + `trust_scoreboard` are derived keys.
- **Offline test suite** (53 new tests): the XLK July-6th labeled failure case
  regression-locked (real scorecard path must block, engine must emit NO
  ENTER), the AAPL laggard -> `KILL_RS_SECTOR` EXIT on first pass, ALL_CLEAR
  emission, coverage-miss synthesis, stale/superseded/overridden matching,
  timeliness lag + late-after-action, graduation math (miss / under-cycles /
  reconciliation-blocked, each with the named reason), fidelity lifecycles
  (clean two-leg, fill-during-cancel orphan + page, unconfirmed cancel,
  out-of-bound slippage), crash recovery (open recs survive restart, no
  duplicate claims in-window), and migration idempotency.

## Risk-path math hardening

The app's *accounting* math was already honest; three places where a
bookkeeping-safe simplification leaked into a live **risk** decision (defend,
kill switch, assignment) are now corrected, and three more flagged items get
permanent verification tests. **No strategy rule, threshold, or trigger level
changed — only the inputs to them.** Payout/ledger outputs are untouched. Full
audit in `AUDIT_RISK_PATH.md`.

### Risk paths now run on honest inputs

- **Unclamped capture on the defend view.** The short-capture meter clamps/floors
  at 0% for payout accounting (an IV spike must never book as negative income) —
  correct there, but it hid an *underwater* short leg from the management view.
  `enrich_short` now also emits a signed `extrinsic_captured_pct_raw` and an
  `extrinsic_above_entry` flag; the position card surfaces the raw figure and an
  "extrinsic above entry (IV event)" indicator, and a new LOW-severity
  `EXTRINSIC_ABOVE_ENTRY` alert fires when a short's extrinsic rises >25% above
  entry. The clamped payout figure is unchanged.
- **Direct sector RS for the kill switch (and gate + scorecard).** RS3M-vs-sector
  was the *difference* of two RS-vs-SPY figures; it is now the true direct ratio
  `rs3m(stock, sector_etf)` over the same 63-day lookback everywhere — the same
  `indicators.rs3m` with a different benchmark (no fork), at zero extra cache
  cost. The kill switch's thinning band no longer lags on large sector moves. The
  entry-context snapshot records `rs3m_vs_sector_method` (snapshot schema v2→v3,
  additive; old snapshots still load).
- **Dividend-adjusted greeks on the assignment path.** The real continuous yield
  `q` (existing `dividends` cache; `q_source` logged, `0` fallback explicit) now
  flows through the delta-coverage guardrail, `portfolio_risk._leg_greeks` (book
  delta / beta-adjusted leverage), and the live `leap_health` roll-timing numbers
  (matching the stored burn marks, which already used q). The dividend-assignment
  trigger's extrinsic is the live quote (already q-aware via the market); when
  there is *no* quote — off-hours before ex-div, where it went silent — it now
  falls back to a q-aware Black-Scholes extrinsic so the escalation still fires.

### Verified and pinned

- **Day-count convention documented and pinned.** Juice/week and burn/week are on
  one shared 7-calendar-day base (θ ÷365 calendar × 7); a permanent worked-example
  test (`test_net_juice_day_count_convention_is_pinned`) encodes it end-to-end so
  it can't drift. θ's ÷365 is unchanged.
- **Payback state-machine validation.** `validate_payback` flags the three silent
  corruption modes (dangling LEAP roll, orphan roll-buy, `legs_remaining`
  mismatch) so a mislabeled execution log can no longer produce a
  plausible-but-wrong payback target; surfaced on `payback_reconciliation` (never
  raises into recompute). A full-cycle fixture asserts the meter at every
  transition, plus mutation negatives.
- **SAR causality property test.** Parabolic SAR (and the full four-light
  published regime) computed on history truncated at date D equals the value at D
  from the full-history run, for every D over a year of fixture bars — the
  invariant the regime backfill relies on. A boundary test documents that the
  guarantee holds only for prefixes sharing the earliest bar, which the backfill
  now makes explicit.

## Order lifecycle: entry order type + broker-side cancel/retry state machine

Two entry-path fixes, both fully exercisable offline (mocked broker + mocked
clock — no order is ever auto-sent to the live broker as part of this work).

**Entry order type.** The live entry was already ONE atomic two-leg NET_DEBIT
diagonal (buy-to-open the deep-ITM LEAP + sell-to-open the weekly short on one
ticket); the gap was that `build_net_order` hardcoded `complexOrderStrategyType`
/`duration` while the roll routed them through config. The entry now reads its own
provenance-tagged constants (`ENTRY_COMPLEX_STRATEGY_TYPE` / `ENTRY_ORDER_DURATION`)
so entry and roll can't silently disagree — `CUSTOM`/`DAY` today, with DIAGONAL a
`[LIVE-VERIFY]` swap. The standalone `buy_leap`/`sell_short` actions stay for
scale-in and leg repair; a fresh two-leg entry routes atomic (UI default).

**Cancel is broker-first, with an explicit state machine.** Cancels already sent
`DELETE` to Schwab before clearing local state and confirmed the async cancel; this
change makes the whole lifecycle explicit and closes the resubmission/partial gaps:

- **Explicit coded states** (`order_lifecycle.py`, pure functions):
  `SUBMITTED → WORKING → { FILLED | CANCEL_REQUESTED → PENDING_CANCEL →
  { CANCELED | FILLED_DURING_CANCEL | PARTIAL_FILL_CANCELED } | REJECTED | EXPIRED }`,
  plus a non-terminal `LOCKED_UNKNOWN` hard lock.
- **Fill-during-cancel** (a fill that lands after the DELETE): the fill is
  reconciled into state, the order is NOT retried, and a CRITICAL alert fires — the
  position is unexpectedly live.
- **Partial fill on cancel**: recorded as a distinct `PARTIAL_FILL_CANCELED` state
  that freezes the position for defensive review, trips the delta-coverage
  guardrail review, and alerts. The app flags; it never auto-fixes an unbalanced
  position.
- **Resubmission gate** (`NO_RESUBMIT_BEFORE_TERMINAL`): a per-position-intent lock
  persisted in `state.json` (survives restart). A new live order for an intent may
  only be sent once the prior order is confirmed terminal at the broker AND
  reconciled; `MAX_RESUBMIT_ATTEMPTS` per session then stops with an alert. This is
  IN ADDITION to the Level-5 account gate, kill switch, and reconciliation freeze —
  none are weakened.
- **DELETE failure handling**: if the DELETE is refused because the order already
  filled, the fill is reconciled; if it's refused while the order is still WORKING,
  the cancel retries per the bounded poll policy and, if exhausted, the position is
  hard-locked (`LOCKED_UNKNOWN`) — no resubmit ever while the broker state is
  unknown.
- **Startup reconciliation**: on app start, every locally non-terminal order is
  re-polled against the broker before any new order activity is allowed for its
  position; an unreachable order hard-locks its position so a crash mid-cancel can't
  orphan a working broker order invisibly.
- **Every transition is an append-only event** in `state.json` (`order_events`,
  with prior/new coded state + raw broker status); `recompute_derived()` derives the
  current `order_state` from the log — order state is never a mutated field.

### What changed

- **`backend/order_lifecycle.py`** (new): the coded-state vocabulary,
  `map_broker_status()`, `is_terminal()`, and the `check_resubmit()` invariant — all
  pure functions, no I/O.
- **`backend/executor.py`**: resubmit gate + per-intent lock on the live entry
  placers; `CANCEL_REQUESTED`/`PENDING_CANCEL`/`PARTIAL_FILL_CANCELED`/
  `FILLED_DURING_CANCEL`/`LOCKED_UNKNOWN` on the cancel path; config-driven bounded
  cancel polling; `reconcile_pending_orders_on_startup()`.
- **`backend/schwab_api.py`**: `build_net_order` takes `complex_strategy_type` /
  `duration` (defaults unchanged for exit / LEAP roll).
- **`backend/config.py`**: `ENTRY_COMPLEX_STRATEGY_TYPE`, `ENTRY_ORDER_DURATION`,
  `ORDER_FILL_TIMEOUT_SEC`, `CANCEL_POLL_INTERVAL_SEC`, `CANCEL_POLL_MAX_ATTEMPTS`,
  `MAX_RESUBMIT_ATTEMPTS`, `REPRICE_ON_RETRY` (`"none"` — never silently chase
  price), `NO_RESUBMIT_BEFORE_TERMINAL` (all provenance-tagged).
- **`backend/logging_handler.py`**: `order_events` / `order_locks` stores,
  `append_order_event`, `get`/`save_order_lock`, `list_pending_orders`, and
  `order_state` derivation in `recompute_derived`.
- **`backend/alerts.py`**: `ORDER_FILLED_DURING_CANCEL`, `ORDER_PARTIAL_FILL_CANCELED`,
  `ORDER_STATE_UNKNOWN`, `ORDER_RESUBMIT_EXHAUSTED` alert types.
- **`backend/app.py`**: `/api/execute` maps `ResubmitLockedError` to HTTP 409;
  startup runs order reconciliation after the durability check.
- **Migration v16** seeds the additive `order_events` / `order_locks` stores.
- **`backend/test_order_lifecycle.py`** (new): the pure state machine + the ten
  lifecycle branches (clean cancel, fill/partial during cancel, DELETE-error races,
  rejection, crash/startup reconcile, lock-held + max-attempts, golden entry JSON) —
  all offline with a mocked broker and clock.

## Payout = juice − LEAP burn (the leftover)

The monthly payout now nets out the **LEAP's weekly extrinsic burn**, so the
headline figure is the *leftover* an operator can actually take rather than the
raw juice: `payout = net juice collected − LEAP extrinsic burn`. The burn is the
REALIZED weekly extrinsic decay from the burn marks (`burn_marks.py`, same
whole-position dollars as the juice ledger), summed over the month and clamped so
a roll or IV spike that grows extrinsic can't masquerade as income.

### What changed

- **`backend/burn_marks.py`**: `monthly_realized_burn()` — realized LEAP extrinsic
  burn per calendar month, summed across tickers from consecutive marks'
  extrinsic drops (negatives clamped to 0 — burn is only ever a cost).
- **`backend/payouts.py`**: every month now carries `net_juice`, `leap_burn`,
  `burn_tracked`, and `net_payout` (the leftover); the payout headline and the
  finalize/paid snapshots are the leftover, with the juice/burn breakdown frozen
  alongside. Totals gain YTD juice and YTD LEAP burn. When a month has no burn
  marks yet the payout degrades cleanly to juice-only and says so.
- **`PAYOUT_READY` alert** now headlines the leftover with the juice − burn
  breakdown.
- **Frontend**: the Payouts cards, history table (Juice / LEAP burn / Leftover
  columns), and totals show the breakdown; the Overview glance headlines the
  leftover with the juice − burn sub.

## Monthly payout tracking

Income is booked as **net juice** (premium sold − buyback) on every short close,
but the dashboard had no month-by-month view of it and no notion of the operator
*paying themselves out* each month. A new **Payouts** tab tracks that: the
current month's estimated payout, the previous month's payout, the full monthly
history, and a per-month finalize → paid record — plus a push alert the moment a
month's payout can be finalized so it doesn't get forgotten.

A month moves through **in progress → finalizable → finalized → paid**. It
becomes *finalizable* — the point its income is locked in — the moment its **last
short of the month closes** (no open short leg still expires in it, so rolling the
final weekly into a next-month expiry flips it immediately), or when the calendar
month ends, whichever comes first. Finalizing snapshots the amount; marking paid
records the withdrawal.

### What changed

- **`backend/payouts.py`** (new). Net juice per calendar month is **derived**
  from the immutable `close_short` executions (same figure the theta ledger keys
  off) — never stored. The only thing persisted is the operator's payout
  bookkeeping: finalized/paid flags, timestamps, the **amounts snapshotted** at
  each step (frozen against later execution corrections), and an optional note.
  The finalizable signal reads the open `short_calls`' expirations (with an
  open_date+dte fallback for paper legs). `view()` returns the current-month
  estimate, the last month's payout, the month-by-month history, and roll-up
  totals (YTD / all-time / paid out / awaiting payout).
- **`PAYOUT_READY` alert** (`backend/alerts.py`). Fires once a month's payout can
  be finalized — its last short of the month has closed, or the month ended —
  with net income earned and not yet finalized: "July 2026 payout ready: $110.00
  net income — its last short of the month has closed." Scoped to the current +
  previous month so it reminds without spamming the back-history, auto-resolves
  when finalized, rides the existing notifier channels (Web Push / ntfy / email),
  and deep-links to the Payouts tab.
- **API**: `GET /api/payouts`, `POST /api/payouts/finalize`,
  `POST /api/payouts/unfinalize`, `POST /api/payouts/mark-paid`
  (`{month, amount?, note?}`; finalize/pay refuse a month still earning juice),
  `POST /api/payouts/unmark-paid`.
- **Frontend**: a new **Payouts** tab (`frontend/src/components/PayoutsTab.jsx`)
  with the est-this-month / last-month cards, totals, and a monthly history table
  with inline finalize / mark-paid / undo. App gains a `?tab=…` deep link so the
  payout push tap lands on the tab. The **Overview** landing shows a compact
  payout glance — this month's estimated payout + last month's — fed by a new
  `payouts` section on `/api/overview` (no extra call), linking to the tab.
- **Migration v15** seeds the additive `payouts` store; net juice stays derived,
  so no income data is copied. Covered end to end by `backend/test_payouts.py`.

## Genius four-light market regime (dwell + secondary indicators)

The market regime (**GREEN / YELLOW / RED**, Level 1 of the entry gate) is no
longer a single breadth + VIX rule. It is now the CFM course's **Genius System**:
four binary indicator "lights" on SPY daily bars, voted to a condition and held
against flapping by a **yellow dwell**. The traffic light is decided by the four
lights + the dwell **only**; breadth and VIX are kept as **secondary,
informational indicators** shown alongside the regime for the operator's own read
(they no longer change the light), and SPY's MA21 up/down trend is dropped
entirely.

### What changed

- **`backend/regime_genius.py`** (new, pure — no I/O, no clock; bars/timestamp/
  prior-series passed in). The four lights (each GREEN when bullish):
  1. close vs slow MA, 2. fast MA vs slow MA, 3. Parabolic SAR vs close,
  4. momentum (ROC) vs zero. **Vote** (`HARD_CFM_RULE`): ≥3 GREEN → GREEN, 2/2 →
  YELLOW, ≥3 RED → RED. Every intermediate is returned as a **decision trace**
  (each light + its values, the raw vote, the dwell state, the secondary
  breadth/VIX indicators, and the published regime).
- **New indicators** (`backend/indicators.py`): `ema`, `roc`, and a from-scratch
  **Wilder `parabolic_sar`** (no TA library) — unit-tested against a
  hand-computed fixture.
- **Yellow dwell** (`HARD_CFM_RULE`, `GENIUS_YELLOW_DWELL_DAYS = 3`): once the
  published regime turns YELLOW it holds YELLOW for a minimum of 3 **trading**
  days (the bar/record sequence, not calendar days) regardless of the raw vote —
  the course's anti-flap rule. Every day records both **`raw_condition`** (today's
  vote) and **`published_regime`** (after the dwell) so calibration sees both.
- **Secondary indicators**: breadth and VIX are **informational only** — they do
  **not** determine the traffic light. Each is reported with its value, a
  reference level (`BREADTH_CONFIRM_MIN_PCT`, `VIX_ELEVATED_THRESHOLD = 25`), and
  a confirming/diverging flag, purely as extra context the operator can weigh.
  (This replaces the earlier downgrade-only veto design per operator direction —
  breadth/VIX must not set the light.)
- **Published vs raw**: the entry gate (Level 1) and the regime-change alert
  consume only the **published** regime; raw four-light flaps never reach them.
- **Persistence** (`backend/regime_history.py`, `DATA_DIR/regime_history.json`):
  one full decision trace per trading day. This is **derived** telemetry
  (recomputable from cached SPY bars, like `iv_history.json`), so it is **not** an
  immutable execution and is **not** rebuilt by `recompute_derived`. Appended once
  per day by nightly maintenance and **backfillable** from cached parquet bars.
- **Entry-context snapshot** (`SNAPSHOT_SCHEMA_VERSION 1 → 2`): the regime section
  now carries the full four-light decision trace. **Additive** — older v1
  snapshots stay valid and still load.
- **Alerts**: a new deduped **`REGIME_CHANGE`** alert fires once per *published*
  transition (keyed on the from→to pair), never on raw flaps.
- **Calibration** (`backend/calibration.py`): `regime_series` /
  `regime_param_compare` / `regime_vs_cycles` recompute the historical raw-vote /
  published series under **alternative parameter sets** from cached bars, for
  offline comparison against realized cycle outcomes. Comparison-only — **no
  auto-tuning**.
- **Parameters are calibration-tunable defaults**: all four indicator parameter
  sets read from provenance-tagged `config.GENIUS_*`. The course fixes the
  indicator *types* and the vote/dwell logic (`HARD_CFM_RULE`); the parameters
  (MA lengths 50/21, SAR 0.02/0.20, ROC(10)) are `PROPOSED_DEFAULT`.
- **Frontend** (read-only): the Overview `RegimeHero` shows the four lights, the
  raw vote, the dwell status ("YELLOW — day 2 of 3 minimum"), and — neutrally, as
  secondary context — any diverging breadth / elevated VIX; the SPY stat is
  removed. The ribbon weather tooltip surfaces the raw vote and dwell day when
  they differ from the published regime.
- **Tests**: per-light units, the hand-computed SAR fixture, all 16 vote
  combinations, the dwell edge cases (hold-through-day-3, day-4 release, re-yellow
  inside the window, raw-crash held, cold start), that breadth/VIX are secondary
  (never change the light), and **labeled synthetic parquet regression fixtures**
  (`backend/fixtures/regime/`):
  a sustained confirmed-green hold, a distribution rollover degrading
  GREEN→YELLOW→RED in order, and a boundary whipsaw whose 1-day raw-green blip the
  dwell absorbs.

### Strike-policy regime wiring — audit finding (scoped follow-up)

The live roll ticket showing "**1×ATR, conservative**" in a YELLOW tape was **not**
a broken wiring: `strike_policy.suggest_strike()` already consumes the regime
status (now the dwell-adjusted **published** regime) and looks it up in
`config.STRIKE_TABLE`. The `1.0×ATR` figure is the literal `yellow`/`conservative`
cell. That table encodes a *shallower-when-safe → deeper-when-dangerous* scheme
(conservative green 0.5×, yellow 1.0×, red 1.5×) that predates — and contradicts —
the documented policy of **1.5× ATR in GREEN, 2.0× in YELLOW** (RED blocks entry).

The documented multiples are now present as `HARD_CFM_RULE` constants
(`STRIKE_ATR_MULT_GREEN = 1.5`, `STRIKE_ATR_MULT_YELLOW = 2.0`). Reconciling the
`STRIKE_TABLE` to them changes calibrated numbers for **both** postures and the
RED defend/roll-down rows, so it is deliberately left as a **separate, reviewable
change** rather than bundled into this regime work. No strike behaviour changed
here beyond the regime feeding it now being the published (dwell-adjusted) regime.

## Weekly theta burn & net-juice accounting

The per-position juice accounting no longer treats the LEAP's **total** entry
extrinsic as a cost to be paid off. The LEAP is held ~8 weeks and exited/rolled
around 130–140 DTE, so only the extrinsic **consumed during the hold window** is
a true cost — the rest is recovered when the LEAP is sold (minus slippage). The
headline per-position metric is now **net juice/week = juice collected/week −
theta burn/week**, and the entry queue ranks on it.

### What changed

- **`burn_projection()`** (new `backend/burn.py`) — the burn is the **difference of
  two Black-Scholes model prices**: the LEAP's model extrinsic at the current DTE
  minus its model extrinsic at the planned exit DTE (same spot & IV), divided by
  the weeks in that window. Never a straight-line proration of total extrinsic
  (`HARD_CFM_RULE BURN_IS_MODEL_DIFF`). Guard rails: auto-extends the window when
  a position is held past plan; floors burn at zero with a `low_extrinsic_flag`
  on deep-ITM drift; adds an explicit round-trip **exit-slippage** term.
- **`planned_exit_dte`** is now per-position state (default `PLANNED_EXIT_DTE = 135`),
  seeded onto existing positions by a forward-only migration (**schema v13 → v14**).
  All burn math keys off this, not off LEAP expiration.
- **Net juice is the headline** (`NET_JUICE_IS_HEADLINE`): `leap_health`, the
  portfolio income rollup (Overview), and the entry-queue ranking
  (`/api/scan/ready`, `queue_state`) all use net juice/week via one shared
  function — the queue and the position view can never disagree. This naturally
  penalizes high-IV candidates (more extrinsic bought → more burn) with no
  separate rule. The legacy `extrinsic_payback` meter is kept as a secondary
  capital-recovery view.
- **Weekly burn marks + divergence** (`backend/burn_marks.py`, telemetry in
  `DATA_DIR/burn_marks.json`, recorded by nightly maintenance at end-of-week):
  realized-vs-projected burn is queryable per position and book-wide — a live
  verification harness for the pricing model. Persistent divergence past
  `BURN_DIVERGENCE_WARN_PCT` surfaces a soft warning badge.
- **Frontend**: a per-position Theta-burn panel (Juice/wk · Burn/wk with a
  trend arrow · Net/wk), a coverage meter with threshold coloring, a weekly
  juice-vs-burn bar view (realized full-opacity, projected lighter), a
  hold-extension readout, and staleness/model-drift badges — all reusing existing
  Tailwind/flex-div primitives (no new chart library).

**Finding (documented in `IMPLEMENTATION_NOTES.md`):** for a real deep-ITM
0.90-delta LEAP the Black-Scholes extrinsic decay is **front-loaded**, so the
spec's "model burn < straight-line proration" and "extending the hold raises
burn/wk" assumptions (ATM-theta intuition) are inverted. The feature's actual
value prop — held-window burn ≈ ⅓ of total entry extrinsic — is confirmed and is
what the tests assert.

## Atomic spread roll orders (short-call roll)

The weekly short-call roll now completes the spec for **atomic** execution: a
live roll transmits ONE Schwab two-leg complex order (buy-to-close the old short
+ sell-to-open the new short) at a single NET_CREDIT / NET_DEBIT limit, so the
pair fills as a unit or not at all — no legging risk, one net crossing instead of
two. The atomic order construction, single `pending_orders` entry, and
per-leg-fill commit already existed; this change closes the remaining gaps.

### What changed

- **Feature flag** `ATOMIC_ROLLS_ENABLED` (default `True`). When off — or when the
  operator explicitly confirms after a rejection — the roll uses the legacy
  **legged** path (two independent single-leg orders, which carry legging risk).
  The legacy path is never a silent fallback.
- **`roll_group_id`** is stamped on both roll legs (equal to the ledger's
  `roll_id`), so a legged pair and an atomic pair are ledger-identical. A
  forward-only migration (schema v11 → v12) backfills it on historical roll
  executions.
- **Per-leg fill allocation is marked** on each execution (`roll_alloc_method`):
  `broker_per_leg` when Schwab reports per-leg fill prices, `proportional_to_mid`
  when it reports only a net (the net is split by the reference mids captured at
  ticket time), or `mid` for paper.
- **Partial fills** (multi-contract rolls) are booked as whole spread units; the
  remainder stays pending until it fills or cancels. All partials of one order
  share one `roll_group_id`.
- **Leg imbalance is a hard stop.** If Schwab ever reports a leg-imbalanced fill
  (one leg filled, the other not) at a terminal state, the position is **frozen**
  (`needs_review`) and a **CRITICAL `ROLL_LEG_IMBALANCE` alert** fires. No
  execution is written and nothing is auto-corrected (`ROLL_LEG_IMBALANCE_ACTION`).
- **Rejection surfaces a reason and an explicit legged-fallback offer** (behind a
  `confirm_leg_manually` confirmation) — never an automatic fallback.
- **Net roll slippage** is measured per roll (realized net vs the reference net
  mid) in `slippage.roll_report` and recorded per roll receipt in `fill_verify`.
- `ROLL_ORDER_DURATION` and `ROLL_COMPLEX_STRATEGY_TYPE` are now config constants
  (see below).

### Paper-economics shift (R4)

Paper fills are booked at the quoted **mid** and were never haircut on the
immutable ledger (the slippage haircut has always been a report-only caveat), so
this change does **not** alter booked paper roll prices. What it changes is the
**accounting model**: a paper roll is now treated as **one net crossing**
(`PAPER_ROLL_HAIRCUT_CROSSINGS = 1`) rather than the old illustrative two-per-leg
round-trip factor. Net roll slippage is reported as a single net figure per roll
instead of doubling a per-leg haircut. **Historical paper comparisons that relied
on the two-crossing round-trip figure will shift slightly** (roll economics look
marginally better under the single-net-crossing model). Booked ledger prices are
unchanged, so realized theta / payback / roll-ledger numbers do not move.

### Items requiring live verification (flagged, not assumed)

These depend on real Schwab behavior and are marked `LIVE_VERIFY` in the code /
audit. Confirm against a live account before production reliance:

1. **`complexOrderStrategyType` enum.** Defaults to `CUSTOM` (the safe superset
   for any strike/expiry call pair). Schwab also documents `DIAGONAL` (different
   expiry) / `VERTICAL` (same expiry); the exact enum its spread-approval logic
   wants is unverified. Configurable via `ROLL_COMPLEX_STRATEGY_TYPE`.
2. **Per-leg fill-price reporting.** The `broker_per_leg` allocation assumes
   Schwab populates per-leg `price` on a complex fill. When it doesn't, the code
   falls back to `proportional_to_mid` off the placement limit — verify which
   path real fills take.
3. **Partial-fill unit behavior.** Whole-spread-unit partial fills and the exact
   `filledQuantity` / per-leg `quantity` fields on a working complex order are
   assumed from the schema, not observed. Verify the partial-fill quantity
   reporting drives the imbalance/partial logic correctly.

### Config constants (provenance-tagged, see `backend/config.py`)

| Constant | Value | Provenance |
|---|---|---|
| `ATOMIC_ROLLS_ENABLED` | `True` | PROPOSED_DEFAULT — feature flag |
| `ROLL_ORDER_DURATION` | `"DAY"` | HARD_CFM_RULE — unfilled = canceled, no trace |
| `ROLL_NET_PRICE_SOURCE` | `"reference_net_mid"` | HARD_CFM_RULE — consistent with fill_verify |
| `ROLL_COMPLEX_STRATEGY_TYPE` | `"CUSTOM"` | PROPOSED_DEFAULT / LIVE_VERIFY |
| `ROLL_LEG_IMBALANCE_ACTION` | `"freeze"` | HARD_CFM_RULE — never auto-correct |
| `PAPER_ROLL_HAIRCUT_CROSSINGS` | `1` | PROPOSED_DEFAULT — single net crossing |

### Scope guard

LEAP-roll paths, the kill-switch, circuit-breaker, entry-gate, and strike-policy
logic are untouched. `state.json` changes are additive with a forward-only
migration. No new third-party dependencies.
