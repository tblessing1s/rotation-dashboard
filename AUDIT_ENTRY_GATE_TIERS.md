# Audit: Entry-Gate Indicator Rationalization — Blocking vs. Informational Tiers + IV Richness (Phase 1)

Written report only. No file other than this one is touched. All references are
`file:line` at audit time (branch `claude/entry-gate-tier-system-r70lc6`, on top
of `7ee7b09`, v2.6.0 / schema v17).

**Headline findings the Phase 2 plan must absorb:**

1. **"Blocking" is not one thing today.** There are *three distinct entry
   verdict surfaces* — `screening.entry_gate` (L1–L4, stop-on-first-fail),
   `/api/scan/ready` + the Scorecard verdict (deliberately excludes L1/L2), and
   `recommendation_engine._entry_blocked` (the codified worst-signal-wins) —
   and each aggregates a *different subset* of checks. A single `tier`
   attribute must be defined against a named surface, or it means nothing.
2. **Several checks the prompt names as current blockers do not exist as
   blockers.** There is **no RSI 40–60 gate check** (the nearest real check is
   the **MFI 40–60** Scorecard CAUTION rule, tagged `[HARD RULE]`), **no IVR
   cap** anywhere (IV rank is display/snapshot-only), and **no
   persistence-of-signal requirement** (the only persistence mechanism is the
   Genius yellow dwell, which is out of scope). Sector **ATR-expanding is
   already informational** at L2. Phase 2's tier map must be written against
   the checks that actually exist.
3. **The transitive-redundancy claim is confirmed — and stronger than stated —
   but only on the one surface where L2 actually binds.** The direct-ratio fix
   from the risk-path hardening item has landed, so the implication is now an
   exact multiplicative identity, not an approximation. However, on the
   Scorecard / ReadyToEnter / ENTER-rec surfaces L2 is deliberately *not*
   enforced, and there the stock-vs-SPY check binds independently.
4. **A per-check `blocking` tier already exists at Level 5**
   (`account_gate._check`, `blocking=` flag; `juice_rich` is already
   non-blocking). Phase 2 should generalize this existing pattern, not invent a
   parallel one.
5. **The XLK July 6th fixture blocks on trend breakage (below-MA200 AVOID),
   not on "ATR expanding + IVR 98th percentile"** as the prompt describes. It
   survives every proposed demotion untouched, but the regression assertion to
   lock is the one that exists, not the one described.

---

## 1.1 Gate check inventory

### Surface A — `screening.entry_gate(ticker)` (L1–L4) — `screening.py:367-457`

Aggregation: every level computes all its checks; `cleared_level` is the
highest contiguous pass from 1 (stop-on-first-fail, `screening.py:449-455`);
verdict is `READY TO ENTER` iff `cleared_level == 4`, else `WAIT`. Within a
level, `pass = all(checks)` — except L1, whose pass is the published regime,
*not* the AND of its four displayed sub-checks.

