> **Implementation status (post-audit).** All three phases are implemented on this
> branch, shadow-only, 973 backend tests passing + frontend building:
> - **P1** — `indicators.rs_line/rs_ema/rs_ema_slope`; `rs_state.py` (four-state
>   collapse + the gated `turning_watch_reason`); `scan_score.py` (0–10 composite);
>   wired into `metrics/scorecard.score_ticker`; Fixture C (`turning_recovery` +
>   `_sector`) → TURNING and Fixture A sector companion → FADING.
> - **P2** — RS + JUICE/WK columns, drawer IVR + RS-vs-SPY + binding-constraint-first,
>   IVR sourced from the existing `iv_history` store.
> - **P3** — SCORE column + verdict-tier default sort; `scan_rejection_log.py`
>   (append-only derived store) wired into the nightly maintenance sweep;
>   `GET /api/scan/rejection-stats`.
>
> The DO-NOT list below held: no change to `compose_verdict`, the WATCH/CAUTION
> ordering, or the Level-2 RS1M choice; every new threshold is `PROPOSED_DEFAULT`.
> The Q2 (RS1M) and Q3 (verdict ordering) items remain audit-and-document only,
> awaiting a calibration decision.

# Phase 0 Audit — Two-Speed RS / SCORE Shadow / Income Column / Calibration Plumbing

**Scope:** the remaining delta on top of the shipped scan restructure. Audit only —
no code changed in this pass. Every citation is `file:line` against the branch
`claude/two-speed-rs-shadow-sh4vl0`. All new thresholds proposed below are
`PROPOSED_DEFAULT`.

Anchor facts confirmed as shipped:
- `compose_verdict` is the single canonical verdict, consumed by both the table
  (`metrics/scorecard.py:463`) and `/api/scan/ready` (`app.py:167`).
- `structure_classifier.classify` / `classify_symbol` are pure, ETF-identical,
  per-axis `INSUFFICIENT_DATA` (`structure_classifier.py:362,374`).
- `symbol_genius.compute` fourth light = `light_sma_slow_vs_slower`
  (`symbol_genius.py:96`); regime path untouched.
- Table today = `SYMBOL | SYM | BASE | INST | VERDICT` (`Scorecard.jsx:46-68`).
- Dwell shadow-log + SYM history store already exist as the derived-telemetry
  template to copy (`symbol_genius_history.py`, wired at `maintenance.py:198-204`).

---

## Q1 — RS computation, EMA-based ToS definition, 21-day slope, kill_switch boundary

**Where RS computes today.** There is one RS core:
`indicators.relative_strength(df, bench, lookback)` (`indicators.py:192-214`). It is
a **two-point percent change of the price ratio**:
`ratio = symbol_close / bench_close`, then `(ratio_now / ratio_then − 1) × 100`
(`indicators.py:204-214`). `rs3m` (63-bar) and `rs1m` (21-bar) are thin lookback
wrappers (`indicators.py:217-226`; lookbacks at `config.py:294-295`).

Call sites:
- vs SPY / vs Sector on each scan row: `screening.py:296-303` (`rs3m_vs_spy`,
  `rs3m_vs_sector`, `rs1m_vs_spy`, `rs1m_vs_sector`).
- Sector gate: `screening.py:244-245` (`rs1m`, `rs3m` per ETF).
- Scorecard row metrics: `metrics/scorecard.py:213-215`.
- Kill switch: `kill_switch.py:40-47`.

**vs-Sector approximation flag.** The "vs-Sector-as-difference-of-vs-SPY"
approximation the prompt references is **NOT in the codebase** — it was explicitly
rejected. Both the scorecard (`metrics/scorecard.py:207-210`) and the kill switch
(`kill_switch.py:19-22`) document that vs-Sector is the **DIRECT**
`rs3m(stock, sector_etf)` over the same 63-day lookback, *not* a vs-SPY difference.
Sector-ETF rows correctly null it out to avoid a tautological ~0
(`metrics/scorecard.py:405-408`, `screening.py:297-298`). **Finding:** there is no
approximation flag to interact with; the two-speed RS module reads the same DIRECT
ratio inputs, so it inherits no approximation debt.

