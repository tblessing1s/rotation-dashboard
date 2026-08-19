# AUDIT — Entry Gate Recalibration (Phase 0)

Scope: SYM vote rule, SAR demotion, Level-4 relaxation, rejection-reason log.
**Findings only. No implementation code was written, modified, or scaffolded.**

All line citations are against the working tree at the time of this audit.

---

## 0. Material findings first

| # | Finding | Impact on the spec |
|---|---|---|
| **F1** | The prompt describes Level 3 as using Symbol Genius (`SMA50>SMA200`). The code uses a **different four-light engine** at Level 3 — `stock_lights` / `genius_lights`, whose 2nd light is `EMA21>SMA50`. Symbol Genius is a **separate** signal that never reaches the entry gate. | The spec must name which engine it retunes. They are two different sets of lights. |
| **F2** | The 4-of-4 vote rule is **one shared function**, `stock_lights.verdict()`, called by *both* symbol-level engines. | A single edit changes Level 3 *and* the SYM signal together. That is probably intended — but it must be a stated decision, not a side effect. |
| **F3** | Level 3 has **three vetoes independent of the light vote**, one of which is an ATR-expansion veto. Any tripped veto forces RED regardless of green count. | The mandatory-core rule in §1 of the spec is not the only path to RED. And veto #2 partially defeats the Level-4 relaxation in §2. |
| **F4** | The Level-4 ATR veto and the SCORE's ATR component are **already decoupled** — separate functions, separate constants. | The spec's "if the audit found shared code, split it" is a no-op. Nothing to split. |
| **F5** | A rejection-reason log **already exists** (`scan_rejection_log.py`), but it is nightly-only, keyed per symbol per **day**, last-write-wins — not per-scan-run append-only. | §3 is an *extension* with a storage-semantics change, not a new build. Needs an explicit decision. |
| **F6** | `gate` is `None` for any ticker with no resolved sector ETF, so **no gate blocks are folded** into that row's verdict. | Pre-existing hole in verdict completeness. Out of scope, but the dual-compute in §4 will surface it. |

---

## 1. Where the gate is computed

Single entry point: **`backend/screening.py:535`** — `entry_gate(ticker, profile=None)`.

| Level | Location | Pass condition |
|---|---|---|
| 1 — Market regime | `screening.py:557-575` | `reg["published_regime"] == "green"`. The four lights are shown as sub-checks but do **not** gate — the published regime does (`screening.py:573`). |
| 2 — Sector not deteriorating | `screening.py:584-594` | Three veto checks: RS1M vs SPY not negative, breadth ≥ `SECTOR_BREADTH_COLLAPSE`, sector not `DISTRIBUTING`. Fail-open on missing data. |
| 3 — Stock lights green | `screening.py:602-621` | `row["verdict"] == stock_lights.GREEN`, where `row` comes from `_stock_row` (`screening.py:329`) → `stock_lights.evaluate` → `stock_lights.compute` (`stock_lights.py:157`). |
| 3.5 — Structure entrable | `screening.py:632-643` | `structure_classifier.structure_entrability(...)` in `(READY, CAUTION)`. |
| 4 — Right spot | `screening.py:645-662` | `row["right_spot"]["pass"]`, produced by `stock_lights.right_spot` (`stock_lights.py:132`). |
| 5 — Account overlay | `backend/account_gate.py:295` (`evaluate`) | Evaluated **separately**, never inside `entry_gate`. Layered in `app.py:236` for `/api/scan/ready`. |

**Stop-on-first-fail:** `screening.py:664-671`. `cleared` is the highest *contiguous* pass from Level 1; the loop `break`s on the first failure. Note that all levels 1–4 are **already fully evaluated** before this loop runs — the levels list is built unconditionally. Only the *cleared_level* derivation stops early. This matters for §3 of the spec (see §5 below).