| # | Check | Where | Threshold | Provenance | Blocks today? | Underlying condition | Correlated with | In entry snapshot? |
|---|-------|-------|-----------|------------|---------------|----------------------|-----------------|--------------------|
| L1 | Published regime green (4 Genius lights, ≥3/4 vote, 3-day yellow dwell) | `screening.py:379-397`, `regime_genius.py` | vote ≥3 of 4; dwell 3 days | `GENIUS_VOTE_GREEN_MIN`/`GENIUS_YELLOW_DWELL_DAYS` = `HARD_CFM_RULE`; MA/SAR/ROC params `PROPOSED_DEFAULT` (`config.py:177-185`) | **Yes** (level pass) | Market-index trend/momentum | The 4 lights are intentionally redundant (debounce) — OUT OF SCOPE | Yes — full trace (`entry_context.py:222-240`), `regime.status` tracked |
| L1-info | Breadth ≥ 60 / VIX bands | `screening.py:174-215` | `REGIME_BREADTH_GREEN=60`, `VIX_*` | `PROPOSED_DEFAULT` (`config.py:156-159,187-196`) | No — explicitly secondary/informational | Market participation / fear | L1 lights | Yes — `regime.breadth`, `regime.vix` tracked |
| L2-a | Sector RS3M vs SPY ≥ +10% | `screening.py:402-403` | `SECTOR_RS3M_MIN = 10.0` | **Untagged** in `config.py:202` (no provenance comment) | **Yes** | Sector leadership vs market | L3-a (transitively composes with L3-b) | Yes — `sector.rs3m_vs_spy` tracked |
| L2-b | Sector breadth ≥ 60% | `screening.py:404-405` | `SECTOR_BREADTH_MIN = 60.0` | **Untagged** (`config.py:203`) | **Yes** | Sector participation | L2-a (both measure sector strength) | Yes — `sector.breadth` tracked |
| L2-c | Sector ATR expanding | computed `screening.py:241`, displayed; **not** an entry_gate check and **not** part of `strong` (`screening.py:242-243`) | boolean (ATR now > ATR 10 bars ago) | untagged | **No — already informational** | Sector volatility regime | Stock `atr_momentum` (scorecard CAUTION) | Yes — `sector.atr_expanding` (untracked field) |
| L3-a | Stock RS3M vs SPY > +5% (stocks) / > 0% (ETFs) | `screening.py:432-433`, bar from `config.rs_vs_spy_min` | `STOCK_RS_VS_SPY_MIN = 5.0`, `_ETF = 0.0` | **Untagged** (`config.py:206-212`) | **Yes** (in entry_gate; also binds Scorecard via L3 short-circuit) | Stock vs market strength | **Transitively implied by L2-a + L3-b on this surface** (see 1.2) | Yes — `stock.rs3m_vs_spy` tracked |
| L3-b | Stock RS3M vs Sector > 0% (waived for ETFs) | `screening.py:434-435` | `STOCK_RS_VS_SECTOR_MIN = 0.0` | **Untagged** (`config.py:213`); mirrored as `[HARD RULE]` in `metrics/thresholds.py:47` | **Yes** — the laggard filter | Stock vs own peers (the AAPL lesson) | Kill switch uses the same figure (`kill_switch`, same `rs3m` direct ratio) | Yes — `stock.rs3m_vs_sector` tracked, + `rs3m_vs_sector_method: "direct"` |
| L4-a | ATR% ≤ 5.0 | `screening.py:441-442` | `CONSOLIDATION_ATR_PCT_MAX = 5.0` | **Untagged** (`config.py:220`) | **Yes** — but see note: **strictly implied by L4-b** | Volatility level (consolidation) | L4-b contains this same condition | Yes — `stock.atr_pct` tracked |
| L4-b | "Near MA21 (consolidating)" = ATR% ≤ 5 **and** \|dist from MA21\| ≤ 4% | `screening.py:443`, `indicators.consolidating` (`indicators.py:280-287`) | `CONSOLIDATION_ATR_PCT_MAX`, `CONSOLIDATION_MA21_DIST_MAX = 4.0` | **Untagged** (`config.py:220-221`) | **Yes** | Price coiled near mean | L4-a is a strict subset of it; `pct_above_ma21` scorecard metric | Yes — `stock.pct_above_ma21` tracked, `consolidating` untracked |

Note the **internal L4 redundancy already present**: L4-a (ATR% ≤ 5) is one of
the two conjuncts inside L4-b, so as a blocker L4-a is a pure no-op — any input
failing L4-a necessarily fails L4-b. It exists separately only so the UI can
show which conjunct failed.

**There is no RSI check and no volume check anywhere in `entry_gate`.** RSI(14)
is computed only for the entry snapshot (`entry_context.py:283-294`,
`stock.rsi`, tracked) and never gated on.

### Surface B — Scorecard composite verdict — `metrics/scorecard.py:237-291` + `363-436`

Aggregation: AVOID dominates CAUTION dominates GO; all applicable reasons
collected; `None` metrics skip their rule. A stock-level entry-gate failure
(L3 or L4 **only** — L1/L2 deliberately excluded, `scorecard.py:303-312`)
short-circuits the row to AVOID. This verdict is what feeds *every downstream
blocking surface* (ReadyToEnter GO filter, ENTER recs, queue tiers).