**EMA-based ToS RS3M Momentum feasibility.** The current RS is a *point-to-point*
figure — it reads two ratio samples and ignores the path between them. ToS "RS3M
Momentum" is the **EMA of the RS line** and its slope. Neither the RS *line series*
nor an EMA-of-a-line exists today; `indicators.ema` (`indicators.py:252-263`) emits
only the EMA of **close**, not of an arbitrary series. So porting ToS requires a
**new pure function** that (a) builds the aligned ratio series
`symbol_close / bench_close`, (b) takes its EMA, (c) reports level (sign of
EMA vs 0-baseline / vs its own history) and 21-day slope. This is a parallel pure
function alongside `relative_strength`, feasible and self-contained; it must not
replace `rs3m`/`rs1m` (kill switch + ranking depend on the exact current figure).

**Where a 21-day slope function lives.** No EMA-line-slope helper exists. Two slope
precedents to model on:
- `metrics/scorecard.py:69-76` `ma50_slope` — value-difference slope
  (`series[-1] − series[-1-lookback]`), the simplest "up/down" read; direct fit for
  the RS-EMA slope sign.
- `structure_classifier.py:120-133` `trend_slope_pct` — least-squares fit as total
  % change; more robust to a single noisy endpoint.
Recommendation: the RS-slope helper lives **in `indicators.py`** (pure, no deps)
next to `relative_strength`, using the value-difference form over the RS-EMA series
(cheap, matches "21-day EMA slope"); the four-state collapse (RISING/FADING/
TURNING/FALLING) is a pure function keyed off `sign(level)` × `sign(slope)`.

**kill_switch boundary — read-only, confirmed.** The kill switch consumes
`indicators.rs3m` only (`kill_switch.py:40-47`) and its rule is a pure function over
those two inputs (`kill_switch.py:53-75`). The two-speed RS work adds a *new* helper
and never touches `rs3m`/`rs1m` or `kill_switch.py`. **No changes to kill_switch.**

---

## Q2 — Level 2 veto divergence (RS1M vs SPY vs. design's RS3M vs SPY)

**What is implemented, where.** Level 2 blocks on `rs1m` (1-month) vs SPY negative,
not `rs3m`:
- Sector computation: `screening.py:242-266` — `strong` keys off
  `rs1m > SECTOR_RS1M_MIN` (`:248`); the deteriorating veto fires on
  `rs1m is not None and rs1m < 0` (`:260-261`), breadth collapse (`:262`), or ETF
  `DISTRIBUTING` (`:264`).
- Gate Level 2 checks: `screening.py:460-467` — "Sector RS1M vs SPY not negative".

**Deliberate faster-read choice — yes.** `screening.py:242-247` documents it
explicitly: "RS3M vs SPY is a laggy 3-month figure that keeps a rolled-over sector
'strong' for weeks after it turns down, so it is kept for DISPLAY only — the gate
keys off `rs1m`." `rs3m` is still computed and surfaced (`screening.py:245,272`) but
does not gate. So the divergence from the original design spec (RS3M vs SPY) is
intentional, not a bug.

**Whipsaw exposure.** RS1M (21-bar) is ~3× more reactive than RS3M (63-bar). A
sector that dips one month then recovers will flip the veto on/off — exactly the
whipsaw the two-speed RS *slope* read is designed to disambiguate (a negative-level
sector with an up-slope is `TURNING`/recovering, not deteriorating). This is the
strongest argument that RS1M-vs-slope is a **calibration** question, not a code
change: the rejection log (Q6) will record, per blocked row, whether the Level-2
RS1M veto was the binding constraint, and the RS-state shadow can show how often a
`TURNING` sector was vetoed by a still-negative RS1M level.

**Recommendation:** document only, this phase. Do **not** change the RS1M choice
(explicit DO-NOT). Flag it as a first-class calibration item (Q9) — the empirical
question is "how many entries did RS1M-negative block that a slope-aware read would
have let through as recovering."

---

## Q3 — Verdict ordering semantics (READY < CAUTION < WATCH < BLOCKED)

**Implemented ordering** (WATCH strictly worse than CAUTION):
- Backend severity ladder: `scan_verdict.py:47` `_SEVERITY = {READY:0, CAUTION:1,
  WATCH:2, BLOCKED:3}`; worst wins at `scan_verdict.py:78`.