**Verdict fold (worst-signal-wins):**
- Signal composition: `backend/scan_verdict.py:62` — `compose_verdict(regime_color, symbol_color, base_stage, inst_flow)`. Severity ladder at `scan_verdict.py:44-52`; `max()` over the three input levels at `scan_verdict.py:79`.
- Gate fold: `backend/scan_triggers.py:506` — `compose_row_verdict(composed, blocks)`. Takes `max` of the signal severity and every block's trigger severity (`scan_triggers.py:521-523`).
- Blocks extracted from the gate: `scan_triggers.py:235` — `gate_blocks(gate, account_gate, ext_context)`. **Explicitly excludes L1, the L3 light vote, and L3.5** (`scan_triggers.py:240-241`) because the signal composition already owns them. It pulls L2 deteriorating reasons (`:250-256`), **L3 tripped vetoes** (`:258-265`), **L4 right-spot failing checks** (`:267-279`), and L5 blocking failures (`:281-287`).
- Wired together per row at `backend/metrics/scorecard.py:565-601`.

---

## 2. SYM light computation and the vote rule

### Two engines, not one

**`backend/symbol_genius.py`** — the SYM column.
- Light keys: `symbol_genius.py:52` → `("close_vs_ma", "structure", "sar", "momentum")`
- Assembled at `symbol_genius.py:84-101`. 4th light is `light_sma_slow_vs_slower(SMA50, SMA200)` (`symbol_genius.py:96`).
- Own param set, deliberately **without** `fast_ma`: `symbol_genius.py:58-71`. Constants `config.py:282` (`SYMBOL_GENIUS_SLOWER_MA = 200`) and `config.py:287` (`SYMBOL_LIGHTS_WARMUP_BARS = 200`).

**`backend/genius_lights.py` (via `stock_lights`)** — Level 3 of the entry gate, and the market regime.
- Light keys: `genius_lights.py:38` → `("close_vs_ma", "fast_vs_slow", "sar", "momentum")`
- 2nd light is `light_fast_vs_slow(EMA21, SMA50)` (`genius_lights.py:83`), **not** SMA50>SMA200.
- `stock_lights.compute` calls `genius_lights.compute(df, params)` at `stock_lights.py:170`.
- Warm-up `config.py:261` (`STOCK_LIGHTS_WARMUP_BARS = 50`).

### The vote rule is a named function, shared

**`backend/stock_lights.py:111`**:

```python
def verdict(greens: int, insufficient: bool, any_veto: bool) -> str:
    if any_veto or insufficient:
        return RED
    if greens >= 4:
        return GREEN
    if greens == 3:
        return YELLOW
    return RED
```

- **Not** a named constant — the `4` and `3` are inline literals at `stock_lights.py:117` and `:119`. There is no `SYM_MIN_GREEN_LIGHTS`-equivalent in `config.py` today.
- **Called by both symbol-level engines:**
  - `stock_lights.compute` → `stock_lights.py:172`
  - `symbol_genius.compute` → `symbol_genius.py:124`, explicitly with `any_veto=False`
- The **market** vote is a *different* function — `genius_lights.vote` (`genius_lights.py:136`), which uses `config.GENIUS_VOTE_GREEN_MIN = 3` (`config.py:190`) and a 2/2→YELLOW rule. The market regime layers dwell on top in `regime_genius.apply_dwell` (`regime_genius.py:98`), `config.GENIUS_YELLOW_DWELL_DAYS = 3` (`config.py:191`).

**Consequence for spec §1:** editing `stock_lights.verdict` retunes **Level 3 of the entry gate *and* the SYM signal in one stroke**, while leaving the market vote untouched. The market-level isolation the spec requires is satisfied for free. The dual effect on Level 3 is not — it needs to be an explicit decision (see Q1 in §8).

### The mandatory-core light exists in both engines

`close_vs_ma` is key #1 in both `LIGHT_KEYS` tuples (`genius_lights.py:38`, `symbol_genius.py:52`) and both are computed by the same `genius_lights.light_close_vs_ma` (`genius_lights.py:77`), against `config.GENIUS_SLOW_MA` (SMA50). So the spec's mandatory-core rule is expressible identically in both.

### Third path to RED the spec does not mention

`stock_lights.evaluate_vetoes` (`stock_lights.py:45-101`) — three vetoes, any one forces RED via the `any_veto` argument:

1. `rs3m_vs_sector` < 0, stocks only (`stock_lights.py:64-77`)
2. `atr_expanding_high_ivr` — `indicators.atr_expanding(df)` **AND** `ivr_percentile >= config.VETO_IVR_PERCENTILE_MIN` (90.0, `config.py:272`) (`stock_lights.py:79-88`)
3. `close_below_ma200` (`stock_lights.py:90-99`)

