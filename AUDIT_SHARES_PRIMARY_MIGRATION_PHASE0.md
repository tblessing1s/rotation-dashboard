# Phase 0 Audit — Shares-Primary Migration (v3.0): Retire LEAP PMCC as Active Structure

**Scope.** The active base leg changes from a deep-ITM LEAP call to **real shares**
(delta ≡ 1.0). LEAPs become **read-only legacy**: existing history stays queryable
and renderable, but no new LEAP position can be opened, rolled, or recommended.
There are zero live positions at cutover, so no live-position migration is required.
The market-regime engine, per-symbol verdict system, entry-gate Levels 1–4, kill
switches, 75% buyback rule, whipsaw guard, ATR strike selection, and the order
lifecycle / reconciliation / freeze / hotfix machinery are structure-agnostic and
must be **provably untouched**.

**This is an audit only. No code was changed.** Every citation is `file:line`
against branch `claude/shares-primary-migration-v3-pzsgoe` at its current head
(`cac5c25`). New constants proposed below are tagged `PROPOSED_DEFAULT`; existing
tagged constants are cited before any new one is proposed. **Hard stop after this
document — no implementation until the owner approves.**

Finding format: `[CATEGORY] file:line — what it assumes — what v3.0 requires — risk if missed`.

---

## TL;DR — the ten things that decide this migration

1. **The prompt's schema numbers are stale. Real head is `CURRENT_VERSION = 19`
   (`migrations.py:20`), not v12.** The shares migration is `_v19_to_v20`, bumping
   to **v20** — not v13 (`_v12_to_v13` at `migrations.py:186` is old middle-of-chain
   history). Every downstream reference to "v13" in the prompt should read **v20**.

2. **Positions are NOT a pure replay of the ledger — only the *ledgers* are.**
   `recompute_derived()` (`logging_handler.py:687-1103`) rebuilds theta_ledger,
   extrinsic_payback, roll_ledger, cycles from executions, but the **position
   holdings themselves are mutated imperatively** by per-action `apply()` closures
   (`executor.py:2389-2561`). The migration's TESTS demand "replaying the full
   ledger from genesis yields identical state (no cache leakage)" — **that
   guarantee does not hold for `state["positions"]` today.** Either the correction
   layer must operate on the derived ledgers (which do replay) or position-holding
   replay must be built. This is the single most important architectural finding.

3. **Most of the "Correction & Manual Reconciliation Layer" already exists and is
   tested.** Append-only typed events already present: `adjustment` +
   `linked_diff_id` (`executor.py:548`), `adopt_broker_trade` (`executor.py:635`)
   with exact undo via `adoption_reversal`/`reversed_by` excluded from replay
   (`executor.py:1400`; exclusion `logging_handler.py:690`), `resolve_expiry`
   (`executor.py:594`), `record_manual_roll` (`executor.py:806`),
   `position_rebuild` (`executor.py:870`), plus the append-only dedupe ledger
   `ingested_transactions` (`transaction_ingest.py:444`). The migration **extends**
   this machinery; it does not invent it. Work Item 10 shrinks to three real gaps
   (see §8).

4. **The reconciliation design doc is out of date on its central premise.**
   `docs/reconciliation.md` states *"all trading goes through this app… there is no
   adopt-external-trade flow."* The code has already moved past this:
   `transaction_ingest.build_report` (`transaction_ingest.py:366`) splits broker
   fills into `matched` vs out-of-band `proposals` (`broker_manual`), and
   `adopt_broker_trade` adopts them. The migration's "adopt broker" requirement is
   **largely already built**; the doc needs updating, not the concept inventing.

5. **Burn must be REMOVED from the shares path, not set to zero — and the code
   proves why.** `coverage()` (`burn.py:250`) treats `burn_per_week_with_slippage
   <= 0` as `status="low_extrinsic"` and caps the ratio. A shares leg carrying
   `burn = 0.0` is therefore **mislabeled as a stressed deep-ITM position** on every
   dashboard. Absence (via `.get()` → `None`) degrades cleanly to "feature off"
   everywhere EXCEPT one hard subscript site: `account_gate.py:105-107` reads
   `net["net_weekly_yield_pct"]` / `net["burn_per_week_ps"]` /
   `net["net_juice_per_week_ps"]` with `[]`, not `.get()` — removing those keys from
   `candidate_net_juice`'s contract **KeyErrors** here.

6. **The single most common real-shares exit has no coded reason and no execution
   path.** `exit_reasons.py:20-101` has no "called away / assigned at strike";
   `executor.py` has only `_close_leap` (the long option) as a realized-P&L exit.
   A called-away lot today falls to `OPERATOR_DISCRETION` (forces a typed note) or
   `RECONCILIATION`, corrupting calibration buckets.

7. **Assignment mechanics are inverted for shares in the operator-facing path.**
   `position_manager.py:229-246` and `reconcile.py:466-471` hard-code *"the short is
   covered by a LEAP, not stock… never exercise the LEAP,"* and `SHORT_STOCK_DETECTED`
   (`reconcile.py:387`) is the assignment signal. With real shares, assignment is a
   **clean delivery of owned shares at strike** — no short stock, no LEAP, no
   dividend liability. Left unchanged, the UI actively misdirects the operator.

8. **The whole extrinsic-payback machine and coverage ratio are LEAP-extrinsic
   denominated and go zero/undefined for shares.** `logging_handler.py:769-883` keys
   every state and transition off `buy_leap`/`close_leap`/`leap_roll_id` and a target
   of "LEAP extrinsic at entry"; `burn.py:234-265` divides juice by burn. Shares have
   no extrinsic to pay back and no burn to divide by → `pct_complete = 0` forever and
   a stuck `low_extrinsic` coverage sentinel. These must be gated on `position_type`.

9. **No equity order path exists at all.** All three order builders hardcode
   `assetType: "OPTION"` (`schwab_api.py:572, 601-603, 645-652`); `VALID_ACTIONS`
   (`executor.py:27`) and `INSTRUCTION` (`executor.py:105`) have no share action.
   `EQUITY` appears only in read/booking paths. Every Schwab equity-order field is
   **UNKNOWN** and must be captured from a live `previewOrder` before trust (§7).

10. **The verdict/regime engine is provably structure-agnostic.**
    `compose_verdict` (`scan_verdict.py:62`) is pure over `(regime, symbol,
    base_stage, inst_flow)` — no burn, leap, or `position_type` reference. The XLK
    July-6 regression (TOPPING × ACCUMULATING → BLOCKED, `test_scan_verdict.py:45,
    74`) keys off none of the migrated surfaces. **The canonical "verdict untouched"
    assertion holds by construction.** Note: `structure_classifier.py`'s `BaseStage`
    is the *stock's chart cycle*, NOT the base leg — despite the name, it needs no
    gating.

---

## Category 1 — LEAP-structural assumptions

Every site below assumes the base leg is an option (strike, expiry, IV, sub-1.0
delta, decaying extrinsic). A shares base is `delta ≡ 1.0`, `extrinsic ≡ 0`, no DTE.

