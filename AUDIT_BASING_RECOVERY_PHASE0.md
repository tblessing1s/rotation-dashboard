# AUDIT — BASING misclassification in `structure_classifier.py` (GDDY recovery-advance case)

**Phase 0. Written audit only. No implementation code in this change.**

Branch `claude/level-4-chart-structure-volume-9y4dbf`, HEAD `5acffb4`.
Baseline: `python -m pytest backend -q` → **1259 passed, 0 failed**.

The task's root-cause narrative is **correct on both counts**. Verified against
the code in §1. Four things it does not anticipate are collected in §0 because
two of them change the Phase-1 spec.

---

## 0. FINDINGS THAT CHANGE THE SPEC — read before approving

### 0.A The Path-B `DECLINING` fall-through is **DEAD CODE**. There is no "existing declining target" to route to.

Spec §1.3 says a vol-expanding, below-SMA200 name should be routed "to the
existing declining/unresolved label per the tree's structure (Phase 0.1
determines the correct fall-through target)."

**Phase 0.1 determines there is no such target.** `structure_classifier.py:336-337`
reads:

```python
    if falling:
        return BaseStage.DECLINING
    return BaseStage.BASING
```

`falling` at line 336 is **provably always False**. Line 319 already returned
`DECLINING` for `falling and not above200`; reaching line 336 requires
`above200 is False` (the `if above200:` at :329 did not fire), so `not above200`
is True, so the :319 guard reduces to `not falling`. `above200` cannot be `None` —
:309 returns `INSUFFICIENT_DATA` first. Line 337 is unreachable.

Confirmed by exhaustive replay over the tree's derived booleans: **`DECLINING`
below the 200-day is only ever reached from :320**, never from :337.

Consequence: **Path B has exactly one reachable outcome today — `BASING`.** That
is the deeper form of defect 2 in the narrative. Adding an ATR check to Path B
therefore needs an *explicit* target decision, not a fall-through:

* **Revive `DECLINING`** — semantically wrong. The name is flat, not falling;
  `DECLINING` elsewhere means a confirmed downtrend, and reusing it would make
  `DECLINING` mean two different things. It is also a `_DEGRADE_BASE` member
  (`scan_diff.py:35`), so every such name would fire a **`SCAN_DEGRADED` alert**
  reading "base rolled over" — factually wrong and noisy.
* **`TOPPING`** — also wrong. `TOPPING` is defined as stalling at *elevated*
  levels above the 200-day (`:326-328`); this name is below it. Also a
  `_DEGRADE_BASE` member, same alert problem.
* **A new label** (e.g. `UNRESOLVED`) — correct semantically, but that is a
  second new label in a change already adding one, and the spec explicitly
  wants blast radius contained.
* **Leave Path B's ATR check out of this change** — the ATR filter's stated
  motivation is symmetry with Path A, not a failing chart. No fixture in the
  task demonstrates a misclassified vol-expanding base; §1.5's "Path B ATR
  fixture" would be asserting a target we have to invent to satisfy the test.

**Recommendation: defer §1.3 (the Path-B ATR check) out of this change** and
ship the dual-window slope + `RECOVERING` alone, which is what the GDDY case
actually requires. If you want the ATR check now, it needs a target decision from
you — see §7 Q1. Note the dead `:336-337` branch should be deleted or made
reachable either way; leaving provably-unreachable code in a decision tree is how
the next reader concludes Path B has a declining path when it does not.

### 0.B An unregistered label **fails open to WATCH** — and structurally cannot reach READY/CAUTION

`structure_entrability` (`structure_classifier.py:397-406`):

```python
397  if base_stage in (INSUFFICIENT_DATA, TOPPING, DECLINING): return BLOCKED
399  if inst_flow  in (INSUFFICIENT_DATA, DISTRIBUTING):       return BLOCKED
401  if base_stage == EARLY_ADVANCE and inst_flow in (ACCUMULATING, EARLY_INTEREST):
                                                              return READY
404  if base_stage == LATE_ADVANCE  and inst_flow == ACCUMULATING:
                                                              return CAUTION
406  return WATCH
```

READY and CAUTION are **exact-equality gated** on `EARLY_ADVANCE` / `LATE_ADVANCE`.
A new label cannot match either, so it falls to `:406` → `WATCH`.