- Genius-color placement drives it: YELLOW → WATCH (`scan_verdict.py:53`), so a
  yellow regime/SYM lands at WATCH, worse than a structure-only CAUTION.
- Frontend mirror: `Scorecard.jsx:39` `VERDICT_ORDER`, `:38` `VERDICT_STATUS`,
  `:255` `FILTERS`.

**Where consumed:**
- Table default sort + column sort: `Scorecard.jsx:262` (default `verdict` asc),
  `sortRows` `:95-109`, `sortVal` `:65`.
- Verdict filter chips: `Scorecard.jsx:255,327-331`.
- Row tone (BLOCKED/CAUTION tint): `Scorecard.jsx:154-163`.
- `/api/scan/ready` membership: keys off `verdict == "READY"` only
  (`app.py:167`) — it does **not** read the CAUTION/WATCH ordering, so a swap does
  not change the shortlist.
- `reasons` are order-independent (`scan_verdict.py:80-82`).

**Design-intent tension.** Original intent: WATCH = promising-but-not-entrable,
CAUTION = entrable-adjacent-degraded — i.e. CAUTION should sort *worse* than WATCH
(the opposite of shipped). A semantic swap would touch only: `_SEVERITY`
(`scan_verdict.py:47`), the color→level map if YELLOW should map to CAUTION not
WATCH (`scan_verdict.py:51-55`), the frontend `VERDICT_ORDER` (`Scorecard.jsx:39`),
and the `test_scan_verdict.py` pins. **Recommendation:** flag as recommendation
only; **no change without approval** (explicit DO-NOT). Note the swap is low-blast-
radius because `/api/scan/ready` is READY-only.

---

## Q4 — Column & sort wiring; one-call-per-row

**Where the column machinery lives** (`Scorecard.jsx`):
- Enum→label/tone maps: `:20-42` (`BASE_LABELS`/`BASE_TONE`/`INST_*`/
  `VERDICT_STATUS`/`VERDICT_ORDER`/`BASE_ORDER`/`INST_ORDER`/`SYM_ORDER`).
- Column defs (declarative `{key,label,render,sortVal}`): `:46-68` `COLUMNS`.
- Column tooltips: `:70-80` `COLUMN_HELP`.
- Generic sort (numeric via `sortVal`, else string): `:95-109` `sortRows`.
- Default sort: `:262` `{key:"verdict", dir:"asc"}`.
- Header render + click-to-sort: `:389-402`; body cell render: `:164-202`.

**Insertion points for RS, JUICE/WK, SCORE:** all three are new `COLUMNS` entries
between `inst_flow` (`:60-63`) and `verdict` (`:64-67`), each with a `sortVal`
(RS via an `RS_ORDER` map like `SYM_ORDER`; JUICE/WK and SCORE numeric). Target
final order `SYMBOL | SYM | BASE | INST | RS | JUICE/WK | SCORE | VERDICT`. Add
label/tone/order maps for the RS four states next to the existing enum maps
(`:20-42`) and tooltips at `:70-80`. Because every cell reads from the already-
assembled `row` object, no data wiring beyond new `row.*` fields is needed.

