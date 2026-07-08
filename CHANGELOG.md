# Changelog

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