**The spec's hard constraint — "nothing in this change may create a new path to
READY/CAUTION" — is therefore satisfied structurally, not by registration.**
`RECOVERING` is WATCH-only whether or not §1.2's registration happens, and
`DISTRIBUTING` / `INSUFFICIENT_DATA` still force `BLOCKED` at `:399` ahead of it.
Registration should still be explicit (a reader must not have to derive this),
but it is belt-and-braces, and a test should assert the *fall-through* property
directly so it survives future edits to the grid.

`is_bench` needs **no registration at all**: it never sees the label. Entrability
`WATCH` becomes a non-READY `structure` input to `compose_verdict`, which becomes
a SIGNAL block, and `scan_triggers.is_bench` returns `False` on any signal block
(`scan_triggers.py:582-583`). "Interesting, not waiting" is already the rule.

### 0.C Three consumers **silently mis-handle** an unregistered label

These are the registration sites §0.2 asks for, and each fails quietly rather
than loudly:

| site | behavior with an unregistered `RECOVERING` | severity |
|---|---|---|
| `scan_score.py:89` — `_BASE_STAGE_SUB.get(base_stage, 0.0)` | scores **0.0**, i.e. **below `TOPPING`'s 0.1** — a recovery would rank beneath a topping name | **worst**: silent, and it is a ranking the operator reads |
| `frontend/.../Scorecard.jsx:84` — `BASE_LABELS[r.base_stage] \|\| "—"` | BASE column renders an **em-dash**, indistinguishable from "no data" | visible but misleading |
| `frontend/.../Scorecard.jsx:83` — `BASE_ORDER[r.base_stage] ?? 9` | sorts **last**, below `INSUFFICIENT_DATA` | minor |

`BASE_TONE` (`:29-33`) falls back to `text-slate-400`, so §1.2's "own color"
requires an entry there too. All four maps are at `Scorecard.jsx:25-45`.

### 0.D `scan_diff.py:97-99` keys the pipeline-entrant alert off the **literal string `"BASING"`**

```python
97   entrant_now  = (today.get("base_stage") == "BASING"
98                   and today.get("inst_flow") == "EARLY_INTEREST")
```

A chart that would have entered the pipeline as `BASING × EARLY_INTEREST` and now
reads `RECOVERING × EARLY_INTEREST` **stops firing `SCAN_PIPELINE_ENTRANT`**.
That is a real behavioural change to the alert feed, caused by relabelling and
invisible in the classifier's own tests. It is not in the task's scope list.
Decide deliberately — see §7 Q2.

---

## 1. Current decision tree, verbatim (§0.1)

`_base_stage`, `backend/structure_classifier.py:300-338`. **The narrative matches
the code exactly**, with the one discrepancy in §0.A.

| line | branch | result |
|---|---|---|
| `:301-302` | `bars < MIN_BARS_BASE` (210) | `INSUFFICIENT_DATA` |
| `:309-310` | `slope`, `above200` or `roc_long` is `None` | `INSUFFICIENT_DATA` |
| `:312` | `rising = slope > SLOPE_RISING_PCT` (+8.0) | — |
| `:313` | `falling = slope < SLOPE_FALLING_PCT` (−8.0) | — |
| `:314` | `extended = pct_above_sma50 > EXT_LATE_PCT` (15.0) | — |
| `:315` | `expanding = atr_posture > ATR_EXPANDING_MAX` (1.10) | — |
| `:316` | `mature = base_count >= LATE_ADVANCE_MIN_BASES` (3) | — |
| `:319-320` | `falling and not above200` | `DECLINING` |
| `:322-325` | `rising and above50` → `above200 and (extended or mature)` | `LATE_ADVANCE` |
| `:322,325` | `rising and above50`, otherwise | `EARLY_ADVANCE` |
| **Path A** `:329-334` | `above200` → `falling or expanding or not above50 or roc_long > 25` | `TOPPING` |
| **Path A** `:334` | `above200`, none of the above | `BASING` |
| **Path B** `:336-337` | `falling` | `DECLINING` — **UNREACHABLE (§0.A)** |
| **Path B** `:338` | otherwise | `BASING` |

Path A applies four filters (`above50`, slope band, `expanding`, `roc_long`);
Path B applies **one live test** — and, per §0.A, that test is vacuous, so Path B
is effectively unconditional. The narrative's "Path B applies no quality filters"
understates it.