### 1a. BSM pricing / IV solve / put-IV substitution on the base leg
- `[LEAP-STRUCTURAL] indicators.py:481 — _bs_call_price is the sole engine; every base-leg cost/extrinsic/burn number is a BS call price — shares price = spot, zero extrinsic — risk: shares get "priced" as a call with phantom extrinsic/theta.`
- `[LEAP-STRUCTURAL] indicators.py:487 — implied_vol_call bisects an IV out of the LEAP mark — shares have no IV — risk: IV solve returns garbage/None, silently voiding downstream burn/delta.`
- `[LEAP-STRUCTURAL] indicators.py:511,517 — _bs_put_price / implied_vol_put exist to recover skew-aware vol for the deep-ITM base CALL from its same-strike OTM put — shares need neither; the entire put-IV substitution path is dead — risk: wasted chain fetch + a vol applied to a non-option.`
- `[LEAP-STRUCTURAL] option_chain.py:330-365 — _augment_call_greeks recomputes base-LEAP delta+IV, preferring the OTM put's IV for the ITM call (349-359) then call_greeks (360) — shares bypass entirely — risk: base "delta" derives from put-IV substitution instead of being 1.0.`
- `[LEAP-STRUCTURAL] indicators.py:542,564,584 — call_greeks_full / leap_weekly_burn / call_greeks return theta/vega/delta/IV for the base call — shares: delta=1.0, theta=vega=0 — risk: book theta/vega double-counts a non-decaying leg; every base-delta consumer reads a BS number.`
- `[LEAP-STRUCTURAL] burn.py:37-48, 88-185 — _model_extrinsic_per_share and burn_projection compute the two-point BS extrinsic difference for the base LEAP — shares: extrinsic ≡ 0 — risk: the whole burn/coverage/net-juice panel treats shares as a bleeding option.`
- `[LEAP-STRUCTURAL] account_gate.py:83,94 — juice_estimate prices the base LEAP via _bs_call_price and nets its model burn via candidate_net_juice as the entry ranking key — shares: leap_cost = spot×shares, burn=0 — risk: candidate ranking penalizes a "LEAP cost" that no longer exists.`
- `[LEAP-STRUCTURAL] leap_policy.py:98-131 — leap_health reads leap[strike/current_bid/contracts/cost_basis], derives intrinsic/extrinsic/weeks-of-juice/yield-on-LEAP-capital and leap_weekly_burn/leap_delta — shares capital = spot×shares, no extrinsic — risk: juice-yield-vs-LEAP-cost adequacy computed against option premium.`

### 1b. Base-leg delta recompute expecting ~0.90 (not 1.0)
- `[LEAP-STRUCTURAL] indicators.py:473 — bs_call_delta = e^(−qT)·N(d1) assumes base delta < 1 and dividend/vol-sensitive — shares delta = exactly 1.0, q/vol-invariant — risk: dividend-adjusted leverage warnings mis-fire on a truly-1.0 leg.`
- `[LEAP-STRUCTURAL] account_gate.py:29-49 — _leap_strike_for_delta bisects a strike whose BS delta ≈ LEAP_TARGET_DELTA (0.90) — shares have no strike/delta target — risk: entry sizing keeps solving a 0.90 strike instead of buying 100×contracts shares.`
- `[LEAP-STRUCTURAL] position_manager.py:251-298 — delta_coverage recomputes each leg's delta via call_greeks, applies LEAP_DELTA_FLOOR (295) + inversion check — shares delta is permanently 1.0, floor unreachable — risk: floor_breach can never legitimately fire yet the code still prices shares as an option.`
- `[LEAP-STRUCTURAL] option_chain.py:374,435,451-458 — LEAP_DELTA_FLOOR=0.50; leap_delta=min_leg_delta; alerts to "roll the LEAP deeper ITM" — shares base is 1.0 forever — risk: nonsensical roll-deeper guidance.`
- `[LEAP-STRUCTURAL] portfolio_risk.py:81-87 — base share_equiv += d×contracts×100 using BS delta d — shares contribute exactly count (line 99 already handles pure shares) — risk: book delta undercounts a shares base (uses ~0.90) unless routed through the shares branch.`
- `[LEAP-STRUCTURAL] leap_policy.py:72-87 + maintenance.py:36-61 + alerts.py:185-208,725-754 — _delta_velocity / snapshot_leap_deltas / check_delta_uncovered / DELTA_VELOCITY all track a moving base delta from delta_history — shares delta never moves — risk: dead alert paths that still recompute shares as an option each cycle and fill delta_history with 1.0s.`
- `[LEAP-STRUCTURAL] indicators.py:665-715 — get_leap_strikes selects candidate strikes in [LEAP_DELTA_MIN, LEAP_DELTA_MAX] near LEAP_TARGET_DELTA — shares base has no strike menu — risk: entry UI offers 0.90-delta LEAP strikes instead of a share quantity.`

### 1c. 180-DTE entry / 135-DTE planned-exit / anti-zombie window slide
- `[LEAP-STRUCTURAL] config.py:423,876,872 — LEAP_TARGET_DTE=180, LEAP_ENTRY_DTE_DEFAULT=190, PLANNED_EXIT_DTE=135 — shares are dateless — risk: entry logic stamps a 180-DTE expiry and the burn window / DTE_PLANNED_EXIT trigger run on a leg with no expiry.`
- `[LEAP-STRUCTURAL] config.py:817,822,844,908 — LEAP_ROLL_DTE_FLOOR=90, LEAP_MIN_EXTRINSIC_WEEKS=4, DELTA_VELOCITY_WINDOW=5, EXTENSION_STEP_WEEKS=1 — all assume a dated, decaying, delta-drifting base — none apply to shares — risk: roll/extension/velocity machinery keeps evaluating a shares base.`
- `[LEAP-STRUCTURAL] burn.py:72-85 — _effective_exit_dte is the anti-zombie window slide: when DTE ≤ planned exit it slides the projection window EXTENSION_STEP_WEEKS×7 down the theta curve, flags extended — shares have no theta curve — risk: zombie-window logic slides a window for a leg that never decays.`
- `[LEAP-STRUCTURAL] burn.py:188-213 + leap_policy.py:139,158-162 — extension_cost / extension_preview ladder (1,2,4 weeks) fabricate rising burn for a longer hold — shares burn stays 0 — risk: UI ladder shows fabricated rising burn.`
- `[LEAP-STRUCTURAL] recommendation_engine.py:324-328 — DTE_PLANNED_EXIT fires when leap_dte <= planned_exit_dte — shares leap_dte is None — risk: trigger never fires or crashes on the missing DTE.`

