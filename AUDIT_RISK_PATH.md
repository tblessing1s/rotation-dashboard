# Audit: Risk-Path Math Hardening (v2.4.x)

Theme: the app's *accounting* math is honest; three places where a
bookkeeping-safe simplification leaks into a live **risk** decision (defend,
kill switch, assignment) are corrected, and three more flagged items get
verification tests. **No strategy rule, threshold, or trigger level changes —
only the inputs to them.**

All references are `file:line` at audit time.

---

## Phase 0 — Audit findings

### 1. `captured_pct` clamp — consumers and classification

Computed in `enrich_short` at **`position_manager.py:106-123`**:

```python
captured = (max(entry_extrinsic - current_extrinsic, 0.0)          # :112  FLOOR ≥ 0
            if entry_extrinsic is not None and current_extrinsic is not None else None)
captured_pct = (min(max(captured / entry_extrinsic * 100, 0.0), 100.0)   # :114  CLAMP [0,100]
                if captured is not None and entry_extrinsic else None)
```

The underwater case (`current_extrinsic > entry_extrinsic`, i.e. the leg moved
against you on a vol spike) is hidden by the **floor at :112**, which pins
`captured` — and therefore `extrinsic_captured_pct` — at `0`, never negative.
The `[0,100]` clamp at :114 only caps *over*-capture (benign).

**Consumers, classified:**

| Consumer | file:line | Class |
|---|---|---|
| producer (`enrich_short`) | `position_manager.py:106-123` | — |
| **payout / accounting** | *none* — real realized income runs through `net_juice_total` (`executor.py:1044-1061`, `payouts.py`), a separate path | (a) |
| position card "% captured" + Meter | `PositionTracker.jsx:367-392` | (b) risk/mgmt |
| aggregate short-capture health | `PositionTracker.jsx:407-417,492-529` | (b) risk/mgmt |
| JuiceStand glass fill | `JuiceStand.jsx:18-35,600-627` | (b) risk/mgmt |
| ProcessRibbon fill | `ProcessRibbon.jsx:442-456` | (b) risk/mgmt |
| Scorecard/History RS columns | (unrelated — RS, not capture) | — |

**Finding:** there is **no payout consumer** of `extrinsic_captured_pct`. Every
consumer is a risk/management view. So the clamp is purely a *display* decision
and the floor actively hides an underwater short leg at defend-decision time.
The clamped value is retained (an IV spike must never book as negative income if
a payout consumer is ever added — the [HARD-CFM-RULE] scope), and a signed raw
figure + flag are added alongside.

### 2. RS-vs-sector — formula and consumers

`indicators.rs3m(df, bench, lookback=RS3M_LOOKBACK)` (**`indicators.py:142-161`**)
is a **direct** ratio-momentum of `symbol/bench` over 63 trading days
(`RS3M_LOOKBACK=63`). It already accepts an arbitrary benchmark, but is only
ever called with `bench = SPY`.

`rs_vs_sector` is currently the **difference of two RS-vs-SPY figures**, computed
independently in three places:

- Kill switch: `kill_switch.py:41` → `rs_vs_spy − sector_rs_vs_spy`; consumed by
  both RED triggers and the yellow "thinning" band at `kill_switch.py:50-61`,
  and the coded exit reason `kill_switch.py:98-100`.
- Entry gate Level 3: `screening.py:269` (via `_stock_row`), gated at
  `screening.py:429-436`; drives row status (`:300`) and the filter sort (`:348`).
- Scorecard: `metrics/scorecard.py:211`; verdict at `:257-261`.
- Entry-context snapshot: reuses the gate's Level-3 row
  (`entry_context.py:266`) — no independent computation.
- UI: `Scorecard.jsx:16`, `HistoryTab.jsx:205`.

**Sector-ETF-as-own-position edge case** (`rs_vs_sector = None`) is guarded in
four places: `kill_switch.py:35-37`, `screening.py:265-268`,
`metrics/scorecard.py:390-392`, gate label `screening.py:427`.

**Cost of switching to direct `rs3m(stock, sector_etf)`: zero extra cache
reads.** Every scan already prefetches SPY + all sector ETFs + constituents and
materialises the sector frames (`screening.py:329,337,409`,
`scorecard.py:450,453`); the difference formula reads all three frames per
symbol, the direct form reads a strict subset (stock + sector). So **all**
consumers switch to direct — no approx/direct labelling split is needed.

### 3. Dividend yield (q) plumbing