These apply **only** on the `stock_lights` path (Level 3). `symbol_genius` passes `any_veto=False` hard-coded (`symbol_genius.py:124`), so SYM has no vetoes. Veto #1 is named untouchable by the spec; vetoes #2 and #3 are not mentioned at all.

---

## 3. SAR usage inventory

| Consumer | Location | Touched by this change? |
|---|---|---|
| Implementation — Wilder SAR, full series | `indicators.py:359` (`parabolic_sar`) | **No** |
| Implementation — last bar only | `indicators.py:414` (`parabolic_sar_last`) | **No** |
| Shared light function | `genius_lights.py:89` (`light_sar`) | **No** |
| Market regime lights | `genius_lights.compute_lights:116`, re-exported `regime_genius.py:53,55` | **No** |
| Market regime vote | `genius_lights.vote:136` | **No** — explicitly out of scope |
| Stock lights (entry gate L3) | via `genius_lights.compute` at `stock_lights.py:170` | Vote rule only, via `stock_lights.verdict` |
| Symbol Genius (SYM column) | `symbol_genius.py:99` | Vote rule only, via `stock_lights.verdict` |
| **Kill switch** | `kill_switch.evaluate:90` — **RS-only** (`_rs_pair` → `classify`). No SAR anywhere in `kill_switch.py`. | **No** |
| Circuit breaker | `circuit_breaker.py` — price/MA/operator-line only, no SAR | **No** |

**Confirmed:** changing `stock_lights.verdict` touches no SAR computation and no SAR consumer other than the two symbol-level verdict mappings. Kill-switch isolation (spec test 7) is guaranteed by construction — it never reads SAR.

**Open flag corroborated:** SAR seeding is path-dependent. `indicators.py:383` seeds `sar = low[0] if up else high[0]` from the frame's **first bar**, so the value depends on how much history the frame carries. `stock_lights.py:21-25` and `config.py:257-261` both document this as the reason for a canonical-start warm-up window. The spec's premise here is accurate.

---

## 4. Level 4 ATR check

### The exact test

`backend/stock_lights.py:132-152`, check id `atr_5d_ema`:

```python
_spot_check("atr_5d_ema", momentum,
            momentum is not None and momentum <= config.SPOT_ATR_MOMENTUM_MAX)
```

- `momentum = indicators.atr_momentum(df)` (`stock_lights.py:135`)
- `indicators.atr_momentum` (`indicators.py:144`) = `ATR(window) / ATR_5EMA(window)`, window = `config.ATR_WINDOW` (9-day Wilder). **No lookback parameter.**
- Threshold `config.SPOT_ATR_MOMENTUM_MAX = 1.0` (`config.py:267`), tagged `PROPOSED_DEFAULT`.
- A `None` value **fails conservatively** (`stock_lights.py:137` — the `is not None` guard means unmeasurable → fail). Documented at `stock_lights.py:135-136`.

The other two right-spot checks, unchanged by the spec:
- `atr_pct` ≤ `config.CONSOLIDATION_ATR_PCT_MAX = 5.0` (`config.py:248`), `stock_lights.py:140-141`
- `extension` ≤ `config.SPOT_ATR_EXTENSION_MAX = 1.5` ATRs (`config.py:266`), `stock_lights.py:144-145`

### Entanglement with the ATR score component — **none found**

There are **three independent** ATR-momentum computations with **three independent** thresholds:

| Consumer | Compute fn | Threshold | Authority |
|---|---|---|---|
| Level-4 veto | `indicators.atr_momentum` (`indicators.py:144`), takes a frame | `config.SPOT_ATR_MOMENTUM_MAX = 1.0` (`config.py:267`) | **Blocking** |
| SCORE component | `metrics/scorecard.atr_momentum` (`metrics/scorecard.py:133`), takes scalars from `compute_inputs` | `scan_score.ATR_CONTRACTING = 1.0` / `ATR_EXPANDING = 1.2` (`scan_score.py:76-77`), applied in `_atr_sub` (`scan_score.py:105`) | Shadow (rank only) |
| `suitability` lens | same scalar fn, `metrics/scorecard.py:133` | `metrics/thresholds.ATR_MOMENTUM_MAX = 1.0` (`thresholds.py:41`) | Demoted readout; **but** see below |