### 1d. Base-leg ROLL construction (rolling the LEAP itself)
- `[LEAP-STRUCTURAL] executor.py:3528-3655 — _roll_leap / _commit_leap_roll / _place_live_leap_roll roll the long leg (STC old LEAP + BTO new target-delta/180-DTE LEAP), building close_leap+buy_leap and transmitting a 2-leg OCC option net order — shares are never rolled — risk: an entire action verb (roll_leap) is invalid for shares and must be gated off.`
- `[LEAP-STRUCTURAL] leap_policy.py:248-327 — roll_cost_estimate prices STC current LEAP + BTO new LEAP_TARGET_DELTA/DTE LEAP via _bs_call_price + _leap_strike_for_delta, reserve/debit-gated — shares: no roll, no BS — risk: reserve gate computes an option debit for shares.`
- `[LEAP-STRUCTURAL] executor.py:2432-2475 — _close_leap splits intrinsic/extrinsic of the closing base leg (extrinsic_remaining) and is the ONLY realized-exit-with-P&L path — shares proceeds = spot×shares, extrinsic 0 — risk: close accounting books phantom extrinsic on a shares exit; and there is no shares-exit analog (see Category 4).`
- `[LEAP-STRUCTURAL] alerts.py:617-660 — check_leap_roll_due reads leap_policy.leap_health().roll_due — shares never roll — risk: recurring "roll your LEAP" alert on a shares base.`

### 1e. Entry-context snapshot fields recording base-leg option data
- `[LEAP-STRUCTURAL] entry_context.py:363-377 — _execution_intent snapshots leap_strike, leap_dte (default LEAP_TARGET_DTE), leap_delta, strike_policy_row — shares base: strike=cost/share, no DTE, delta=1.0 — risk: the FROZEN entry snapshot records option intent for a shares base, corrupting calibration-harness inputs (and the snapshot is immutable once written, executor.py:2419-2421).`
- `[LEAP-STRUCTURAL] entry_context.py:34-41 — _TRACKED_FIELDS deliberately excludes leap_delta/strike/expiry as "operator intent, not telemetry" — for shares these are meaningless, not merely untracked — risk: schema carries option-only fields the migration must reinterpret or null.`
- `[LEAP-STRUCTURAL] executor.py:2322-2334, 2404-2411 — _buy_leap builds the immutable base-leg record + apply() leg dict with strike/expiration/extrinsic_captured/dte — shares base needs a different leg shape (shares/count/cost-per-share) — risk: the immutable entry record (replay source of truth) stamps option fields on a shares base.`

### Files to gate behind `position_type` (Category 1)
`leap_policy.py`, `burn.py`, `indicators.py` (base-leg pricing/greeks), `option_chain.py`
(`_augment_call_greeks`, `get_leap_strikes`, Execute-panel sizing), `account_gate.py`
(`_leap_strike_for_delta`, `juice_estimate`, `_position_reserve`, `evaluate`),
`position_manager.py` (`delta_coverage`, `enrich_leap`), `executor.py` (`_buy_leap`,
`_close_leap`, `_roll_leap`/`_commit_leap_roll`/`_place_live_leap_roll`),
`entry_context.py`, `portfolio_risk.py`, `alerts.py` (delta/roll alerts),
`recommendation_engine.py` (DTE/delta triggers + `_enter_ticket` LEAP-leg builder
~lines 200-228, 320-328), `config.py` (the LEAP constants), `logging_handler.py`
(`recompute_derived` leap_dte/trailing juice stamping ~1050-1073), `maintenance.py`.

---

## Category 2 — Burn composition

Burn is the long LEAP's theta decay. It is **model-difference (two-point BS), never
straight-line** today (`burn.py:150-164`; enforced by tests `test_burn.py:38,63`, not
a runtime switch — `BURN_IS_MODEL_DIFF` at `config.py:862` is documentation only).
For shares, burn is zero by construction and must be **removed**, not zeroed.

- `[BURN] burn.py:216-231 — net_juice_per_week(juice, burn_pw_slip) = juice − burn is THE single net definition (position view + entry queue) — shares path must bypass this (net ≡ gross − slippage) — risk: passing burn=0.0 returns gross numerically but violates "remove entirely" and trips the coverage trap below.`
- `[BURN] burn.py:234-265 (coverage) — PRESENT-BUT-ZERO TRAP: line 250 treats burn_per_week_with_slippage <= 0 as status="low_extrinsic" and caps the ratio — a shares leg at burn=0 is mislabeled a stressed deep-ITM position on every dashboard — risk: THE sharpest absent-vs-zero trap; this is why burn must be absent, not 0.`
- `[BURN] burn.py:268-315 (candidate_net_juice) — prices a hypothetical LEAP and subtracts burn_pw_ps from weekly extrinsic; returns burn_per_week_ps / net_juice_weekly_pct — the ENTRY-QUEUE ranking metric — shares net MUST equal gross — risk: single most important removal site; its consumer at account_gate.py:105-107 hard-subscripts the burn keys (KeyError if dropped — keep keys valued None/gross or switch to .get).`
- `[BURN] indicators.py:564-581 (leap_weekly_burn) — coarse burn = −theta_day×7×contracts×100 from the leg mark; feeds net_weekly_maintenance + CAPITAL_BURN alert — shares: zero — risk: None-guarded consumers degrade safely; a literal 0 makes a shares position read "self_funding" and still evaluate the alert path.`
- `[BURN] burn.py:72-85, 188-213 (anti-zombie window / extension_cost) — theta-curve slide + rising-burn ladder — meaningless for shares (no DTE) — risk: extension_preview cards render nonsense.`
- `[BURN] leap_policy.py:127-155, 222-237 — leap_health / aggregate_health fold burn into net_maintenance + coverage; multi-leg _sum skips None — shares leg must skip this block — risk: .get()/None-safe for absence; present-but-zero misreads coverage (low_extrinsic).`
- `[BURN] account_gate.py:93-107 (juice_estimate) — reads candidate_net_juice results via DIRECT SUBSCRIPT net["net_weekly_yield_pct"]/["burn_per_week_ps"]/["net_juice_per_week_ps"] (105-107) — shares candidate must return net==gross with keys present — risk: THE one hard KeyError site if burn keys are removed from the contract.`
- `[BURN] metrics/scorecard.py:466-475, 540-543 — Gross·Burn·Net scan columns (burn_weekly_pct = juice − net) and juice_floor_block (hard tier when NET ≤ 0) — all .get()/None-guarded (absent safe), but with burn gone net==gross so which names hard-block shifts — risk: shifts the juice floor's effect; also see Category 7 recalibration.`
- `[BURN] burn_marks.py:76-238 — weekly realized-vs-projected telemetry (DATA_DIR/burn_marks.json) feeding the divergence badge + monthly_realized_burn → payouts — shares record nothing (nothing decays); monthly_realized_burn returns {} cleanly — risk: SAFE (payouts degrade to juice-only); but the weekly mark job must not run on share legs or it records phantom decay.`
- `[BURN] payouts.py:217-234, 276-324 — monthly_leap_burn wrapped try/except→{}; _net_payout = net_juice − leap_burn(or 0); burn_tracked = leap_burn is not None — shares months have no marks → payout = full juice — risk: SAFE by design (burn treated 0-when-untracked); shares show "LEAP burn n/a".`
- `[BURN] alerts.py:661-680 (check_capital_burn) — None early-return, then fires when all recent weeks j−burn<0 — shares never burn → correctly never fires (None absent) — risk: SAFE.`
- `[BURN] position_manager.py:576-593 / app.py:446-462 / queue_state.py:68-72 / scan_score.py:131-172 — net_juice_rollup, /api/burn/<t> panel, queue ranking, SCORE viability all read net via .get() — risk: all None-safe (absent → None); the Burn panel would render empty cards for shares (hide/404 it).`

