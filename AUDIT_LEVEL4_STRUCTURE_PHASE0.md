# AUDIT — Level 4 Chart-Structure Metrics (Shadow Mode) + Phase-Aware Volume Check

**Phase 0. Written audit only. No implementation code in this change.**

Repo state: branch `claude/level-4-chart-structure-volume-9y4dbf`, baseline
`python -m pytest backend -q` → **1191 passed, 0 failed** (after installing the
`cffi`/`cryptography` pair the container shipped broken; `pywebpush`/`http-ece`
still fails to build, which `CLAUDE.md` documents as expected and non-fatal).

Every claim below is cited `file:line`. Where the prompt's stated premise does not
match the code, the audit says so explicitly rather than restating the premise —
five of them are stale or wrong, and two of those materially change the Phase 1
spec. Those are collected in **§8 Premise corrections**.

---

## 1. Level 4 inventory

### 1.1 What Level 4 actually is

The gate is assembled in `screening.entry_gate` (`backend/screening.py:602`). It
has **six** levels, not four — 1, 2, 3, **3.5**, 4, and a Level 5 that is *not*
evaluated here. Level 4 is built at `backend/screening.py:712-736` and is a pure
**READ** of a `right_spot` dict that was already computed upstream by
`stock_lights.compute` (`backend/stock_lights.py:272`) and lifted off the row at
`backend/screening.py:713`:

```python
spot = row.get("right_spot") or {"checks": [], "pass": False}
spot_by_id = {c["id"]: c for c in spot.get("checks") or []}
```

Level 4's `pass` is `bool(spot.get("pass"))` (`backend/screening.py:726`) — the
three `_check(...)` entries at `backend/screening.py:715-724` are **display
relabels only**. They re-read `.get("pass")` off the same dicts; they do not
re-evaluate anything. Consequence for Phase 1: **adding a check to
`screening.py`'s `l4_checks` list would not change the level's pass/fail**, but
adding one to `stock_lights._right_spot_from` would. That asymmetry is the safest
possible place to add a shadow metric — see §6.3.

### 1.2 The three checks — the authoritative table

All three are produced by `stock_lights._right_spot_from`
(`backend/stock_lights.py:207-220`) over inputs gathered once by `_spot_inputs`
(`backend/stock_lights.py:223`).

| # | check id | input series / function | threshold | provenance tag | feeds |
|---|---|---|---|---|---|
| 1 | `atr_pct` | `indicators.atr_pct(df)` — 9-day Wilder ATR ÷ close × 100 (`backend/indicators.py:89`) | `<= config.CONSOLIDATION_ATR_PCT_MAX` = **5.0** (`backend/config.py:248`) | **NONE — untagged** | verdict + suitability |
| 2 | `atr_5d_ema` | `indicators.atr_momentum(df)` — ATR ÷ ATR_5EMA (`backend/indicators.py:144`) | `<= stock_lights.atr_momentum_max(ruleset)` (`backend/stock_lights.py:193`): legacy `SPOT_ATR_MOMENTUM_MAX` = **1.0** (`backend/config.py:267`), proposed `L4_ATR_EXPANSION_MAX` = **1.05** (`backend/config.py:334`) | `PROPOSED_DEFAULT` on both constants | verdict + suitability |
| 3 | `extension` | `indicators.atr_extension(df)` — `(close − SMA21) ÷ ATR` (`backend/indicators.py:154-167`) | `<= config.SPOT_ATR_EXTENSION_MAX` = **1.5** (`backend/config.py:266`) | `PROPOSED_DEFAULT` | verdict + suitability |

Failure semantics: a `None` value **fails** (`backend/stock_lights.py:209, 212,
215` — every predicate is `value is not None and value <= threshold`). The
docstring states the intent: *"A check with no data (None) fails conservatively
(you can't confirm a right spot you can't measure)"* (`backend/stock_lights.py:236`).
`blocked_by` is `[f"spot:{c['id']}" ...]` (`backend/stock_lights.py:219`).

**`CONSOLIDATION_ATR_PCT_MAX` carries no provenance tag** (`backend/config.py:248`
— the comment is descriptive, not a tag). It is the only Level-4 threshold in that
state. Phase 1 must not touch it; the audit flags it so a later pass can tag it
deliberately.

### 1.3 What Level 4 does NOT contain

The prompt's premise — *"low ATR%, price near MA21, ATR contracting, RSI 40–60"* —
is **wrong on two of four legs**:

* **There is no RSI check anywhere in the gate.** `indicators.rsi`
  (`backend/indicators.py:55`) exists but is never called by Level 4 or by any
  gate level. The 40–60 band the prompt is remembering is the **MFI** band in the
  *suitability* lens: `T.MFI_MIN` / `T.MFI_MAX` (`backend/metrics/thresholds.py:34-35`),
  applied at `backend/metrics/scorecard.py:279-281`. It is tagged
  `[HARD RULE]` — *"the 40–60 MFI band is from Travis's own CFM entry criteria"*
  (`backend/metrics/thresholds.py:33`). Do not touch it.