| Check | Rule | Threshold | Provenance (`metrics/thresholds.py`) | Blocks today? | Underlying condition | Correlated with | In snapshot? |
|---|---|---|---|---|---|---|---|
| rs3m_vs_sector < 0 → AVOID (non-ETF) | `scorecard.py:257-261` | 0.0 | `[HARD RULE]` (`thresholds.py:47`) | **Yes** | Laggard filter (duplicate of L3-b) | L3-b — same figure, same threshold | Yes (`scorecard.metrics.rs3m_vs_sector`) |
| below MA200 → AVOID | `scorecard.py:262-263` | boolean | `[HARD RULE]` (structural) | **Yes** | Broken long-term trend | below_ma50, ma50_slope | Yes (`below_ma200`) |
| ATR extension > 3.0 → AVOID | `scorecard.py:264-266` | `ATR_EXTENSION_MAX = 3.0` | `[CALIBRATE]` | **Yes** | Overextension above MA21 | L4-b distance leg (inverse direction) | Yes (`atr_extension`) |
| **MFI outside 40–60 → CAUTION (non-ETF)** | `scorecard.py:274-276` | `MFI_MIN/MAX = 40/60` | **`[HARD RULE]` — "from Travis's own CFM entry criteria"** (`thresholds.py:33-35`) | **Yes** (CAUTION ≠ GO blocks every downstream surface) | Money-flow coil (mid-range) | This is the closest real check to the prompt's "RSI 40–60 band" — but it is MFI, and it is tagged HARD | Yes (`mfi`) |
| **volume_ratio < 0.8 → CAUTION (non-ETF)** | `scorecard.py:277-279` | `VOLUME_RATIO_MIN = 0.8` | `[CALIBRATE]` | **Yes** | Thin participation | volume_acceleration, obv (computed, not gated) | Yes (`volume_ratio`) |
| **atr_momentum > 1.0 → CAUTION (non-ETF)** | `scorecard.py:280-282` | `ATR_MOMENTUM_MAX = 1.0` | `[HARD RULE]` (definitional: expanding vs contracting) | **Yes** | Volatility expanding = wants APP not CFM | L4-a/b ATR level; sector `atr_expanding` | Yes (`atr_momentum`) |
| below MA50 → CAUTION | `scorecard.py:283-284` | boolean | `[HARD RULE]` (structural) | **Yes** | Medium trend stress | ma50_slope, below_ma200 | Yes (`below_ma50`) |
| ma50_slope < 0 → CAUTION | `scorecard.py:285-287` | 0 (sign) | `[HARD RULE]` (structural) | **Yes** | Trend rolling over | below_ma50 | Yes (`ma50_slope`) |

Computed but **never gated** (already informational): `pct_above_ma200`,
`volume_acceleration`, `obv_above_ema`, `obv_pct_distance`, `rs3m_vs_spy` (as a
scorecard metric — it only gates via the L3 short-circuit), `juice_ok` (shown,
never blocks), `has_weeklies` (flags, never blocks).

### Surface C — Level 5 Account & Juice — `account_gate.evaluate` (`account_gate.py:203-352`)

Aggregation: `pass = no blocking failures`; **each check already carries a
`blocking: bool`** (`account_gate.py:198-200`) — the existing tier mechanism.
Enforced server-side in `executor._enforce_account_gate` (`executor.py:488-512`)
on every `buy_leap`, overridable with a typed, logged `override_reason`. This is
the **only** gate the executor enforces — L1–L4 are advisory surfaces at
execution time.

| id | Threshold | Provenance | Blocking today |
|---|---|---|---|
| `cash_reserve` | post-trade cash ≥ 2×ATR reserve (`RESERVE_ATR_MULT = 2.0`) | `PROPOSED_DEFAULT` (`config.py:773-778`) | **Yes** |
| `position_limit` | ≤ 2 concurrent (`MAX_CFM_POSITIONS`) | **`HARD_CFM_RULE`** (`config.py:762-764`) | **Yes** |
| `capital_limit` | ≤ $38,000 deployed (`MAX_DEPLOYED_CAPITAL`) | `PROPOSED_DEFAULT` (`config.py:766-767`) | **Yes** |
| `sector_concentration` | ≤ 1 per sector (`MAX_POSITIONS_PER_SECTOR`) | `PROPOSED_DEFAULT` (`config.py:769-771`) | **Yes** |
| `juice_adequacy` | weekly yield ≥ ~1.9%/wk (derived from **`HARD_CFM_RULE`** `CYCLE_RETURN_MIN/CYCLE_WEEKS_MAX`, `config.py:780-786`); ETF bar 1.0% `PROPOSED_DEFAULT` | mixed | **Yes** |
| `juice_rich` | actual > 1.75× history-implied (`JUICE_RICH_FACTOR`) | `PROPOSED_DEFAULT` (`config.py:788-791`) | **No — already informational** (`blocking=False`, `account_gate.py:305-311`) |
| `earnings_in_cycle` | no earnings inside 8 weeks (`CYCLE_WEEKS_MAX`) | rule `HARD_CFM_RULE`-adjacent ("be out or really deep"), window from HARD constant | **Yes** |

### Surface D — the codified worst-signal-wins ENTER aggregation — `recommendation_engine._entry_blocked` (`recommendation_engine.py:510-526`)

`blockers = candidate.blockers ∪ {scorecard verdict ≠ GO} ∪ {L1 regime ≠ green}
∪ {each L5 blocking failure}`. Empty ⇒ ENTER rec emitted. Note carefully what is
**absent**: L2 sector strength is *not* consulted here (candidates come from the
Scorecard GO subset, which excludes L1/L2 by design — `recommendation_runner.py:115-139`,
`app.py:135-217`). `candidate.blockers` is always `[]` as built today
(`recommendation_runner.py:137`).