One further note for §0.6: `EARLY_ADVANCE` is reachable **below** the 200-day
(`:322-325` — the `LATE_ADVANCE` test requires `above200`, the `EARLY_ADVANCE`
fall-through does not). Verified by replay: below-200 outcomes today are
`{DECLINING, EARLY_ADVANCE, BASING}`. So a below-200 name already has a live path
to READY; the GDDY case misses it only because the long slope is flat.

### `trend_slope_pct` (`:120-133`)

```python
120  def trend_slope_pct(df, window: int = SLOPE_WINDOW) -> float | None:
126      if df is None or len(df) < window: return None
127      y = df["Close"].astype(float).to_numpy()[-window:]
128      x = np.arange(window, dtype=float)
129      slope = float(np.polyfit(x, y, 1)[0])        # price per bar
130      mean = float(y.mean())
133      return slope * (window - 1) / mean * 100.0   # total % change of the fit line
```

A single least-squares fit over the last `window` closes, returned as the fit
line's total % change across the window, normalized by the window's mean price so
a $40 and a $400 name are comparable. **Confirms defect 1**: one fit through a
down-leg and an up-leg of similar magnitude nets to near zero regardless of how
violent either leg was.

### Constants (§0.1 third bullet)

Every one is **already tagged `PROPOSED_DEFAULT`** — none is a `HARD_CFM_RULE`:

| constant | value | line |
|---|---|---|
| `MIN_BARS_BASE` | 210 | `:84` |
| `SLOPE_WINDOW` | 150 | `:89` |
| `SLOPE_RISING_PCT` | +8.0 | `:90` |
| `SLOPE_FALLING_PCT` | −8.0 | `:91` |
| `EXT_LATE_PCT` | 15.0 | `:94` |
| `ATR_EXPANDING_MAX` | 1.10 | `:95` |
| `LATE_ADVANCE_MIN_BASES` | 3 | `:96` |
| `LONG_LOOKBACK` / `LONG_GAIN_PCT` | 200 / 25.0 | `:97-98` |

The module banner (`:29-33`) states they all live next to the logic deliberately
and are candidates for promotion into `config` later.

---

## 2. Consumers of the structure label (§0.2)

| # | consumer | file:line | reads |
|---|---|---|---|
| 1 | `structure_entrability` | `structure_classifier.py:387-406` | the grid — see §0.B |
| 2 | `scan_verdict.compose_verdict` | `scan_verdict.py:72` | entrability → the canonical verdict |
| 3 | scorecard row | `metrics/scorecard.py:588, 596, 620` | `row["base_stage"]`, both rulesets |
| 4 | shadow SCORE | `scan_score.py:60-67, 86-90`; called `metrics/scorecard.py:688` | `_BASE_STAGE_SUB` — **§0.C** |
| 5 | scan table BASE column | `Scorecard.jsx:25-45, 83-84` | label / tone / sort — **§0.C** |
| 6 | column help text | `Scorecard.jsx:230` | prose listing the stages |
| 7 | Ready-to-Enter shortlist | `app.py:249` | passthrough `r.get("base_stage")` — no mapping, safe |
| 8 | transition diff — degrade | `scan_diff.py:35, 80-84` | `_DEGRADE_BASE = {"TOPPING","DECLINING"}` |
| 9 | transition diff — entrant | `scan_diff.py:97-99` | literal `"BASING"` — **§0.D** |
| 10 | calibration log | `scan_rejection_log.py:130` | persisted per symbol per scan run |
| 11 | Level 3.5 gate | `screening.py:695-703` | `classify_symbol` → entrability |

**Persistence.** The label reaches `state.json` **only** indirectly: the entry
snapshot freezes the scorecard row's metrics (`entry_context.py`), and the
append-only calibration store `scan_rejection_log` records `base_stage` per
symbol per scan (`:130`, stamped with `date` + `scan_id` at `:371`). No schema
change is required to *add* a label — the field is a free string.

