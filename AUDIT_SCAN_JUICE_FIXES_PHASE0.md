# Phase 0 Audit — Scan Fixes: Juice Economics, WATCH/BENCH Split, IVR

**Scope:** the live pipeline scan exposed one real defect (juice economics never
reach the gate) and several smaller ones (economics-blind SCORE, fabricated `~1D`,
WATCH≡BENCH, blank IVR, `-0%` rendering). **Audit only — no code changed.** Every
citation is `file:line` against branch `claude/scan-pipeline-triggers-ciwizz` at
its current head. All new constants proposed below are `PROPOSED_DEFAULT`; existing
tagged constants are cited before any new one is proposed. Hard stop after this doc.

---

## TL;DR — five root causes, all located

1. **Juice adequacy never enters the canonical (table) verdict.** `score_ticker`
   folds only L2/L3/L4 blocks — it calls `gate_blocks(gate, ext_context=…)` with
   **no `account_gate`** (`metrics/scorecard.py:520`). L5 (incl. juice) is layered
   only in `/api/scan/ready`. So the Scorecard bench is computed with zero juice
   awareness — PNC/XYZ/HUM/EA/AES slip on. **P1 defect.**
2. **Even where juice is evaluated it's misclassified.** `juice_adequacy` is
   `CONDITIONAL` (`scan_triggers.py:91`) → a *clearable* WATCH block, not a safety
   BLOCK. And the L5 check judges **gross** yield, not the **net** figure the table
   shows. **P1.**