**Conclusion:** the spec's conditional ("if the audit found shared code, split it") does not fire. The veto and the score are already independently tunable. Introducing `L4_ATR_EXPANSION_MAX` and pointing `stock_lights.right_spot` at it changes the veto alone.

**Caveat — a fourth ATR consumer that IS blocking:** the Level-3 veto `atr_expanding_high_ivr` (`stock_lights.py:79-88`) uses a *different* expansion test — `indicators.atr_expanding` (`indicators.py:98`): ATR now vs ATR 10 bars ago, boolean. Paired with IVR ≥ 90 it forces the Level-3 verdict to RED. **Relaxing Level 4 will not admit an expanding-ATR name whose IVR is ≥ 90 — Level 3 still kills it.** Given defect (B)'s rationale is "let juice adequacy bind instead of ATR posture", and high IVR is precisely where the juice is richest, this veto is the one most likely to keep blocking exactly the names the change is meant to admit. Not in scope per the spec's §5 untouchables — flagging for a decision (Q3).

**`suitability` is not fully demoted:** `metrics/thresholds.ATR_MOMENTUM_MAX` feeds `compute_verdict` (`metrics/scorecard.py:286`) → `row["suitability"]` (`metrics/scorecard.py:659`), which **is** read by the recommendation pipeline: `recommendation_runner.py:135` filters on `suitability == "GO"`, and `recommendation_engine.py:527-529` blocks entry when it isn't `GO`. So an ATR-expansion CAUTION still suppresses recommendations even after Level 4 is relaxed. In scope only as a blast-radius note (Q4).

---

## 5. Existing rejection / telemetry paths

**`backend/scan_rejection_log.py` already implements most of spec §3.**

- Storage: `LOG_PATH = os.path.join(config.DATA_DIR, "scan_rejection_log.json")` (`scan_rejection_log.py:35`). Standalone store under `DATA_DIR`, **not** `state.json`, **not** rebuilt by `recompute_derived` — documented `scan_rejection_log.py:17-21`. This is the established pattern for scan telemetry, so the spec's "NOT interleaved with trade events in state.json" is already satisfied and needs no new decision.
- Record shape: `_record_from_row` (`scan_rejection_log.py:87-135`). Already persists `verdict`, `binding_constraint`, `binding_level`, `binding_check`, `binding_kind`, `price`, `score` + `score_parts`, `rs_state`/`rs_level`/`rs_slope`, `net_juice_weekly_pct`, `base_stage`, `inst_flow`, **`sym`**, `sector_rs1m`, `iv_rank`, and the whole shadow-floor block.
- Binding extraction: `binding_constraint` (`scan_rejection_log.py:71`) is a **read** of `row["verdict_reasons"][0]`, never a re-evaluation. Ordering guaranteed by `compose_row_verdict`'s sort (`scan_triggers.py:531`), which that function's comment explicitly maintains for this consumer (`scan_triggers.py:513-514, 530`).
- Writer: `record_scan` (`scan_rejection_log.py:208-235`). **Single nightly writer** — `maintenance.py:219`, over the memoized full-universe sweep.
- Reads: `series` (`:140`), `latest_before` (`:145`), `summary` (`:158`). API surface `app.py:929` → `/api/scan/rejection-stats`.
- Retention: `config.SCAN_REJECTION_LOG_DAYS = 180` (`config.py:295`), trimmed at `scan_rejection_log.py:229`.

**Sibling store:** `backend/scan_diff_log.py` — the daily transition log, explicitly mirroring this module's storage discipline (`scan_diff_log.py:13`). Written from `maintenance.py:232-240` using `scan_rejection_log.latest_before()` as yesterday's baseline.

**Shadow juice-floor logger:** `scan_triggers.shadow_floor` (`scan_triggers.py:410-455`) computes the observation; `score_ticker` attaches it as `row["shadow_floor"]` (`metrics/scorecard.py:531`); `_record_from_row` persists five fields from it (`scan_rejection_log.py:126-131`). It is **not** appended to `blocks`, so it carries zero authority — `metrics/scorecard.py:528-530` says so explicitly.

### Gaps against spec §3

