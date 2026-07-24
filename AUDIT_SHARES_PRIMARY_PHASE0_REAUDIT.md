# Phase 0 Re-Audit — Shares-Primary Revamp (retire LEAP PMCC as active base)

**Audit only. No implementation code was written or changed for this document.**
Every finding cites `file:line` against the current head of branch
`claude/big-revamp-7kj7vy`. Where a claim could not be verified, it is marked
explicitly rather than inferred. **Hard stop at the end — no implementation
until the owner approves.**

---

## 0. Scope note the prompt does not know (read first)

The uploaded prompt is written as if this were a greenfield migration off a
pure LEAP-base codebase ("do not assume prior session state", "schema v12 → v13").
**Neither is true of the tree being audited.** This branch already contains a
substantially-built shares-primary migration delivered over six commits (merged
via PRs #240/#241 in prior sessions). An honest fresh audit has to describe the
code that exists, so for each §3 concern below I report three things:

1. the base-position coupling that exists,
2. whether it is **already migrated / gated** for shares, and
3. what **remains** un-migrated.

Two corrections to the prompt's stated facts, both verified:

- **Schema is v20, not v13.** `CURRENT_VERSION = 20` (`backend/migrations.py:20`).
  The shares migration is `_v19_to_v20` (`migrations.py:306-329`), a pure
  *additive* upgrade — it tags every existing position `LEAP_PMCC_LEGACY` and
  adds an empty `acquisition_records` lot log; it rewrites no executions. The
  prompt's `_v12_to_v13` (`migrations.py:186`) is unrelated mid-chain history.
- **The `position_type` discriminator, shares executor path, dividend ledger,
  covered-lot math, assignment mechanics, correction layer, and a 35-case test
  surface already exist.** The migration is not un-started; it is un-finished.

The test suite is effectively green: **1025+ pass**. The only failures are
environmental (optional `pywebpush`/`cffi`, and one `universe_screen` nightly test
that returns `None` without live data) — the last reproduces identically on
`master`, so it is not a regression from this work.

---

## TL;DR — the ten findings that decide the remaining work

1. **`SHARES` is the only type ever stamped explicitly; `LEAP_PMCC_LEGACY` is
   always implicit-by-default.** `_buy_shares.apply` stamps `SHARES`
   (`executor.py:2844,2850`); `_buy_leap.apply` never writes a type — it relies on
   the skeleton default `position_type: None` (`executor.py:417`) resolving to
   legacy via `position_types.of` (`position_types.py:25-30`). This asymmetry is
   the load-bearing invariant of the whole migration.

2. **Most LEAP machinery is gated on *leg presence*, not on `position_type`.**
   `burn.py`, `leap_policy.py` lifecycle, the nightly burn-mark sweep, and
   `net_juice_rollup` all degrade correctly for a shares position only because a
   shares position has no `leap_legs` — never because of an explicit type check.
   The **one** payback path with a real `position_type` gate is
   `logging_handler.py:870` (`if position_types.is_shares(p): continue`).

3. **The single net-juice definition already contracts cleanly for shares.**
   `net_juice_per_week` (`burn.py:216-231`) = `juice_per_week − burn_per_week_with_slippage`,
   and slippage rides *inside* the burn term (`burn.py:170-172`), not as a separate
   subtraction. For shares, burn is absent, so `net == gross`. The KeyError risk the
   original audit flagged at `account_gate.py:106-108` is **already fixed** — those
   sites now use `.get()`, not `[]`.

4. **The juice adequacy floor is still LEAP-denominated and has no shadow mode.**
   The active floor is `config.JUICE_FLOOR_WK = 1.5` (`config.py:313`,
   `PROPOSED_DEFAULT`), enforced as a **hard SAFETY block** in
   `scan_triggers.juice_floor_block` (`scan_triggers.py:292-319`) against a
   **LEAP-cost denominator** (`account_gate.py:349`: `weekly_extr / leap_cost`).
   `SHARES_JUICE_FLOOR_PCT = 1.5` exists (`config.py:443`) but **is not wired into
   any gate**, and **no `JUICE_FLOOR_MODE` SHADOW/ENFORCE switch exists anywhere**
   (grep: zero matches). This is the §4.6 gap.

5. **`notional_controlled` does not exist.** Grep for it across backend +
   `frontend/src` returns **zero matches**. The §4.5 metric is unbuilt.

6. **The 100-share atomic covering unit is derived and displayed, and blocked at
   entry sizing — but not enforced on the short-sale execution path.**
   `covered_lots` (`position_manager.py:537-548`) and the `round_lot_size`
   entry block (`account_gate.py:323-332`) exist, but `executor.py` uses
   `SHARES_PER_LOT` only to convert contracts→qty (`executor.py:1817,2893`); grep
   finds no `coverable_lots`/`naked` ceiling check at sell-short time.

7. **`buy_leap` / `roll_leap` / `close_leap` are still live actions.** They remain
   in `executor.py:VALID_ACTIONS` (`executor.py:28-30`) and run unconditionally.
   There is **no read-only-legacy enforcement** — `position_types.py:6-7` *says*
   "no new LEAP may be opened," but no dispatch guard implements it. The only action
   gate is `FROZEN_BLOCKED_ACTIONS` (`executor.py:44-45`), which is the
   reconciliation freeze, unrelated to the migration.

8. **The reconcile freeze-lift substrate is already structure-agnostic.**
   `reevaluate_freezes` / `freeze_status` / stable-id persistence
   (`reconcile.py:739-813`, `691-722`) key only off ticker + benign-ness + resolution
   status. The one genuinely LEAP-shaped node to retire is `SHORT_STOCK_DETECTED`
   (`reconcile.py:379-383,420-426,539-549`). Shares-primary work must plug into the
   freeze loop, **not** re-implement it, and must honor the `stable_id` contract or
   it re-opens the re-freeze regression the hotfix closed.

9. **The frontend has zero `position_type` awareness.** Grep of `frontend/src`
   for `position_type|is_shares` returns only unrelated substring matches.
   `ExecuteTab.jsx` still narrates the LEAP flow ("buy LEAP · sell/close/roll ·
   sell the LEAP to exit", `ExecuteTab.jsx:273`); there is no Legacy toggle in
   `SettingsTab.jsx`; `BurnPanel.jsx` renders LEAP-only concepts ungated. This is
   the largest remaining gap (§4.7).

10. **Verdict engine is provably untouched, and a test pins it.**
    `test_shares_migration.py:313` asserts
    `compose_verdict("GREEN","GREEN","TOPPING","ACCUMULATING")["verdict"] == "BLOCKED"`,
    with the comment that `compose_verdict` has no `position_type`/burn/leap term.
    The XLK July-6 regression (`test_stock_lights.py:84`) still asserts RED verdict +
    `veto:atr_expanding_high_ivr`.

---

## 3.1 Base-position coupling

### state.json schema (v20 record shapes)
- Version constant: `CURRENT_VERSION = 20` (`migrations.py:20`); top-level skeleton
  stamps `schema_version`, `positions[]`, `executions[]` in `_default_state`
  (`logging_handler.py:39-92`).
- **Position record skeleton** (`executor.py:409-427`, `_ensure_position`): every
  position carries a `position_type` (default `None` → legacy), *both* a `leap` /
  `leap_legs` container *and* a `shares` block
  `{"count", "cost_basis_per_share", "cap", "pct_to_cap", "acquisition_records"}`,
  plus `short_calls[]` and `planned_exit_dte` (LEAP-only; burn math keys off it).
- **LEAP leg dict** (`executor.py:2592-2599`): `strike, contracts, cost_basis,
  intrinsic, extrinsic, dte, expiration, extrinsic_at_entry, extrinsic_collected_to_date`.
- **Shares block append** (`executor.py:2824-2841`): weighted-average cost basis +
  append-only `acquisition_records` (`{date, qty, price, source, execution_id}`).
- **Execution log entries**: `buy_leap` (`executor.py:2517-2522`) vs the new v20
  `buy_shares` (`executor.py:2785-2789`, `{action:"buy_shares", qty, price_per_share,
  execution_total, stock_price}`). All executions get id/date/live_transmitted via
  `append_execution` (`logging_handler.py:271-289`). Multi-tranche legs are read
  through `logging_handler.leap_legs` (`logging_handler.py:95-102`).
- Read-time projection: `_v19_to_v20` (`migrations.py:306-329`) backfills the legacy
  tag + empty lot log; executions are never rewritten (append-only preserved).

### position_manager.py base math
- `position_capital` (`position_manager.py:552-563`) sums LEAP leg cost first, then
  adds `shares.count × cost_basis_per_share`. `deployed_capital` (`:566-572`) sums it
  over open positions.
- `delta_coverage` (`position_manager.py:269-342`) is the **explicit shares-vs-legacy
  branch**: SHARES (`:290-309`) returns `min_leg_delta = 1.0`, `long_delta = lots`,
  `floor_breach = False`, `naked_short = short_contracts > coverable_lots`; LEGACY
  (`:310-342`) computes per-leg Greeks and checks `min_leg_delta < LEAP_DELTA_FLOOR`
  (`:339`).
- `enrich_short` (`position_manager.py:111-259`) swaps assignment narrative on
  `position_type` (`:227-236` shares "covered by REAL SHARES" vs `:249-258` legacy
  "never exercise the LEAP").
- **Coupling that remains:** `enrich_position` (`position_manager.py:401-511`) calls
  `leap_policy.leap_health` unconditionally whenever `legs` exist (`:436-448`); a pure
  shares position is spared only by leg-absence. `net_juice_rollup`
  (`position_manager.py:629-658`) reads `leap_health` only, so a shares base
  contributes **nothing** to the portfolio net-juice total (`:641-652`) — a real gap
  once shares carry short-call juice.

### burn.py callers / consumers / gating
- Public surface: `burn_projection` (`:88`), `net_juice_per_week` (`:216`),
  `coverage` (`:234`), `candidate_net_juice` (`:268`), `exit_slippage_est` (`:51`).
- Non-test callers: `account_gate.py:94-97` (candidate ranking, **unconditional** —
  no type gate, because no position exists yet at candidate time);
  `maintenance.py:97` (`snapshot_burn_marks`, gated on `leap` presence at `:86`);
  `leap_policy.py:149-162` (`burn_projection`/`net_juice_per_week`/`coverage`/
  `extension_cost`, ungated).
- `burn.py` itself has **zero** internal `position_type` gating — it is a pure LEAP
  theta model; all gating is at the callers, and only by leg-presence.

### LEAP lifecycle / roll-at-135-DTE / anti-zombie
- Module is `leap_policy.py` ("LEAP long-leg lifecycle", `:1`). `roll_policy`
  (`:32-47`) triggers on `leap_dte < LEAP_ROLL_DTE_FLOOR` (`=90`, `config.py:833`) or
  extrinsic runway `< LEAP_MIN_EXTRINSIC_WEEKS` (`=4`, `config.py:838`).
- The roll target is `planned_exit_dte` = `PLANNED_EXIT_DTE = 135`
  (`config.py:885-888`; read `leap_policy.py:139`; stamped `executor.py:426`).
- **Anti-zombie window slide lives in `burn.py`, not `leap_policy.py`:**
  `_effective_exit_dte` (`burn.py:72-85`) slides the exit DTE one `EXTENSION_STEP_WEEKS`
  (`=1`, `config.py:921`) at a time when held past plan and flags `extended`; the flag
  propagates through `burn_projection` (`:144-147`) and the UI ladder
  (`leap_policy.py:156-162`). None of this is gated on `position_type`.

### executor.py base-open construction
- `_buy_leap` (`executor.py:2510-2617`): builds the immutable execution, classifies
  merge/add tranche, `apply` appends to `leap_legs` and sets `position["leap"]`.
  **Never stamps `position_type`** — relies on the default.
- `_buy_shares` (`executor.py:2774-2856`): the shares parallel; `apply` (`:2823-2855`)
  computes weighted-avg basis, appends the acquisition record, and stamps
  `position_type = SHARES` on the first buy (`:2844`) / `setdefault` on adds (`:2850`).

---

## 3.2 Net juice computation

- **Single definition:** `net_juice_per_week(juice_per_week, burn_per_week_with_slippage)`
  = `round(juice_per_week − burn_per_week_with_slippage, 2)` (`burn.py:216-231`; docstring
  asserts "the SINGLE definition … the position view and the entry queue both call
  this"). Percentage wrapper `candidate_net_juice` (`burn.py:268-315`), pct at `:313`.
- **Consumers:** live/position view — `leap_policy.py:153,200`,
  `position_manager.net_juice_rollup:629-658`, `BurnPanel.jsx:84`, `Overview.jsx:234-247`,
  `ProcessRibbon.jsx:468,552`. Entry ranking — `account_gate.py:94-111`, `app.py:212,234-235`,
  `queue_state.py:72`. Scorecard — `metrics/scorecard.py:468,474-475,585`,
  `Scorecard.jsx:111-115`. Alerts — via the `leap_health` block (`test_alerts.py:222`).
  Payback meter — a **distinct** realized-ledger field also named `net_juice`
  (`alerts.py:614,960,967`, `PayoutsTab.jsx`, `HistoryTab.jsx`); do not conflate the two.
- **Burn vs slippage — same path.** Slippage is computed
  (`exit_slippage_est`, `burn.py:51-69`), amortized (`burn.py:171`), and **added into
  burn** (`burn.py:172`, `burn_pw_slip = burn_pw + slippage_pw`) before net juice
  subtracts the single combined term. There is no separate slippage subtraction from
  net juice. (`backend/slippage.py` is a different order-slippage utility that does
  **not** feed net juice.)
- **Day-count (reported, not fixed — this is a flagged open item per §5):** mixed and
  deliberately reconciled — BS pricing uses `/365` (`burn.py:43`, `account_gate.py:76,80`);
  burn is bucketed to weeks via `/7.0` (`burn.py:34,166-167`). Documented as
  `HARD_CFM_RULE` in the docstring (`burn.py:222-228`) and pinned by
  `test_burn.py:105,141`.

---

## 3.3 Juice adequacy floor

- **Definition:** `JUICE_FLOOR_WK = float(os.environ.get("CFM_JUICE_FLOOR_WK","1.5"))`
  (`config.py:313`, `PROPOSED_DEFAULT`, GROSS juice/wk). Sibling, **not yet wired**:
  `SHARES_JUICE_FLOOR_PCT = 1.5` (`config.py:443`, `PROPOSED_DEFAULT`, "denominated
  against DEPLOYED SHARE CAPITAL … pending recalibration").
- **Gates that read the floor:** `scan_triggers.juice_floor_block`
  (`scan_triggers.py:292-319`, reads `JUICE_FLOOR_WK` at `:308`); `metrics/scorecard.py:540`;
  `scan_score.py:80,135,171` (a local SHADOW copy, zero authority). The
  `account_gate.py` `juice_adequacy` check (`:345-360`) uses a *different* bar
  (`CYCLE_RETURN_MIN/CYCLE_WEEKS_MAX` ≈ 1.9%/wk, `:348,160`), not `JUICE_FLOOR_WK`.
  `execution_gate.py` and `screening.py` read no floor.
- **Denominator today = LEAP cost.** `weekly_yield = weekly_extr / leap_cost * 100`
  (`account_gate.py:349,105`); comment `scan_triggers.py:299` confirms "weekly
  extrinsic / LEAP cost, before burn." The share-notional denominator exists only as
  the un-wired `SHARES_JUICE_FLOOR_PCT`.
- **Safety block, not a ranked SCORE input.** Juice is classified `SAFETY`
  (`scan_triggers.py:96-97`, "never benchable … structural"), and
  `_BLOCK_SEVERITY[SAFETY] = BLOCKED` (`:331`) — a sub-floor name is forced to
  BLOCKED. The same pct also feeds the composite SCORE (`scan_score.py:170-171`), but
  that SCORE is SHADOW with **zero** decision authority (`scan_score.py:1,9-16`).
  `compose_verdict` (`scan_verdict.py:62-88`) never sees the floor — it is layered as a
  separate Level-5 block (`scan_triggers.py:281-289`).
- **No SHADOW/ENFORCE mode for the floor.** No `JUICE_FLOOR_MODE` constant anywhere;
  `CFM_JUICE_FLOOR_WK` tunes the *threshold value* only. The floor is always live.

---

## 3.4 Capital, caps, covering units

- **$38K deployed:** `MAX_DEPLOYED_CAPITAL = 38000` (`config.py:965-966`,
  `PROPOSED_DEFAULT`), enforced `account_gate.py:308-315` (`capital_limit`), deployed
  sum from `position_manager.position_capital`.
- **2 positions:** `MAX_CFM_POSITIONS = 2` (`config.py:961-963`, `HARD_CFM_RULE`),
  enforced `account_gate.py:302-306`.
- **500-share cap:** `SHARE_CAP = 500` (`config.py:427`) — a **per-stock accumulation**
  cap, not a book-wide share-equivalent cap. Enforced in `position_manager.py:490-495,689`
  and surfaced `app.py:1456`.
- **$13K reserve:** two concepts — static `RESERVE_REQUIRED = 13000` (`config.py:1081`,
  `position_manager.py:578`) and dynamic 2×ATR `RESERVE_ATR_MULT = 2.0`
  (`config.py:972-977`), enforced `account_gate.py:197-207,279-299`. The dynamic reserve
  is `RESERVE_ATR_MULT × ATR × contracts × 100` — still contract-shaped.
- **Share-equivalent from LEAP:** derived as a Greek in `portfolio_risk.py:68-102`
  (`share_equiv += delta × contracts × 100` for LEAP `:85`; `-= delta × n × 100` for
  short `:95`; `+= shares.count` at delta 1.0 `:99`; `delta_shares` output `:108`).
  There is no single canonical number reconciling capital sizing
  (`account_gate.py:275`, `leap_cost × contracts × 100`) with book delta.
- **100-share atomic unit:** `SHARES_PER_LOT = 100` (`config.py:429-433`, `HARD_CFM_RULE`);
  `covered_lots` (`position_manager.py:537-548`, fragment never rounds up);
  entry SIZE-BLOCK `round_lot_size` (`account_gate.py:323-332`, SHARES-only).
  **Gap:** executor uses `SHARES_PER_LOT` only for contracts→qty conversion
  (`executor.py:1817,2893`); no `short_contracts ≤ coverable_lots` ceiling is enforced
  at sell-short time.
- **`notional_controlled`: ABSENT** (zero matches repo-wide). §4.5 unbuilt.

---

## 3.5 Reconciliation overlap map (reported, not resolved)

- **Freeze-lift substrate is done and structure-agnostic:** `_diff_open`
  (`reconcile.py:739-743`), `reevaluate_freezes` (`:746-777`), `freeze_status`
  (`:780-813`), stable-id persistence `_persist_report`/`_persist_resolution`
  (`:691-722`), resolution/ack entry points (`:842-867`). These key only off ticker,
  benign-ness, and `resolution.status`.
- **The "incident hotfix" is a *separate* effort** on the order-submission path
  (tick rounding, Decimal serialization, quote validation, `client_order_ref`
  idempotency, `SUB_UNKNOWN` state — `AUDIT_INCIDENT_HOTFIX.md:275-293`,
  `test_incident_hotfix.py:124-364`). It is **not** the reconcile freeze work; do not
  conflate. The freeze subsystem is covered by `test_reconcile_freeze_gate.py`.
- **The one genuinely LEAP-shaped node:** `SHORT_STOCK_DETECTED`
  (`reconcile.py:34,379-383,420-426`) with hard-coded "covered by a LEAP, not stock …
  do NOT exercise the LEAP" guidance (`reconcile.py:539-549`; mirrored
  `position_manager.py:234-238`). Shares-primary retires this branch.
- **Already share-migrated in reconcile:** `_is_share_reduction` (`:514-524`),
  `record_called_away` suggestion for an EQUITY reduction (`:563-569`), and the
  economic diff classes `COST_BASIS_MISMATCH`/`EXTRINSIC_MISMATCH` (`:550-557`).
- **Overlap / sequencing risk:** shares-primary work is confined to
  **detection + classification + suggestion** (`reconcile.py:257-276,379-431,527-569`);
  the **freeze/lift/persist** substrate (`:691-813`) is done. The risk is (a)
  duplicating the freeze substrate instead of plugging into it, and (b) editing
  classification without emitting a stable `stable_id` (`reconcile.py:326+,694-697`),
  which would silently reopen the re-freeze regression the stable-id fix closed. **Do
  not resolve unilaterally** (per §5); the tightest coupling point is the stable-id
  contract.

---

## 3.6 Dead-after-migration inventory

Classification key: **DELETE** (truly dead once LEAP is read-only) ·
**GATE** (behind `position_type`; still runs for legacy) · **RETAIN** (needed to
render legacy history, or shared with the shares path).

| Item | Lines | Class | Basis |
|---|---|---|---|
| `leap_policy.py` `leap_health`/`aggregate_health` | 327 (module) | RETAIN-legacy | called under `if legs:` (`position_manager.py:436-448`); legacy still needs it |
| `leap_policy.py` `roll_cost_estimate`/`roll_policy` | `:32,248` | DELETE on shares path | roll recs never fire for SHARES; currently **ungated** (`alerts.py:620-734`, `app.py:355`) |
| `burn.py` live burn card | 329 (module) | RETAIN-legacy | `maintenance.py:97` gated on `leap` presence |
| `burn.py` `candidate_net_juice` ranking use | `:268` | DELETE-candidate | `account_gate.py:94-97` runs unconditionally; no LEAP opened on shares path |
| `burn_marks.py` | 238 (module) | RETAIN-legacy | `maintenance.py:101`, `app.py:425-462` render legacy burn history |
| executor `_buy_leap`/`_close_leap` | `:2510,2620` | DELETE-once-VALID_ACTIONS-trimmed | mutate/open LEAP legs; no legacy read need |
| executor `_roll_leap` + commit/place chain | `:3880-4130` | DELETE | LEAP roll only |
| executor `_roll_short`/`_commit_roll` chain | `:2928-3431` | RETAIN (shared) | short-call roll is a live SHARES action |
| `logging_handler.py` payback meter | `:859-899` | RETAIN-legacy | **already gated** `is_shares → continue` (`:870`) |
| `logging_handler.py` LEAP extrinsic accounting | `:591,916,1034,1069,1126` | RETAIN-legacy | derives/renders legacy history |
| `RollModal.jsx` | 368 | RETAIN (shared) | submits `roll_short` (`:127`), a live SHARES action — **not** dead |
| `BurnPanel.jsx` | 198 | GATE | renders LEAP-only concepts; must not render for SHARES; currently ungated |

**Not-yet-enforced read-only-legacy:** `buy_leap`/`roll_leap`/`close_leap` remain in
`executor.py:VALID_ACTIONS` (`:28-30`) and run unconditionally. Removing/guarding them is
the enforcement half of "LEAP becomes read-only legacy," and it does not exist yet.

---

## 3.7 Test surface

### XLK July-6 regression (must pass unchanged)
Fixture `backend/fixtures/regime/xlk_july6_rollover.parquet`
(`fixtures/regime/build_fixtures.py:90,122`). Two regressions consume it:

- `test_stock_lights.py:84` `test_july6_xlk_rollover_caught_by_both_layers` — verbatim:
  - `:90` `assert eng["lights"]["sar"]["signal"] == "red" or eng["lights"]["momentum"]["signal"] == "red"`
  - `:91` `assert eng["greens"] < 4`
  - `:97` `assert indicators.atr_expanding(df) is True`
  - `:99` `assert res["verdict"] == stock_lights.RED`
  - `:100` `assert "veto:atr_expanding_high_ivr" in res["veto_reasons"]`
  - `:104` `assert no_veto["verdict"] != stock_lights.GREEN`
- `test_shares_migration.py:313` `test_verdict_engine_unchanged_by_migration` — verbatim
  (`:315-318`): `compose_verdict("GREEN","GREEN","TOPPING","ACCUMULATING")` →
  `assert v["verdict"] == "BLOCKED"`, with the comment "compose_verdict has no
  position_type/burn/leap term."

### Tests constructing a LEAP-shaped position — 25 files
Heaviest: `test_leap_lifecycle.py` (25 fns / 656 lines), `test_adoption_reverse.py`
(13 fns / 462), `test_reconcile.py`, `test_account_gate.py`, `test_cfm.py`,
`test_multi_leap.py` (6 fns), `test_leap_cost_scale_repair.py` (6 fns),
`test_alerts.py`, `test_atomic_roll.py` (16 fns), `test_burn.py`. These stay valid for
the legacy read path; none needs deletion for the migration, but any that would open a
*new* LEAP via a public entry point will need re-basing on `LEAP_LEGACY` fixtures if
`buy_leap` is later removed from `VALID_ACTIONS`.

### New shares-migration tests already present
- `test_shares_migration.py` — 19 tests: v19→v20 backfill, absent-type→legacy,
  lot-aware buy, called-away lifecycle, sell-shares P&L, no-payback-meter-for-shares,
  covered-lot fragment math, naked-short flag, round-lot SIZE-BLOCK (pass+fail),
  ex-div WARN in/out of cycle, dividend-income-own-ledger, called-away note-free,
  assignment note shares-vs-legacy, append-only history edit, cost-basis-mismatch diff,
  stable-id ack persistence, verdict-unchanged.
- `test_adoption_reverse.py` — 13 tests: reverse-adoption, manual-roll extrinsic
  computation, rebuild-from-broker economics, void/restore executions,
  set_position_legs, save_transactions append-only re-derivation.

Together these cover the prompt's §6 checklist items 1–10; the gaps below are the
items **not** yet exercised because they are **not** yet built.

---

## 4. Remaining work vs the prompt's §4 spec (the actionable synthesis)

**Already delivered (do not redo):** §4.1 schema v20 + read-time upgrade · §4.2 shares
semantics (delta 1.0, weighted basis, 100-share lot with fragment rejection at the
entry gate) · §4.3 burn retirement via absence · §4.4 dividend ledger + ex-div WARN ·
§4.8 economic reconcile diffs · §4.9 called-away exit path · the §6 test surface for
all of the above.

**Genuine gaps (candidate Phase 1 scope, in rough priority):**

1. **§4.6 Juice floor shadow mode** — add `JUICE_FLOOR_MODE` (default `"SHADOW"`) and
   wire `SHARES_JUICE_FLOOR_PCT` against a share-notional denominator, evaluated and
   logged but non-blocking in SHADOW; hard floor (`net ≤ 0`) stays `HARD_CFM_RULE`.
   Currently the floor is LEAP-denominated and always enforcing.
2. **§4.5 `notional_controlled`** — a derived read-only account metric; wholly absent.
3. **§4.2/executor covering-unit enforcement** — enforce `short_contracts ≤
   coverable_lots` at sell-short in `executor.py` (today only derived/displayed and
   blocked at entry sizing).
4. **§4.5 reserve recompute against share notional** — log old-vs-new side by side for
   one calibration period; the 2×ATR reserve is still `× contracts × 100`.
5. **Read-only-legacy enforcement** — guard/remove `buy_leap`/`roll_leap`/`close_leap`
   so no new LEAP can be opened (spec intent; `position_types.py:6-7` states it, code
   does not enforce it).
6. **§4.7 Frontend** — the largest gap: shares Execute flow (symbol · lot count ·
   limit price) replacing the LEAP flow (`ExecuteTab.jsx:273`); Legacy toggle in
   `SettingsTab.jsx`; gate `BurnPanel.jsx` and other LEAP affordances behind
   `position_type`; Positions tab shares rendering (qty, basis, unrealized, short leg,
   weekly juice, next ex-div); `net_juice_rollup` counting shares' short-call juice.
7. **§3.5 reconcile classification** — retire `SHORT_STOCK_DETECTED` on the shares
   path, honoring the stable-id contract; plug into the existing freeze loop.

**Open verification flags left untouched (per §5):** day-count `/365` vs `/7` mixing,
`captured_pct` clamp, RS-vs-sector approximation, `q=0` defaults, payback
state-machine coverage, SAR seed path-dependence.

---

## HARD STOP

This is the Phase 0 deliverable. **No implementation code has been written.** Awaiting
explicit approval before starting any §4 work, and — because most of §4.1–4.4/4.8/4.9
already exists — a decision on which of the seven gaps above to actually build, and in
what order.