**Load-bearing conclusion.** Removing the burn key degrades safely everywhere via
Python `.get()` / JS `undefined` EXCEPT **`account_gate.py:105-107`** (hard `[]`
subscript). Setting burn to a literal `0.0` instead of absent actively **corrupts
`coverage()` status** (`burn.py:250` → `low_extrinsic`). Correct move: the shares
path never calls the burn engine; where a shared return contract is unavoidable
(candidate_net_juice → account_gate), keep the keys present with `None`/gross values
or convert the subscripts to `.get()`. **Do not let burn=0 leak into the shares
path, and do not let removal leak into legacy display/defend/kill/assignment paths.**
Legacy positions retain model-difference burn (`burn.py:150-164`) for historical
display only.

**Not burn — do not confuse.** `executor.py:1290-1291, 2528-2535` and
`logging_handler.py:840-844` compute a *realized* `net_juice` = extrinsic_sold −
extrinsic_paid_back (short-premium bookkeeping), named "net_juice"/"burn" in
comments but unaffected by the shares migration. Do not strip these.

---

## Category 3 — Position sizing & coverage

- `[SIZING-COVERAGE] config.py:427 — SHARE_CAP=500 ("accumulate to 500 shares per stock, then rotate") IS the 500-share / 5-covered-lot ceiling, today enforced only as a shares SIDECAR (can_add_shares, position_manager.py:613-624) — shares become the PRIMARY base — risk: cap semantics collide when the base itself is shares (double-count LEAP-equivalent + real shares).`
- `[SIZING-COVERAGE] config.py:419 — LEAP_CONTRACTS=1 is the base unit in option contracts (×100) — v3.0: "contracts" becomes literal 100-share covered lots — risk: sizing/reserve/capital (account_gate.py:126,196-203,254,266) scale by contracts×100, must mean share count.`
- `[SIZING-COVERAGE] config.py:947,950,1064-1065 — MAX_CFM_POSITIONS=2, MAX_DEPLOYED_CAPITAL=38000, CAPITAL=35000, RESERVE_REQUIRED=13000; there is NO per-position ~$10-15K constant (only implied 38000÷2-3) — v3.0 needs a real per-position lot-cost cap — risk: no coded gate stops a high-priced name whose one 100-share lot blows the envelope.`
- `[SIZING-COVERAGE] config.py:883-892 — COVERAGE_HEALTHY=3.0/MARGINAL=2.0/DISPLAY_CAP=10.0; ratio = juice/wk ÷ burn/wk — shares have ZERO burn → denominator 0 → coverage undefined — risk: the coverage-health concept is meaningless for shares; every shares position reads low_extrinsic forever (burn.py:250).`
- `[SIZING-COVERAGE] portfolio_risk.py:85-99 — share_equiv = delta×contracts×100 for the LEAP (85) minus short delta×n×100 (95), THEN literal shares.count (99) — v3.0: the LEAP delta term must be REPLACED by literal owned share count at delta 1.0 — risk: base directional exposure stays a Greek estimate; book delta mis-stated.`
- `[SIZING-COVERAGE] position_manager.py:266-298 (delta_coverage) — long coverage = Σ leg delta×contracts; floor_breach = min leg delta < LEAP_DELTA_FLOOR; inverted check — v3.0: shares delta=1.0 so this must become a literal covered-lot count (floor(shares/100) ≥ short contracts) — risk: the coverage guardrail is a delta comparison that can't trip; must be rewritten as a lot-count check.`
- `[SIZING-COVERAGE] position_manager.py:444-498 — shares block (count/cap/pct_to_cap/locked) + position_capital sums LEAP cost_basis + shares.count×cost_basis_per_share — v3.0: short contract count must derive as floor(shares.count/100); leap_legs=[] → leap_totals None (408-409) blanks capital/juice aggregations — risk: no code derives "how many covered shorts my lot supports"; the 100-share→1-contract atomic floor + fragment flag does not exist.`
- `[SIZING-COVERAGE] account_gate.py:113-146 (sector_size_suggestion) — the only sizing lever is ADVISORY ("never changes the ENFORCED contract count") — v3.0 needs an ENFORCED round-lot SIZE-BLOCK — risk: nothing rejects a lot whose price×100 exceeds the per-position cap; only book-wide cash/reserve/MAX_DEPLOYED are enforced.`
- `[SIZING-COVERAGE] account_gate.py:193-203 (_position_reserve) — reserve = RESERVE_ATR_MULT×ATR×contracts×100 — shape already right for shares if "contracts" means covered lots — risk: add SIZE-BLOCKED (block when price×100 > per-position cap) alongside.`

**Fragment/SIZE-BLOCKED status:** neither a literal `floor(shares/100)` covered-lot
derivation, a fragment flag (150 shares → 1 coverable + fragment), nor a SIZE-BLOCKED
verdict exists today. All three are net-new (HARD_CFM_RULE: fragments below one
covered lot must never be sellable-against).

---

## Category 4 — Assignment & exit paths

- `[ASSIGNMENT-EXIT] exit_reasons.py:20-101 — NO "called away / assigned at strike" reason exists; LEAP-specific reasons are DELTA_COVERAGE (34) and LEAP_ROLL (43); structure-agnostic reasons (KILL_SWITCH_* 24-25, CB_* 28-31, WHIPSAW_BREAKER 33, EARNINGS_WINDOW 35, RECONCILIATION 36, TARGET_REACHED/OPERATOR_DISCRETION 37-38) carry unchanged — v3.0 REQUIRES a new structure-agnostic CALLED_AWAY/ASSIGNED_AT_STRIKE reason in CLOSE_TIME+AUTOMATED, ideally note-free — risk: the commonest real-shares exit falls to OPERATOR_DISCRETION (forces a note, 88-90) or RECONCILIATION, corrupting calibration buckets.`
- `[ASSIGNMENT-EXIT] executor.py:2432-2475 (_close_leap) — the ONLY realized-exit-with-P&L path: realized_pnl = close_total − cost_basis, intrinsic/extrinsic split — v3.0 needs the SHARES analog: proceeds = strike×100×contracts, realized_pnl vs share cost basis (position_manager.py:493-497), status→closed, CALLED_AWAY reason — risk: no shares-called-away booking exists; closest is _close_leap (wrong leg) or _apply_adjustment (moves count, books no proceeds/P&L → invisible to income/calibration).`
- `[ASSIGNMENT-EXIT] reconcile.py:384-390,411-441,466-471 — assignment detected as SHORT_STOCK_DETECTED (broker short stock on a LEAP underlying) with "buy back the short stock… Do NOT exercise the LEAP" guidance — v3.0: with shares, called-away shows up as owned shares REDUCED at strike (MISSING/QUANTITY_MISMATCH on EQUITY), a clean exit — risk: today's logic treats it as a generic missing-instrument diff and the realized sale at strike is never booked automatically.`
- `[ASSIGNMENT-EXIT] position_manager.py:208-247 (enrich_short assignment_risk) — hard-codes "covered by a LEAP, not stock: assignment creates SHORT STOCK that owes the dividend" (229-232) and "never exercise the LEAP" (245-246); fires roll-before-ex-div triggers to AVOID short stock — v3.0: covered-share assignment = clean delivery at strike (the planned exit), so "roll before ex-div" default flips — risk: the management view instructs inverted mechanics and nags to roll away a fine called-away exit.`
- `[ASSIGNMENT-EXIT] transaction_ingest.py:307-311,343-344 + executor.py:693,908 — equity/assignment legs "never count as booked," short stock out-of-band labeled "assignment likely"; both leg-builders skip equity ("booked via adjustment, not here") — v3.0: a called-away lot is a normal SELL of owned shares — risk: real-share assignment ingests as an anomaly, not a covered-call exit.`