3. **`~1D` is fabricated, and it is NOT juice.** The extension ESTIMATED trigger's
   `int(round(days)) or 1` (`scan_triggers.py:195`) turns any sub-half-day estimate
   into `1`. Refutes the juice-leak hypothesis (juice isn't in the table path). **P1.**
4. **WATCH≡BENCH because a signal-WATCH is benchable.** `structure:WATCH`
   (BASING×EARLY_INTEREST intake) is classified `CONDITIONAL`
   (`scan_triggers.py:72`), so `is_bench` (`scan_triggers.py:328-336`) returns True
   for every WATCH row. **P2.**
5. **IVR is blank** because the scan universe has no IV history
   (`iv_history` covers held/viewed names only) and the scan-time BSM path uses
   **realized** vol, not implied. HVR-from-bars is the viable fix. **P2.**

---

## Q1 — Juice in the gate

**Where L5 juice evaluates today.** `account_gate.evaluate` builds a blocking
`juice_adequacy` check (`account_gate.py:327-334`): `weekly_yield >= target`, where
`weekly_yield = weekly_extr / leap_cost * 100` (`account_gate.py:323`) — the
**GROSS** weekly yield — and `target = weekly_yield_target_pct(ticker)`
(`account_gate.py:149-156`).

**Does the canonical verdict consume it? No.** The table verdict is composed in
`score_ticker` from `gate_blocks(gate, ext_context=ext_context)`
(`metrics/scorecard.py:520`) — **`account_gate` is not passed**, so no L5 check
(juice included) ever enters `row["verdict"]` / `row["bench"]`
(`metrics/scorecard.py:522,528`). L5 is folded only in the `/api/scan/ready`
overlay (`gate_blocks(None, account_gate=l5)` in `app.py`). **This is how the bench
admitted sub-floor rows: the table never checks juice at all.**

**Second-order gap:** even the L5 check that *does* exist judges **gross** yield
(`account_gate.py:323`), while the defect (EA/AES `-0%`) is about **net** juice —
`net_juice_weekly_pct`, gross minus LEAP burn (`burn.py:313`, surfaced on the row at
`metrics/scorecard.py:443`). A name with positive gross but negative net (burn >
income) passes `juice_adequacy` today.

**Existing juice constants (reuse-first).**
- `weekly_yield_target_pct` (`account_gate.py:149-156`) = `CYCLE_RETURN_MIN /
  CYCLE_WEEKS_MAX * 100` ≈ **1.875 %/wk**, from `CYCLE_RETURN_MIN=0.15` /
  `CYCLE_WEEKS_MAX=8` (`config.py:941,944`, **HARD_CFM_RULE**); ETFs use
  `ETF_WEEKLY_JUICE_TARGET_PCT` (`config.py`). These are **gross** targets on the
  gross-yield check.
- No existing **net** juice-floor constant. So the P1 floor is a genuinely new
  `PROPOSED_DEFAULT` (`JUICE_FLOOR_WK`, initial 1.5 %/wk **net**), distinct from the
  gross target above — cited, not silently duplicated. The hard floor (`net ≤ 0`)
  needs no constant.

**Fix (P1-1a):** add a **net** juice-floor **safety** block in `score_ticker`,
computed purely from `row["net_juice_weekly_pct"]` (already on the row — no account
state, so it fits the memoized market sweep):
- `net_juice_per_week ≤ 0` ⇒ BLOCKED (hard floor).
- `net_juice_per_week < JUICE_FLOOR_WK` ⇒ BLOCKED, binding `L5 juice: X% < floor Y%`.
- ETFs pass through the identical path (the floor self-eliminates low-IV ETFs; no
  ETF branch). Also reclassify `juice_adequacy` → `SAFETY` in `scan_triggers._KIND`
  (`scan_triggers.py:91`) for the `/api/scan/ready` overlay.

---

## Q2 — Block classification table (as shipped)

From `scan_triggers._KIND` (`scan_triggers.py:68-92`):

| Level | Check id | Shipped kind | Correct? |
|---|---|---|---|
| signal | `regime` (BLOCKED/WATCH/CAUTION) | SAFETY / CONDITIONAL / CONDITIONAL | ok |
| signal | `symbol` (…) | SAFETY / CONDITIONAL / CONDITIONAL | ok |
| signal | **`structure` : WATCH** | **CONDITIONAL** | **❌ makes BASING intake benchable (Q5)** |
| L2 | `rs1m_negative` / `breadth_collapsing` | CONDITIONAL | ok |
| L2 | `under_distribution` | SAFETY | ok |
| L3 | `veto:rs3m_vs_sector` | CONDITIONAL | ok |
| L3 | `veto:atr_expanding_high_ivr` | CONDITIONAL | ok |
| L3 | `veto:close_below_ma200` | SAFETY | ok |
| L4 | `atr_pct` | CONDITIONAL | ok |
| L4 | `atr_5d_ema` / `extension` | ESTIMATED | ok (but see Q3 fabrication) |
| L5 | `earnings_in_cycle` | CALENDAR | ok |
| L5 | `sector_concentration` / `position_limit` / `capital_limit` / `cash_reserve` | CONDITIONAL | ok |
| L5 | **`juice_adequacy`** | **CONDITIONAL** | **❌ must be SAFETY (Q1) — low IV doesn't clear on a date** |

**Two misclassifications:** `juice_adequacy` (→ SAFETY) and `structure:WATCH`
(→ not benchable; see Q5). No other structural check is benchable — the L4/L5
conditionals genuinely clear by waiting (a pullback, a sector slot, an earnings
date).

---

## Q3 — `~1D` forensics

**Mechanism.** The only trigger that emits a day count in the table is the L4
`extension` ESTIMATED trigger. `_estimate_days` (`scan_triggers.py:177-195`)
computes `days = (excess_atr × atr) / ma21_rise_per_day`, then
**`return int(round(days)) or 1`** (`scan_triggers.py:195`). Any estimate that
rounds to `0` (a name barely over the 1.5-ATR extension line, or MA21 rising fast)
becomes `1` — a fabricated `~1D`.

**Arithmetic (representative).** `excess_atr = 0.03`, `atr = 2.0`,
`ma21_rise_per_day = 1.5` → `days = 0.04` → `round → 0` → `or 1` → **`~1D`**. This
fires for many unrelated rows precisely because "just barely extended" is common on
a green tape.

**The other L4 estimate never fires a count.** `atr_5d_ema` needs
`contraction_per_day`, which `_ext_trigger_context` never supplies (it sets only
`momentum_excess` — `metrics/scorecard.py` `_ext_trigger_context`), so
`_estimate_days` returns `None` for it (`scan_triggers.py:187-189`). The `~1D`
population is therefore **entirely `extension`**.

**Root cause refutes the juice hypothesis.** Juice is not in the table path (Q1), so
`~1D` cannot be juice leaking through. Reclassifying juice (1a) will **not** remove
`~1D`; it removes the *rows* (they go BLOCKED) only where juice binds — a genuinely
benched, juice-adequate, slightly-extended name still shows a bad `~1D` until the
guard lands.

**Fix (P1-1c), regardless of root cause.** A minimum-information guard in
`_estimate_days`: if inputs are degenerate (`excess ≤ 0`, `rate ≤ 0`, missing
series, or `round(days) < 1`), return `None` so the trigger renders as its
**condition word** (`_CLEARS`, e.g. "pull back within 1 ATR of MA21"), never a
fabricated count. Drop the `or 1`.

---

## Q4 — SCORE composition

`scan_score.compute_score` (`scan_score.py:132-157`). Net juice is **already an
additive** component: weight `W_NET_JUICE = 1.5` (`scan_score.py:35`), sub-score
`_juice_sub` scaling to `JUICE_FULL = 3.8` (`scan_score.py:74,125-129`). The
weighted sum returns at `scan_score.py:156`. Consumed at `metrics/scorecard.py:557-563`
(`row["score"]`); the bench sort tie-breaks on it (`Scorecard.jsx:403-411`
`sortBench`), as does the verdict-tier sort (`Scorecard.jsx` `sortRows`).

**Insertion point (P1-1b).** Wrap the returned quality score with a
**multiplicative viability factor** at `scan_score.py:156`:
`score_final = score_quality × min(net_juice_wk / JUICE_TARGET_WK, 1.0)`, clamped to
0 for negative net juice. `JUICE_TARGET_WK` = new `PROPOSED_DEFAULT` (1.5 %/wk),
distinct from `JUICE_FULL` (the additive sub-score's scale). **Authority unchanged**
— SCORE stays shadow (`scan_score.py:9-16`, "ZERO AUTHORITY"); the factor changes
the value, not the role.

**Acceptance property (test over Fixture E + an XBI-like fixture):** PNC
(0.12 %/wk → factor 0.08) must **not** outscore XBI (0.33 %/wk → factor 0.22):
`8.3 × 0.08 = 0.66 < 8.2 × 0.22 = 1.80`.

---

## Q5 — WATCH/BENCH merge point

**Where derived.** `row["verdict"]` and `row["bench"]` at `metrics/scorecard.py:522,528`;
bench from `scan_triggers.is_bench(verdict, triggers)` (`scan_triggers.py:328-336`).

**Why they're coextensive.** `is_bench` returns True for any non-READY row whose
triggers are all non-safety (`scan_triggers.py:336`). A BASING×EARLY_INTEREST
intake maps to structure entrability **WATCH** (`structure_classifier.py:406`) →
composed verdict WATCH → a `structure:WATCH` **signal** block → classified
`CONDITIONAL` (`scan_triggers.py:72`) → non-safety → **bench = True**. Since WATCH is
the only degrade level for every non-safety block (`_BLOCK_SEVERITY`,
`scan_triggers.py:274`), **every WATCH row becomes bench** → the 26/26 collapse.

**Every consumer of WATCH / bench (all must respect the split):**
- Backend: verdict/bench assign `metrics/scorecard.py:522,528`; persisted
  `bench` in the rejection log `scan_rejection_log.py:92` (feeds the diff).
- Frontend `Scorecard.jsx`: `counts` fold `:479-486` (WATCH tier + separate BENCH);
  pipeline throughput `:490-494` (`bench`/`le14`/`beyond`); BENCH filter predicate
  `:503`; `sortBench` `:403-411`; verdict + bench tag render `:120-127`; BENCH
  filter chip `:541-553`.
- Diff: `scan_diff.diff_symbol` reads `was_bench` `scan_diff.py:53`.

**Corrected derivation (P2-2a).** BENCH = structure-**entrable** AND blocked only by
**clearable GATE** blocks (L2/L4/L5 calendar/conditional/estimated), no safety. A
**signal-level** WATCH (regime yellow, SYM yellow, or `structure:WATCH`/BASING) is
WATCH-only, never bench. Concretely: `is_bench` must return False when any trigger
is a non-READY signal (`regime`/`symbol`/`structure`), and require ≥1 real gate
block. BASING intake → WATCH-only; Fixture D (extended) → bench; PNC (post-1a) →
BLOCKED.

**Diff-event remap (P2-2a).** `BENCH_READY` (`scan_diff.py:56-61`) reads `was_bench`
from the stored record — it stays actionable once `bench` is correct. There is **no
WATCH→BENCH event today** (`scan_diff.py:28-32`); add a LOW "pipeline progress"
event (`today.bench && !was_bench`) in `diff_symbol` after the READY-transition
block (`scan_diff.py:64`), reusing the already-present `bench` fields.

---

## Q6 — IV availability

- **Scan-time BSM uses REALIZED vol, not implied.** `juice_estimate` sets
  `hv = indicators.hist_vol(df)` and `sigma = hv/100` (`account_gate.py:65,71`) and
  prices every leg off that `sigma` (`account_gate.py:76,80,83,94`). **No per-symbol
  implied vol is produced during the sweep** — so the prompt's "append ATM IV from
  the BSM engine" is not achievable: the engine consumes vol, it doesn't derive IV
  from market prices. The only IV on a row is a **read** of the local store
  (`metrics/scorecard.py:481-485`, no provider call).
- **Existing IV store covers held/viewed names only.** `iv_history`
  (`DATA_DIR/iv_history.json`, `iv_history.py:27`); `iv_rank`/`iv_percentile`
  `iv_history.py:92-118`; `_MIN_POINTS=20` (`iv_history.py:29`). Written by chain
  views (`option_chain.py:584`) and nightly `snapshot_iv` over **`open_tickers()`
  only** (`maintenance.py:135,173`). The scan universe is **not** covered → blank
  IVR for swept-but-unheld names.
- **Schwab quote drops its volatility field.** `_parse_quote_node`
  (`schwab_api.py:168-193`) parses `last/close/bid/ask/mark/underlyingPrice/theta/
  delta/openInterest/quoteTimeMs` — **no `volatility`/`iv`** (Schwab sends a 52-wk
  HV `volatility` on the wire; it's discarded at ingest). Per-contract IV exists
  only in the **chain** (`schwab_api.py:699`, consumed `option_chain.py:348,581`) —
  a fetch, not a quote.
- **Where a new artifact would live.** Mirror `iv_history` / `scan_rejection_log` /
  `symbol_genius_history` single-writer + atomic-`os.replace` discipline
  (`iv_history.py:30,46-57`; `scan_rejection_log.py:36,54-65`), wired into the
  nightly sweep reusing `sweep_results` (`maintenance.py:213-221`).

**Conclusion (P2-2b).** True scan-wide IVR is **descope-eligible** (needs chains, a
provider cost) — reported, not built. Deliver **HVR from bars now** (no store
needed — HV recomputes from cache), and let true IVR **graduate automatically** via
the existing `iv_history` once a name's chain-sourced depth crosses a
`PROPOSED_DEFAULT` threshold. Optionally capture Schwab's dropped quote `volatility`
(52-wk realized HV) at ingest for a cheap persisted realized-vol series — still not
IV, so flagged descope-eligible.

---

## Q7 — HVR inputs

- `indicators.hist_vol(df, window=20)` returns a **scalar** annualized realized vol
  (`indicators.py:169-180`). **No rolling HV series / percentile-of-self exists** —
  the HVR function is net-new: mirror `_atr_series` for the rolling series
  (`indicators.py:119-132`) and `iv_history.iv_rank` for the percentile math
  (`iv_history.py:92-118`).
- **Depth:** `config.HISTORY_DAYS = 400` calendar days (`config.py:330`,
  `data_handler.py:136`) ≈ ~275 trading bars ⇒ **≥252 bars available** post-refresh
  for a rolling-252 HV rank.
- **INSUFFICIENT_DATA:** mirror the `< window → None` precedent (`indicators.py`
  `hist_vol` 174-177, `sma` 30-31, `rsi` 57) and `iv_history._MIN_POINTS`
  (`iv_history.py:112`): `< 252` bars ⇒ INSUFFICIENT_DATA / None. Drawer label
  **`HVR`** (or `IVR*`) — visually distinct from true IV rank.

---

## Q8 — Fixture data

- **Fixture E (PNC shape).** `early_advance_accum`-shape structure (EARLY_ADVANCE ×
  ACCUMULATING, RS RISING, SYM green, green regime) but **low realized vol** (tight
  daily ranges) ⇒ low `hist_vol` ⇒ low BSM extrinsic ⇒ **net juice below floor** via
  `juice_estimate` (pure over bars). Assert VERDICT BLOCKED, binding = L5 juice,
  **not on bench**. **Hard-floor variant:** even lower vol / higher burn ⇒ negative
  net juice ⇒ same BLOCKED assertion via `net ≤ 0`. Synthetic is **cleaner than
  cache** (deterministic control of vol → juice), built in
  `fixtures/structure/build_fixtures.py`.
- **XBI-like comparison fixture (SCORE acceptance).** Same clean structure but
  **higher vol** ⇒ higher net juice (~0.33 %/wk vs PNC ~0.12 %/wk). Assert the PNC
  shape does **not** outscore the XBI shape under the multiplicative factor.
- Juice inputs need no account state — `juice_estimate` is pure over bars, so both
  fixtures are self-contained.

---

## Implementation split (hard stop after this doc)

**P1 — Juice economics (the defect) + `~1D` guard (gate + pure functions).**
1. **Net juice-floor SAFETY block** in `score_ticker`, pure from
   `row["net_juice_weekly_pct"]`: `net ≤ 0` ⇒ BLOCKED; `net < JUICE_FLOOR_WK`
   (`PROPOSED_DEFAULT` 1.5 %/wk) ⇒ BLOCKED, binding `L5 juice: X% < floor Y%`. ETFs
   identical. Reclassify `juice_adequacy` → SAFETY (`scan_triggers.py:91`).
2. **Fixture E pair** (PNC low-vol below floor + negative-net variant) + **XBI-like**
   comparison fixture.
3. **SCORE multiplicative juice factor** at `scan_score.py:156`
   (`× min(net/JUICE_TARGET_WK, 1)`, clamp 0); acceptance test PNC ≯ XBI. Shadow
   unchanged.
4. **`~1D` guard**: minimum-information check in `_estimate_days`
   (`scan_triggers.py:195`) — degenerate inputs / `round(days) < 1` ⇒ `None`
   (condition word), drop `or 1`.

**P2 — WATCH/BENCH split, IVR, formatting.**
1. **`is_bench` fix** (`scan_triggers.py:328-336`): signal-WATCH (regime/symbol/
   `structure`) is never bench; bench requires an entrable structure + ≥1 clearable
   gate block. BASING → WATCH-only.
2. **Diff remap** (`scan_diff.py`): keep `BENCH_READY` actionable; add a LOW
   WATCH→BENCH "pipeline progress" event.
3. **HVR** (`indicators` new pure rolling-HV-rank, ≥252 bars, INSUFFICIENT_DATA
   path; drawer `HVR`). **True IVR** graduates via existing `iv_history`; a scan-wide
   IV fetch stays **descoped** (reported).
4. **Formatting**: net juice signed 2-dp always (`fmt` gains `minimumFractionDigits`
   + a `-0` clamp, `ui.jsx:133-136`; and/or more precision at `burn.py:313`).
   Trigger discipline: **calendar plain** (drop the `~` at `scan_triggers.py:349`),
   **estimate tilded** (keep `scan_triggers.py:351`, `Scorecard.jsx:125`,
   `ReadyToEnter.jsx:161` — but only for genuine estimates), **conditional as
   words**.

### DO-NOT (held throughout)
No SCORE authority (gate/sizing/ready-selection/recommendations) — the juice factor
changes its value, not its role. Juice adequacy is never benchable under any
framing. No ETF-specific juice rule — one floor, identical path. No executor /
kill_switch / circuit_breaker / order-path / regime-constant / classifier-genius
changes beyond what the audit identifies. No new per-symbol IV quote fetch (HVR from
bars + existing `iv_history`; Schwab `volatility` capture is descope-eligible). No
guessed Schwab fields; pure functions + offline fixtures. IV artifact append-only
derived. Every new constant `PROPOSED_DEFAULT`; existing tagged constants reused.
Structure/RS/dwell thresholds are **not** tuned here — calibration stays deferred
until the rejection log accumulates **post-fix** data (pre-fix bench data is
contaminated by the juice bug).

### Expected post-P1 acceptance snapshot
The 26-row bench collapses to only structure-complete, juice-adequate names;
EA/AES/PNC/XYZ/HUM/XLF/XLU all BLOCKED with L5-juice binding constraints; no `~1D`
rows without shown arithmetic.
