# AUDIT — Remove RS3M-vs-Sector Completely (Entry Gate, Kill Switch, Display, Telemetry)

**Phase 0. Written audit only. No implementation code in this change.**

Branch `claude/level-4-chart-structure-volume-9y4dbf`, HEAD `ce0abce`.
Baseline: `python -m pytest backend -q` → **1241 passed, 1 failed**. The single
failure is **pre-existing on `master`** and unrelated to this work — see §9.

Three premises in the task description do not survive contact with the code, and
**two of them change what this removal actually is.** They are stated up front
because the decision record specified in Phase 1 would otherwise be written on
false facts.

---

## 0. PREMISE CORRECTIONS — read before approving

### 0.A "RS3M-vs-SPY … remains a hard entry veto" — **it is not one, and never runs at entry**

There is **no RS3M-vs-SPY entry veto anywhere in the codebase.** The entry gate's
Level 3 (`screening.entry_gate`, `backend/screening.py:602`) passes iff the stock
verdict is GREEN, which folds in exactly three vetoes
(`stock_lights.evaluate_vetoes`, `backend/stock_lights.py:43-99`):

| # | veto id | line |
|---|---|---|
| 1 | `rs3m_vs_sector` | `backend/stock_lights.py:66-77` |
| 2 | `atr_expanding_high_ivr` | `backend/stock_lights.py:79-87` |
| 3 | `close_below_ma200` | `backend/stock_lights.py:89-97` |

`rs3m_vs_spy` is not among them. `config.STOCK_RS_VS_SPY_MIN` (= 5.0,
`backend/config.py:234`) has exactly **two** production readers:

* `backend/kill_switch.py:77` — the YELLOW "thinning" leg, **not** an entry gate;
* `backend/app.py:1560` — a read-only settings echo.

`config.rs_vs_spy_min()` (`backend/config.py:244-247`), whose docstring still says
*"for the Level 3 'beats SPY' leg"*, has **zero production callers** — only
`backend/test_cfm.py:1131-1135`. The "beats SPY" leg it names was removed when
Level 3 became the Symbol Genius light vote; `backend/screening.py:404-406`
records the change in a comment: *"RS3M (3-month) is DISPLAY / kill-switch only
now — kept on the row so the UI and snapshot still show it, but **it no longer
gates entry**."*

**Consequence, and this is the material one:** `rs3m_vs_sector` is the **only
relative-strength entry veto that exists**. Removing it does not leave the SPY leg
holding the line at entry — it removes RS-based entry vetoing **entirely**. The
task frames this as removing one of two RS legs; it is removing the only one.
Entry then rests on Market Genius (L1), sector deterioration (L2), the SYM
four-light vote + the ATR/IVR and MA200 vetoes (L3), structure entrability (L3.5),
the right spot (L4) and the account overlay (L5). That may well be the intent —
but it must be approved knowing it, not on the stated premise.

### 0.B "computed as the difference of two RS-vs-SPY values rather than a direct comparison" — **false; it is already the direct ratio**

All **three** computation sites use `indicators.rs3m(stock_df, peer_df)` — the true
63-day ratio against the peer frame, not a vs-SPY difference:

* `backend/stock_lights.py:70` — the entry veto;
* `backend/kill_switch.py:55` — the kill switch;
* `backend/metrics/scorecard.py:216-217` — the scorecard row;
* (and `backend/screening.py:410` for the row's display value).

The codebase says so explicitly in three places, having deliberately migrated off
the approximation: `backend/kill_switch.py:21-25` (*"This is the true ratio, NOT
the vs-SPY difference approximation … the kill switch is the critical laggard
filter and fires on large moves, exactly where the difference approximation
diverges most"*, tagged `[HARD_CFM_RULE / KILL_SWITCH_RS_SOURCE]`),
`backend/metrics/scorecard.py:209-212`, and `backend/entry_context.py:274-280`.
`config.SNAPSHOT_SCHEMA_VERSION = 3` (`backend/config.py:1135-1138`) exists
**precisely to record that migration**, and `backend/test_kill_switch.py:73-124`
contains two tests pinning direct-vs-approximation divergence.

**The "approximation" rationale is therefore not available for the decision
record.** The invalid-benchmark rationale (cap-weighted mega-cap concentration is
not a true peer group) stands on its own and is sufficient. Writing "it was an
approximation" into a permanent decision record would be a factual error that a
future reader could not reconcile with the code history.

### 0.C The kill switch's sector dependency is **two legs, not one**

The task describes removing "the kill-switch exit-now trigger". The sector value is
read **twice** in `kill_switch.classify` (`backend/kill_switch.py:59-89`): once for
the RED exit-now leg (`:68`) and once in the **YELLOW** "thinning" leg (`:76`). See
§3. Removing only the RED leg leaves a dangling reference; removing the value
entirely also changes YELLOW behaviour, which is a second, unstated safety change.

---

## 1. Complete inventory of RS3M-vs-Sector (§0.1)

Classification per the task's (a)–(h). **Anything not listed here must not be touched.**

### (a) Entry gate blocking/veto logic

| file:line | what |
|---|---|
| `backend/stock_lights.py:66-77` | veto 1 — `rs3m_vs_sector < 0`, stocks only; `tripped` → verdict RED → Level 3 fails |
| `backend/stock_lights.py:50-52` | the veto's docstring contract |
| `backend/stock_lights.py:43-44` | `evaluate_vetoes(df, sector_df, …)` — `sector_df` exists **only** for this veto |
| `backend/stock_lights.py:262` | `compute()` passes `sector_df` through; its only use |
| `backend/screening.py:425-428` | `_veto_frame` resolution + the `stock_lights.compute(sector_df=…)` call |
| `backend/scan_triggers.py:79` | `"veto:rs3m_vs_sector": CONDITIONAL` in `_KIND` — makes it a benchable block |
| `backend/scan_triggers.py:108` | `"veto:rs3m_vs_sector": "RS3M vs sector > 0"` — the path-to-READY phrasing |
| `backend/scan_triggers.py:266-273` | `gate_blocks` lifts tripped L3 vetoes into `blocks` (carries verdict authority) |
| `backend/metrics/scorecard.py:266-270` | **suitability AVOID** — `rs3m_vs_sector < RS3M_VS_SECTOR_MIN` |

> **Note on the suitability leg.** `backend/metrics/scorecard.py:270` is not
> cosmetic. As established in `AUDIT_LEVEL4_STRUCTURE_PHASE0.md` §2.3,
> `suitability` gates the recommendation pool
> (`backend/recommendation_runner.py:135`), the internal queue
> (`backend/queue_state.py:67`) and the intraday hot-refresh set
> (`backend/refresh_policy.py:113`). Removing it widens all three.

### (b) Kill-switch exit-now trigger

| file:line | what |
|---|---|
| `backend/kill_switch.py:68-71` | **RED exit-now** — `rs_vs_sector < 0` → `status=red`, `alert=True` |
| `backend/kill_switch.py:76` | **YELLOW thinning** — `rs_vs_sector < STOCK_RS_VS_SECTOR_MIN + 2` (see §0.C) |
| `backend/kill_switch.py:83` | `rs3m_vs_sector` on the returned verdict dict |
| `backend/kill_switch.py:125-127` | `exit_reason_code` → `ExitReason.KILL_SWITCH_SECTOR` (dominates SPY) |
| `backend/kill_switch.py:52-56` | `_rs_pair` computes the sector leg |
| `backend/kill_switch.py:60` | `classify(ticker, rs_vs_spy, rs_vs_sector)` — the public pure-core signature |
| `backend/recommendation_engine.py:303-312` | feeds frozen snapshot values into `classify`; selects `KILL_RS_SECTOR` |
| `backend/recommendation_engine.py:310` | picks `sector_bars` vs `spy_bars` for `_first_rs_negative` |
| `backend/position_manager.py:710-716` | `can_add_shares` blocks adds on kill-switch red/yellow; message prints the sector value |

### (c) Computation / derivation

| file:line | what |
|---|---|
| `backend/kill_switch.py:55` | `indicators.rs3m(stock, sector_df)` |
| `backend/stock_lights.py:70` | `indicators.rs3m(df, sector_df)` |
| `backend/metrics/scorecard.py:216-217` | `indicators.rs3m(df, sector_df)` |
| `backend/screening.py:410` | `rs3m_vs_sector = indicators.rs3m(df, peer_df)` |
| `backend/screening.py:414` | `rs1m_vs_sector` — **the ranking key**, see §1-note |
| `backend/metrics/scorecard.py:478-482` | the self-comparison guard (`is_sector_etf` / `is_own_benchmark` → None) |
| `backend/income_profile.py:172-185` | `is_own_benchmark` — exists to protect this leg and the kill switch |
| `backend/recommendation_runner.py:68-71` | `kill_switch._rs_pair` call, writes `tk["rs3m_vs_sector"]` |

There is **no difference-of-two-RS helper** — see §0.B. Nothing to remove there.

> **§1-note — `rs1m_vs_sector` is a separate signal and is NOT in scope.**
> `backend/screening.py:414, 461, 493, 539` — RS1M-vs-sector is the **ranking key
> within GREENs** (`rank_key`), not a veto. The task scopes the removal to
> **RS3M**-vs-Sector. Removing RS1M-vs-sector would silently change candidate
> ordering, which no part of the task asks for. **Flagged for explicit decision;
> excluded from the Phase-1 plan unless approved.**

### (d) UI / display

| file:component | what |
|---|---|
| `frontend/src/components/Scorecard.jsx:537` | `<Readout label="RS3M Sec" value={pct(row.rs3m_vs_sector)} />` — drawer |
| `frontend/src/components/Scorecard.jsx:90-95` | the **`RS` table column** — `row.rs_state`, the two-speed vs-**sector** state |
| `frontend/src/components/Scorecard.jsx:59-62` | `rsTitle(row, "sector")` tooltip |
| `frontend/src/components/Scorecard.jsx:242` | the `rs_state` column-help text ("vs the sector") |
| `frontend/src/components/Scorecard.jsx:458` | the sector-ETF badge: *"RS vs Sector is N/A (it IS the sector)"* |
| `frontend/src/components/HistoryTab.jsx:211` | closed-cycle summary — `RS vs Sec {pct(summary.rs3m_vs_sector)}` |
| `frontend/src/components/HistoryTab.jsx:149` | exit-reason humanizer comment naming `KILL_SWITCH_SECTOR` |
| `frontend/src/components/ExecuteTab.jsx:45` | blocked-reason label: *"RS3M vs Sector negative (weaker than its own sector)"* |
| `frontend/src/components/Overview.jsx:77-81, 483-495` | Needs-attention items from `kill_switch` alerts |

**Layout / shared-component coupling (§0.7).** Two real couplings:

1. **`Scorecard.jsx` `COLUMNS`.** Removing the `RS` column changes
   `COLUMNS.length`, which is the `colSpan` for the expanded drawer row
   (`:489`) and the empty-state row (`:902`). Both derive it, so they follow
   automatically — but the drawer's `RS vs SPY` readout (`:539-546`) shares
   `RS_TONE` / `RS_GLYPH` / `RS_LABELS` / `rsTitle` with the sector column. Those
   maps must **survive**; only the sector call sites go.
2. **`ExecuteTab.jsx:45`** is a lookup map keyed by block id. Deleting the key is
   safe only if the renderer falls back for unknown ids — must be confirmed
   before editing.

### (e) Telemetry / logging

| file:line | what |
|---|---|
| `backend/alerts.py:35` | `KILL_SWITCH_SECTOR` alert type, `CRITICAL`, tagged `HARD_CFM_RULE` |
| `backend/alerts.py:100` | membership in the push-notification type set |
| `backend/alerts.py:174-178` | `check_kill_switch` emits the sector alert (precedence over SPY) |
| `backend/scan_rejection_log.py` (via row) | `rs_state` / `rs_level` / `rs_slope` — the **vs-sector** two-speed shadow |
| `backend/history.py:74` | `rs3m_vs_sector` in `_CYCLE_COLS` (CSV export) |
| `backend/position_manager.py:716` | the sector value in the can-add-shares reason string |
| `backend/recommendation_engine.py:376, 391` | snapshot fields on the emitted recommendation |

### (f) Persistence / event-sourcing

| file:line | what |
|---|---|
| `backend/rec_types.py:43` | `TriggerRule.KILL_RS_SECTOR` — **persisted on recommendation records** |
| `backend/rec_types.py:60` | membership in the valid-trigger frozenset |
| `backend/recommendation_engine.py:48` | membership in the exit-trigger set |
| `backend/recommendation_engine.py:408` | `KILL_RS_SECTOR → "KILL_SWITCH_SECTOR"` exit-code map |
| `backend/exit_reasons.py:24` | `ExitReason.KILL_SWITCH_SECTOR` — **stamped on closes in state.json** |
| `backend/exit_reasons.py:57` | membership in `CLOSE_TIME` |
| `backend/entry_context.py:38` | `"stock.rs3m_vs_sector"` in `_TRACKED_FIELDS` (data-quality) |
| `backend/entry_context.py:185` | `rs3m_vs_sector` in the frozen scorecard metric keys |
| `backend/entry_context.py:268` | frozen into the snapshot's stock section |
| `backend/entry_context.py:271` | `rs1m_vs_sector` (see §1-note) |
| `backend/entry_context.py:278` | `rs3m_vs_sector_method: "direct"` — the v3 provenance field |
| `backend/entry_context.py:285` | `rs3m_vs_sector_benchmark` (v21) |
| `backend/entry_context.py:399, 413, 428` | `summary()` / null-snapshot / exit-metrics carriers |
| `backend/config.py:1135-1138` | `SNAPSHOT_SCHEMA_VERSION = 3` and its v3 note |

### (g) Tests and fixtures — see §6.

### (h) Config / constants

| file:line | what |
|---|---|
| `backend/config.py:241` | `STOCK_RS_VS_SECTOR_MIN = 0.0` |
| `backend/config.py:270` | comment naming the veto |
| `backend/metrics/thresholds.py:45-48` | `RS3M_VS_SECTOR_MIN = 0.0`, tagged **`[HARD RULE]`** |
| `backend/app.py:1561` | `"stock_rs_vs_sector_min"` in the settings echo |

> `RS3M_VS_SECTOR_MIN` carries a `[HARD RULE]` provenance tag
> (`backend/metrics/thresholds.py:44-47`). Removing a `[HARD RULE]`-tagged
> constant is exactly the kind of change the tag exists to make deliberate. The
> decision record must name it.

---

## 2. RS3M-vs-SPY isolation (§0.2)

### 2.1 Where the SPY leg lives

* **Entry veto: does not exist.** See §0.A. Nothing to isolate at entry.
* **Kill-switch confirmed-close trigger:** `backend/kill_switch.py:72-75`
  (`rs_vs_spy < 0` → RED, *"Exit within 1-2 days … confirm on close"*), with
  `exit_reason_code` at `backend/kill_switch.py:128-130` and the SPY value
  computed at `backend/kill_switch.py:50`.
* **YELLOW leg (shared):** `backend/kill_switch.py:76-77`.

### 2.2 Shared elements and what remains

| shared element | shared how | after removal |
|---|---|---|
| `indicators.rs3m` (`backend/indicators.py:248`) | both legs call it; benchmark frame is the only difference | **unchanged** — SPY leg keeps calling it identically |
| `kill_switch._rs_pair` (`:18-56`) | returns the pair | returns the SPY value only; `backend/kill_switch.py:50` untouched |
| `kill_switch.classify` (`:59`) | 3-arg signature | signature narrows — **every caller must be updated**: `:92`, `backend/recommendation_engine.py:303` |
| `config.STOCK_RS_VS_SPY_MIN` | YELLOW leg only | **stays**; still read at `:77` after the sector disjunct is dropped |
| `config.STOCK_RS_VS_SECTOR_MIN` | RED + YELLOW legs | **removable** |
| `income_profile.resolve` / `is_own_benchmark` | resolves the peer frame | SPY leg never used it — but see §2.4 |
| `data_handler.get_daily(config.BENCHMARK)` | SPY frame | **unchanged** |

**Evaluation order.** RED-sector (`:68`) → RED-SPY (`:72`) → YELLOW (`:76`) is an
`if/elif/elif` chain. Deleting the first branch promotes RED-SPY to first. Because
the branches are mutually exclusive and sector-RED **dominated** SPY-RED, every
case that previously hit RED-SPY still does, in the same way. **The SPY leg's
inputs, threshold and behaviour are unchanged**; only cases that used to be
short-circuited by the sector branch now fall through to it — which is the
intended loosening, enumerated in §3.2.

### 2.3 The sector-ETF data fetch — **it stays. Not removable.**

The sector gate is emphatically **not** its only consumer:

| consumer | file:line |
|---|---|
| Level 2 sector-deterioration veto (rs1m / breadth / inst_flow) | `backend/screening.py:644-660` |
| `sector_rs1m` → the shadow SCORE | `backend/metrics/scorecard.py:701-702` |
| two-speed RS-vs-sector shadow | `backend/metrics/scorecard.py:679-687` |
| account gate sector concentration | `backend/account_gate.py:167-169, 415-418` |
| sweep sector frames | `backend/metrics/scorecard.py:854-860` |
| nightly maintenance / universe health / tier poll | `backend/maintenance.py:201, 244, 274`; `backend/universe_health.py:39-66`; `backend/tier_poll.py:65` |

**No dead weight to flag.** The fetch stays; no separate approval needed.

### 2.4 Dead code the removal exposes — **listed separately, not bundled**

Removal makes these unreachable. Each needs its own yes/no:

1. `stock_lights.evaluate_vetoes`'s `sector_df` **and** `benchmark` parameters
   (`backend/stock_lights.py:43-44`) — veto 1 is their only consumer, so both
   become dead, as does `compute()`'s `sector_df` pass-through (`:262`) and
   `screening._veto_frame` (`backend/screening.py:425`).
2. `config.rs_vs_spy_min()` (`backend/config.py:244-247`) — **already dead today**
   (§0.A), not caused by this change. Out of scope; noted so it is not mistaken
   for fallout.
3. `income_profile.is_own_benchmark` (`backend/income_profile.py:172`) — after
   removal its remaining consumers are the scorecard display guard
   (`backend/metrics/scorecard.py:481`) and `screening.py:411`. It survives, but
   its stated purpose (`:177-180`, protecting the veto and the kill switch) is
   half-gone; the docstring will be stale.

---

## 3. Kill-switch structure (§0.3)

### 3.1 The full decision tree — `backend/kill_switch.py:59-89`

```
classify(ticker, rs_vs_spy, rs_vs_sector):
  1. rs_vs_sector is not None and rs_vs_sector < 0        :68  -> RED,    alert=True   "EXIT immediately"
  2. elif rs_vs_spy is not None and rs_vs_spy < 0         :72  -> RED,    alert=True   "Exit within 1-2 days (confirm on close)"
  3. elif rs_vs_sector < STOCK_RS_VS_SECTOR_MIN + 2 (=2)  :76  -> YELLOW, alert=False  "thinning"
     OR   rs_vs_spy   < STOCK_RS_VS_SPY_MIN     (=5)      :77
  4. else                                                 -> GREEN, hold
```

Precedence is strict `if/elif`: **sector-RED dominates SPY-RED.** Confirmed
independently at `backend/kill_switch.py:125-130` (`exit_reason_code` checks
sector first) and `backend/alerts.py:174-183` (sector alert first, `elif` SPY) —
pinned by `backend/test_alerts.py:65` (*"sector rule wins when both trip"*).

Sector value is `None` (leg inapplicable) when: the position is a sector ETF or is
its own benchmark (`backend/kill_switch.py:53-55` via
`income_profile.is_own_benchmark`), or the peer frame is missing.

### 3.2 THE SAFETY SURFACE BEING GIVEN UP — enumerate, do not infer

**Scenario A — sector-RED fires and SPY never does (authority lost outright).**
`rs_vs_sector < 0` and `rs_vs_spy >= 0`: the name lags its sector while still
beating SPY. Today: immediate exit. After: **GREEN or YELLOW, no exit signal at
all** — YELLOW only if `rs_vs_spy < 5`, and with `rs_vs_spy` between 0 and 5 the
position reads YELLOW-but-not-alerting; at `rs_vs_spy >= 5` it reads **fully
GREEN**. This is the largest loss: a name rolling over inside a strong sector,
previously an immediate exit, becomes a clean hold.

**Scenario B — sector-RED fires before SPY-RED (timing lost).**
Both eventually go negative but sector crosses first — the ordinary laggard case,
since a stock usually underperforms its own sector before it underperforms the
broad market. Today: exit at the sector crossing. After: exit only at the SPY
crossing, an **exit-now → exit-in-1-2-days** downgrade *plus* the lag between the
two crossings. `backend/test_alerts.py:58, 65` pins today's dominance for
`rs3m_vs_spy=-3.0, rs3m_vs_sector=-2.0`.

**Scenario C — YELLOW warning lost (§0.C, unstated in the task).**
`0 <= rs_vs_sector < 2` and `rs_vs_spy >= 5`: today YELLOW ("thinning toward the
kill line"). After: **GREEN**. This also feeds `position_manager.can_add_shares`
(`backend/position_manager.py:714`), which blocks share adds on red **or yellow**
— so after removal the system will permit adding to a position it currently
refuses to add to. **This is a second behavioural change the task does not
mention and must be explicitly accepted.**

**Scenario D — recommendation-engine exits.**
`backend/recommendation_engine.py:303-312`: `KILL_RS_SECTOR` recommendations stop
being generated. `backend/recommendation_engine.py:48` (exit-trigger set) and
`:408` (exit-code map) lose their sector entries.

**Not lost:** every SPY-RED case still fires, unchanged (§2.2).

### 3.3 Monitoring / alerting / push paths referencing the sector trigger

| path | file:line |
|---|---|
| Alert type + CRITICAL severity | `backend/alerts.py:35` |
| **Push-notification** type set | `backend/alerts.py:100` |
| Alert emission | `backend/alerts.py:174-178` |
| Kill-switch API | `backend/app.py:901-903` (`/api/kill-switch`) |
| Overview aggregate | `backend/app.py:1050` |
| Open-position add guard | `backend/position_manager.py:710-716` |
| Needs-attention UI | `frontend/src/components/Overview.jsx:77-81, 483-495` |
| Push test coverage | `backend/test_webpush.py:152` |

---

## 4. Persistence and event-sourcing impact (§0.4)

### 4.1 Where sector values reach `state.json`

1. **Entry snapshots.** `executor._capture_entry_context`
   (`backend/executor.py:2653-2663`) → written to the execution at
   `backend/executor.py:2765-2766` and the position at `:2800`. The snapshot's
   stock section carries `rs3m_vs_sector`, `rs1m_vs_sector`,
   `rs3m_vs_sector_method`, `rs3m_vs_sector_benchmark`
   (`backend/entry_context.py:268-285`).
2. **Exit reasons.** `ExitReason.KILL_SWITCH_SECTOR`
   (`backend/exit_reasons.py:24`) is stamped on closes and is in `CLOSE_TIME`
   (`:57`). **These strings are already in the historical ledger.**
3. **Recommendation records.** `TriggerRule.KILL_RS_SECTOR`
   (`backend/rec_types.py:43`), persisted and **immutable** — pinned by
   `backend/test_recommendation_runner.py:79`.

### 4.2 Recompute tolerance — **already safe; verified**

`logging_handler.recompute_derived` touches the snapshot in exactly one place:
`backend/logging_handler.py:1139`, `entry_context.summary(entry.get("entry_context"))`.
`summary()` (`backend/entry_context.py:388-415`) is a **pure key-read with
`.get()`** over `stock`/`scorecard`/`regime`/`iv` sections. It never validates a
schema, never enumerates keys, and never raises on unexpected ones.

**Therefore: old events carrying sector fields remain readable and are ignored
without error after removal.** No migration is required and none should be
written. Nothing may be stripped — `CLAUDE.md` states the execution log is
append-only and immutable.

**Two hard constraints for Phase 1:**

* **`ExitReason.KILL_SWITCH_SECTOR` and `TriggerRule.KILL_RS_SECTOR` must NOT be
  deleted from their enums.** Historical closes and recommendation records carry
  those exact strings, and `backend/exit_reasons.py`, `backend/history.py:74` and
  `frontend/src/components/HistoryTab.jsx:149` all read them back for display.
  Deleting the members would break the History tab and the CSV export for every
  past sector exit. They must be **retained and marked retired** — removed from
  the *emitting* paths (`kill_switch.exit_reason_code`,
  `recommendation_engine`, `alerts`) and from `CLOSE_TIME` (no new close may set
  them), while remaining valid for reads. This is the same pattern
  `LEGACY_UNRECORDED` already uses (`backend/exit_reasons.py:51`).
* **`SNAPSHOT_SCHEMA_VERSION` → 4.** New snapshots stop carrying the sector
  fields, which is a shape change. Bump to 4 with an additive note; v1/v2/v3
  snapshots stay valid and readable by their own tag
  (`backend/config.py:1128-1138` establishes exactly this convention).
  `backend/entry_context.py:38` (`_TRACKED_FIELDS`) must drop
  `"stock.rs3m_vs_sector"` or the data-quality alert will fire on every new
  snapshot for a field that is intentionally absent.

---

## 5. XLK July 6th canonical fixture (§0.5)

### **ANSWER: NO — the fixture does not depend on the sector RS leg. It passes unmodified.**

Fixture: `backend/fixtures/regime/xlk_july6_rollover.parquet` (207 bars), built by
`backend/fixtures/regime/build_fixtures.py:90-121`, pinned immutable by
`backend/fixtures/structure/build_fixtures.py:13-15`.

**Every** XLK assertion site invokes the lights with **`sector_df=None,
is_etf=True`**, so the sector veto is not merely un-tripped — it is
**inapplicable** (`applicable = not is_etf and df is not None and sector_df is not
None`, `backend/stock_lights.py:68`):

| site | call | line |
|---|---|---|
| `test_stock_lights.py::test_july6_xlk_rollover_caught_by_both_layers` | `sector_df=None, is_etf=True` | `:98, :103` |
| `test_gate_ruleset.py::test_1_xlk_july6_identical_under_legacy_ruleset` | `sector_df=None, is_etf=True` | `:63, :66` |
| `test_gate_ruleset.py::test_1b_…proposed_ruleset` | same | `:88` |
| `test_dividend_profile.py::test_1_xlk_july6_regression_unchanged` | same | `:121, :124, :129` |

The BLOCKED verdict is carried by **two independent layers**, both explicitly
asserted and neither sector-related:

1. **The four-light vote** — `greens < 4`, SAR red or momentum red
   (`test_stock_lights.py:89-91`; `test_gate_ruleset.py:56-58`). `test_1b`
   strengthens this to `greens == 0` with the mandatory core red
   (`test_gate_ruleset.py:85-86`).
2. **The ATR-expanding / high-IVR veto** — `veto:atr_expanding_high_ivr`
   (`test_stock_lights.py:97-99`; `test_gate_ruleset.py:62-64`), asserted to fire
   independently, with the low-IVR arm confirming the lights alone still deny
   GREEN (`test_stock_lights.py:102-104`).

`test_stock_lights.py:95-97` states the design intent in a comment: *"Run through
the ETF path (is_etf=True), where the rs3m-vs-sector veto is waived, **so this
veto is the one that must catch it**."* The fixture was deliberately built so the
sector leg is not load-bearing.

Separately, `backend/test_shares_migration.py:386-391` composes the XLK verdict
from `compose_verdict("GREEN","GREEN","TOPPING","ACCUMULATING") == "BLOCKED"` —
structure-classifier only, no RS term.

**No assertion changes are required, and no re-pinning to a different carrying
gate is needed.** The Phase-1 requirement reduces to: run these four tests
unmodified and confirm they still pass. **The fixture must not be touched.**

---

## 6. Test inventory (§0.6)

### (a) DELETE — tests only the removed feature

| test | file:line |
|---|---|
| `test_four_green_stock_vetoed_by_negative_rs3m_vs_sector` | `test_stock_lights.py:188-199` |
| `test_rs_pair_negative_sector_triggers_red` | `test_kill_switch.py:22-40` |
| `test_direct_vs_approx_rs_sector_parity_on_small_moves` | `test_kill_switch.py:73-82` |
| `test_kill_switch_direct_rs_sector_fires_where_approx_is_late` | `test_kill_switch.py:84-124` |
| sector-veto legs in the dividend-profile tests | `test_dividend_profile.py:264, 270-273, 554` |
| sector-veto AVOID cases | `test_scorecard.py:237, 292-293` |
| `test_stock_row_sector_etf_has_no_vs_sector_rs` (veto leg) | `test_cfm.py:1077-1093` |

> `test_kill_switch.py:73-124` are the two tests pinning **direct vs
> approximation**. They exist because the migration off the approximation was a
> deliberate `HARD_CFM_RULE` decision. Deleting them removes the only executable
> record of §0.B. **Recommend preserving that fact in the decision record.**

### (b) REWRITE — must now assert sector absence

| test | file:line | new assertion |
|---|---|---|
| `test_rs_pair_waives_self_comparison_for_a_sector_etf_position` | `test_kill_switch.py:42-71` | re-point to the SPY-only path |
| `test_evaluate_all_skips_closed_positions` | `test_kill_switch.py:126+` | `classify` signature change |
| `test_kill_switch_alerts_sector_beats_spy` | `test_alerts.py:55-65` | **precedence test — becomes SPY-only**; must assert no `KILL_SWITCH_SECTOR` is ever emitted |
| exit-reason mapping | `test_exit_reasons.py:77-80` | sector input no longer yields `KILL_SWITCH_SECTOR` from a live evaluation |
| `KILL_RS_SECTOR` recommendation tests | `test_recommendation_engine.py:77-92, 106, 185`; `test_recommendation_runner.py:66, 79` | re-point to `KILL_RS_SPY_CONFIRMED` |
| entry-snapshot field sets | `test_entry_context.py:89, 94, 211, 219` | drop sector keys; bump expected schema to 4 |
| scorecard row field set | `test_scorecard.py:222, 257, 378-387, 429` | drop `rs3m_vs_sector` |
| gate-block veto shape | `test_scan_triggers.py:90, 96` | veto id no longer exists |
| `test_position_mgmt.py:395, 403` | `can_add_shares` | reflect Scenario C |

### (c) UNTOUCHED — must pass unmodified (historical-read coverage)

`test_history.py:82, 87, 140, 152` · `test_leap_lifecycle.py:521` ·
`test_trust_derive.py:40, 62` · `test_webpush.py:152` ·
`test_regime_history.py:116, 127` · **all four XLK sites (§5)**.

These read `KILL_SWITCH_SECTOR` / `KILL_RS_SECTOR` **from historical records** and
are precisely the coverage proving §4.2 tolerance. **They must keep passing
without modification** — that is the acceptance test for "historical events remain
readable". If any of them requires a change, the retirement approach in §4.2 is
wrong and must be revisited.

### (d) NEW — required by §1.4

* Kill-switch: positive assertion that no input can produce a sector trigger.
* Entry composition: all gates pass + sector RS would have been negative → READY.
* Historical-event tolerance: recompute over a fixture with old sector fields.
* Grep-level cleanliness check.

---

## 7. Scope questions requiring explicit decision before Phase 1

1. **§0.A** — removal eliminates RS-based entry vetoing *entirely*, not one of two
   legs. Confirm.
2. **§0.C / Scenario C** — the YELLOW thinning leg and the `can_add_shares` guard
   change too. Confirm.
3. **§1-note** — `rs1m_vs_sector` (the GREEN ranking key) is sector-relative but
   is **RS1M, not RS3M**. In or out? *Recommend: out.*
4. **§1(d)** — `row.rs_state` (the `RS` **table column**) is the two-speed
   vs-**sector** state whose LEVEL axis literally is `indicators.rs3m(df,
   sector_df)` (`backend/rs_state.py:6-9`). It is shadow, but it is a
   sector-relative RS3M surface, it feeds the shadow SCORE
   (`backend/scan_score.py:63-66`) and it can append a WATCH annotation to
   `verdict_reasons` (`backend/metrics/scorecard.py:692-695`). In or out?
   *Recommend: in for the UI (§1.3 requires "no UI surface renders sector RS in
   any form"), out for the shadow SCORE unless approved — dropping it would
   silently change SCORE ranking.*
5. **§2.4** — the newly-dead `sector_df` / `benchmark` parameters: remove now or
   leave?
6. **§0.B** — confirm the decision record omits the "approximation" rationale.

---

## 8. Recommended Phase-1 sequencing (not implemented)

1. Decision record first (`CHANGELOG.md` + a `docs/` decision entry), so the code
   comments at the removal sites can cite it.
2. `kill_switch.classify` signature + all callers; retire (do not delete) the
   enum members; drop from `CLOSE_TIME`.
3. Alerts / push / API / UI.
4. Entry veto + suitability AVOID + `scan_triggers` maps.
5. `entry_context` + `SNAPSHOT_SCHEMA_VERSION` → 4.
6. Config constants.
7. Tests per §6, XLK four sites run unmodified as the acceptance gate.

---

## 9. Baseline test failure — pre-existing, unrelated, NOT caused by this work

`backend/test_portfolio_risk.py::test_nightly_refresh_updates_position_dividends`
fails on this branch **and on `master`** (verified by checkout). It is not
environmental noise but a **latent bug**, surfaced by a cold cache:

* `backend/maintenance.py:275-277` calls `universe_screen.screen(names, frames)`
  where `data_handler.get_many` returns `None` frames when no provider is
  configured and no parquet cache exists.
* `universe_screen.evaluate` (`backend/universe_screen.py:61`) is typed
  `df: pd.DataFrame | None` but calls `indicators.rsi(df)` at `:69`.
* `indicators.rsi` (`backend/indicators.py:55-56`) has **no None guard** and
  dereferences `df["Close"]` → `TypeError: 'NoneType' object is not subscriptable`,
  caught at `backend/maintenance.py:280` and surfaced as a report error.

It passed in the previous session only because that container's `DATA_DIR` had a
warm cache. **Out of scope for this task** — reported, not fixed. It is a
one-line guard in `universe_screen.evaluate` if you want it addressed separately.

---

**END OF PHASE 0. Awaiting explicit approval — and answers to §7 — before Phase 1.**