### Payback state machine (extrinsic_payback) — LEAP-specific in full
- `[ASSIGNMENT-EXIT] logging_handler.py:769-823 — the cycle-scoped replay machine: FRESH CYCLE (buy_leap → collected=0, target=entry extrinsic, 791-794), ROLL-CARRY (matching leap_roll_id → target += new extrinsic, 782-783), ADD/MERGE (leap_add → target grows, 784-790), COLLECT (close_short → collected += net, 796-809), PARTIAL-LEG (close_leap legs_remaining>0 → carry, 814-818), TRUE-EXIT RESET (close_leap last leg → pop, 819-823) — EVERY state keys off buy_leap/close_leap/leap_roll_id and a target of "LEAP extrinsic at entry" — shares have no extrinsic to pay back — risk: for a shares base the meter's denominator is zero → pct_complete=0 forever (839), a meaningless income hurdle; must be gated on position_type.`
- `[ASSIGNMENT-EXIT] logging_handler.py:825-883 — per-position meter (leap_extrinsic_at_entry/collected/remaining_to_payback) + book-wide extrinsic_summary "income is only real once LEAP extrinsic is paid off" — LEAP-specific headline — v3.0: shares have $0 to recover, juice is immediately real — risk: at_entry falls back to leap.extrinsic_at_entry (832); a stale LEAP field fabricates a phantom hurdle; headline income understated.`
- `[ASSIGNMENT-EXIT] logging_handler.py:591-656 (validate_payback) — leap_roll_id latch / legs_remaining integrity (dangling_leap_roll, orphan_roll_buy, legs_remaining_mismatch) — inert for shares — risk: harmless dead checks; payback_reconciliation.ok trivially true.`

**Payback test-coverage gaps (known open flag, confirmed).** Payback is tested only
for LEAP roll-carry / multi-tranche / true-exit / validate_payback failures
(`test_leap_lifecycle.py:106-323`, `test_multi_leap.py:62-113`); `test_payouts.py`
covers juice/burn/intrinsic-melt but **never a called-away/assignment close and never
a shares-only position**. The migration can silently produce phantom paybacks with no
failing test to catch it — new fixtures required (see Tests).

---

## Category 5 — Schema surface & the proposed v20 shape

**Actual chain.** `CURRENT_VERSION = 19` (`migrations.py:20`); dict at
`migrations.py:306-325`, walked by `migrate()` (`migrations.py:328-366`). Every
migration ADDS only; snapshots before running; aborts without a rollback point
(`MigrationAbortedError`). Immutability is contractual: *"Migrations only ADD
structure — they never rewrite executions (those are immutable)"* (`migrations.py:5-8`,
reiterated per-migration); no writer mutates `state["executions"]` beyond `.append`
(`logging_handler.py:286`); reversals filter at derive time, never rewrite
(`derived_executions`, `logging_handler.py:670-684`).

**v12→v13 (`migrations.py:186`) is old history** — it added per-position
`entry_context`. The prompt conflates that with the new migration. **The shares
migration is `_v19_to_v20`, `CURRENT_VERSION → 20`.**

Base-leg option structure fields and where written:
- `[SCHEMA] executor.py:2329-2334 — buy_leap execution record (strike, execution_price per-contract $, execution_total, extrinsic_captured, stock_price, expiration) — v3.0 needs a PARALLEL buy_shares record type, never a reshaped buy_leap — risk: reshaping breaks derived replay + append-only.`
- `[SCHEMA] executor.py:2404-2411 — LEAP leg dict (strike, contracts, cost_basis, extrinsic, extrinsic_at_entry, expiration; delta NOT stored — it lives in delta_history + live reads) — v3.0 SHARES base needs its own lot record discriminated by position_type — risk: conflating shares into a leap leg poisons extrinsic_payback.`
- `[SCHEMA] executor.py:394-407 — position skeleton ALREADY carries shares{count, cost_basis_per_share, cap, pct_to_cap} + SHARE_CAP=500 — v3.0 extends to a lot-aware record — risk: leaving it half-modeled means shares can't be a real base leg.`
- `[SCHEMA] logging_handler.py:54, 701-760 (theta_ledger) + 769-858 (extrinsic_payback) — derived from executions but keyed on buy_leap/close_leap — v3.0 must gate payback/theta keying on position_type — risk: a SHARES base feeds a phantom payback target (829-834).`

**Proposed v20 shape (prose; PROPOSED_DEFAULT).** Add `_v19_to_v20`, bump to 20.
Introduce a `position_type` discriminator on each position — `"LEAP_PMCC_LEGACY"`
(backfilled onto every existing position by the migration, since all current
positions are diagonal LEAP+short) vs `"SHARES"`. Extend the existing
`shares{count, cost_basis_per_share, cap, pct_to_cap}` into a lot-aware record:
`shares{symbol, qty, lot_cost_basis, acquisition_records:[{date, qty, price, source,
execution_id}]}`. Preserve append-only history via **NEW record types only** — add
execution actions `buy_shares` / `sell_shares` and an assignment/called-away booking
action to `VALID_ACTIONS` (`executor.py:27`) and the `INSTRUCTION` map
(`executor.py:105`); NEVER rewrite historical `buy_leap`/`close_leap` records. The
short leg (`short_calls`) is unchanged — it covers `shares` instead of a `leap` leg.
Reuse `append_execution` (`logging_handler.py:271-289`) verbatim for the new actions
(single-writer atomic contract). All legacy records are tagged
`LEAP_PMCC_LEGACY` at read time — no historical mutation.