### Surface E — freshness gate

`STALE_BLOCKS_GO = True` — **`HARD_CFM_RULE`** (`config.py:272`), enforced in
`/api/scan/ready` (`app.py:166-208`): a stale-input GO is quarantined into
`stale_blocked`, never listed as ready. Unaffected by this work item, but it is
part of the blocking surface and must keep consuming blocking-tier semantics.

### Checks named in the prompt that do NOT exist

| Prompt's check | Reality |
|---|---|
| "L4 RSI 40–60 band" | No RSI gate exists. RSI(14) is snapshot telemetry only (`entry_context.py:275`). The real mid-range-coil check is **MFI 40–60**, a Scorecard CAUTION rule tagged **`[HARD RULE]`**. Demoting it is a tier change to a HARD-provenance check — allowed by the letter of 2.7 (no *threshold value* changes) but it is the one demotion that touches course canon; Phase 2 should surface this to the operator explicitly. |
| "L4 low-volume check" | Exists as the Scorecard CAUTION `volume_ratio < 0.8` (`[CALIBRATE]`), not as an L4 gate check. |
| "L4 ATR contracting (hard gate)" | L4's actual checks are ATR *level* (≤5%) + near-MA21. ATR *direction* lives in the Scorecard CAUTION `atr_momentum > 1.0` (`[HARD RULE]`, definitional). Both currently block (via different surfaces). |
| "L2 ATR expanding" (listed as remaining blocking) | Sector ATR-expanding is **already informational** — computed and displayed (`screening.py:241,249`) but not part of L2's pass or the sector `strong` flag. No change needed; it must simply be *labeled* informational rather than silently so. |
| "IVR cap" (listed as remaining blocking) | **No IVR cap exists.** `iv_rank` is computed (`iv_history.py:92-118`), rendered in the option-chain IV view (`option_chain.py:138-172` — text label only, "rich vs its own year"), carried in rec features (`recommendation_engine.py:369`) and the entry snapshot (`iv.iv_rank`/`iv.iv_percentile`, tracked). Nothing anywhere blocks on it. Phase 2 cannot "keep it blocking"; it can only keep it informational (or a new blocking check would have to be *added*, which the prompt does not ask for). |
| "Persistence-of-signal requirement" | Does not exist. The only dwell/persistence logic is the Genius 3-day yellow dwell (out of scope) and recommendation validity windows (`REC_VALID_HOURS` — dedup, not signal persistence). Nothing requires a GO/gate-pass to persist N days before entry. |

---

## 1.2 The transitive-redundancy claim — CONFIRMED (exact, not approximate), with a surface caveat

**Status of the direct-ratio fix: already landed.** Commit `8b5065e`
("Risk-path math hardening: honest capture, **direct sector RS**, dividend
greeks") switched RS-vs-sector to the direct `rs3m(stock_df, sector_df)` ratio
everywhere: entry gate (`screening.py:262-273`), scorecard
(`metrics/scorecard.py:203-212`), and the kill switch consume the same figure.
The snapshot records `rs3m_vs_sector_method: "direct"` and
`SNAPSHOT_SCHEMA_VERSION` was bumped to 3 for it (`config.py:808-811`,
`entry_context.py:268-272`). **Nothing to fix here; the difference-approximation
caveat in the prompt is obsolete.**

**The math, under the current (direct-ratio) computation.** `indicators.rs3m`
(`indicators.py:142-161`) computes the percent change over 63 bars of the price
ratio. For any three aligned series,

```
(S/SPY)_now/(S/SPY)_then = [(S/X)_now/(S/X)_then] × [(X/SPY)_now/(X/SPY)_then]
```

is an identity, i.e. `(1 + rs_spy/100) = (1 + rs_sec/100) × (1 + rs_x/100)`.
With L2-a requiring `rs_x ≥ 10` and L3-b requiring `rs_sec > 0`:

```
rs_spy = 100·[(1 + rs_sec/100)(1 + rs_x/100) − 1] > rs_x ≥ 10 > 5
```

So a non-ETF candidate passing L2-a and L3-b clears L3-a's +5% bar with **at
least 5 points of headroom** — the implication is exact and strict, stronger
than the prompt's "modulo the ratio-vs-difference approximation."

Residual edge cases, all immaterial to the 5-point headroom:

- **Rounding:** each `rs3m` rounds to 2dp (`indicators.py:161`); worst combined
  distortion ~±0.02 points.
- **Index alignment:** each pairwise ratio reindexes the benchmark onto the
  stock's dates and drops NaNs; a stock with missing bars (halt, recent
  listing) shifts its 63-bar window relative to the sector-vs-SPY window
  computed on the sector's own index. Divergence is possible in pathological
  cases but cannot plausibly bridge a >5-point gap.