The BSM engine (`indicators.py:308-464`) threads `q` through every
pricing/greeks/IV function; each defaults `q=0.0`. Dividend data path exists:
`dividends.yield_for(ticker)` (**`dividends.py:93-116`**) returns a cached,
override-aware continuous yield (decimal), `0.0` when unknown, **offline-safe**
(providers gated on `configured()` → no network in tests). Ex-div events:
`dividends.next_dividend` (`dividends.py:192-219`).

**q call-site classification:**

| Call site | file:line | Class |
|---|---|---|
| option-chain greeks | `option_chain.py:342,355-361` | (a) q passed |
| maintenance burn-mark sweep | `maintenance.py:96-100` | (a) q passed |
| `account_gate._leap_strike_for_delta` (offline juice estimate) | `account_gate.py:34-40,76-83` | (b) irrelevant (estimate, q≈cancels) |
| `leap_policy.juice_estimate` / roll preview | `leap_policy.py:76,83,288,297`; `executor.py:1982` | (b) irrelevant (symmetric preview) |
| `indicators.calculate_extrinsic` | `indicators.py:474-496` | (b) N/A (no model) |
| **delta-coverage guardrail** | `alerts.py:173,191` (`check_delta_uncovered`) | **(c) biases risk** |
| **`portfolio_risk._leg_greeks`** | `portfolio_risk.py:30-38` (no q param at all) | **(c) biases risk** |
| **`leap_policy.leap_health` production callers** | `alerts.py:521,553,591,625`; `position_manager.py:284,286` | **(c) biases risk** (roll timing) |
| dividend-assignment trigger extrinsic | `alerts.py:409-410`; `position_manager.py:166` | see note |

