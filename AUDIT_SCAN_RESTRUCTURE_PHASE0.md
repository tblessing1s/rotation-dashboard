# Phase 0 — Scan Restructure Audit (findings)

**Status: AUDIT ONLY. No code was changed.** This document answers questions 1–8
with `file:line` citations, then proposes an ordered Phase 1 scope, explicit
descopes, and the `HARD_CFM_RULE` / locked-baseline collisions found.

> **Hard stop.** Do not begin Phase 1 without explicit approval of these findings.

A recurring, load-bearing discovery up front, because it colors every answer:
**there are two scan backends, not one.**
- `screening.py` (`_stock_row`, `entry_gate`) — the gate-oriented rows.
- `backend/metrics/scorecard.py` — the *display* scorecard the frontend actually
  renders (`Scorecard.jsx`), plus `/api/scan/ready`.

They compute **three different verdicts** over the same signals (detail in Q5).
Unifying them behind one `VERDICT` function is the spine of this restructure.

---

## Q1 — Genius engine reuse

**How a light-set is parameterized.** The shared engine is `genius_lights.py`.
Parameters come from `default_params()` (`genius_lights.py:44-57`), which reads the
provenance-tagged `config.GENIUS_*` constants; `_params()` merges a per-call
override dict (`genius_lights.py:60-64`); `compute_lights()` and `compute()` accept
that override (`genius_lights.py:101-115`, `genius_lights.py:152-173`). So MA
lengths, SAR AF, ROC window, and the vote threshold are all injectable **without
editing the engine** — for those knobs a Symbol Genius instance is a pure
override.

**Where the fourth-light definition lives, and the substitution problem.** The
four lights are assembled in `compute_lights` (`genius_lights.py:101-115`). Light 2
(`fast_vs_slow`) is hardwired as `indicators.ema(df, p["fast_ma"])` vs
`indicators.sma(df, p["slow_ma"])` (`genius_lights.py:106-107`, `111-113`) through
`light_fast_vs_slow` (`genius_lights.py:83-86`). The light's operands are fixed by
*type*: fast is always an **EMA**, slow always an **SMA**. There is **no injection
point that swaps the operand type or the light's identity**. The Symbol Genius
fourth light is `SMA50 > SMA200` — two SMAs — which cannot be produced by any
value of `fast_ma`/`slow_ma` (that would give `EMA(x) > SMA(y)`, never
`SMA(50) > SMA(200)`).

> **Finding:** the `SMA50>SMA200` substitution **cannot be a pure param override**.
> It requires a new light function plus a light-set-assembly injection point in
> `genius_lights` (a small, additive engine extension) — **not** a fork of the
> whole engine, and **not** an edit to `compute_lights` itself (which carries a
> byte-identical guarantee, below).

**Can a Symbol Genius instance be created without modifying the shared engine?**
Partially. The 3 shared lights (ROC10>0, SAR<close, close>SMA50) reuse the engine
verbatim. The 4th light does not — it needs one new pure light + a way to compose
a 4-light set from `{shared 3} + {new 4th}`. Today `stock_lights.compute` calls
`genius_lights.compute(df, params=params)` with `params` defaulting to `None`
(`stock_lights.py:154`), i.e. it always uses the **regime constants**.