* **There is no "price near MA21" percent-band check.** Leg 3 is `extension`,
  measured in **ATR units** (`(close − SMA21) ÷ ATR ≤ 1.5`), not percent. This
  matters for the Phase-1 `consolidation_phase` derivation — see §3.3 and §8.2.

### 1.4 Dead code at Level 4 — a real finding

`indicators.consolidating(df)` (`backend/indicators.py:421-428`) is the *original*
single-flag consolidation test — *"Low ATR% and price near MA21"* — and it is the
only thing in the tree that reads `config.CONSOLIDATION_MA21_DIST_MAX` (= 4.0,
`backend/config.py:249`; read at `backend/indicators.py:428`).

**`indicators.consolidating` has zero call sites.** Verified by
`grep -rn "consolidating(" --include=*.py backend/` → the definition only. It was
superseded by the three-check right spot; both `screening.py:490` and
`entry_context.py:307` now carry `"consolidating": spot["pass"]` as an explicit
back-compat alias, with comments saying exactly that (`backend/screening.py:488-490`,
`backend/entry_context.py:305-307`).

So **`config.CONSOLIDATION_MA21_DIST_MAX` is a dead constant reachable only through
dead code.** This is the trap in the Phase-1 spec: the spec says to derive
`consolidation_phase` by reusing *"the exact existing checks — ATR contracting AND
price within MA21 proximity band"*. The MA21-proximity band **is not an existing
live check**. Reusing it would resurrect a dead threshold and quietly introduce a
fourth Level-4 input. See §8.2 for the recommended substitution.

### 1.5 Where else Level 4 is consumed

* `scan_triggers.gate_blocks` (`backend/scan_triggers.py:274-295`) extracts the
  failing Level-4 checks into `blocks`, which **do** carry verdict authority.
* Trigger kinds per check id (`backend/scan_triggers.py:82-85`): `atr_pct` →
  `CONDITIONAL`, `atr_5d_ema` → `ESTIMATED`, `extension` → `ESTIMATED`. None is
  `SAFETY`, so a Level-4-only failure is benchable (§5).
* `screening.py:768-781` replays Level 3 and Level 4 per ruleset for the shadow
  record — a read of `right_spot_by_ruleset` (`backend/screening.py:733-735`),
  never a re-evaluation (`backend/scan_triggers.py:276-277` comments this too).

---

## 2. Verdict vs suitability wiring

### 2.1 The canonical verdict path

There are **two independent verdicts** on every scan row, and this is deliberate.

**The canonical one** is `row["verdict"]` (`backend/metrics/scorecard.py:612`),
produced by the `_fold` closure at `backend/metrics/scorecard.py:592-601`:

```
scan_verdict.compose_verdict(regime, SYM, base_stage, inst_flow)   # 3 signals
        ⊕ scan_triggers.gate_blocks(gate, ...)                     # the FULL gate
        ⊕ scan_triggers.juice_floor_block(net, gross)              # NET juice safety
    → scan_triggers.compose_row_verdict(...)                       # worst-wins
```

* `compose_verdict` (`backend/scan_verdict.py:62`) is worst-of-three over regime /
  symbol / structure on the ladder `READY < CAUTION < WATCH < BLOCKED`
  (`backend/scan_verdict.py:45`).
* `compose_row_verdict` (`backend/scan_triggers.py:522`) folds every failing gate
  block onto that. A `SAFETY` block forces `BLOCKED`; a clearable one degrades to
  `WATCH` (`backend/scan_triggers.py:16-20`).
* Row fields written: `verdict`, `verdict_reasons`, `binding`, `triggers`,
  `path_to_ready`, `eligible_days`, `bench` (`backend/metrics/scorecard.py:612-618`).

So a Level-4 failure **does** reach the canonical verdict — via `gate_blocks`, not
via the `l4_checks` display list. This was the explicit fix for the "AAPL READY +
fails entry gate level 4" bug (`backend/metrics/scorecard.py:602-606`,
`backend/scan_triggers.py:5-8`).

`screening.entry_gate` also emits its own `verdict` string — `"READY TO ENTER" if
cleared == 4 else "WAIT"` (`backend/screening.py:738-739`), where `cleared` is the
highest **contiguous** passing level (`_cleared_level`, `backend/screening.py:557-566`).
Because Level 3.5 sits between 3 and 4 in the list, `cleared == 4` requires 3.5
**and** 4 to pass (`backend/screening.py:707-710`). This string is the gate's own
readout; the scan table does not display it.

### 2.2 Where SUITABILITY: CAUTION is computed

`compute_verdict(metrics)` — `backend/metrics/scorecard.py:242-297`. The CAUTION
rules run at `backend/metrics/scorecard.py:277-295`; AVOID dominates and returns
early at `backend/metrics/scorecard.py:273-274`.

It is assigned to the row at **two** points in `score_ticker`:

1. **Gate short-circuit** (`backend/metrics/scorecard.py:692-698`): if
   `_failed_stock_gate_level(gate)` (`backend/metrics/scorecard.py:340`) returns a
   level from `_STOCK_GATE_LEVELS = (3, 3.5, 4)` (`backend/metrics/scorecard.py:317`),
   suitability is forced to `"AVOID"` with reason `"fails entry gate level 4
   (consolidating)"` (`_GATE_LEVEL_NAMES`, `backend/metrics/scorecard.py:306-307`),
   and `score_ticker` **returns early** (`backend/metrics/scorecard.py:698`).
2. **Otherwise** (`backend/metrics/scorecard.py:702-704`) from `compute_verdict`.

### 2.3 Does the canonical verdict consume suitability? — **No.**

Verified three ways:

* **Ordering.** `row["verdict"]` is written at line **612**; `row["suitability"]`
  at lines **695 / 703**. The verdict is finalised ~90 lines before suitability
  exists on the row.
* **Inputs.** `_fold` (`backend/metrics/scorecard.py:592-601`) reads only
  `regime_color`, `sym["color"]`, `cls["base_stage"]`, `cls["inst_flow"]`, `gate`,
  `ext_context`, and `juice_block`. `metrics.get("volume_ratio")` and every other
  suitability input are absent.
* **Declared intent.** `backend/metrics/scorecard.py:562-565`: *"The older
  GO/CAUTION/AVOID CFM-suitability lens is retained as `suitability` (a demoted
  drawer readout)"*; and `backend/metrics/scorecard.py:690-691`: *"Not the headline
  verdict — a demoted signal."*

**So READY + SUITABILITY: CAUTION on the same row is by design, not a bug.** The
prompt's "known prior symptom" is a description of the *intended* post-fix state.
`suitability` is rendered only inside the expanded drawer
(`frontend/src/components/Scorecard.jsx:472` `<Readout label="Suitability" .../>`;
reasons at `:475-478`), never in the row's verdict column.

**But `suitability` is not decorative — it still has real authority downstream:**

| consumer | line | what it gates |
|---|---|---|
| `queue_state` | `backend/queue_state.py:67` | `suitability == "GO"` → internal queue membership |
| `recommendation_runner` | `backend/recommendation_runner.py:135` | `"GO"` → recommendation candidate pool |
| `recommendation_runner` | `backend/recommendation_runner.py:146` | `"verdict": r.get("suitability")` on the emitted record |
| `refresh_policy` | `backend/refresh_policy.py:113` | `"GO"` → the intraday hot-refresh set |

This is the load-bearing consequence for Phase 1: **a spurious volume CAUTION
demotes a name out of the recommendation pool and out of the hot-refresh set**, even
though the headline verdict still reads READY. The phase-aware volume fix therefore
changes real behaviour on those three paths — it is *not* a display-only change,
and the Phase 1 tests must cover it. `recommendation_runner.py:123` names the split
explicitly: *"regime-unaware CFM-suitability signal (`suitability`), not the
regime-aware [verdict]"*.

---

## 3. Volume check location

### 3.1 The threshold

`VOLUME_RATIO_MIN = 0.8`, `backend/metrics/thresholds.py:28`, tagged
`[CALIBRATE] proposed default` (`:27`). One definition, one reader.

### 3.2 Call sites — complete

| # | site | role |
|---|---|---|
| 1 | `backend/metrics/scorecard.py:81` | `volume_ratio(volume, volume_20ma)` — the pure metric (`volume / volume_20ma`, `None` on missing or zero denominator) |
| 2 | `backend/metrics/scorecard.py:230` | computed into the metrics dict inside `metrics_for` |
| 3 | `backend/metrics/scorecard.py:282-284` | **the only threshold comparison in the tree** |
| 4 | `backend/metrics/scorecard.py:304` | display rounding, 2 dp (`_ROUND`) |
| 5 | `backend/entry_context.py:187` | frozen into the entry-context snapshot at execution |
| 6 | `frontend/src/components/Scorecard.jsx:468` | `<Readout label="Vol×" .../>` in the drawer |

The comparison verbatim (`backend/metrics/scorecard.py:282-284`):

```python
vr = metrics.get("volume_ratio")
if not is_etf and vr is not None and vr < T.VOLUME_RATIO_MIN:
    caution.append(f"volume ratio {vr:.2f} < {T.VOLUME_RATIO_MIN:g} (thin participation)")
```

Inputs: `inp["volume"]` = the last bar's `Volume`; `inp["volume_20ma"]` = its
20-bar rolling mean (`backend/metrics/scorecard.py:196-197`, window from
`config.VOL_AVG_WINDOW = 20`, `backend/config.py:372`).

### 3.3 Does it know about market phase? — **No, in any sense.**

The only conditioning is `not is_etf` — an asset-class waiver, documented at
`backend/metrics/scorecard.py:246-256` and `:277-279` (the MFI band, the thin-volume
floor and the ATR-expansion check are waived for ETFs as growth-stock momentum
filters). There is **no** consolidation, breakout, entry-day or trend-phase input
anywhere in `compute_verdict`'s signature — it takes one flat `metrics` dict
(`backend/metrics/scorecard.py:242`) and never sees the gate, the right spot, or the
structure classifier.