| Spec requirement | Current state | Gap |
|---|---|---|
| Record per scan **run** | One record per symbol per **day**; same-day rerun **overwrites** (`scan_rejection_log.py:226-227`) | Semantics change. "Append-only, grows monotonically" (spec test 6) **contradicts** the current last-write-wins design. |
| `first_failing_level` | `binding_level` exists but is the *most decisive* block (worst severity first, then earliest level — `scan_triggers.py:531`), **not** the first failing level | Different quantity. Both are defensible; they are not the same number. |
| `all_level_results` for **every** level | Only the binding is stored | New field. Cheap — `entry_gate` already evaluates all of L1–L4 unconditionally (`screening.py:548-662`); only `cleared_level` stops early (`screening.py:665-671`). L5 is *not* evaluated in `entry_gate` at all — it is a separate call (`account_gate.evaluate`, `account_gate.py:295`) made only in `/api/scan/ready` (`app.py:236`) for rows that already reached READY. Capturing L5 for every scanned name is a **new cost**: it loads state and can resolve live cash (`position_manager.capital_summary` → `account_gate.resolve_operating_cash`). |
| `legacy_verdict` / `proposed_verdict` | Neither exists | New. |
| `sym_lights` breakdown | Only the composite `sym` color | New. Note the spec's field names `{core, sma_cross, sar, roc}` match **Symbol Genius** keys (`close_vs_ma`, `structure`, `sar`, `momentum`), not the Level-3 stock-lights keys (`close_vs_ma`, `fast_vs_slow`, `sar`, `momentum`). |
| `scan_id` | No scan-run identifier exists anywhere | New concept. |
| Schema version field | Absent | New. |

---

## 6. Verdict consumers (blast radius for dual-compute)

**Canonical scan verdict** — `row["verdict"]`, set at `metrics/scorecard.py:595`:

| Consumer | Location | Reads |
|---|---|---|
| `/api/scan/scorecard` | `app.py:126` | full rows |
| `/api/scan/ready` | `app.py:189` | filters `verdict == "READY"` — **the shortlist gate** |
| Scan tab table | `frontend/src/components/Scorecard.jsx:176` (column), `:255-270` (sort), `:339-340` (row tone), `:399-401` (binding drawer) | |
| Ready-to-Enter panel | `frontend/src/components/ReadyToEnter.jsx` | via `/api/scan/ready` |
| Universe health | `universe_health.py:62` | |
| Daily transition diff | `scan_diff.py:52-53, 120, 124` — BENCH→READY / fresh-READY / degrade events, fanned out through the notifier (`maintenance.py:232-245`) | |
| Rejection log | `scan_rejection_log.py:89` | |
| Bench membership | `scan_triggers.is_bench` (`scan_triggers.py:550`), set at `metrics/scorecard.py:601` | |

**`suitability`** (the demoted GO/CAUTION/AVOID lens, `metrics/scorecard.py:659`) — a *separate* consumer chain that the spec does not mention:

- `recommendation_runner.py:135` — candidate selection filters `suitability == "GO"`
- `recommendation_engine.py:527-529` — `_entry_blocked` blocks unless `GO`
- `calibration.py:41, 72, 261`

**`stock_verdict`** (the raw Level-3 light verdict) — surfaced separately at `metrics/scorecard.py:477` and `app.py:254`, rendered in `Scorecard.jsx` and `ReadyToEnter.jsx`.

**Entry-gate snapshot:** `entry_context.py:121, 172-175` calls `screening.entry_gate` and freezes `right_spot` + `enterable` (`entry_context.py:303-304`) onto the immutable `buy_*` execution. **Changing the Level-4 rule changes what future snapshots record** — historical snapshots are unaffected (append-only), but a snapshot taken under `GATE_RULESET="proposed"` will not be comparable to one taken under `"legacy"` unless the ruleset is stamped into the snapshot. Not in the spec (Q5).

---

## 7. Fixture inventory

**`backend/fixtures/regime/xlk_july6_rollover.parquet`** — built by `fixtures/regime/build_fixtures.py:90-119`, registered `:122`. A 220-bar uptrend followed by a 30-bar hard breakdown.

Assertions against the **parquet**:

1. `test_stock_lights.py:84-104` — `test_july6_xlk_rollover_caught_by_both_layers`:
   - Layer 1: `genius_lights.compute(df)` → `lights["sar"] == "red" OR lights["momentum"] == "red"`, and **`greens < 4`**
   - Layer 2: `indicators.atr_expanding(df) is True`; with `ivr_percentile=95.0, is_etf=True` → `verdict == RED` and `"veto:atr_expanding_high_ivr"` in `veto_reasons`
   - With `ivr_percentile=10.0` (veto disarmed) → `verdict != GREEN`
2. `test_dividend_profile.py:104-134` — `test_1_xlk_july6_regression_unchanged`: the same two layers, plus profile-invariance (lights / verdict / greens / right_spot identical under `DIVIDEND_COMPOUNDER`).

**⚠ This fixture is directly at risk from spec §1.** Both tests assert `greens < 4` and then `verdict != GREEN`. Under a 3-of-4 rule those two are no longer equivalent: if the rollover bar yields exactly 3 greens with `close > SMA50` still true, the **proposed** ruleset returns GREEN where legacy returned YELLOW. The safety net is the ATR/IVR veto (layer 2), which is untouched and still forces RED at `ivr=95` — but the `ivr=10.0` assertion at `test_stock_lights.py:103` and `test_dividend_profile.py:124` has **no veto backstop** and is the one that can flip.

I did not run the fixture to determine the actual green count on the last bar — that is Phase 1 work. **The spec's test 1 (byte-identical under `GATE_RULESET="legacy"`) is satisfiable; the existing assertions as written are legacy-only and will need a proposed-ruleset counterpart rather than a rewrite.**

Other gate-related fixtures:
- `fixtures/regime/`: `sustained_green.parquet`, `distribution_rollover.parquet`, `v_bottom_whipsaw.parquet` — asserted by `test_regime_regression.py:47-70` against the **market** regime + dwell. Untouched by this change (market vote out of scope).
- `fixtures/structure/`: `early_advance_accum`, `early_advance_extended`, `early_advance_low_juice`, `topping_distribution` (+ `_sector`), `turning_recovery` (+ `_sector`) — built by `fixtures/structure/build_fixtures.py`, asserted in `test_structure_fixtures.py`. `early_advance_extended` is the Level-4 extension case; `early_advance_low_juice` is the juice-floor case. Both are adjacent to §2.
- `fixtures/structure/build_fixtures.py:14` states the XLK fixture is deliberately left untouched because the regime regression pins it.
- `test_recommendation_engine.py:285-330` uses **synthetic frames**, not the parquet, for a separate XLK July-6 assertion on `suitability` + no-ENTER.

---

## 8. Risks, unknowns, and code/prompt discrepancies

### Discrepancies — code is ground truth, reported not fixed

**D1 (material).** Prompt §"Current entry gate" Level 3 says *"Symbol Genius four-light requiring 4-of-4 green (close>SMA50, SMA50>SMA200, Parabolic SAR under price, ROC(10)>0)"*. The code's Level 3 uses `stock_lights` → `genius_lights`, whose second light is **`EMA21 > SMA50`** (`genius_lights.py:83`), not `SMA50 > SMA200`. The SMA50>SMA200 light belongs to `symbol_genius` (`symbol_genius.py:96`), which feeds the **scan verdict composition** (`metrics/scorecard.py:568`) and **never reaches `entry_gate`**. Two distinct engines, two distinct signals.

**D2.** Prompt Level 2 says *"Sector strong — sector RS/breadth/ATR checks"*. The code reframed this to a **deterioration veto** (`screening.py:577-583`): it blocks only on positive evidence (RS1M negative, breadth collapsing, sector distributing) and fails **open** on missing data. There is no ATR check at Level 2.

**D3.** Prompt Level 3 says *"RS gates PLUS Symbol Genius four-light"*. RS3M is **display / kill-switch only** and explicitly no longer gates entry (`screening.py:353-354`). RS-vs-sector survives at Level 3 as a **veto** inside `stock_lights.evaluate_vetoes` (`stock_lights.py:64-77`), not as an RS gate.

**D4.** Prompt Level 4 says *"ATR% under max, ATR actively contracting, extension ≤ N ATRs"*. Accurate — but "actively contracting" is `ATR/ATR_5EMA <= 1.0`, i.e. **contracting *or flat***, not strictly contracting (`stock_lights.py:139`, `config.py:267`).