- **ETFs:** the implication does not apply — L3-b is waived for every ETF
  (`screening.py:434-435`). A sector ETF trivially satisfies its 0% bar
  whenever its own L2-a passes (they are the same series); a curated ETF
  (QQQ-style) keeps L3-a (bar 0%) as a genuinely independent check. **The
  demoted L3-a must therefore stay computed and displayed for ETFs, where it is
  the only stock-vs-market signal** — demotion satisfies this since nothing
  stops being computed.

**The surface caveat (important for the demotion rationale).** The implication
holds only where L2-a is enforced *and* L3-b is enforced together — i.e. the
`entry_gate` verdict surface (ExecuteTab, snapshot `gates.entry_gate`). On the
Scorecard/ReadyToEnter/ENTER-rec surfaces, L1/L2 are deliberately excluded
(`scorecard.py:303-312`, `app.py:140-144`), so there L3-a binds independently:
a leader in a *weak* sector (`rs_x < 10`) can pass L3-b yet fail L3-a. Demoting
L3-a to informational therefore *does* change behavior on those surfaces — a
strong-in-weak-sector name that today short-circuits to AVOID via L3 would
become GO-eligible (subject to every other check). That is exactly the kind of
hypothesis the shadow-verdict counterfactual should adjudicate, but Phase 2's
doc note must state it honestly: on the shortlist surfaces this is a real
loosening, not the removal of a no-op.

---

## 1.3 Worst-signal-wins aggregation — location, inputs, enums, UI

**The codified aggregation** is `recommendation_engine._entry_blocked`
(`recommendation_engine.py:510-526`), consumed by `evaluate` at `:616-626`.
Inputs per candidate: `verdict` (Scorecard composite), `level5.blocking_failures`
(L5), market `regime.status` (L1), `candidate.blockers` (currently always
empty). Output: a list of blocker strings; ENTER emitted iff empty. Regression
tests: `test_recommendation_engine.py:247-257`
(`test_enter_blocked_by_any_worst_signal`, parameterized over CAUTION / AVOID /
L5-fail / yellow / red) and the XLK July 6th lock (`:268-319`).

**Verdict enums by surface** (no shared enum exists):

- `entry_gate`: `verdict ∈ {"READY TO ENTER", "WAIT"}` + `cleared_level 0–4`;
  per-check `{label, value, pass}` — **no severity/tier field**
  (`screening.py:358-360`).
- Scorecard: `verdict ∈ {GO, CAUTION, AVOID}` + `reasons: [str]`.
- L5: `pass: bool` + per-check `{id, label, pass, blocking, detail}` +
  `blocking_failures: [id]` + `warnings: [id]` — **the existing tier notion**.
- `/api/scan/ready`: partition into `ready` / `near_misses` / `stale_blocked`.
- Recommendation: `ActionType.ENTER` emitted-or-not; `input_snapshot.blockers`.
- `stock_filter` rows: `status ∈ {ready, wait, no}` + `blocked_by ⊆ {regime,
  sector, stock, consolidation}` (`screening.py:292-307`).

**Existing severity/tier notions:** (a) the L5 `blocking` flag — the only
per-check tier; (b) the Scorecard's AVOID>CAUTION hierarchy — per-*rule*, not
exposed per-check to the UI; (c) alert severities (CRITICAL/HIGH/MEDIUM/LOW in
`alerts.py:33-67`) — a separate taxonomy, untouched by this work.

**UI rendering today** (full survey in agent report; key points):

- `ExecuteTab.jsx` `GateLevel` (L1–L4): two-state ✓/✗ per check — no
  informational rendering exists. `AccountGate` (L5): **three-state** — ✓ pass,
  ✗ blocking fail, amber `!` non-blocking fail — keyed off `c.blocking`. This
  is the pattern to generalize.
- `Scorecard.jsx`: verdict pill (GO/CAUTION/AVOID), row tinting, expandable
  `reasons`. No per-check tier.
- `ReadyToEnter.jsx`: renders `level5.blocking_failures` via `REASON_LABELS`;
  non-blocking L5 warnings are *not* surfaced; `stale_blocked` rendered
  separately with `StaleBadge`.
- Entry evidence: only `entry_context.summary()` (5 fields) is rendered, in
  `HistoryTab.jsx` `CycleRow`. The full snapshot (per-level checks, per-check
  L5 detail, data_quality) is stored but has no UI. A `shadow_verdict` would
  land in the same stored-but-summarized bucket unless explicitly added to the
  summary/UI.
- **No alert fires on gate verdicts** — nothing to migrate there. The only
  adjacent alert is LOW-severity `SNAPSHOT_DATA_QUALITY`
  (`executor.py:1110-1123`).

