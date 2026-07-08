# IMPLEMENTATION_NOTES.md — Weekly Theta Burn & Net Juice Accounting

Companion to `AUDIT.md` (Phase 0). Records what shipped, the assumptions made,
which `PROPOSED_DEFAULT` constants most need calibration once realized-burn data
accumulates, and the Phase 0 / mid-build findings that altered the plan. Scope
was **accounting and display only** — no order/roll/exit path was touched, no
second pricer was built, and no external Python dependency was added.

## Test status

`backend/` suite: **545 passed** (509 pre-existing + 36 new/adjusted), fully
offline — mocked clock and mocked pricing inputs throughout, no live API calls,
no `time.sleep`, no wall-clock in tests. Frontend builds clean (`vite build`).
Run: `cd backend && python -m pytest -q`.

New test files: `test_burn.py` (24 pure-function cases), `test_burn_marks.py`
(10 telemetry/divergence cases). Adjusted: `test_leap_lifecycle.py` (v14
migration), `test_exit_reasons.py` (CURRENT_VERSION no longer hard-coded to 13),
`test_account_gate.py` (net-juice fields).

---

## What changed

| Module | Role | Purity |
|---|---|---|
| `backend/burn.py` (new) | `burn_projection` (two-point model burn), `extension_cost`, `net_juice_per_week`, `coverage`, `candidate_net_juice` (queue metric). | **Pure** — plain values in, deterministic out; no I/O, no clock reads. Built only on `indicators._bs_call_price`. |
| `backend/burn_marks.py` (new) | Weekly mark telemetry in `DATA_DIR/burn_marks.json`: `record_mark`, `series`, `weekly_due` (cadence gate), `divergence`, `aggregate_divergence`. | Impure (one small JSON file, like `iv_history`); never touches state.json. |
| `backend/config.py` | New `# Weekly theta burn & net juice` constant block, each tagged `HARD_CFM_RULE` / `PROPOSED_DEFAULT`. | — |
| `backend/migrations.py` | `_v13_to_v14`: seeds `planned_exit_dte` default onto every position; `CURRENT_VERSION` 13 → 14. | Additive. |
| `backend/executor.py` | New positions' shell carries `planned_exit_dte`. | — |
| `backend/leap_policy.py` | `leap_health` now computes the model burn via `burn_projection` (keyed off `planned_exit_dte`, trailing realized vol) and exposes `net_juice_per_week`, `coverage`, `extension_preview`, `model_burn_per_week`, `burn_projection`, `planned_exit_dte`. Legacy `leap_weekly_burn` / `net_weekly_maintenance` kept (coexist). | Impure (reads bars), no state writes. |
| `backend/account_gate.py` | `juice_estimate` adds `net_weekly_yield_pct` / `burn_weekly_per_share` / `net_weekly_extrinsic_per_share` via `burn.candidate_net_juice`. | — |
| `backend/metrics/scorecard.py` | Row gains `net_juice_weekly_pct` / `burn_weekly_per_share`. | — |
| `backend/maintenance.py` | `snapshot_burn_marks`; `nightly_refresh` records a weekly mark when `burn_marks.weekly_due()`. | Impure, best-effort. |
| `backend/position_manager.py` | `net_juice_rollup` (sums net across open positions). | — |
| `backend/app.py` | `/api/burn/<ticker>` (panel detail + weekly series); overview payload gains `theta.net_juice_rollup` and `burn_divergence`; `/api/scan/ready` ranks on net. | — |
| `backend/queue_state.py` | Ranked queue sorts on net juice (falls back to gross when net is unavailable). | Pure adapter. |
| Frontend | `BurnPanel.jsx` (new: three metric cards + coverage meter + weekly bars + extension readout + stale/drift badges), wired into `PositionTracker`; Overview net-juice rollup + drift badge; `api.burn`. | Reuses existing primitives; no new library. |

### Single source of truth (spec §6)

The queue metric (`account_gate.juice_estimate` → `candidate_net_juice`) and the
live position view (`leap_policy.leap_health`) both compute net juice as
`burn.net_juice_per_week(gross, burn.burn_projection(...))`. The formula is not
forked — `test_burn.py::test_queue_and_position_view_agree_for_identical_inputs`
asserts identical inputs yield an identical net figure.

### Decisions confirmed with the operator before building

- **Coexistence, not removal** — the legacy `extrinsic_payback` meter /
  `theta_ledger.extrinsic_summary` income-hurdle stays as a secondary
  capital-recovery view; net juice is layered on as the new headline. Keeps the
  execution-derived accounting and its tests intact.
- **Marks live in `DATA_DIR/burn_marks.json`** (telemetry), not in the
  append-only execution record — mirroring `iv_history`. The `planned_exit_dte`
  field itself *is* position state and goes in state.json via the v14 migration.

---

## Findings that altered the plan

### 1. Deep-ITM LEAP extrinsic decay is FRONT-loaded — two spec assumptions are inverted