**Default sort spec** ("verdict tier, then SCORE desc within tier; JUICE/WK desc
until SCORE ships"): the current single-column sort (`:95-109`) sorts one key.
This needs a small **composite comparator** (tier via `VERDICT_ORDER`, then
`-score` / `-net_juice`) as the *default*, while per-column click-sort stays
single-key. Cleanest as a dedicated default-comparator path rather than overloading
`sortRows`.

**One classifier/genius call per row — honest current state.** Within
`score_ticker` the classifier and genius are each called **once**:
`symbol_genius.compute(df)` (`metrics/scorecard.py:461`),
`structure_classifier.classify(df)` (`:462`). RS/JUICE/SCORE columns must read
**already-computed** row fields and add **zero** new per-row `classify`/`compute`
calls — that invariant is preserved as long as the SCORE function (Q5) and the RS
state consume `row`/`sig`, not fresh frames. Caveat for accuracy: the *scan sweep*
already computes structure more than once across gate + scorecard — `entry_gate`
independently calls `classify_symbol` at Level 3.5 (`screening.py:508`) and the
scorecard calls `classify` again (`:462`). That pre-existing duplication is out of
scope; the requirement is only that the **new columns add none**.

---

## Q5 — SCORE boundary (proof it cannot leak into gate/ready/sizing/recommendation)

**Where SCORE should compute.** Proposed: a **pure function over the already-
computed row inputs** (sector strength magnitude, base maturity from
`cls["signals"]["base_count"]`, `inst_flow`, ATR posture
`sig["atr_posture"]`, distance from MA21 `row["pct_above_ma21"]`, net juice
`row["net_juice_weekly_pct"]`, RS state), invoked inside `score_ticker`
(`metrics/scorecard.py:461-471`) **after** the metrics + composed verdict are
already built, writing only `row["score"]` (+ optional `row["score_parts"]`). No
new `data_handler.get_daily`, no provider calls — same contract as the rest of
`metrics/scorecard.py:8-11`.

**Boundary proof (nothing consumes `row["score"]`):**
- `compose_verdict` takes only `(regime_color, symbol_color, base_stage,
  inst_flow)` (`scan_verdict.py:62-63`) — no score parameter exists; adding a row
  field cannot reach it.
- `/api/scan/ready` selects on `verdict == "READY"` (`app.py:167`) then sorts on
  net juice (`app.py:224-226`) — never reads score.
- Sizing advisory reads only sector `strong`/`deteriorating`
  (`account_gate.py:132-133`) — never score.
- Recommendation/refresh pipeline reads `suitability` (the demoted GO/CAUTION/
  AVOID lens, `metrics/scorecard.py:474-487`), not `verdict` or a score.
- Entry gate (`screening.py:420-547`) computes structure/lights/spot directly and
  never reads scorecard rows.
So SCORE is display + log only by construction: it is written after every gate
decision and read by nobody in the decision path. The audit must (and does) cite
this boundary; the implementation should add a `test_scorecard.py` assertion that
score is absent from the `/api/scan/ready` selection inputs.

---

## Q6 — Rejection-reason logging path (append-only, derived-style)

**Where scan runs assemble results.** `metrics/scorecard._compute_scorecard`
(`metrics/scorecard.py:491-528`) builds the full row list; the full-universe sweep
is memoized (`:550-552`). Each row already carries everything the log needs:
`verdict` (`:470`), `verdict_reasons` (`:471`), `net_juice_weekly_pct` (`:441`),
`sym`/`base_stage`/`inst_flow` (`:465-468`), plus the gate (`:519`) for the binding
constraint.

**Binding-constraint extraction is a READ, not a re-evaluation.** The gate already
computes stop-on-first-fail: `entry_gate` returns `cleared_level`
(`screening.py:538-547`), and the first-failing leg is simply
`cleared_level + 1` (the pattern already used at `metrics/scorecard.py:357`). For a
non-READY row the binding constraint is the single first-failing check — read off
the existing `gate["levels"]` pass flags / `cleared_level`, no recompute. For rows
whose verdict is driven by the composed inputs rather than the gate,
`composed["reasons"]` (`scan_verdict.py:80-82`) already names the worst input(s);
the binding constraint is `reasons[0]`.

**Storage recommendation — a separate derived artifact, NOT state.json.** Follow
the `symbol_genius_history.py` template exactly: a standalone JSON store under
`config.DATA_DIR`, append-only, one record per symbol per scan/day, NOT in
state.json and NOT rebuilt by `recompute_derived` (`symbol_genius_history.py:8-14`).
This preserves the single-writer state.json discipline (execution log stays the
sole source of truth). Wire the nightly append in `maintenance.py` right after the
SYM shadow-log block (`maintenance.py:198-204`), and/or append per background scan
in `screening._run_background_scan` (`screening.py:89-96`). Record per symbol:
`date, verdict, binding_constraint, score, rs_state, net_juice_weekly_pct`
(matches the prompt's field list). Drawer for non-READY rows leads with the binding
constraint — the drawer already renders `verdict_reasons` first
(`Scorecard.jsx:208-214`); reorder so the binding constraint heads that list.

---

## Q7 — IVR sourcing (build vs descope)

**IVR is already sourced — no new provider calls needed.** `iv_history.iv_rank`
(`iv_history.py:92-118`) returns `iv_rank` + `iv_percentile` from a local trailing-
year store (`DATA_DIR/iv_history.json`), accrued one point/day from IVs the app
already computes: the option-chain view records weekly ATM IV
(`option_chain.py:584`), nightly maintenance records held names. The scan path
**already reads it** — `screening._stock_row` pulls
`iv_history.iv_rank(ticker)["iv_percentile"]` for the volatility veto
(`screening.py:309-312`). Minimum sample gate `_MIN_POINTS = 20`
(`iv_history.py:29,112`) returns `None` below that (correct — no misleading number).

**Recommendation: BUILD (cheap).** Add `iv_rank`/`iv_percentile` to the scorecard
row (read once in `score_ticker`, same place the veto path reads it) and render in
the drawer next to Juice/wk (`Scorecard.jsx:227-240`). No new Schwab fields, no
guess at provider data. Caveat to surface: coverage is only as deep as the accrued
history — names with < 20 stored points show `—`, and a fresh universe name has
none until it has been option-chain-viewed or maintenance-swept. That is a data-
maturity limitation, not a reason to descope.

---

## Q8 — Fixtures

**A/B coverage as shipped** (`test_structure_fixtures.py`, builder
`fixtures/structure/build_fixtures.py`):
- Fixture A `topping_distribution` — TOPPING × DISTRIBUTING → BLOCKED, while Symbol
  Genius reads 4/4 GREEN (`test_structure_fixtures.py:52-70`). The "lights alone are
  insufficient" crux.
- Fixture B `early_advance_accum` — EARLY_ADVANCE × ACCUMULATING → READY, green SYM,
  BLOCKED under RED regime via `compose_verdict` (`test_structure_fixtures.py:76-102`).

**Fixture C (NVDA shape) needs:** EARLY_ADVANCE + EARLY_INTEREST + RS3M-vs-Sector
negative with RS slope up ⇒ RS state `TURNING`, VERDICT non-READY with binding
constraint = Level 3, TURNING annotation present. This requires **RS-line inputs
(stock + sector series)**, which the structure fixtures don't currently carry — the
builder only synthesizes a single symbol's OHLCV (`build_fixtures.py:36-45`).
**Recommendation: synthesize, don't use live NVDA cache.** A synthetic pair
(stock frame + sector frame) is cleaner and deterministic — it can be tuned so
EARLY_ADVANCE + EARLY_INTEREST hold on the stock while the stock/sector ratio EMA is
below zero but rising (the `TURNING` state), which a real ~276-bar NVDA cache cannot
be pinned to reproducibly. Add both frames to `build_fixtures.py` and pin in
`test_structure_fixtures.py`. **Fixture A extension:** add an RS-state assertion —
build/attach a sector frame for `topping_distribution` such that its RS state
evaluates to `FADING` (level ≥ 0, slope down — distribution-into-strength). This is
additive to the existing A pins.

---

## Q9 — Calibration-capture gaps (what one future pass needs)

Existing shadow/telemetry logs today:
- Regime history (`regime_history`, `maintenance.py:184-190`).
- SYM flip shadow-log (`symbol_genius_history`, `maintenance.py:198-204`).
- IV history (`iv_history`, `option_chain.py:584`).
- Burn marks (`burn_marks`, `maintenance.py:210-214`).

**What a single calibration pass must jointly evaluate, and the gaps:**

| Calibration target | Needs | Logged today? |
|---|---|---|
| Dwell (SYM/regime yellow hold) | SYM color sequence, flip counts | ✅ `symbol_genius_history` / `regime_history` |
| Level-2 RS1M vs RS3M choice (Q2) | per-row: was Level-2 RS1M the binding constraint; sector RS1M **and** RS3M level + slope | ❌ **gap** — rejection log (Q6) must capture binding constraint; RS-state log must capture sector RS level+slope |
| Structure thresholds (all `PROPOSED_DEFAULT`, `structure_classifier.py:74-114`) | per-row base_stage/inst_flow + the underlying `signals` (slope_pct, base_count, udvr, dist_days…) | ⚠️ **partial** — `classify` returns `signals` (`structure_classifier.py:366-371`) but nothing persists them; rejection/score log should store at least base_stage/inst_flow, ideally the deciding signal |
| SCORE weights (Q3/Q5) | per-row SCORE + its component parts + the eventual outcome | ❌ **gap** — SCORE + `score_parts` must be logged (Q6 record) so weight sensitivity is measurable |
| RS-slope graduation to blocking | per-row RS state (vs Sector + vs SPY), level, slope, over time | ❌ **gap** — the two-speed RS shadow log is new; must persist state + raw level/slope, not just the glyph |
| Gate-too-strict question | per-row binding constraint distribution over time | ❌ **gap** — this is exactly the rejection log (Q6) |

**Signals calibration needs that are NOT logged anywhere today:** (1) the binding
constraint per non-READY row; (2) RS level + 21-day slope (both pairings); (3) the
SCORE and its parts; (4) the structure `signals` that drove the stage. All four are
delivered by the Q6 rejection log + the RS/SCORE shadow fields if those records
store raw values (not just labels). **Recommendation:** make every shadow record
store the raw numeric (RS level+slope, score+parts, deciding signal), never only the
collapsed state/word — a label-only log cannot answer a threshold-graduation
question later.

---

## DO-NOT compliance (confirmed for the implementation phases)

- No rebuild/refactor of `structure_classifier`, `symbol_genius`, `scan_verdict`,
  or the Level 3.5 / Level 2 gate work — RS/SCORE are additive pure functions +
  columns.
- RS slope and SCORE never enter `compose_verdict`/blocking/kill_switch/sizing/
  recommendations (Q1, Q5 boundary proofs).
- No second verdict — `TURNING` annotation (if gated in) appends a WATCH **reason
  string** to `verdict_reasons` for already-non-READY rows only, via the canonical
  verdict's reasons list (`scan_verdict.py:80-82` / `Scorecard.jsx:211`).
- WATCH/CAUTION ordering and Level-2 RS1M choice: audit-and-document only
  (Q2, Q3).
- No touch to executor / kill_switch / circuit_breaker / order paths / regime
  engine constants.
- No Schwab field guessing; all new logic pure, fixture-tested offline.
- Rejection log is append-only derived-style under `DATA_DIR`, never state.json
  (Q6).
- Every new threshold tagged `PROPOSED_DEFAULT`.

---

## Recommended implementation split (hard stop here — no code this pass)

**P1 — pure functions + fixtures (no UI, no logging):**
1. `indicators`: new RS-line-EMA + 21-day-slope helper (parallel to
   `relative_strength`, `indicators.py:192`); pure four-state collapse
   RISING/FADING/TURNING/FALLING.
2. SCORE 0–10 pure function over already-computed row inputs (lands in
   `metrics/scorecard.py`, read-only over `row`/`sig`).
3. Fixture C (synthetic stock+sector pair → EARLY_ADVANCE/EARLY_INTEREST/TURNING)
   and Fixture A RS-state extension (→ FADING), pinned in
   `test_structure_fixtures.py`.

**P2 — table columns + drawer:**
4. RS column (vs Sector primary, vs SPY in drawer), JUICE/WK column
   (`row.net_juice_weekly_pct`, existing number), composite default sort
   (tier → SCORE desc, JUICE/WK desc until SCORE ships) — `Scorecard.jsx:46-68,
   95-109, 262`.
5. Drawer: IVR readout (`iv_history.iv_rank`, already sourced) + binding-constraint-
   first ordering of `verdict_reasons`; optional `TURNING` WATCH annotation gated
   on this audit (informational only, non-READY rows).

**P3 — SCORE shadow column + rejection logging + calibration capture:**
6. SCORE column (shadow, zero authority; boundary test).
7. Rejection log: new `DATA_DIR` derived store (copy `symbol_genius_history.py`),
   wired at `maintenance.py:198-204` and/or `screening._run_background_scan`;
   record `date, verdict, binding_constraint, score, rs_state,
   net_juice_weekly_pct` + raw RS level/slope + score parts (Q9).