---

## 1.4 Data availability for IV richness

**What the metric needs per candidate:** ~60 daily IV observations
(`IV_RICHNESS_LOOKBACK_DAYS`) each pairable with realized vol over the
following 10 trading days — i.e. **≥ ~70 trading days of stored daily IV** plus
daily closes over the same span.

**Realized-vol side: fully available for the whole universe.** The parquet
daily-bar cache holds `HISTORY_DAYS = 320` bars per symbol
(`config.py:229`), refreshed by the EOD batch; `indicators.hist_vol`
(`indicators.py:119-130`) already computes annualized realized vol over an
arbitrary window. Forward 10-day realized vol at each historical date is a pure
computation over cached closes. No API cost.

**Implied-vol side: the binding constraint.** The only stored IV history is
`DATA_DIR/iv_history.json` (`iv_history.py`): **one weekly-ATM IV point per
calendar day per ticker**, capped at 260 points, minimum 20 for a rank. Points
accrue from exactly two paths:

1. Opportunistically, whenever the option-chain view is computed for a ticker
   (`option_chain.py:584` — `iv_history.record(ticker, weekly_iv)`).
2. Nightly, but **for held names only**: `maintenance.nightly_refresh` →
   `snapshot_iv(open_tickers())` (`maintenance.py:113-125,173`), which calls
   `option_chain.option_chain(t)` for the side effect.

There is **no chain polling for the screening universe or even the on-deck
queue**: the tiered scheduler never schedules `CHAIN`
(`market_scheduler.py:272` — "CHAIN and any other kind: never scheduled
here"); chains are fetched on demand only. `burn_marks.json` stores nightly
model marks for held names but its `iv` input is trailing *realized* vol
(`maintenance.py:96-101`), not an IV solve — it is not a usable IV history.
The BSM engine's IV solves (`indicators.implied_vol_call/put`) are computed
transiently and never persisted outside `iv_history.json`.

**Consequences for Phase 2, matching the prompt's fallback clause exactly:**

- **Screening universe: `INSUFFICIENT_DATA` essentially everywhere**, and it
  cannot be backfilled — historical option chains are not retrievable from
  Schwab/Alpha Vantage on this stack, so the 60-day window can only accrue
  forward, one point per day, per ticker that gets a daily chain fetch.
- **On-deck candidates + held names: feasible prospectively.** Extending the
  nightly `snapshot_iv` call from `open_tickers()` to `open + on-deck`
  (`QUEUE_ONDECK_COUNT = 5`) costs ~5–7 chain calls/day — trivially within the
  Schwab budget (`SCHWAB_DAILY_CALL_LIMIT` 40k `PROPOSED_DEFAULT`; chains
  aren't AV-limited). Even so, a fresh on-deck name needs ~3 calendar months
  of accrual before its 60-day richness window fills; the metric must render
  `INSUFFICIENT_DATA` (with the day count, as `iv_rank` already reports
  `days`) until then. Held names that have been held/watched for months may
  already qualify.
- The **horizon subtlety**: at evaluation date T, only IV observations dated
  ≤ T−10 trading days have a *complete* subsequent-realized-vol window. The
  pure function must exclude the last 10 days' IV points from the mean (or the
  freshest points would need truncated horizons). With the 60-day lookback that
  leaves ~50 usable pairs at steady state.
- One point/day is weekly-ATM-ish short-tenor IV — exactly the "ATM-ish
  short-tenor" the prompt specifies (it is the suggested-strike/median weekly
  IV, `option_chain.py:581-583`), and 10 trading days ≈ the tenor is a
  reasonable match. No new data *shape* is needed, only wider accrual coverage.

---

## 1.5 Calibration harness touchpoints

**What exists** (`calibration.py`):

- `collect_rows` (`:46-77`): replays `metrics_for` + `compute_verdict` over
  cached parquet history (per ticker, every `step=5` bars after a 210-bar
  warm-up), pairing each as-of row with forward 4w/8w price returns.
  Threshold sensitivity is done by re-bucketing the *same* metric rows under
  overridden thresholds (`_verdict_with`, `:32-43` — ATR-extension sweep, MFI
  band sweep). Crucially, this replay **already recomputes the raw metric
  values**, so re-aggregating them under any tier map is pure post-processing.
- `load_closed_cycles` (`:83-115`): yields `(entry_context, exit_reason,
  outcome)` per closed cycle carrying a frozen snapshot; legacy cycles are
  counted and skipped, never fabricated.