**Accidental-sharing flag (the "must NOT silently share the fourth-light
constant" risk).** Any Symbol Genius built by calling `genius_lights.compute`
without an explicit, distinct params object silently inherits
`config.GENIUS_FAST_MA = 21` / `GENIUS_SLOW_MA = 50` via `default_params()`
(`genius_lights.py:51-52`, `config.py:193-194`). The exact collision surface is
`genius_lights.compute_lights:107` (fast light) + `default_params:51-52`. Symbol
Genius must be constructed with its **own** params/light-set, never the default.

**Note:** `regime_genius.py:46-56` *re-exports* the shared light functions by name
(no wrapping) — this is the "byte for byte" identical guarantee between the market
and stock lights. `SMA200` math already exists in the stock layer as a veto
(`MA200_WINDOW = 200`, `stock_lights.py:39`; `close < ma200` veto,
`stock_lights.py:82-90`) — so the indicator is available, just not wired as a
light.

---

## Q2 — Dwell state

**Where the 3-day yellow dwell persists (regime).** In the standalone telemetry
store `regime_history.json` (`regime_history.py:31`), explicitly kept **out of
state.json** and **not** rebuilt by `recompute_derived` because it depends on
market bars, not the executions ledger (`regime_history.py:4-8`). The dwell is
driven by replaying the persisted **published** series: `prior_published()`
(`regime_history.py:90-97`) → `regime_genius.compute_trace(..., prior_published)`
(`screening.py:205-206`). Dwell logic is `regime_genius.apply_dwell`
(`regime_genius.py:98-149`).

**Bar-replay vs persisted state (preferred = replay).** `regime_history.backfill`
already demonstrates a **full replay** of the dwell from bars alone: it recomputes
`compute_trace` over growing prefixes and accumulates `published`
(`regime_history.py:193-200`). The identical pattern derives per-symbol dwell from
each name's bar history — matching the "derive, don't persist" philosophy. **No
state.json persistence is required.**

**Intended asymmetry — and a semantics collision.** The task's Symbol Genius dwell
is *GREEN→YELLOW dwells 3 days; RED is immediate.* The **regime** dwell is
**not** that: inside the yellow window it holds YELLOW even against a raw RED
(`regime_genius.py:130-134`; docstring "a yellow condition cannot change for at
least N days regardless of the raw vote," `regime_genius.py:111-113`).

> **Finding:** `apply_dwell` cannot be reused as-is for Symbol Genius — its hold is
> symmetric (holds yellow vs *both* green and red), while Symbol Genius wants
> hold-vs-green-only / red-immediate. Symbol Genius needs a **new, asymmetric**
> dwell function and its own `PROPOSED_DEFAULT` dwell constant (do not overload the
> HARD `GENIUS_YELLOW_DWELL_DAYS`). Also note stock lights ship **no dwell in v1**
> by design (`stock_lights.py:11-12`) — so this is genuinely new surface.

**SAR seed path-dependence at symbol scale.** Parabolic SAR is prefix-causal
**only** when every recompute starts from bar 0 — it seeds from the first two bars
(`indicators.py:305-307`), and the regime backfill anchors every day's recompute
to the earliest cached bar, never a rolling sub-window (`regime_history.py:184-196`;
pinned by `test_regime_regression.py:95` `test_sar_is_prefix_causal_equals_full_history`
and `:108` `test_four_light_regime_prefix_equals_full_history`).

Per-symbol, this worsens:
1. **Cost:** every one of ~530 names needs a full-prefix replay from its earliest
   bar to reproduce a dwell episode (vs one SPY series today).
2. **Reproducibility drift:** each symbol's parquet has a *different* earliest bar
   and depth (`data_handler` cache, `HISTORY_DAYS = 320` calendar days ≈ ~220
   trading bars, `config.py:271`), and that earliest bar **moves as old bars age
   out of the cache**. Because SAR reseeds from bar 0, the same historical date's
   dwell can recompute to a different value after the cache rolls — a
   reproducibility exposure the single, long-retained SPY series does not have as
   acutely. This is the known open flag, and it does worsen across a full scan
   universe.

---

## Q3 — Classifier inputs

**Daily-bar depth (the binding constraint).** `HISTORY_DAYS = 320` is a
**calendar-day** window (`config.py:271`; used as `now - timedelta(days=320)` in
`data_handler._fetch:136`), i.e. **~220 trading bars**. SMA200 needs 200 bars, so
it *barely* computes but yields only ~20 bars of SMA200 *series* — far short of a
robust 150-day slope plus base counting. The classifier's stated need is **≥250
bars**.

> **Finding (major):** current cache depth (~220 trading bars) is **insufficient**
> for the classifier. Also, the Alpha-Vantage fallback uses `.tail(HISTORY_DAYS)`
> = up to 320 *rows* (`data_handler.py:147`), so provider choice changes depth
> (~220 via Schwab start-date vs up to 320 via AV) — an inconsistency to fix
> before the classifier can rely on depth. Raising depth is a Phase-1 precondition.

**Parquet cache coverage / insufficient-history behavior.** One parquet per symbol
under `active_cache_dir()` (`data_handler.py:82-84`), fresh for 12h
(`data_handler.py:87-91`); demo mode is purely cache-backed
(`data_handler.py:172-173`). On insufficient history, indicators return **`None`**
(`indicators.sma:30-31`) and the scorecard propagates `None` fields
(`metrics/scorecard.py:159-163`, `201-231`). **There is no explicit
`INSUFFICIENT_DATA` enum anywhere** — the current contract is silent `None`.

> **Finding:** `classify_symbol` must return an explicit `INSUFFICIENT_DATA`
> outcome (never a guessed stage). It surfaces on the scorecard row where the
> other metric fields are emitted (`metrics/scorecard.py:201-231`, `score_ticker`
> `:374-458`), and the shared VERDICT (Q5) must map it to BLOCKED/unknown, not GO.

**Where each input is computed today, and whether it is pure over bars:**

| Classifier input | Exists today? | Location | Pure over bars? |
|---|---|---|---|
| OBV, OBV 20-EMA | **Yes** | `_obv_series` `metrics/scorecard.py:149-152`; `obv_20ema` `:176`; `obv_vs_ema` `:90-96` | Yes (but lives in `metrics/scorecard`, not `indicators`) |
| ATR vs 5-day-EMA (ATR posture) | **Yes** | `atr_5ema` `indicators.py:135-141`; `atr_momentum` `indicators.py:144-151` **and** `metrics/scorecard.py:128-132` | Yes |
| Price-divergence check (price vs OBV) | Partial | OBV distance `obv_vs_ema:90-96`; no explicit divergence-vs-price logic | Buildable from pure inputs |
| 50-day up/down **volume ratio** | **No** | — (only `volume_ratio` = vol/vol20 `:76-80`, `volume_acceleration` = vol5/vol20 `:83-87`) | New (pure) |
| Accumulation-vs-distribution **day count** | **No** | — | New (pure) |
| 150-day **slope** | **No** (template exists) | `ma50_slope` `metrics/scorecard.py:66-73` is MA50/`T.MA50_SLOPE_LOOKBACK`, not price/150 | New (pure), templated on `ma50_slope` |
| SMA200, base counting | SMA200 yes (`indicators.sma(df,200)`, `compute_inputs:185`); base counting **No** | — | New (pure); see Q4 |

Net: **OBV and ATR-posture are already pure functions**; **up/down volume ratio,
accumulation/distribution day count, 150-day slope, and base counting are new** —
all implementable as pure functions over bars.

---

## Q4 — Base-count memory

Base counting has memory (increment on breakout to new highs from a ≥25-day
consolidation with <~30% depth; reset on undercut of a prior base low or a
completed decline). Options:

- **(a) Full replay inside `classify_symbol(bars)`** — pure, self-contained.
- **(b) Derived in `recompute_derived()`** — **architecturally wrong here.**
  `recompute_derived` keys off the executions/positions ledger only; regime (also
  market-bar-derived) is explicitly excluded for exactly this reason
  (`regime_history.py:4-6`). Base count is market-bar-derived, not execution-derived,
  so it does not belong in that recompute.
- **(c) Persisted state (state.json)** — violates the single-writer / append-only /
  derived-not-hand-edited invariant (CLAUDE.md: state.json is the single source of
  truth, the execution log is append-only, positions/ledgers are *derived*). This is
  the same argument `regime_history` uses to stay out of state.json
  (`regime_history.py:4-8`).

> **Recommendation: (a) full replay inside `classify_symbol(bars)`.** It is pure,
> mirrors the Parabolic-SAR prefix-causal precedent (`indicators.py:283-335`) and
> the regime backfill replay (`regime_history.py:160-209`), and needs **no
> state.json schema change**. If replay cost later proves a problem across the
> universe, the correct fallback is a **standalone derived telemetry store** in the
> `regime_history.json` / `iv_history` / `burn_marks` mold (market-data telemetry,
> recomputable, out of state.json) — **never** state.json. Same bar-depth caveat as
> Q3 applies: replay is only as good as the cached history.

---

## Q5 — Scan pipeline & UI inventory

### Backend row producers

**`screening._stock_row`** (`screening.py:265-349`) — gate-oriented. Emits
`ticker, sector, rs3m_vs_spy, rs3m_vs_sector, rs1m_vs_spy, rs1m_vs_sector,
is_sector_etf, is_etf, atr_pct, lights, greens, verdict, insufficient, vetoes,
vetoed, veto_reasons, right_spot, enterable, stock_green, consolidating,
blocked_by, rank_key, status` (`:321-349`). Worst-signal-wins `blocked_by`
(`:303-313`), `status` ∈ ready/wait/no (`:315-320`).

**`metrics/scorecard.py`** — the row the **frontend renders**. `metrics_for`
(`:201-231`) emits `price, rs3m_vs_spy, rs3m_vs_sector, pct_above_ma21,
pct_above_ma200, atr_extension, below_ma50, below_ma200, ma50_slope,
volume_ratio, volume_acceleration, obv_above_ema, obv_pct_distance, mfi,
atr_momentum`; `score_ticker` (`:374-458`) additionally lifts `lights /
stock_verdict / right_spot` from the entry gate's L3/L4 detail (no recompute,
`:415-421`), adds `juice_weekly_pct / net_juice_weekly_pct` (`:433-436`),
earnings, and the `GO/CAUTION/AVOID` verdict (`compute_verdict:237-291`,
applied `:455-457`).

### Frontend columns (`Scorecard.jsx`, declarative `COLUMNS` `:12-67`)

| Column | Source field | Restructure class |
|---|---|---|
| Ticker (`:13`) | `row.ticker` (+ etf flags) | **promote** → `SYMBOL` |
| Lights (`:18-31`) | `r.lights`, `r.right_spot` | **promote → re-source** to Symbol Genius `SYM` (4th light swaps to SMA50>SMA200) |
| Price (`:33`) | `r.price` | keep (support) |
| RS3M SPY (`:34`) | `r.rs3m_vs_spy` | **demote** (drawer) |
| RS3M Sec (`:35`) | `r.rs3m_vs_sector` | **demote** |
| %>MA21 (`:36`) | `r.pct_above_ma21` | **demote** |
| ATR ext (`:37`) | `r.atr_extension` | **demote** |
| MFI (`:38`) | `r.mfi` | **demote** |
| Vol× (`:39`) | `r.volume_ratio` | **demote** (feeds INST up/down-vol) |
| ATR mom (`:40`) | `r.atr_momentum` | **demote** (feeds BASE ATR posture) |
| OBV (`:41`) | `r.obv_above_ema` | **demote** (feeds INST) |
| Juice/wk (`:42-53`) | `r.juice_weekly_pct` | keep (drives Ready sort) |
| Earnings (`:54-65`) | `r.earnings_days` | keep (gate input) |
| Verdict (`:66`) | `r.verdict` | **replace** → new READY/CAUTION/BLOCKED |

No column is strictly *dead*: the demoted readouts are classifier inputs or
drawer detail. New columns: **BASE, INST**.

**Header regime light — confirmed, and per-row MKT correctly absent.** The
single page-level regime light renders in the **Navbar** (`Navbar.jsx:67-70`,
`<Light status={regimeStatus} label="Regime">`), fed from `Overview`
(`Overview.jsx:385-387`) via app state (`App.jsx:19`, `:176`). The Scan table adds
only a text **banner** when yellow/red (`Scorecard.jsx:110-119`, rendered
`:359-363`). There is no per-row MKT column and none should be built — regime is a
verdict input only.

**BASE/INST as separate sortable columns from a single classifier call.** `COLUMNS`
is a declarative array (`Scorecard.jsx:12-67`); `sortRows` handles any `num: true`
column generically (`:121-139`); a new sortable column = pushing one `{key,label,
num:true}` object (+ optional `COLUMN_HELP` entry `:72-102`). One classifier call
per row is naturally supported: `score_ticker` already computes per-ticker once
(`metrics/scorecard.py:374-458`) — add a single `classify_symbol(bars)` call there,
split its `(BaseStage, InstFlow)` into two row fields, and back two `COLUMNS`
entries. **Display-only split, one call.**

**Short labels location.** The idiomatic home is a module-level `const` map at the
top of the owning component (e.g. beside `VERDICT_STATUS` `Scorecard.jsx:9`), or —
if reused across tabs — exported from `ui.jsx` like `GENIUS_LIGHT_LABELS`
(`ui.jsx:53-59`). **Not** truncated strings in JSX; the current inline OBV `↑/↓`
render (`Scorecard.jsx:41`) is the anti-pattern to replace with an enum→label map.

**Ready-to-Enter assembly + the shared-verdict requirement.** `/api/scan/ready`
composes the scorecard **GO subset + Level 5** (`app.py:135-164`); the frontend
`ReadyToEnter.jsx` renders server order and does **not** sort client-side — the
task's `net_juice_per_week` maps to the already-present `net_juice_weekly_pct`
(`metrics/scorecard.py:436`, surfaced `app.py:202`), so only a sort needs adding.

> **Finding (spine of the restructure):** `VERDICT` is computed **three** ways
> today — `stock_lights.verdict` (GREEN/YELLOW/RED, `stock_lights.py:102-113`),
> `entry_gate` `cleared_level → READY TO ENTER/WAIT` (`screening.py:489-496`), and
> scorecard `compute_verdict → GO/CAUTION/AVOID` (`metrics/scorecard.py:237-291`).
> The scan display and the gate **do not** share a verdict function, and the
> scorecard **deliberately excludes** regime/sector (`metrics/scorecard.py:306-312`,
> `app.py:140-144`). The new worst-signal-wins `VERDICT` (Market Genius ∧ Symbol
> Genius ∧ structure entrability → READY/CAUTION/BLOCKED) must be a **single shared
> function** consumed by the gate, the scan display, and `/api/scan/ready`.
> Precedent exists: `recommendation_runner._entry_candidates` already reuses "the
> Scorecard's own worst-signal GO subset + Level 5, exactly what /api/scan/ready
> composes" (`recommendation_runner.py:119-143`) — this is the "Recommendation
> record sharing the future automation code path" the task references. Extend that
> pattern; do not add a fourth composition.
>
> **Behavioral note:** because today's scorecard verdict *excludes* regime, making
> "a RED regime renders every row BLOCKED" is a real behavior change from the
> current display (which intentionally does not let a red tape blanket the table,
> `metrics/scorecard.py:306-312`). This is expected and correct under the new spec,
> but call it out — it is not a no-op.

---

## Q6 — Gate integration

**Where Levels 3 & 4 evaluate.** `screening.entry_gate`: Level 3 = stock lights
GREEN (`screening.py:444-468`), Level 4 = right spot / consolidation
(`screening.py:470-487`). Stop-on-first-fail = the highest contiguous pass from 1
(`screening.py:489-495`), terminal verdict keys off `cleared == 4`
(`screening.py:496`).

**Where Structure (Level 3.5) inserts.** Between L3 (stock-beats-peers — the
`rs3m_vs_sector < 0` veto is folded into the L3 verdict, `stock_lights.py:59-69`)
and L4 (consolidation). Concretely between `screening.py:468` and `:470`.
Stop-on-first-fail is preserved *if* two things are updated:
1. the terminal check `cleared == 4` (`screening.py:496`) — with a 3.5 (or a
   renumber to 1–5) the "fully cleared" constant changes;
2. `metrics/scorecard.py` hardcodes the stock-level set `_STOCK_GATE_LEVELS = (3,4)`
   (`:312`) and `_GATE_LEVEL_NAMES` (`:303-304`) — a structure level must be added
   there or scorecard will not see structure failures via `_failed_stock_gate_level`
   (`:334-354`).

**ETF path — classifier must run identically.** The ETF-specific branches are:
(1) `rs3m_vs_sector` veto **waived** for ETFs (`stock_lights.py:61`, `evaluate:196`);
(2) RS1M ranking uses `rs1m_vs_spy` for ETFs (`screening.py:279-282`, `:347`);
(3) scorecard growth filters waived for ETFs (`metrics/scorecard.py:254,260,275-282`);
(4) ETF juice bar (`account_gate.py:118-119`); (5) L3 lower "beats-SPY" ETF bar
(`config.py:230`, `rs_vs_spy_min:234-237`). The "deliberate alternate
RS3M-vs-Sector path" is the ETF vs-sector **waiver**.

> **Confirmed:** the classifier reads only price/volume structure — it never
> touches RS-vs-sector or any is_etf branch — so it does **not** route through the
> ETF alternate path and there is **no collision**. It must be called with the
> **same** `classify_symbol(bars)` for ETFs (no is_etf branch inside the classifier).

---

## Q7 — Regression fixtures

**Locating the July 6th XLK snapshot.** It is the synthesized regime fixture
`backend/fixtures/regime/xlk_july6_rollover.parquet`, built by
`build_fixtures.py:90-115` (`xlk_july6_rollover`). No other "July 6 XLK" artifact
exists.

**Bar depth — fails the ≥250 requirement.** `up = 150 + np.linspace(0,70,190)`
(190 bars, `:106`) + `sell[1:]` (17 bars, `:107-108`) = **207 bars**. SMA200
computes but yields only ~7 bars of series — **insufficient** for the classifier's
150-day slope + base counting.

**Symbol Genius outcome on it — conflicts with Fixture A's premise.** The fixture
is deliberately built so the **last bar is ≤2 green** (docstring `:96-98`: the
selloff flips SAR above price and drives ROC(10) negative; the ~70-point tail
selloff also drops close below SMA50). Symbol Genius shares three of those lights
(close>SMA50, SAR, ROC) and swaps in SMA50>SMA200 (still true after only 17 down
bars). So Symbol Genius scores ~1 green → **RED**, not GREEN.

> **Finding:** Fixture A as specified ("Symbol Genius **MAY be GREEN** — proving
> trend lights alone are insufficient") **cannot** be built from this fixture — its
> lights already reject it, so it is a "lights-red" case, not a "lights-green but
> structure-topping" case. A **new** synthetic fixture is needed: ≥250 bars, a long
> advance **just beginning to top** (classifier → TOPPING via 150-slope flattening
> + ATR posture) while ≥3 lights stay green (Symbol Genius GREEN/YELLOW), composing
> to VERDICT=BLOCKED. Do **not** modify the existing fixture to make Symbol Genius
> red (it is already red, and the task forbids that change anyway).

**InstFlow on the synthetic fixtures — degenerate.** All fixtures set **constant
volume** (`Volume = 1,000,000`, `build_fixtures.py:46`). InstFlow is
volume-derived (up/down volume ratio, OBV, accumulation/distribution days), so on
constant volume OBV degenerates to a cumulative sign and up/down volume is
meaningless.

> **Finding:** Fixtures A and B need **volume-varied** data to exercise InstFlow at
> all. The expected InstFlow for a genuine July-6-style high-volume rollover would
> be DISTRIBUTING, but that cannot be demonstrated on the current constant-volume
> fixture.

**Fixture B (red-regime composition).** Regime enters `VERDICT` as an invisible
input; tests already stub `regime()` (the gate reads `regime()`,
`screening.py:411`). Fixture B = a symbol frame yielding SYM-green +
EARLY_ADVANCE×ACCUMULATING, composed under a **stubbed RED regime** → must be
BLOCKED. `sustained_green.parquet` (220 bars) is close in shape but is <250 bars
and constant-volume.

> **Finding:** Fixture B needs a **purpose-built synthetic fixture** (≥250 bars,
> volume-varied) plus a red-regime stub — it cannot be built cleanly from existing
> snapshot data. It is valuable precisely because it pins the invisible-regime-input
> behavior that today's scorecard verdict does **not** have (Q5).

---

## Q8 — Level 2 reframe (sector as veto, not selector)

**Where Level 2 evaluates.** `entry_gate` L2 (`screening.py:431-442`) +
`_compute_sectors` (`screening.py:231-259`).

**The premise is partly stale — Level 2 is already lighter than described.** The
gate already bars on **RS1M vs SPY > `SECTOR_RS1M_MIN` (0.0)** + breadth ≥
`SECTOR_BREADTH_MIN` (60) (`screening.py:435-440`; `config.py:216,221`), **not**
the RS3M>+10% in the task. `SECTOR_RS3M_MIN = 10.0` is now **display-only**
(`config.py:215`; `screening.py:242-244`). `atr_expanding` is computed
(`screening.py:247`) but **not** in `l2_checks` — it is not a gate condition today.

**What changes structurally if Level 2 becomes a veto.** The `strong` flag feeds:
(1) `entry_gate` L2 `pass` → `cleared_level` stop-on-fail (`screening.py:441`,
`:489-495`); (2) `_stock_row` `sector_strong` → `blocked_by "sector"` and `status`
(`screening.py:306-307`); (3) `sectors()[etf].status` green/red/yellow
(`screening.py:250`) consumed by `stock_filter` (`:372`) and the sector UI.
Flipping the predicate to "block only on RS1M<0 **or** breadth collapsing **or**
sector under distribution, else pass" widens `green`, so `cleared_level` reaches
L3 more often and more names surface. Ordering/short-circuit is unchanged (L2 still
before L3/L4). The **scorecard is unaffected** because it already excludes L1/L2
from its verdict (`metrics/scorecard.py:306-312`) — it already treats sector as
context, which is exactly the direction of this reframe.

**Does Level 5 already cover the concentration risk?** **Yes.** One-position-per-
sector is enforced at Level 5 (`account_gate.py:272-281`, `sector_concentration`
check) with `MAX_POSITIONS_PER_SECTOR = 1` (`config.py:883`). So Level 2's implicit
concentration management is already covered downstream; reframing L2 to a veto does
not lose it.

**Do Level 3's stock-RS3M-vs-Sector and the kill switch share computation with
Level 2?** No shared constant, and only the generic RS core is shared:
- Level 3 "beats peers" is the `rs3m_vs_sector < 0` veto using the **direct**
  `indicators.rs3m(df, sector_df)` ratio (`stock_lights.py:59-69`), floor
  `STOCK_RS_VS_SECTOR_MIN` (`config.py:231`).
- Kill switch uses the **same direct** `indicators.rs3m(stock, sector_df)`
  (`kill_switch.py:38-48`).
- Level 2 uses `indicators.rs1m(sector_etf, spy)` — a **different** computation
  (sector-vs-SPY, 1-month), floor `SECTOR_RS1M_MIN` (`screening.py:244`,
  `config.py:221`).

They share only the generic `relative_strength` core (`indicators.py:192-214`), of
which `rs1m`/`rs3m` are thin lookback wrappers (`:217-226`).

> **Confirmed:** changing `SECTOR_RS1M_MIN` cannot affect the Level-3 or kill-switch
> `rs3m_vs_sector` checks — different lookback, different benchmark, different
> constant. Nothing breaks.

**Sizing-modifier location (cite only).** Contracts resolve to
`config.LEAP_CONTRACTS` (`account_gate.py:218`; `config.py:348`, per-trade
editable); `proposed_cost = leap_cost * contracts * 100`
(`account_gate.py:230`). A sector-strength SIZING modifier (strong=full,
neutral=reduced) would attach where `contracts` is set — `account_gate.evaluate`'s
`contracts` param and the Execute flow that supplies it. **Not designed here.**

**Difference-approximation exposure — already closed in current code.** The
"RS-vs-sector = difference of RS-vs-SPY" approximation is **not used** in these
paths. The kill switch explicitly uses the **direct ratio**, tagged
`HARD_CFM_RULE / KILL_SWITCH_RS_SOURCE` (`kill_switch.py:21-27`); the scorecard
does likewise (`metrics/scorecard.py:205-207`); config reiterates "the ratio, not
the vs-SPY difference approximation" (`config.py:922`). Level 2 uses sector-vs-SPY
`rs1m` **directly** (`screening.py:244`).

> **Finding:** the reframe does **not** increase exposure to the difference
> approximation — that approximation is not on any of these code paths. The task's
> "known open flag" is, in the current code, already resolved to the true ratio.

---

## Recommended Phase 1 scope (ordered, smallest safe increments)

0. **Precondition — bar depth.** Raise cached daily depth to reliably ≥250 trading
   bars and reconcile Schwab vs Alpha-Vantage depth (`config.HISTORY_DAYS` is
   calendar-day based ≈220 trading, `config.py:271`; AV `.tail` path differs,
   `data_handler.py:147`). Everything else is starved without this. *(No new
   feature; a data precondition.)*
1. **Pure `classify_symbol(bars) -> (BaseStage, InstFlow | INSUFFICIENT_DATA)`.**
   Full-replay base counting (Q4 option a); reuse existing pure inputs (OBV, ATR
   posture, `ma50_slope` template) and add the new pure functions (up/down volume
   ratio, accumulation/distribution day count, 150-day slope). Explicit
   `INSUFFICIENT_DATA`, never a guessed enum. All thresholds tagged
   `PROPOSED_DEFAULT`. Unit-tested on **volume-varied** fixtures.
2. **Symbol Genius light-set.** Add a 4th-light injection point to `genius_lights`
   (additive, not a fork, not a `compute_lights` edit) for `SMA50>SMA200`; give it
   its **own** params object (never `default_params`) and its **own** warm-up
   (≥200 bars); reuse the stock verdict mapping (`stock_lights.verdict`).
3. **Single shared `VERDICT` function.** Worst-signal-wins(Market Genius regime,
   Symbol Genius color, structure entrability grid) → READY/CAUTION/BLOCKED.
   Consumed by the entry gate, the scorecard display, and `/api/scan/ready` —
   replacing the three parallel compositions (Q5). RED regime ⇒ every row BLOCKED.
4. **Gate Level 3.5 (structure).** Insert between L3 and L4; update the terminal
   `cleared` check (`screening.py:496`) and `metrics/scorecard._STOCK_GATE_LEVELS` /
   `_GATE_LEVEL_NAMES` (`:303-304`, `:312`); preserve stop-on-first-fail.
5. **Scan-table restructure.** Collapse to `SYMBOL | SYM | BASE | INST | VERDICT`;
   demote readouts into the existing expand drawer (`Scorecard.jsx:227-241`); BASE
   and INST as declarative sortable `COLUMNS` from the single `classify_symbol`
   call; enum→label maps (in `Scorecard.jsx` or exported from `ui.jsx`).
6. **Ready-to-Enter sort by `net_juice_per_week`** (already present as
   `net_juice_weekly_pct`, `metrics/scorecard.py:436`).
7. **Fixtures A & B** (new, ≥250 bars, volume-varied) + the red-regime stub for B.
8. **Level 2 veto reframe** — independent; can land after the classifier. Block
   only on RS1M<0 / breadth collapsing / sector under distribution; Level 5 already
   covers concentration (`account_gate.py:272-281`).

## Descope from Phase 1 (explicit)

- **Symbol Genius YELLOW on open-position cards (Positions surface).** This is a
  clean **reuse point** — the same shared `VERDICT`/Symbol Genius function, a second
  consumer alongside the scan — but it is not required for the scan restructure.
  **Descope**, and record the reuse point so Phase 2 wires Positions to the same
  function rather than re-deriving.
- **Symbol Genius asymmetric dwell** (green→yellow 3d, red-immediate). New function
  + new `PROPOSED_DEFAULT` constant; shadow-log flip frequency first, matching the
  existing "no stock-level dwell in v1" stance (`stock_lights.py:11-12`).
- **Sector-strength SIZING modifier** (Q8 optional) — cite-only here; not designed.

## Collisions with `HARD_CFM_RULE` / the locked math baseline

- **`STOCK_LIGHTS_WARMUP_BARS = GENIUS_SLOW_MA = 50` (HARD, `config.py:251`)** is
  the SAR warm-up for stock lights. Symbol Genius's SMA200 4th light needs ≥200
  bars, so its insufficient/warm-up boundary is **200, not 50**. Do **not** overload
  the HARD 50-bar constant — Symbol Genius needs its own `PROPOSED_DEFAULT` warm-up.
- **`GENIUS_YELLOW_DWELL_DAYS = 3` (HARD, `config.py:191`) + `apply_dwell`
  semantics** (`regime_genius.py:98-149`) are symmetric-hold; Symbol Genius's dwell
  is asymmetric (red-immediate). Do **not** reuse `apply_dwell` or the HARD dwell
  constant — new function + new `PROPOSED_DEFAULT`.
- **`compute_lights` byte-identical guarantee** (`regime_genius.py:46-56`, pinned by
  `test_regime_regression.py:95,108`). The Symbol Genius 4th light must be a
  **separate light-set assembly**, never an edit to `compute_lights`.
- **`GENIUS_VOTE_GREEN_MIN = 3` (HARD, `config.py:190`)** governs the *market* vote;
  Symbol Genius must use the *stock* verdict mapping (4/4=GREEN, 3=YELLOW, ≤2=RED,
  `stock_lights.py:102-113`), not the market vote — no collision as long as that
  separation is kept.
- **`KILL_SWITCH_RS_SOURCE` direct-ratio (HARD, `kill_switch.py:27`)** is untouched
  by the Level 2 reframe (different computation, Q8) — flagged so it stays untouched.
- **No state.json schema change** is required for base counting (Q4 option a) —
  respects the derived-not-hand-edited / single-writer / append-only invariant.

**Every new classifier and Symbol Genius threshold is `PROPOSED_DEFAULT`** and must
be tagged as such in any Phase 1 proposal; the only `HARD` items touched are the
ones above, and the audit's recommendation is to work **around** them (new
constants/functions), not through them.
