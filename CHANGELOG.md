# Changelog

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