Note the internal inconsistency this creates today: `compute_verdict` **already**
penalises ATR expansion at `backend/metrics/scorecard.py:285-287` (*"wants APP, not
CFM"*) — i.e. it already assumes a consolidation context — while simultaneously
penalising the low volume that a genuine consolidation produces. The two CAUTIONs
are pulling in opposite directions on the same chart. That is precisely the defect
the Phase 1 change targets, and it is worth stating in the commit message.

**Availability of the phase inputs at the comparison site.** `compute_verdict` is
called at `backend/metrics/scorecard.py:702` with `row` — and by that line the row
already carries everything needed:

* `row["atr_momentum"]` — written by `metrics_for` (`backend/metrics/scorecard.py:231`),
  the same figure `right_spot`'s `atr_5d_ema` check compares.
* `row["atr_extension"]` — `backend/metrics/scorecard.py:226`, the same figure
  `right_spot`'s `extension` check compares (both are `(close − SMA21) ÷ ATR`;
  `backend/indicators.py:157-158` explicitly says *"Same figure the scorecard
  reports"*).
* The Level-4 `detail` is reachable off `gate` via `_gate_level_detail(gate, 4)`
  (`backend/metrics/scorecard.py:333-339`), which returns the `right_spot` dict
  with each check's own `pass` — **the exact existing check results, no
  re-derivation**.

**Recommendation:** derive `consolidation_phase` from `_gate_level_detail(gate, 4)`'s
`right_spot.checks[].pass` for `atr_5d_ema` **and** `extension` — those are the two
live checks that mean "ATR contracting" and "price not stretched from MA21". Do
**not** use `indicators.consolidating` or `CONSOLIDATION_MA21_DIST_MAX` (§1.4). When
`gate` is `None` (`backend/metrics/scorecard.py:831` — many `score_ticker` callers
pass no gate), `consolidation_phase` must be `False`, preserving today's behaviour
exactly.

---

## 4. Available inputs

### 4.1 Fetch depth and provider fields

`config.HISTORY_DAYS = 400` **calendar** days (`backend/config.py:381`), documented
as *"≈ ~275 trading bars"* (`backend/config.py:373-374`). Both providers key off the
same start date so the earliest bar is provider-independent
(`backend/data_handler.py:135`; the Alpha Vantage `.loc[start:]` slice and its
rationale at `backend/data_handler.py:145-153`).

**Schwab** — `SchwabClient.get_daily_bars` (`backend/schwab_api.py:307-334`).
Confirmed field names, read at `backend/schwab_api.py:326-330`:

| DataFrame column | Schwab candle key | line |
|---|---|---|
| index | `datetime` (ms epoch → America/New_York → naive) | `:323-324` |
| `Open` | `open` | `:326` |
| `High` | `high` | `:327` |
| `Low` | `low` | `:328` |
| `Close` | `close` | `:329` |
| `Volume` | `volume` | `:330` |

Params: `periodType=year, frequencyType=daily, frequency=1, startDate=<ms>,
needExtendedHoursData=false` (`backend/schwab_api.py:313-314`). **There is no
`adjusted`/`adjClose` field in the request or the response handling** — the six
columns above are the entire contract. Phase 1 must use only these
(prompt DO-NOT #5).

**Alpha Vantage** — `daily_bars` (`backend/alpha_vantage.py:65`) calls
`function=TIME_SERIES_DAILY` (`:69`) and maps `1. open` … `5. volume` (`:76-77`).

### 4.2 Adjusted vs unadjusted — a genuine caveat

`TIME_SERIES_DAILY` is Alpha Vantage's **raw, as-traded** series (the adjusted
series is the separate `TIME_SERIES_DAILY_ADJUSTED` function, which this repo does
not call). Schwab's `pricehistory` is split-adjusted but not dividend-adjusted.
**The two providers therefore disagree across a split**, and neither is
dividend-adjusted.

The repo does not currently document this. It has not mattered because every
existing consumer is short-window or ratio-based (ATR%, ATR momentum, 21/50/200-day
MAs, 63-day RS). It **starts** to matter for a 126- or 252-bar trailing high: an
unadjusted pre-split print sits 2–4× above post-split price and would make
`dist_from_high_pct` read catastrophically negative for a full year after any split.

**Recommendation for Phase 1 (not implemented here):** compute the trailing high
over **`Close`**, not `High`, and additionally guard it — if the trailing max
exceeds the last close by more than a sanity multiple (a `PROPOSED_DEFAULT`, e.g.
2.5×), return `insufficient_data` rather than a number. That converts a silent
wrong reading into an explicit unmeasured one, matching the classifier's
`INSUFFICIENT_DATA` discipline (`backend/structure_classifier.py:20`). It is also
why `dist_from_high_pct` must stay in shadow until calibrated against real fetched
bars.

### 4.3 Computability — each Phase 1 metric

Everything below is computable **offline from the existing cached frames. No new
fetches are required** (prompt DO-NOT #6 is satisfiable).

| metric | window | needs | verdict |
|---|---|---|---|
| `dist_from_high_pct` (126) | 126 bars | `Close` | ✅ comfortable — ~275 bars typical |
| `dist_from_high_pct` (252) | 252 bars | `Close` | ⚠️ **thin margin** — see below |
| `ma21_slope` | 21 + 10 = 31 bars, ÷ ATR (9) | `Close`, `High`, `Low` | ✅ trivially available; `indicators.sma` `:28`, `indicators.atr` `:71` |
| `tightness` | 60 bars | `Close` (+ ATR for the fallback denominator) | ✅ `indicators._atr_series` `:119` gives the ATR-sum fallback |
| `higher_lows` | 30 bars, 3-bar pivot | `Low` | ✅ |
| `consolidation_phase` | — | already-computed Level-4 check results | ✅ pure read, §3.3 |

**The 252-bar caveat is real and must be handled, not assumed away.** 400 calendar
days ≈ 275 trading bars — only ~23 bars of headroom over 252 — and `config.py:378-380`
warns in-tree: *"a cache filled under a shorter old window keeps serving those
shorter frames until it refetches"*. Newly-listed symbols are shorter still. The
`INSUFFICIENT_DATA` path for the 252-bar display value will therefore fire in
production, not just in tests. The four fixture files under
`backend/fixtures/structure/` are 270 bars each and `xlk_july6_rollover.parquet` is
**207** — so the 252-bar leg is *not* computable on the XLK fixture at all, which is
exactly the "insufficient history" test case the Phase 1 spec asks for.

Existing precedent for a 252-bar window over the same frames: `indicators.hv_rank`
(`backend/indicators.py:183`, `lookback: int = 252`), consumed at
`backend/metrics/scorecard.py:552-555`. It already degrades to `None` on short
history. Follow that shape.

### 4.4 Multi-week close clustering

`tightness` needs the range of the last 15 closes over the range of the prior
60-bar advance. Both are plain slices of `df["Close"]`. `structure_classifier`
already does exactly this kind of causal windowed replay — `trend_slope_pct`
(`backend/structure_classifier.py:120`) and `base_count`
(`backend/structure_classifier.py:204`, a full replay detecting sideways stretches
that make no new high). `base_count`'s "no new high for N bars" machinery is the
nearest existing analogue and its conventions should be matched rather than
re-invented.

---

## 5. WATCH vs BENCH status

**The prompt's premise is stale. The WATCH/BENCH de-collapse is already shipped,
implemented and regression-tested.**

`scan_triggers.is_bench` (`backend/scan_triggers.py:566-587`) defines BENCH as a
**derived VIEW over WATCH rows**, never a fifth verdict value
(`backend/scan_triggers.py:18-20`, `:563-565`). A row is BENCH iff:

1. not READY and has ≥1 clearable **gate** block (levels 2–5), **and**
2. no `SAFETY` block, **and**
3. no non-READY **signal** block (regime / symbol / structure).

Rule 3 is the de-collapse itself, and the code says so at
`backend/scan_triggers.py:575-577`: *"This is what keeps WATCH and BENCH from
collapsing into synonyms."*

Regression coverage, all currently passing:

* `backend/test_scan_triggers.py:247-254` — BASING × EARLY_INTEREST intake is
  WATCH-only, never bench. Comment at `:250`: *"This is the WATCH/BENCH de-collapse."*
* `backend/test_scan_triggers.py:257-262` — a YELLOW SYM is WATCH-only, never bench.
* `backend/test_scan_triggers.py:265-274` — clear signals + an L4-only block **is**
  bench.
* `backend/test_scan_triggers.py:277-285` — a safety block excludes from bench.

Downstream the distinction is live: `row["bench"]` (`backend/metrics/scorecard.py:618`),
the `SCAN_BENCH_READY` / `SCAN_WATCH_BENCH` transition events
(`backend/scan_diff.py:28-30`, `:59-71`) with distinct alert priorities HIGH / LOW
(`backend/alerts.py:71-73`), a dedicated BENCH filter chip
(`frontend/src/components/Scorecard.jsx:226, 488`) and a mutually-exclusive count
(`frontend/src/components/Scorecard.jsx:577-581`).

**Recommendation: ship independently. There is nothing to wait on.** This change
must not touch WATCH/BENCH semantics (prompt DO-NOT #7), and it structurally cannot:
`is_bench` reads only `verdict` and `triggers`, and a shadow metric appended to
neither `blocks` nor `triggers` is invisible to it. Note for the Phase 1 test plan:
that invisibility should be **asserted**, not assumed — `is_bench` is the cheapest
place for an accidental authority leak to show up.

---

## 6. Shadow-mode plumbing

There are **two** shipped shadow patterns. Phase 1 should reuse the **weekly-juice
floor** one exactly, as the prompt directs.

### 6.1 The pattern, end to end

**a. Computation — pure, returns an observation, never a block.**
`scan_triggers.shadow_floor` (`backend/scan_triggers.py:426-470`). Its contract is
stated in a banner comment at `backend/scan_triggers.py:350-357`:

> *"Everything below is SHADOW: it is computed, returned and logged, and it has
> ZERO authority. Nothing here is ever appended to the `blocks` list that feeds
> `compose_row_verdict` — that list is what gives a finding verdict authority, so
> keeping the shadow observation OUT of it is the load-bearing invariant of this
> whole feature. There is deliberately NO config switch that would turn any of this
> into a block."*

That is the invariant to replicate verbatim: **the `blocks` list is the authority
boundary.** Note also `pass` is `None` — not `False` — when inputs are unpriceable
(`backend/scan_triggers.py:469-470`): *"an unmeasurable name is unmeasured, never a
recorded failure."* The Phase 1 `insufficient_data` requirement is the same rule.

**b. Row attachment — additive keys only.**
`backend/metrics/scorecard.py:527-531`, with the guarantee restated inline:
*"Deliberately NOT appended to `blocks`: that list is what carries verdict
authority."* And at `:516-518`: *"Purely ADDITIVE row keys … nothing here is
appended to `blocks` below, so the canonical verdict is bit-for-bit what it was
before this block existed."*

Critically, it is attached at line **529**, i.e. **before** `row["verdict"]` is
computed at line 612 — and is still invisible to it, because `_fold` simply does not
read it. Placement is not the safeguard; the input list is.

**c. Logging — extend the existing record, do not fork.**
`scan_rejection_log._record_from_row` (`backend/scan_rejection_log.py:96-165`) reads
five shadow-floor keys off the row at `backend/scan_rejection_log.py:138-142`. The
store: `DATA_DIR/scan_rejection_log.json` (`backend/scan_rejection_log.py:35`),
`SCHEMA_VERSION = 2` (`:45`) with a changelog comment at `:41-44`, append-per-scan-run
keyed by `scan_id` (`record_scan`, `backend/scan_rejection_log.py:269-315`), atomic
tmp-then-`os.replace` (`:64-69`), never in `state.json`, never rebuilt by
`recompute_derived` (`backend/scan_rejection_log.py:18-21`). Single writer: the
nightly sweep at `backend/maintenance.py:215-225`, which reuses the memoized
scorecard rather than recomputing.

**d. Roll-up read.** `scan_rejection_log.summary` (`backend/scan_rejection_log.py:188`)
tallies per-profile floor pass/fail at `:195-213` and returns them at `:239-243`.
Exposed read-only at `GET /api/scan/rejection-stats` (`backend/app.py:921-933`).

**e. Display — visually marked, non-blocking.**
The combined-yield cell tints amber only on `floor.pass === false` and puts the
whole floor read in the hover title, ending with the literal words *"zero blocking
authority"* (`frontend/src/components/Scorecard.jsx:131, 138-143`). The dedicated
panel `ShadowFloorLog` (`frontend/src/components/Scorecard.jsx:864`) carries an
explicit **`NO AUTHORITY`** badge at `:877-881`. A second precedent — the
shadow-ruleset divergence chip — sits at `frontend/src/components/Scorecard.jsx:702-719`
and the shadow SCORE tooltip at `frontend/src/components/Scorecard.jsx:170` (*"SHADOW — a rank over quality inputs;
does not affect the verdict, sizing, or Ready-to-Enter"*).

### 6.2 The seven-point checklist Phase 1 must satisfy

1. Pure function, returns an observation dict, never mutates.
2. `None` / `insufficient_data` for unmeasurable — never a silent `0`, never a
   recorded failure.
3. Attached to the row as **purely additive keys**.
4. **Never appended to `blocks`** — the load-bearing invariant.
5. **No config switch that could grant it authority.** Graduation must be a
   deliberate reviewed code change (`backend/scan_triggers.py:355-357`).
6. Logged by extending `_record_from_row` and bumping `SCHEMA_VERSION` to `3` with a
   changelog line at `backend/scan_rejection_log.py:41-44` — **extend, do not fork
   the store**.
7. Displayed with explicit shadow styling and a "no authority" affordance.

### 6.3 The one place where "additive" is not automatic

The four structure metrics must **not** be added to
`stock_lights._right_spot_from`'s `checks` list (`backend/stock_lights.py:208-217`).
That list feeds `blocked_by` (`:219`) → `right_spot.pass` (`:220`) → Level 4's
`pass` (`backend/screening.py:726`) → `gate_blocks` (`backend/scan_triggers.py:274-295`)
→ the canonical verdict. Adding a check there grants blocking authority in one line.
The safe seam is `score_ticker` (§6.1b) — same place `shadow_floor` and the shadow
SCORE attach.

Two smaller notes for Phase 1:

* `entry_context.py:187` freezes a whitelist of row fields at execution time. The
  new keys are **not** on it and should **not** be added — freezing a shadow metric
  into the execution record implies it informed the decision.
* `scan_triggers._KIND` (`backend/scan_triggers.py:69-97`) must gain **no** entries. An id there is by definition a block.

### 6.4 The annotation hook (`STRUCTURE_LABEL`)

The prompt offers "JSON sidecar **or** state event". **Recommend the sidecar.**

`state.json`'s execution log is the append-only trading record, and
`CLAUDE.md` is explicit that positions and ledgers are *derived* from it by
`recompute_derived`. A subjective compelling/not-compelling label is **not** a
trading fact; putting it there would make `recompute_derived` step over an event
type it has no derivation for. Every comparable non-trading annotation in this repo
already lives outside state: `burn_marks.py` (*"Marks are telemetry, not a trading
record, so — like `iv_history` — they live in `DATA_DIR/burn_marks.json`, OUT of the
append-only state.json execution record"*, `backend/burn_marks.py:17-20`), plus
`iv_history`, `regime_history`, `symbol_genius_history`, `scan_diff_log` and
`scan_rejection_log` itself.

Recommended shape: a new `structure_labels.py` mirroring `burn_marks.py`'s storage
discipline — `DATA_DIR/structure_labels.json`, module `_lock`, atomic
tmp-then-`os.replace`, append-only, keyed `{ticker: [{date, scan_id, label, note}]}`
so a label joins back to the exact scan record via `scan_id`
(`backend/scan_rejection_log.py:300`). Surface as a curl-able
`POST /api/scan/structure-label` alongside the existing read-only endpoints at
`backend/app.py:921-947`. Note that `backend/auth.py:45` maintains an `_OPEN_PATHS`
allowlist — the new route is **not** on it and must not be added; it should sit
behind the normal session auth like every other `/api/scan/*` route.

---

## 7. Regression surface

### 7.1 Fixture location

`backend/fixtures/regime/xlk_july6_rollover.parquet` — **207 bars**, columns
`[Open, High, Low, Close, Volume]`, built by
`backend/fixtures/regime/build_fixtures.py:90-121` (*"An XLK-shaped ETF tape: a long
calm advance that ROLLS OVER hard"*). It is registered in the builder's map at
`backend/fixtures/regime/build_fixtures.py:122`.

It is explicitly pinned as immutable:
`backend/fixtures/structure/build_fixtures.py:13-15` notes that the newer
insufficient-data fixture was *rebuilt* to ≥250 bars precisely because *"the
original xlk_july6_rollover fixture is left untouched because the regime regression
pins it."* **Phase 1 must not regenerate or extend it.**

At 207 bars it is below `structure_classifier.MIN_BARS_BASE = 210`
(`backend/structure_classifier.py:84`), so `BaseStage` reads `INSUFFICIENT_DATA`
(`backend/structure_classifier.py:301-302`) — which is itself pinned at
`backend/test_shares_migration.py:388`.

### 7.2 The assertions that prove verdict logic is untouched

Two suites carry the canonical XLK regression. **`test_gate_ruleset.py` is the one
to extend** — its own header calls it *"the canonical assertion that verdict logic
is untouched"* (`backend/test_gate_ruleset.py:48-50`).

**`backend/test_gate_ruleset.py:52-75`** — `test_1_xlk_july6_identical_under_legacy_ruleset`:

| line | assertion | what it proves |
|---|---|---|
| `:56-58` | `sar` or `momentum` red; `greens < 4` | the four-light vote still denies GREEN |
| `:61` | `indicators.atr_expanding(df) is True` | the ATR input is unchanged |
| `:62-64` | `verdict == RED`; `"veto:atr_expanding_high_ivr" in veto_reasons` | the veto fires independently through the ETF path |
| `:65-66` | low-IVR path still `!= GREEN` | the veto is not the *only* thing denying entry |
| `:71-72` | `res["verdict"] == stock_lights.verdict(greens, insufficient, vetoed)` | the authoritative verdict is the pure function's output — **no rerouting** |
| **`:73`** | **`res["right_spot"] == stock_lights.right_spot(df, LEGACY)`** | **the Level-4 gate output is byte-identical to a fresh legacy evaluation — the single most load-bearing assertion for this change** |
| `:74-75` | `by_ruleset[LEGACY]` verdict and right_spot both equal the authoritative ones | the shadow record added a record, not a decision path |

`backend/test_gate_ruleset.py:78-92` (`test_1b`) adds that the fixture **cannot flip**
under the proposed ruleset either — 0/4 green with the mandatory core red, asserted
for both IVR arms.

**`backend/test_dividend_profile.py:102-133`** carries the same shape plus the
profile-invariance leg (`:127-133`: identical `lights` / `verdict` / `greens` /
`right_spot` under `DIVIDEND_PEER_BENCHMARK`), with a docstring stating the rule
Phase 1 inherits: *"A diff in either is a defect in this work item, not a new
baseline"* (`backend/test_dividend_profile.py:105-108`).

Line `:73` is the assertion to lean on: `right_spot` is a **plain dict of scalars**,
so `==` is a genuine structural byte-comparison. If a Phase 1 metric leaked into
`_right_spot_from`'s `checks` list, that one line fails. It is the tripwire.

### 7.3 Recommended additional Phase 1 regression assertions

Not implemented in Phase 0; listed so Phase 1 has an agreed target:

1. **Whole-row byte-identity.** Snapshot `score_ticker(...)` on a fixture
   pre-change, then post-change assert every pre-existing key is `==`, allowing only
   the new additive keys. Stronger than asserting individual fields.
2. **Zero blocking authority.** Force all four metrics to worst-case on a READY
   fixture; assert `verdict`, `verdict_reasons`, `binding`, `triggers`,
   `path_to_ready`, `eligible_days`, **`bench`** and `suitability` all unchanged
   (§5 — `bench` is the cheapest leak detector).
3. **The `blocks` invariant, asserted directly.** Assert no `scan_triggers._KIND` entry and
   no `blocks` entry exists for any structure metric id — mirroring how
   `shadow_floor`'s invariant is documented but, today, only implicitly tested.
4. **Threshold immutability.** `assert T.VOLUME_RATIO_MIN == 0.8` and
   `assert config.CONSOLIDATION_ATR_PCT_MAX == 5.0 and
   config.SPOT_ATR_MOMENTUM_MAX == 1.0 and config.SPOT_ATR_EXTENSION_MAX == 1.5`.
5. **Suitability-path coverage.** Because `suitability` gates the recommendation
   pool, the queue and the hot-refresh set (§2.3), the phase-aware volume tests must
   assert the *downstream* effect — a consolidating name with `vr = 0.72` now reads
   `GO` and therefore enters `recommendation_runner`'s pool
   (`backend/recommendation_runner.py:135`) — not merely that the CAUTION string
   disappeared.
6. **XLK insufficiency.** On the 207-bar XLK fixture the 252-bar leg must report
   `insufficient_data`, never a number and never a pass.

---

## 8. Premise corrections

Five statements in the task description do not match the code. Two change the spec.

| # | premise | actual | impact |
|---|---|---|---|
| 8.1 | Level 4 checks include **RSI 40–60** | No RSI check exists in the gate. The 40–60 band is **MFI**, in the *suitability* lens (`backend/metrics/thresholds.py:33-35`, applied `backend/metrics/scorecard.py:279-281`), tagged `[HARD RULE]` | Informational — do not touch it |
| 8.2 | Level 4 has a **"price near MA21"** proximity band to reuse for `consolidation_phase` | The live check is `extension` in **ATR units** (`≤ 1.5 ATR`). The percent band `CONSOLIDATION_MA21_DIST_MAX = 4.0` is reachable **only** through `indicators.consolidating`, which has **zero call sites** (§1.4) | **Spec-changing.** Derive `consolidation_phase` from the live `atr_5d_ema` + `extension` check results (§3.3). Reusing the dead constant would resurrect dead code and add a fourth Level-4 input |
| 8.3 | The entry pipeline is **4 levels + account gate** | Six: 1, 2, **3.5**, 4, and L5 as a separate per-request overlay (`backend/screening.py:707-710`, `:733`). Also Level 2 is now a **deterioration VETO**, not a strength selector (`backend/screening.py:644-651`) | Informational — the L3.5 structure classifier already does part of what the prompt asks Level 4 to do; the new metrics must not duplicate it |
| 8.4 | **READY + CAUTION on the same row** is a known defect | It is the intended post-fix design: `suitability` is a demoted drawer readout, deliberately independent of the canonical verdict (`backend/metrics/scorecard.py:562-565`, `:690-691`) | **Spec-changing in scope.** But `suitability` still gates the recommendation pool, the queue and hot-refresh (§2.3) — so the volume fix has real downstream effect and must be tested there |
| 8.5 | WATCH/BENCH is **coextensive**, a known structural defect | Already fixed, implemented and regression-tested (`backend/scan_triggers.py:566-587`; `backend/test_scan_triggers.py:247-285`) | Ship independently; nothing to wait on |

---

## 9. Open questions for Phase 1 approval

1. **`structure_score` denominator on partial data.** When one metric is
   `insufficient_data`, is the score `n/3`, `n/4`, or `None`? Recommend reporting
   `{"score": n, "of": k, "insufficient": [...]}` so calibration can filter rather
   than guess — and never a bare `0`, per the prompt's own rule.
2. **`dist_from_high_pct` split contamination.** §4.2 — confirm the `Close`-based
   window plus the sanity guard, or accept that the metric is unreliable across a
   split for ~12 months and label it as such in the log.
3. **Scope of the phase-aware volume fix.** It changes `suitability`, which changes
   the recommendation pool, the internal queue and the hot-refresh set (§2.3). That
   is a real behaviour change, correctly scoped by the prompt — confirming it is
   intended, since everything *else* in Phase 1 is shadow-only.
4. **`_STOCK_GATE_LEVELS` interaction.** A Level-4 failure short-circuits
   `score_ticker` at `backend/metrics/scorecard.py:698`, returning **before**
   `compute_verdict` runs. So on an L4-failing row the volume CAUTION never fires
   today and the phase-aware branch is unreachable there. Confirm the structure
   metrics should still be computed and logged for those rows — recommend **yes**
   (they are the "gate passed / gate failed, structure N/4" comparison the whole
   calibration exists to make), which means attaching them **before** line 692.

---

**END OF PHASE 0. Awaiting explicit approval before Phase 1 implementation.**
