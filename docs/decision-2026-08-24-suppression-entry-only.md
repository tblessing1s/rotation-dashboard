# Decision: suppression governs the entry universe only

Date: 2026-08-24 · `TRAVIS_EXTENSION` · Status: **in force**

## The invariant

> Suitability suppression governs the **ENTRY UNIVERSE** only. Open positions are
> monitored, defended, killed, and reconciled at full cadence regardless of tier.

A name hidden from the scan is a name we will not *enter*. It is not a name we
stop *watching*. Those two are easy to conflate in code and catastrophic to
conflate in production: a suppressed name holding an open position that stopped
being kill-switch-evaluated would be an undefended position that looks fine
because nothing is looking at it.

## Why it holds today

Not by convention — structurally. Every position-management path derives its
working set from `state["positions"]`:

| Path | Derivation |
|---|---|
| Kill switch | `kill_switch.evaluate_all` — `state["positions"]` |
| Defend / roll | `recommendation_runner.build_market_snapshot` — `state["positions"]` |
| Reconciliation | `reconcile.expected_view_from_state` + the broker's own list |
| Assignment handling | `position_manager` — `state["positions"]` |
| Portfolio risk / reserves | `portfolio_risk.portfolio_view` — `state["positions"]` |
| Order lifecycle | `state["pending_orders"]` / `order_events` |
| Accrual | the executions ledger |
| Intraday bar refresh | `refresh_policy.hot_tickers` **Tier 1 = open positions, never truncated** |

None of them imports the scan machinery at all. Suppression is applied at exactly
one function, `metrics.scorecard.split_by_suitability`, called from three
entry-facing surfaces only: `/api/scan/scorecard`, `/api/scan/ready`, and
`recommendation_runner._entry_candidates`.

## How it is enforced against future changes

Three tests in `backend/test_suitability_tiers.py`, all of which fail loudly
rather than quietly:

1. `test_no_position_management_path_reads_a_tier` — an AST walk asserting no
   module outside the declared allow-list imports `suitability_tiers`. Verified
   non-vacuous by injecting a synthetic leak into `kill_switch` and confirming
   the test catches it.
2. `test_position_management_paths_derive_from_positions_not_scan_rows` — an AST
   walk asserting the seven position modules import no scan machinery
   (`scorecard`, `scan_cache`, `queue_state`).
3. `test_open_position_paths_are_byte_identical_under_suppression` — a fixture
   with an open position in a `SUPPRESSED_STRUCTURAL` name, enforcement ON,
   asserting kill-switch, defend/roll, portfolio-risk and reconciliation output
   is byte-identical to the same fixture with suppression disabled. It guards
   against a vacuous pass by first asserting each path produces real output.

## What would violate it

- Deriving any position path's working set from scan rows, the scan cache, or
  the entry queue — "the names we're watching" must never mean "the names in the
  scan".
- Calling `split_by_suitability` from anywhere that is not entry-facing.
- Adding a tier check inside `scan_triggers.is_bench`, the entry gate, the kill
  switch, or the verdict composition. Bench-ineligibility is applied at the
  visibility choke point precisely so `is_bench` stays a pure fold over the gate.
- Letting a recheck cadence skip evaluation for names with open positions.

## Related decision: recheck dates do not gate evaluation

The tier spec called for suppressed names to skip full evaluation until their
`next_recheck_date`. That was **not** implemented, and the dates are computed,
persisted and displayed only. Three reasons, recorded here because the omission
looks like an oversight otherwise:

1. **Cache coherence.** `scan_cache.store` replaces the day's sweep wholesale and
   `reusable` computes `missing` from absent rows. A skipped name has no row, so
   it is permanently missing and the incremental path recomputes it on every
   request — the skip costs more than it saves.
2. **It starves its own input.** Suppressed names would stop emitting capacity
   observations. A STRUCTURAL name sampled every 30 days needs ~20 months to
   reach `CAPACITY_MIN_OBS`; if it ages out of the retention window first it
   flips to INSUFFICIENT_HISTORY, which is unsuppressible — the scheme
   un-suppresses everything it was meant to hide.
3. **Stale readmission tests.** Readmission compares `current` against the floor.
   Under monthly sampling that reading is up to 30 days old.

Full analysis: `AUDIT_SUITABILITY_SUPPRESSION_PHASE0.md` §4.