**Note on the assignment trigger (the prompt's R3(a)):** the audit finds this
extrinsic is computed from the **live quote** (`current_bid − intrinsic`), *not*
from a BSM model — so it is **q-independent as written, and already q-aware
implicitly via the market** (the market prices the coming dividend into the bid).
The real gap is that the path goes **silent** when `current_bid is None`
(`alerts.py:407` → `continue`) — exactly the off-hours-before-ex-div window where
a dividend-capture assignment is most likely. R3(a) is therefore implemented as a
**q-aware model-extrinsic fallback** (exogenous σ from `hist_vol`, correct q) used
*only* when no live quote exists, `q_source` logged. When a quote exists it is
used unchanged. This is additive, testable, and does not degrade the quote path.

### 4. Day-count conventions — worked example

- **θ**: `call_greeks_full` returns `theta_year / 365.0` = **theta per calendar
  day** (`indicators.py:420`). Deliberate engine choice — unchanged.
- **burn/wk**: 7 **calendar** days everywhere. `leap_weekly_burn = −θ_day × 7`
  (`indicators.py:440`); `burn_projection` divides the model-difference by
  `weeks = (current_dte − exit_dte)/7` (`burn.py:166-172`), calendar DTEs.
- **juice/wk (realized, the headline)**: mean realized `net_juice` per **ISO
  calendar week** (`logging_handler.py:622-628`, weeks bucketed by
  `datetime.isocalendar()`). ≈ 7 calendar days.
- `net_juice_per_week` (`burn.py:216-223`) subtracts the two; the live position
  path (`leap_policy.py:153`) feeds it realized juice/wk and model burn/wk.

**Worked example** (fixture `test_burn.py:25` — deep-ITM ~0.90Δ LEAP, spot 100,
strike 79, IV 30%, current 195 DTE, planned exit 135 DTE):

| Quantity | Value | Arithmetic |
|---|---|---|
| extrinsic_now (195 DTE) | $281.01 | BS call − intrinsic, T=195/365 |
| extrinsic_at_exit (135 DTE) | $178.45 | BS call − intrinsic, T=135/365 |
| projected_burn_total | $102.56 | 281.01 − 178.45 |
| weeks_remaining | 8.5714 | (195−135)/7 ← calendar/7 |
| projected_burn_per_week | $11.97 | 102.56 / 8.5714 |
| `leap_weekly_burn` (θ×7) | $11.98/wk | −θ_day × 7 × 100 ← ×7 calendar |

The two independent burn routes agree ($11.97 ≈ $11.98/wk), both on the **7
calendar-day** base — same base as realized juice/wk. **Consistent** → pin with a
permanent test (R4). The one asymmetry (documented, *not* changed to avoid
altering entry-ranking calibration): the *candidate estimate* path prices the
weekly short at a 5-day tenor (`account_gate.py:75`, `t_week = 5/365`) while burn
is 7-day — a modelling choice in the entry-queue estimate, not in the realized
headline, and not a live risk decision.

### 5. Payback state machine — transitions and coverage

Lives in `recompute_derived` (**`logging_handler.py:366-401`**); fully derived by
replaying `executions` into `cycle_collected` / `cycle_target` /
`_pending_close_roll`. Transitions:

| Transition | replay | stamp |
|---|---|---|
| new cycle | `:383-385` (else) | `executor.py:854-866` |
| multi-tranche add | `:377-382` (`leap_add` in merge/add) | `executor.py:868-877` |
| LEAP roll (`leap_roll_id`) | `:389-392` latch + `:372-376` consume | `executor.py:1820-1868` |
| defensive short roll | via `close_short` net juice `:387-388` (no dedicated branch) | `roll_short` |
| true exit (`legs_remaining→0`) | `:398-401` | `executor.py:1006` |

**Gaps (no validation today — silent plausible-but-wrong):**
- **Dangling roll**: a `close_leap` with `leap_roll_id` and no following matching
  `buy_leap` leaves `_pending_close_roll[t]` set and never clears
  `cycle_collected/target` → phantom carry forever. Undetected.
- **`legs_remaining` inconsistency**: stamped value (`executor.py:1006`) is
  trusted with no cross-check vs execution history; wrong value → premature true
  exit or a cycle that never ends. No reconciliation.
- **Missing `leap_roll_id`**: `.get()` truthiness only, no validation.

Existing coverage: roll continuity (`test_leap_lifecycle.py:106-125`), true-exit
reset (`:128-144`), merge/add/partial (`test_multi_leap.py:48-115`), defensive
roll juice (`test_cfm.py:864-889`). **Not covered:** dangling roll, mis-stamped
`legs_remaining`, missing-id validation, end-to-end executor-roll → meter.

### 6. SAR causality

`parabolic_sar` (**`indicators.py:218-270`**) seeds from bars 0–1 of the passed
frame and recurses forward (AF 0.02→0.20). It is **forward-causal**:
`parabolic_sar(df)[i] == parabolic_sar(df.iloc[:i+1])[-1]` **exactly**, provided
both share bar 0. The backfill (`regime_history.py:184-192`) uses prefix
truncation `spy.iloc[:i+1]` of one frame → all prefixes share bar 0 → **within a
run the backfill is internally consistent** (this is the property to pin).

The latent risk (documented, boundary-tested): SAR's seed depends on the *first
two bars of the passed window*, so a **shifted start** (different bar 0 — e.g. the
rolling `HISTORY_DAYS=320` cache dropping old bars over time) yields a different
SAR for early dates. The determinism guarantee is a property of the caller's
*prefix-from-earliest-bar* discipline, not of SAR alone. The fix per spec is to
keep the backfill a full-history recompute from the earliest cached bar (it
already is) and **pin the prefix-determinism invariant with a property test** so
it can never silently break. No functional change is required (verify item).

---

## Implementation plan

| R | Change | Snapshot bump? | Migration? |
|---|---|---|---|
| R1 | `enrich_short`: add `extrinsic_captured_pct_raw` (signed) + `extrinsic_above_entry`. Frontend surfaces raw+flag when above entry. New LOW alert `EXTRINSIC_ABOVE_ENTRY`. | no | no |
| R2 | Switch **all** RS-vs-sector consumers to direct `rs3m(stock, sector_etf)` (kill switch, gate L3, scorecard). Preserve None edge case. Add `rs3m_vs_sector_method:"direct"` to snapshot. | **yes → 3** | no (additive snapshot field) |
| R3 | Wire q into delta-coverage (`alerts.py`), `portfolio_risk._leg_greeks`, and `leap_health` callers; add q-aware model-extrinsic fallback for the assignment escalation when no quote; `q_source` logged via new `dividends.q_with_source`. | no | no |
| R4 | Verify (consistent) → permanent worked-example test; document convention on `net_juice_per_week`. | no | no |
| R5 | Full-cycle payback fixture asserting state at every transition; add `logging_handler.validate_payback` (loud) + wire into recompute (flag, never raises into exec path); mutation negative tests. | no | no |
| R6 | SAR + four-light prefix-determinism property test over 260-bar fixture; shifted-start divergence boundary test; make backfill's earliest-bar anchoring explicit. | no | no |

RS variant is **direct** everywhere → no silent mixing; the snapshot records the
method for provenance. Snapshot schema `2 → 3` (additive field only; v1/v2
snapshots stay valid). No state.json schema change, no migration.