Pricing a realistic 0.90-delta LEAP (S=100, K=79, IV=30%) through the app's own
Black-Scholes engine shows extrinsic decaying **faster early, slower late** — the
inverse of at-the-money theta:

| DTE | 190 | 135 | 90 | 30 |
|----|----|----|----|----|
| extrinsic/sh | 2.72 | 1.78 | 1.05 | 0.27 |
| decay rate | — | 0.0171/day | 0.0164/day | 0.0130/day |

Consequences, confirmed numerically and adjudicated with the operator
("assert the true invariant; document it"):

- **Spec test case 1 ("model burn < straight-line proration") is false** for a real
  LEAP. The tests instead assert the *true* invariant, which is the feature's
  actual value prop: **held-window burn (190→135) ≈ ⅓ of total entry extrinsic**
  (0.94/sh vs 2.72/sh) — i.e. the weekly hurdle is roughly one-third of what
  total-extrinsic accounting implied (spec point #1, confirmed). Plus the exact
  two-point identity `burn == extrinsic_now − extrinsic_at_exit`.
- **Spec §5 "extending the hold *raises* burn/wk" is inverted.** 135 DTE is
  genuinely a flat region — model burn/wk barely moves across an 8-week extension
  and, if anything, eases toward expiry. `extension_cost` reports the honest
  number; the with-slippage figure falls as the fixed round-trip slippage
  amortizes over more weeks. The UI shows the actual numbers ("$Y/wk over +N wk
  vs $X now") rather than claiming a direction the instrument doesn't support.
  The real anti-zombie risk for a deep-ITM LEAP is delta saturation / the roll
  floor, already owned by the delta-velocity warning and `LEAP_ROLL_DTE_FLOOR`.

This same mismatch appears in the existing codebase's prose (e.g. the
`LEAP_ROLL_DTE_FLOOR` comment "theta steepens under ~90 DTE") — ATM intuition
applied to a deep-ITM instrument. The **realized-vs-projected divergence harness**
(`burn_marks`) is precisely the tool to keep this honest against live data.

### 2. No weekly job existed — hooked into the nightly tick with a weekly gate

The audit found only a nightly maintenance run (single in-process writer, one Fly
volume). The weekly mark job runs inside that tick, gated by
`burn_marks.weekly_due()` (fires once per ISO week, end-of-week, with a weekend
catch-up) — it does not spawn a second scheduler.

### 3. IV basis for the LEAP burn = trailing realized vol

`burn_projection` takes IV as a plain value; the callers resolve it. For the LEAP
leg everywhere (live panel, weekly mark, queue candidate) the basis is the
ticker's trailing realized vol (`indicators.hist_vol`) — the same offline BS basis
`juice_estimate` and `roll_cost_estimate` already use, and far more stable than
implying a deep-ITM call's IV from its own near-intrinsic mark (the reason the
put-IV substitution exists on the chain path). Using one basis everywhere keeps
the queue, panel, and marks mutually consistent; the divergence harness is what
reveals if that basis systematically mis-estimates realized decay.

---

## `PROPOSED_DEFAULT` constants — calibration priority

Ranked by how much a wrong value distorts behavior, to be tuned once
realized-burn marks accumulate:

1. **`PLANNED_EXIT_DTE = 135`** — the single biggest lever. Every burn figure,
   the net-juice headline, and the queue ranking key off it. Calibrate to the
   book's *actual* median exit DTE from closed cycles.
2. **`LEAP_ENTRY_DTE_DEFAULT = 190`** — sets the hypothetical entry window for the
   queue metric; with (1) it fixes the ranking's burn term. Calibrate to actual
   median entry DTE.
3. **`LEAP_SLIPPAGE_PCT_FALLBACK = 0.5`** (% of LEAP price) — used whenever no
   fresh chain spread is cached, which is most of the time in bulk screening. The
   coverage ratio and net juice both include it. Calibrate from realized LEAP
   exit fills vs mid.
4. **`BURN_DIVERGENCE_WARN_PCT = 25`** — needs several weeks of marks before it
   means anything; expect to widen or tighten once the realized series exists.
5. **`COVERAGE_HEALTHY = 3.0` / `COVERAGE_MARGINAL = 2.0`** — reasonable starting
   thresholds; revisit against the distribution of live coverage ratios.
6. **`BURN_LOW_EXTRINSIC_FLOOR = 0.10`/sh** and **`COVERAGE_DISPLAY_CAP = 10.0`** —
   guard-rail cosmetics; unlikely to need tuning.
7. **`EXTENSION_STEP_WEEKS = 1`** — display granularity only.

`BURN_IS_MODEL_DIFF` and `NET_JUICE_IS_HEADLINE` are `HARD_CFM_RULE` — not knobs.

---

## Explicitly NOT done (per scope)

- No order/roll/exit path touched; no live-trading code changed.
- No second options pricer — everything is `indicators._bs_call_price`.
- No straight-line proration anywhere, including display paths.
- The legacy `extrinsic_payback` accounting is retained, not removed.
- No new frontend chart library; the weekly bars reuse the flex-div idiom.
- No external Python dependency added.