**Architectural caveat (repeat of TL;DR #2).** Positions are imperatively mutated by
`apply()` closures (`executor.py:2389-2561`) and are NOT replayed by
`recompute_derived`; only the ledgers replay. The correction-determinism acceptance
test ("replay from genesis yields identical state") is satisfiable for the ledgers
but **not for `state["positions"]` as built today.** Decide during Phase 2 design
whether corrections operate on the derived ledgers (which replay) or whether
position-holding replay is added.

---

## Category 6 — Dividend surface

What exists: `dividends.py` computes a continuous yield `q` (24h cached,
unknown→0.0 safe no-op, `dividends.py:93-116`) and holds an ex-div `next_dividend`
event (`dividends.py:167-232`); BSM is q-aware throughout (`indicators.py:469-605`);
an assignment-risk WARN already fires when short extrinsic < coming dividend before
ex-div (`alerts.py:422-502`, severity HIGH/defend, registered 1031;
`position_manager.py:110-246`); the defensive-roll path exists
(`recommendation_engine.py:354-356, 410, 448`, DIVIDEND_ASSIGNMENT_RISK → ROLL_OUT);
the nightly job already refreshes the dividend cache and syncs each position's
snapshot (`maintenance.py:129-160`).

- `[DIVIDEND] schwab_api.py:332-342 — get_instrument_fundamental returns the whole "fundamental" block; the ONLY field the code names is divYield (percent) — v3.0 needs a real ex-div DATE — UNKNOWN: no ex-div field is named. VERIFY: GET instruments?symbol=<payer>&projection=fundamental and inspect the fundamental object's keys.`
- `[DIVIDEND] dividends.py:175-197 — ex-div key names are GUESSED candidates (nextDivExDate/divExDate/dividendDate/divDate; amount divPayAmount/divAmount/divFreq) probed best-effort — the entire assignment-risk surface depends on one resolving — UNKNOWN: none confirmed against a live payload. VERIFY: dump one live fundamental for KO/JNJ, confirm which keys exist + units (percent vs decimal, per-payment vs annual).`
- `[DIVIDEND] alpha_vantage.py:127-130 vs dividends.py:194-197 — overview() documents only DividendYield; dividends.py reads ov["ExDividendDate"]/["DividendPerShare"] which the client never validates — UNKNOWN at the client layer. VERIFY: call function=OVERVIEW, confirm ExDividendDate (YYYY-MM-DD, can be "None") + DividendPerShare are present and non-stale.`
- `[DIVIDEND] transaction_ingest.py:130-133 — non-TRADE activity ("dividends, transfers, fees") is DROPPED, returns (None,None) — v3.0 needs the cash-dividend transaction to book income — HIGH: real dividend cash is silently discarded. VERIFY: inspect Schwab GET /transactions type values for a cash dividend (likely DIVIDEND_OR_INTEREST / RECEIVE_AND_DELIVER) to know what to newly accept.`
- `[DIVIDEND] account_gate.py:349-362 (earnings_in_cycle) — the pattern to MIRROR: computes days_until, tests 0<=days<=cycle_days, blocking with typed override — v3.0 ex-div-in-cycle check should be a sibling checks.append() right after 362 — HIGH if missed: entry gate never screens short-call cycles straddling an ex-div on a payer.`
- `[DIVIDEND] account_gate.py:367-371,384 — next_dividend IS fetched but only attached to the payload ("feeds ASSIGNMENT_RISK once the position exists"), NOT a _check, NOT blocking — v3.0 must promote it into a Level 5 _check — MEDIUM: data is on hand; only the check node is missing.`
- `[DIVIDEND] transaction_ingest.py:130-133 + rec_types.py:15-67 + logging_handler.py:470-882 — NO dividend income event type exists anywhere; ledgers derive only juice/extrinsic over buy_leap/sell_short/etc. — v3.0 wants dividend income recorded as ITS OWN ledger event (income taxonomy kept clean, NOT juice) — HIGH: held-share dividends are invisible to P&L; a naive add would contaminate the juice/theta ledger — needs a separate ledger structure and a new event enum (rec_types has no income member).`
- `[DIVIDEND] alerts.py:52,480-481; position_manager.py:230-232; reconcile.py (SHORT_STOCK_DETECTED) — ALL guidance hard-codes "the short is covered by a LEAP, not stock" / "never exercise the LEAP" — v3.0 base leg is real shares; assignment simply delivers held shares, no synthetic short stock — HIGH: this semantics becomes WRONG and will misdirect the operator (overlaps Category 4).`
- `[DIVIDEND] earnings.py:113-160,180-193 — earnings has a Schwab↔AV cross-check + staleness flag + a cache-only bulk reader (cached_earnings); dividends.py has NEITHER (no conflict, no stale, no cached_dividend) — MEDIUM: free-tier ex-div dates go stale/wrong silently; bulk scans risk a fetch storm — mirror _is_stale + a cache-only reader + a DIVIDEND_DATE_STALE alert.`
- `[DIVIDEND] indicators.py:455-459 — the continuous-yield BSM is EUROPEAN; comment says "early exercise near ex-div ignored — tiny here" — v3.0 makes discrete ex-div early exercise on the SHORT call the whole point — MEDIUM: delta math won't capture the discrete assignment jump; the event-based check (alerts.py:422-502) must carry it, not q.`

---

## Category 7 — Order construction

- `[ORDER] schwab_api.py:559-654 — build_single_leg_order / build_net_order / build_roll_order all hardcode assetType "OPTION" (572, 601-603, 645-652); INSTRUCTION (executor.py:105-110) maps only 4 option actions; VALID_ACTIONS (executor.py:27) has no share action — v3.0 needs a new equity ORDER path — risk: reusing the option builders for shares sends a malformed order; today shares are booked/reconciled but the app has NEVER transmitted an equity order.`
- `[ORDER] schwab_api.py:607-654 + executor.py:2760 (build_roll_order) — the atomic two-leg roll = BUY_TO_CLOSE old short + SELL_TO_OPEN new short, one NET ticket, base leg never referenced — CONFIRMED unchanged for a shares base — UNKNOWN: whether Schwab needs a covered-strategy tag / different approval when shares (not a LEAP) cover the short; ROLL_COMPLEX_STRATEGY_TYPE is a LIVE_VERIFY constant (schwab_api.py:623-627). VERIFY: previewOrder a covered-call roll with shares held.`
- `[ORDER] schwab_api.py — equity share-purchase fields are ALL UNKNOWN: orderType (LIMIT/MARKET for equity), instruction ("BUY"/"SELL" — code only uses BUY_TO_OPEN etc.), instrument.assetType "EQUITY" as an ORDER field (only appears in READ parsing), quantity semantics (shares vs option ×100), session/duration/orderStrategyType reuse — VERIFY: build a share order and call preview_order (schwab_api.py:388-397) against a live account, capture the accepted JSON, BEFORE any place_order. Do NOT guess field names.`
- `[ORDER] executor.py:514-517 / reconcile.py:256-259 / transaction_ingest.py:126,159 — EQUITY appears ONLY in adjustment/reconcile/ingest READ paths — v3.0 must add the missing equity ORDER path with FROZEN_BLOCKED gating (executor.py:36-37) — risk: omitting freeze gating lets share risk be added to an unverified (frozen) position.`

**Reconciliation hotfix gate still applies: no live submission is enabled by this
migration.** All new order paths are constructed and previewed only.

---

## Category 8 — Reconciliation & data-correction friction (owner pain point)

**Framing.** The correction machinery is ~80% built and tested. The migration closes
three real gaps; everything else is consolidation.

### Existing append-only correction machinery to build on (already tested)
`adjustment` + `linked_diff_id` (`executor.py:548`); `adopt_broker_trade`
(`executor.py:635`) + exact undo via `adoption_reversal`/`reversed_by` excluded from
replay (`executor.py:1400`; `logging_handler.py:690`); `resolve_expiry`
(`executor.py:594`); `record_manual_roll` (`executor.py:806`); `position_rebuild`
(`executor.py:870`); append-only dedupe ledger `record_ingested`
(`transaction_ingest.py:444`); diff resolution/ack (`reconcile.mark_diff_resolved:722`,
`ack_diff:734`). Tests: `test_adoption_reverse.py`, `test_transaction_ingest.py`,
`test_reconcile.py`, `test_trust_derive.py`. This already maps closely onto the
prompt's requested `MANUAL_TRADE_INGEST` (adopt), `ACKNOWLEDGED_DIVERGENCE` (ack),
`FULL_POSITION_RESYNC` (position_rebuild), and compensating-undo requirements.

### The three real gaps
- `[RECON] executor.py:1235 (_apply_txn_edit) — HistoryTab "save transactions" REWRITES strike/premium/execution_price/close_price/net_juice on EXISTING execution records in place (1240-1294), called from save_transactions:1206 — v3.0 requires this route through an APPENDED typed correction event instead — HIGH: this is the ONE genuine in-place mutation of the append-only log; audit trail lost, no broker verification, directly violates the core migration principle. Principal target of Work Item 10c.`
- `[RECON] reconcile.py:335,368-395 — the diff classifier compares existence and signed QUANTITY only (MATCH/QUANTITY_MISMATCH/MISSING/UNEXPECTED/SHORT_STOCK/EXPIRED_WORTHLESS); NO field is compared for cost basis, entry extrinsic, premium, or price — v3.0 needs per-field economic diff classes (COST_BASIS_MISMATCH, EXTRINSIC_MISMATCH) feeding correction events — HIGH: economic divergence reads CLEAN forever — the exact pain the owner reports is structurally invisible.`
- `[RECON] reconcile.py:630 + :310 (reevaluate_freezes reads only report["last"]; diffs re-id diff_001… each run) — a resolved/acked diff's resolution lives only on the current snapshot report — v3.0 must persist resolutions/acks as append-only events keyed to a STABLE diff identity — HIGH: the next reconcile run drops prior acknowledgements; freeze can re-assert, acked non-issues re-surface, and "who resolved what" is lost.`

### Freeze semantics (trigger/lift/enforce) — confirmed intact, do not disturb
- `[RECON] reconcile.py:626 (reevaluate_freezes) SET; :722/:734 LIFT via resolution/ack; executor.py:493 (_enforce_not_frozen → HTTP 409) ENFORCE; also set directly on partial-fill-cancel (executor.py:2209) and leg-imbalance (executor.py:3043) — v3.0 correction events must integrate with this, not bypass it — risk: freeze reason today lives only on mutable position state (needs_review/review); if a position is later rebuilt the "why frozen" provenance can vanish — record the imbalance as a typed divergence event.`
- Note: `execution_gate.py` is the time-of-day settle gate, NOT the reconciliation freeze — unrelated despite the name.

### Determinism & stale-cache risks (bear on the correction-determinism test)
- `[RECON] logging_handler.py:687-694 — ledgers replay deterministically from derived_executions, excluding reversed adoptions — SAFE foundation for corrections.`
- `[RECON] trust_derive.py:420-421 (derive_order_fidelity merge-retain) — graded verdicts persist after order_events roll off the 1000 cap and cannot be overwritten by re-derivation — v3.0: a correction that changes a ticket's economics leaves a STALE fidelity verdict — LOW/MEDIUM: invalidate/re-grade retained verdicts when a correction touches their execution ids.`
- `[RECON] trust_derive.py:462,506 (RECONCILED_CLEAN = NOT_YET_IMPLEMENTED) — the post-fill economic reconciliation grade is a permanent stub blocking graduation (:590) — v3.0 can implement it against the new per-field economic diff — MEDIUM: no automated economic post-fill verification exists today.`
- `[RECON] fill_verify.py:114,166 (verify_live_fills) — detects per-leg price drift beyond PRICE_TOLERANCE but is READ-ONLY, emits no correction — v3.0: surface a proposed price-correction event (like an ingestion proposal) — MEDIUM: a confirmed discrepancy has no in-app remedy; the operator falls back to the mutating _apply_txn_edit path.`

### Discover-but-no-remedy screens (the friction UX)
- `[RECON] frontend — the ONLY real correction UI is PositionTracker.jsx ReviewPanel/DiffRow (:15,:39: resolve-expiry :57, adjustment :62, acknowledge :76). Five other screens announce divergence with no inline remedy: Overview.jsx:53, PositionTracker.jsx:1178, JuiceStand.jsx:502, ProcessRibbon.jsx:413, DataHealth.jsx:475-518. DataHealth.jsx:293 has adopt/undo for out-of-band trades only. TrustScoreboard.jsx:187 shows reconciliation NOT_YET_IMPLEMENTED — v3.0 reconciliation diff view should be the single linked destination from every discovery point — MEDIUM: operator must know to navigate to the position row; this is the reported friction.`
- `[RECON] frontend BurnPanel.jsx:105-118 / Overview.jsx:255 (MODEL DRIFT) — burn-MODEL divergence, NOT broker divergence, and has no remedy — keep visually distinct from broker divergence in the new diff view — LOW: two unrelated "divergence" signals with different remedies.`

**Record-reality principle (Work Item 10d) — current posture.** The reconciler
already refuses to auto-correct ("detects, freezes, suggests; you commit truth,"
`docs/reconciliation.md`) and records adjustments that can encode any state. What is
missing is the explicit rule that a correction encoding an invariant violation
(uncovered short, fragment lot) is **recorded unblocked** while it fires a prominent
flag that **blocks recommendations** until cleared — the recommendation-side block
does not exist as a first-class concept yet (recommendations are only blocked by the
existing freeze, `recommendation_runner` short-circuit, `test_reconcile_freeze_gate.py:99`).

---

## Category 9 — Known open flags intersecting this work

- `[OPEN-FLAG] burn.py:222-228 + indicators.py:561 — day-count: burn/wk = theta_year÷365 (calendar-day) ×7 vs juice/wk = one weekly cycle; pinned by test_burn.test_net_juice_day_count_convention_is_pinned — TOUCHED: shares contribute ZERO burn so the theta/365 term drops out of net_juice_per_week entirely — severity DROPS to LOW/RESOLVED for shares; the flag survives only for the short leg.`
- `[OPEN-FLAG] position_manager.py:155-175 (captured_pct clamp) — clamped [0,100] for accounting; captured_pct_raw + extrinsic_above_entry unclamped for management [CAPTURE_CLAMP_SCOPE] — NOT TOUCHED: lives entirely on the SHORT call (structure-agnostic), which survives unchanged — severity unchanged.`
- `[OPEN-FLAG] position_manager.py:251 + indicators.py:584-601 — q=0 is the DEFAULT at Greek sites but real callers thread q (alerts.py:197, portfolio_risk.py:81/91, position_manager.py:273/282, option_chain.py:360, leap_policy.py:127) — SEVERITY CHANGES: the R3(b) concern (q=0 over-states LONG LEAP coverage) DISAPPEARS with no LEAP; q now matters ONLY for short calls on dividend payers, where it drives the ex-div assignment-risk path (position_manager.py:214-233) — risk: any short-call Greek site silently keeping q=0 UNDER-triggers the early-assignment-before-dividend warning — the exact case shares make MORE likely.`
- `[OPEN-FLAG] indicators.py:359-410 (parabolic_sar seed path-dependence) — trend seeded from the first two bars then carried recursively — NOT TOUCHED: SAR runs on the underlying's daily history, independent of base-leg structure — severity unchanged (latent, orthogonal).`

---

## Provably-untouched surfaces (regime / verdict / gate Levels 1–4)

- `compose_verdict` (`scan_verdict.py:62`) is PURE over `(regime_color, symbol_color,
  base_stage, inst_flow)` — no burn, leap, or position_type reference. Worst-signal-wins.
- The **XLK July-6 regression** (TOPPING × ACCUMULATING → BLOCKED,
  `test_scan_verdict.py:20,45,74`, plus topping-distribution BLOCKED regardless of
  regime `:106-111`, and RED-flips-READY `:116-121`) keys off none of the migrated
  surfaces. **It must remain byte-identical.**
- `structure_classifier.py` (`BaseStage`/`InstFlow`) is a pure per-symbol OHLCV read
  — despite "Base" in the name it never touches the base leg and needs no gating.
- No `position_type` / `SHARES` / `LEAP_PMCC` token exists in the codebase today —
  the discriminator is greenfield; nothing silently pre-branches on it.

Do NOT touch: `compose_verdict`, `regime_genius.py`, RS computation, kill switches,
whipsaw guard, ATR strike selection, entry-gate Levels 1–4, order
lifecycle/reconciliation/freeze/hotfix.

---

## Constants: provenance & recalibration flags (do NOT retune)

- **Juice adequacy floor (Work Item 7).** Existing gross floor is
  `weekly_yield_target_pct` ≈ 1.875%/wk = `CYCLE_RETURN_MIN/CYCLE_WEEKS_MAX×100`
  (`account_gate.py:149-156`) — this denominator is **LEAP cost**. v3.0 keeps the
  number but the denominator becomes **deployed share capital** (`spot×shares`).
  Per the prompt, do not change the number — **log adequacy against the new share
  denominator to the rejection-reason log** (`scan_rejection_log.py`) for
  recalibration. Flag: `PROPOSED_DEFAULT` juice floor 1.5%/wk, NEW denominator.
- **Dividend early-assignment WARN** (`PROPOSED_DEFAULT`): flag when ex-div ∈
  short-call tenor AND short extrinsic < expected dividend → WARN + recommend
  roll/adjust (not a hard block). Path already exists (`alerts.py:472-485`);
  promote an entry-gate sibling at `account_gate.py:362`.
- **Round-lot / SIZE-BLOCKED** (`PROPOSED_DEFAULT`): round lots only; block any
  underlying whose 100-share lot exceeds the per-position cap (no coded per-position
  dollar cap exists — only book-wide `MAX_DEPLOYED_CAPITAL=38000`, `config.py:950`).
- **Schema** (`PROPOSED_DEFAULT`): bump to **v20** (not v13).

---

## Test surface the migration must add (offline, fixture-driven)

Full SHARES lifecycle (entry-gate → weekly roll → defensive roll-down → 75% buyback →
assignment/called-away exit → kill-switch exit), asserting ledger events + coded exit
reasons. Net-juice composition: burn term provably absent for SHARES, present for
LEGACY. Dividend: ex-div in tenor with extrinsic < dividend → WARN; extrinsic >
dividend → no flag. Round-lot: high-priced lot > cap → SIZE-BLOCKED. Fragment: 150
shares → 1 coverable contract + fragment flag, never 2. **XLK July-6 regression:
still BLOCKED, byte-identical.** Schema v19→v20 read test: legacy records render,
non-actionable, history hashes/counts unchanged. Correction determinism: apply
correction → all derived **ledgers** recompute; replay from genesis identical (note
the position-holding caveat in Category 5 — the test as written is satisfiable for
ledgers, not for `state["positions"]` without additional work). Compensating-event,
adopt-broker (MANUAL_TRADE_INGEST + freeze lifts; keep-app → ACKNOWLEDGED_DIVERGENCE),
and record-reality (uncovered short recorded, NAKED flag fires, recommendation blocked)
fixtures.

**Known gap to fill regardless:** payback has zero coverage for shares-only cycles or
called-away closes (`test_payouts.py`, `test_leap_lifecycle.py`).

---

## Open questions for the owner (before Phase 2)

1. **Position-holding replay (Category 5 / TL;DR #2).** The correction-determinism
   acceptance test assumes full replay from genesis. Ledgers replay; positions are
   imperatively mutated. Do we (a) scope corrections to operate on derived ledgers,
   or (b) build position-holding replay? This shapes the whole correction layer.
2. **`_apply_txn_edit` retirement (`executor.py:1235`).** Confirm the HistoryTab
   edit table should be re-pointed at appended correction events (with an event
   preview before commit), removing the only in-place mutation.
3. **Reconciliation doc conflict.** `docs/reconciliation.md` says "no
   adopt-external-trade flow," but `adopt_broker_trade` + out-of-band `proposals`
   already exist. Confirm the doc should be updated to match the migration's
   owner-does-out-of-band-ToS-trades reality.
4. **Coverage ratio for shares.** With burn ≡ 0 the juice/burn coverage concept is
   undefined. Retire the coverage card for SHARES, or redefine it (e.g. juice vs.
   capital-at-risk)? (Number unchanged either way pending owner direction.)
5. **Schwab equity/dividend field verification (UNKNOWNs).** Approve a read-only
   `previewOrder` + `fundamental`/`OVERVIEW` capture pass against the live account
   to resolve the equity-order and ex-div field names before Phase 2 wires them.

---

## Acceptance (Phase 0)

This report is delivered for owner review. **No code changed. No live submission
enabled.** Implementation (Phase 2) begins only on explicit approval. All citations
are `file:line` against `cac5c25`.