**BASING's entrability row, in full**: `BASING × {ACCUMULATING, EARLY_INTEREST,
NO_INTEREST}` → `WATCH`; `BASING × {DISTRIBUTING, INSUFFICIENT_DATA}` → `BLOCKED`
(at `:399`, ahead of any base-stage test). `RECOVERING` inherits exactly this
shape via the `:406` fall-through.

---

## 3. Slope machinery reuse (§0.3)

**Directly reusable, no duplication.** `trend_slope_pct(df, window=SLOPE_WINDOW)`
(`:120`) already takes `window` as a parameter; the body is window-agnostic
(`:126-133`). A short window is `trend_slope_pct(df, STRUCT_SLOPE_WINDOW_SHORT)`
— same function, same normalization, no new code path.

**Where it threads:** `_signals` (`:254-286`) computes every signal once for both
decision trees and the drawer. `"slope_pct": trend_slope_pct(df)` is at `:275`; a
sibling `"slope_pct_short"` belongs immediately beside it. `_base_stage` reads
`sig["slope_pct"]` at `:303` and would read the short value the same way. **Same
price series, no new fetch** — `_signals` takes the frame the caller already holds.

Bar-count interaction: `trend_slope_pct` returns `None` when `len(df) < window`
(`:126`). With `MIN_BARS_BASE = 210` already enforced at `:301`, a 40-bar window
can never be `None` at the point Path A/B runs. The new value must **not** be
added to the `:309` `INSUFFICIENT_DATA` guard — it is not a binding input, and
adding it would change nothing today while creating a trap if the short window
is ever lengthened past 210.

---

## 4. ATR availability on Path B (§0.4)

**Available, already computed, zero extra work.**

`sig["atr_posture"]` is set once in `_signals` at `:280` via
`indicators.atr_momentum(df)` (`indicators.py:144-152` — `ATR / ATR_5EMA`, the
same ratio the scorecard reports). `_base_stage` derives `expanding` from it at
`:315` — **before any branch**, so the variable is in scope on Path B exactly as
on Path A. No re-computation, no new call.

**Missing-data handling.** `atr_momentum` returns `None` on short history or a
zero base. Path A's pattern at `:315` is
`sig["atr_posture"] is not None and sig["atr_posture"] > ATR_EXPANDING_MAX` —
i.e. **a missing ATR reads as NOT expanding (fail-open)**, matching the module's
"missing data never vetoes" posture. Path B can mirror it for free by reusing the
same `expanding` local rather than re-testing. Note `atr_posture` is *not* in the
`:309` sufficiency guard, so a `None` here does not force `INSUFFICIENT_DATA` —
that is existing, deliberate behavior on Path A and would carry over unchanged.

---

## 5. Fixture data availability (§0.5)

### How classifier fixtures are built

`backend/fixtures/structure/build_fixtures.py` — a deterministic **synthesizer**,
not a capture tool. It writes committed parquet from hand-specified close/high/
low/volume arrays via `_ohlcv()` (`:37`), on a `pd.bdate_range` index. Seven
fixtures exist, **270 bars each**, asserted in `test_structure_fixtures.py`.
Regenerated with `python -m fixtures.structure.build_fixtures` from `backend/`;
outputs are committed so tests never rebuild.

Pattern to follow: `turning_recovery.parquet` is the closest existing analog —
already a recovery shape, built for the two-speed RS fixtures.

The builder's docstring says "≥ the classifier's 250-bar BaseStage floor"
(`:4-5`) — **stale**, the floor is 210 (`structure_classifier.py:84`). Cosmetic.

### ⚠️ Real GDDY bars **cannot** be captured in this environment

GDDY **is** in the universe (`tickers_by_sector.txt:17`, XLK). But:

* `schwab_api.configured()` → **False**
* `alpha_vantage.configured()` → **False**
* no parquet cache exists under `DATA_DIR`

and `CLAUDE.md` states no live Schwab call is ever made from the suite. **There is
no path to real GDDY daily history through 2026-08-21 from here.**

So the "GDDY 2026-08-21 canonical fixture" must be a **synthetic reproduction of
the GDDY signature** — ~110→75 decline Feb–Jun, ~75→97 rally Jun–Aug, close 0.7%
under SMA200, above a rising SMA50, an early-August expansion candle — built to
satisfy the stated measurements. That is a legitimate regression pin: it pins the
*shape* the classifier must not call BASING. But it must be **named and
documented as synthetic** (e.g. `recovery_v_shape.parquet`, docstring citing the
GDDY 2026-08-21 observation it reproduces). Naming a synthetic series
`gddy_2026_08_21` would misrepresent it as captured market data to every future
reader.

If you can export real GDDY bars (CSV/parquet) from a machine with credentials,
that is strictly better and I can build the fixture from it instead — see §7 Q3.

---

## 6. Blast radius on existing labels (§0.6)

### Who consumes `trend_slope_pct`

Only `_base_stage`, via `rising`/`falling` derived once at `:312-313`:

| use | line | branch |
|---|---|---|
| `falling` | `:319` | `DECLINING` (below-200 downtrend) |
| `rising` | `:322` | `EARLY_ADVANCE` / `LATE_ADVANCE` claims |
| `falling` | `:332` | `TOPPING` |
| `falling` | `:336` | Path B — **dead (§0.A)** |

No other module calls `trend_slope_pct` (`grep`: definition + `_signals:275` only).

### Isolation is clean — no refactor needed

The spec's requirement (short window affects **only** flat-band disambiguation;
advance/topping claims keep keying off the long window) is satisfied by adding a
**separate local**, e.g. `rising_short = slope_short > STRUCT_SHORT_SLOPE_RISING_PCT`,
consulted only inside the Path A/B region. `rising` and `falling` stay bound to
the long slope and keep feeding `:319`, `:322`, `:332` byte-for-byte. The
branches share a *variable name*, not a *value* — nothing needs restructuring.

### Which names re-route

Only names in the **flat long-slope band** (−8% ≤ slope ≤ +8%) **below SMA200**
whose 40-bar slope exceeds +8%: today `BASING`, after `RECOVERING`. Both map to
`WATCH`, so **no verdict changes** — this is a labeling change, matching the
task's framing. What *does* change, and is not in the task's list:

* the **SCORE ranking** of those names (§0.C — 0.5 → 0.0 unless registered);
* the **`SCAN_PIPELINE_ENTRANT` alert** for those names (§0.D — stops firing);
* the **BASE column** rendering (§0.C).

Path A is untouched: the new test only runs below SMA200 per §1.2. Above-200
verdicts cannot move.

---

## 7. Questions requiring a decision before Phase 1

1. **§0.A — the Path-B ATR check (§1.3) has no valid fall-through target.**
   *Recommend: drop §1.3 from this change* and ship the slope fix alone; delete
   the dead `:336-337` branch with a comment. Alternatives if you want it now:
   revive `DECLINING` (semantically wrong + fires a false "rolled over" alert),
   or add a second new label `UNRESOLVED`.
2. **§0.D — `SCAN_PIPELINE_ENTRANT` stops firing** for charts relabelled
   `RECOVERING`. Options: accept; add `RECOVERING` to the entrant condition; or
   emit a distinct event. *Recommend: add it to the entrant condition* — a
   recovery + early interest is the same pipeline signal the alert exists for.
3. **§0.5 — the GDDY fixture will be synthetic** unless you can export real bars.
   Confirm, and confirm the neutral filename.
4. **§1.4 — the duration counter breaks the classifier's purity contract.**
   `classify` is documented PURE, no I/O, no clock, prefix-causal
   (`structure_classifier.py:12-17`); "incremented per classifier run" is
   inherently stateful and would make the same frame return different output on
   the second call, breaking the replay guarantee the fixtures rely on.
   **`scan_rejection_log` already records `base_stage` per symbol per scan run
   with a `date` and `scan_id` (`:130`, `:371`)** — `days_in_current_structure`
   is *derivable* from it with no new state, no counter, and no purity violation.
   *Recommend: derive it as a read over the existing log* rather than adding a
   counter.

---

## 8. Recommended Phase-1 sequencing (not implemented)

1. `STRUCT_SLOPE_WINDOW_SHORT` + bands; `slope_pct_short` in `_signals:275`.
2. `RECOVERING` on `BaseStage`; the Path-B routing at `:335-338`; delete the dead
   `:336-337`.
3. Explicit registration: `structure_entrability`, `_BASE_STAGE_SUB`,
   `BASE_LABELS` / `BASE_TONE` / `BASE_ORDER`, column help — plus the §7 Q2
   decision on `scan_diff`.
4. Tests per §1.5, with the fall-through property (§0.B) asserted directly and
   the XLK July-6 fixture run unmodified.
5. §7 Q4's duration read, if approved as a derivation.

---

**END OF PHASE 0. Awaiting approval — and answers to §7 — before Phase 1.**