- `regime_series` / `regime_vs_cycles`: the Genius-parameter counterpart
  (out of scope here, but the structural template for "replay under alternative
  config and bucket outcomes by realized cycle result").

**What the per-check counterfactual needs, and where it comes from:**

1. **Per-evaluation per-check results.** Already available in both data paths:
   the historical replay recomputes every scorecard metric (so each CAUTION/
   AVOID rule can be re-evaluated as a named check), and the frozen snapshots
   carry `gates.entry_gate.levels[].checks[]` (label/value/pass),
   `gates.account_gate.checks[]` (id/pass/blocking), and every scorecard metric
   (`scorecard.metrics`). Gap: entry-gate checks in snapshots are keyed by
   display *label* (which embeds the threshold text, e.g. "RS3M vs SPY >
   +5%"), not by a stable id — the tier system should introduce stable check
   ids so the harness and snapshots can join reliably across config changes.
2. **A pure re-aggregation function** `verdict(check_results, tier_map)` that
   both the live path and the harness call — this is precisely the
   `shadow_verdict` mechanism of 2.1 (shadow = re-aggregate with all
   informational checks treated as blocking). Once it exists, "how would
   outcomes have differed" is: for each closed cycle, `actual = agg(checks,
   tiers)`, `counterfactual = agg(checks, all_blocking)`, and per check c,
   `flipped_by_c = agg(checks, tiers ∪ {c: blocking}) ≠ actual`.
3. **Outcome joining**: `load_closed_cycles` already returns the coded
   `exit_reason` and `net_return_pct`/`target_met` per cycle — the per-check
   table ("entries this check alone would have blocked → their coded exit
   outcomes") is a group-by over (2) × (3).
4. **Honest sample-size framing**: with ~2 positions and 4–8 week cycles the
   closed-cycle N is tiny; the harness should also run the same per-check
   counterfactual over the `collect_rows` historical replay (forward *price*
   returns as the outcome proxy, with the existing mid-fill caveat) so each
   check gets both a small-N labeled table and a large-N proxy table.
5. **Forward-looking capture**: the `shadow_verdict` recorded on every live
   evaluation (2.1) makes future cycles' counterfactuals free; the harness
   work above is what makes *past* cycles and synthetic history usable.

---

## 1.6 Blast radius

Consumers of entry verdicts / per-check results, and what the tier change does
to each:

| Consumer | Where | Impact |
|---|---|---|
| Executor entry enforcement | `executor.py:285-289,488-512` (L5 only) | Untouched by the three demotions (all are L1–L4/scorecard-side). Gains: stash `shadow_verdict` for the snapshot. |
| ENTER recommendation path | `recommendation_engine._entry_blocked` + `_entry_candidates` (`recommendation_runner.py:115-139`) | Behavior change by design: candidates failing *only* MFI-band/volume-ratio (CAUTION → non-GO today) become ENTER-eligible; L3-a short-circuit no longer AVOIDs a strong-in-weak-sector name. Shadow verdict must be recorded on the rec's `input_snapshot`. |
| Ready-to-Enter shortlist | `app.py:135-217` (GO filter), `ReadyToEnter.jsx` | Same behavior change; UI gains info-tier badges + shadow-verdict hint. `stale_blocked` path (HARD `STALE_BLOCKS_GO`) unchanged. |
| Scorecard verdict + UI | `metrics/scorecard.py:237-291,423-435`, `Scorecard.jsx` | The demoted rules stop producing verdict-changing CAUTION/AVOID but stay computed and displayed (muted "info" reasons). GO/CAUTION/AVOID counts shift. |
| Queue / tiered scheduler | `queue_state.py:67-85` (GO rows → Tier 1 on-deck), `refresh_policy.py:108-110` (GO rows → hot set) | More names may qualify as GO → on-deck/hot sets can grow; both are capped (`QUEUE_ONDECK_COUNT`, `HOT_TICKERS_MAX`) so budget impact is bounded. |
| Entry-context snapshots | `entry_context.py` (`_TRACKED_FIELDS:34-41`, sections) | Additive only: `shadow_verdict`, per-check tier, IV richness section. `SNAPSHOT_SCHEMA_VERSION` 3 → 4 (additive, per the versioning HARD rule). Note: L5 check `detail` values are currently *dropped* from snapshots (`entry_context.py:314-330`) — unchanged, but the new fields must not repeat that pattern for anything the harness needs. |
| History / cycle summary | `entry_context.summary` (`:359-371`), `history.py:74-81` CSV | `summary()`/CSV should gain `shadow_verdict` (additive column — CSV consumers tolerate new columns; test `test_history.py:154` asserts presence, not absence). |
| Alerts | none fire on gate verdicts (survey result) | No migration. `SNAPSHOT_DATA_QUALITY` denominator grows if new fields are tracked in `_TRACKED_FIELDS` — adding IV-richness as a *tracked* field will raise null-fractions for the whole universe until IV accrual catches up; consider tracking it as untracked-informational first to avoid false data-quality alerts. |
| Trust scoreboard | `trust_derive.py` (matches recs to actions) | Indirect only: more/fewer ENTER recs changes coverage denominators; no schema change needed. |
| Calibration harness | `calibration.py` | Extended per 1.5. |

**Test fixtures whose expectations change (and legitimacy):**

- **XLK July 6th** (`test_recommendation_engine.py:268-319`): **must still
  block — and does.** Its blocking verdict comes from `below_ma200 → AVOID`
  (hard breakdown through the 50/200-day MAs; also below-MA50/slope CAUTIONs
  and the L4 consolidation fail via ATR blowout on a −2.2/day collapse), none
  of which is demoted. **The prompt's description of its blockers ("ATR
  expanding, IVR 98th percentile") does not match the fixture** — the fixture
  sets no IV history at all, and no IVR check exists. Phase 2 should extend the
  test to assert both actual and shadow verdicts, keeping the existing
  assertions intact.
- **AAPL laggard**: no named "AAPL laggard" fixture exists today. The
  behavior is covered generically: `rs3m_vs_sector < 0 → AVOID` in scorecard
  tests, L3-b in `test_cfm.py` gate tests, and `KILL_RS_SECTOR` in
  `test_kill_switch.py` / rec-engine tests. Phase 2 adds the named entry-side
  fixture per 2.6.
- **`test_recommendation_engine.py:247-257`** (`test_enter_blocked_by_any_worst_signal`):
  parameterized on CAUTION/AVOID verdicts — still valid as written (a CAUTION
  verdict, however produced, still blocks; only *which rules produce* CAUTION
  changes). Legitimate, likely unchanged.
- **`test_scorecard.py`** (32 tests): any test asserting CAUTION/AVOID *because
  of* MFI band or volume ratio must be updated to expect the informational
  rendering instead — a legitimate, intended change. Tests asserting AVOID via
  MA200/extension/rs-vs-sector are unaffected.
- **`test_cfm.py`** gate tests (L3/L4 check shapes, labels, `blocked_by`):
  additive tier fields must not break `{label, value, pass}` consumers;
  demotions change `entry_gate` level-pass composition only if L3-a/L4 checks
  are re-tiered *within* `entry_gate` — Phase 2 must decide whether
  `cleared_level` is computed over blocking checks only (recommended, with the
  old composition preserved in the shadow verdict).
- **`test_entry_context.py` / snapshot completeness**: gains fields; the
  never-lose-fields assertion (2.6 "snapshot completeness") should be added,
  not merely preserved.
- **`test_account_gate.py`**: unaffected by demotions; extended for tier-map
  config provenance.

**Sector ETF / curated ETF paths** (`is_etf` waivers throughout) interact with
every demotion — the demoted MFI/volume/atr-momentum CAUTIONs are *already
waived for ETFs* (`scorecard.py:270-282`), so the demotions change nothing for
ETFs; only non-ETF candidates are affected. Tier config must not accidentally
turn ETF waivers ("not applicable") into "informational fail" — N/A and
info-fail must stay distinguishable in the shadow verdict.

---

## Phase 2 design implications (summary of what the approved plan should say)

1. Generalize the **existing L5 `blocking` flag** into a config-driven tier map
   keyed by **stable check ids** across all surfaces (introduce ids for L1–L4
   and scorecard-rule checks); every assignment tagged `PROPOSED_DEFAULT`.
2. Define tiers against **named surfaces**, and compute `shadow_verdict` with
   one shared pure re-aggregation function used by live paths and the harness.
3. Actual demotions that map to real checks: **L3-a stock-vs-SPY** (entry_gate
   + scorecard L3 short-circuit leg), **MFI 40–60 CAUTION** (flagging its
   `[HARD RULE]` provenance to the operator), **volume-ratio CAUTION**. Confirm
   with the operator that MFI-for-"RSI" is the intended reading before
   implementation, since the prompt names a check that doesn't exist.
4. IV richness: pure function over `iv_history.json` + parquet closes with the
   10-day-complete-horizon exclusion; `INSUFFICIENT_DATA` below a minimum pair
   count; extend nightly `snapshot_iv` to on-deck names to start accrual;
   screening-universe values will be `INSUFFICIENT_DATA` for months — render
   honestly.
5. Keep untouched: Genius regime (all of it), kill switch, all thresholds, all
   `HARD_CFM_RULE` values, `STALE_BLOCKS_GO`, L5 blocking set, snapshot
   never-blocks/never-narrows rules.

**HARD STOP — awaiting approval before any Phase 2 implementation.**