**D5.** Prompt Level 5 lists "juice checks". Under shares-primary the **blocking** juice floor is disabled outright: `scan_triggers.juice_floor_block` returns `None` when `config.LEGACY_LEAP_READONLY` (`scan_triggers.py:315`), which is a hard `True`. The only juice evaluation in the path is `shadow_floor`, which never blocks. So defect (B)'s stated remedy — *"juice adequacy (Level 5) becomes the binding constraint"* — **has no blocking mechanism to become binding.** Relaxing Level 4 without re-arming a juice block admits premium-poor names with nothing downstream to stop them. This is the single most consequential finding for the spec's design rationale.

**D6.** `metrics/scorecard.py:786` reads `gate = screening.entry_gate(t, ...) if etf else None`, where `etf` is the ticker's **sector ETF symbol** (`sector_of[t]`), not an is-ETF flag. A ticker with no sector mapping gets `gate=None`, so `gate_blocks(None)` returns `[]` and **no L2/L3-veto/L4 block is folded into its verdict** — it can read READY on signals alone. Pre-existing; the dual-compute will make it visible.

### Risks

- **R1 — shared vote function (F2).** One edit to `stock_lights.verdict` moves both Level 3 and SYM. Under worst-signal-wins these are two of the inputs being relaxed simultaneously, so the divergence rate will be higher than a SYM-only change would produce.
- **R2 — Level-3 ATR veto survives (F3/§4).** High-IVR expanding-ATR names stay blocked at L3 regardless of the Level-4 relaxation.
- **R3 — no juice backstop (D5).** See above.
- **R4 — `suitability` chain unchanged (§4).** Recommendations will still suppress names the relaxed gate admits, so the Scan tab and the recommendation panel will visibly disagree during the shadow period.
- **R5 — fixture flip (§7).** The `ivr=10.0` assertions have no veto backstop under a 3-of-4 rule.
- **R6 — L5 for every row (§5).** Logging `all_level_results` including L5 means evaluating the account gate per scanned name; on a ~500-name sweep that is a state load and potentially a live cash resolution per name. Needs an explicit design decision (batch once per sweep, or log L5 as "not evaluated").
- **R7 — append-only vs idempotent-per-day (§5).** Spec test 6 contradicts the existing store's semantics.
- **R8 — entry snapshot comparability (§6).** `entry_context` freezes `right_spot`/`enterable` without a ruleset stamp.

### Questions requiring an answer before Phase 1

1. **Q1** — Given D1: does §1 retune **Level 3** (`stock_lights`, EMA21>SMA50), **SYM** (`symbol_genius`, SMA50>SMA200), or **both**? They share `stock_lights.verdict`, so "both" is the zero-effort default and "one only" requires splitting the function. Which is intended?
2. **Q2** — Should `SYM_MIN_GREEN_LIGHTS` be one constant for both engines, or two independent constants?
3. **Q3** — Given R2: is the Level-3 `atr_expanding_high_ivr` veto in scope? Leaving it means the Level-4 relaxation is a partial measure for exactly the high-IV names it targets.
4. **Q4** — Given R4: should the dual-compute also produce a proposed `suitability`, or is the Scan/recommendation disagreement acceptable during shadow?
5. **Q5** — Should `entry_context` stamp the active `GATE_RULESET` into the frozen entry snapshot?
6. **Q6** — Given R7: extend `scan_rejection_log` in place with a new schema version, or stand up a second per-run store alongside it? And does "append-only, grows monotonically" override the existing idempotent-per-day rule, or apply only to new per-run records?
7. **Q7** — Given R6: how should L5 be captured for names that never reach the L5 evaluation today?
8. **Q8** — Given D5: is re-arming a share-denominated blocking juice floor a prerequisite for §2, or is it accepted that §2 ships with no downstream income constraint? (The spec's §5 forbids touching the shadow floor — confirming that is deliberate given the rationale depends on it.)
9. **Q9** — `scan_id`: is a per-run identifier needed, or is per-symbol-per-day sufficient given the nightly single-writer design?

---

## Status

**Phase 0 complete. HARD STOP — awaiting explicit approval before Phase 1.**
No implementation code written. No constants changed. No files modified other than this audit document.
