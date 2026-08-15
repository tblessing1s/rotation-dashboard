# Audit — Dividend Income Profile & Position Builder (v1), Phase 0

**Status:** Phase 0 written audit. **No implementation code has been written.** HARD STOP
in effect pending review.

**Tree audited:** `claude/rotation-dividend-income-profile-htavhg` @ `d6f3af8`
(merge of #252). No open PRs on the repo at audit time.

**Baseline test state (this container):** `1057 passed, 1 failed`.
The single failure is `test_portfolio_risk.py::test_nightly_refresh_updates_position_dividends`,
which fails on `report["errors"] == []` because `universe_screen` raises
`'NoneType' object is not subscriptable` with no cached OHLCV present. It is an
offline-environment artifact, pre-existing and unrelated to this work item.
(Backend deps had to be installed manually — the SessionStart hook's install did
not take in this container; `pywebpush`/`http-ece` will not build here, as CLAUDE.md
predicts, and the distro `cryptography` is broken under pyo3 and had to be
reinstalled from PyPI. None of that is code.)

---

## Correction to the prompt's premises (read before the items)

Three premises in the task description do not match the tree. They change what
Phase 1 has to build, so they are stated up front rather than buried.

| Prompt says | Tree actually has | Citation |
|---|---|---|
| "schema v13" → migration v13 → v14 | `CURRENT_VERSION = 20`. The shares-primary migration is **v19 → v20**. The new migration is **v20 → v21**. | `backend/migrations.py:20`, `:306-330`, `:332-352` |
| "RS-vs-sector is currently computed as a difference of RS-vs-SPY values (open verification flag)" | The approximation has **already been removed**. Every RS-vs-sector site computes the direct `indicators.rs3m(stock, sector_df)` ratio, and entry snapshots record `rs3m_vs_sector_method: "direct"` explicitly so a regression can't be silent. | `kill_switch.py:18-48`, `screening.py:296-303`, `stock_lights.py:59-69`, `metrics/scorecard.py:210`, `entry_context.py:272-278` |
| "Level 3 = RS3M vs Sector" | Level 3 is now **stock lights GREEN** (`stock_lights`). RS3M-vs-sector survives as (a) a **veto** inside the lights and (b) the kill switch. It is no longer a named Level-3 leg. | `screening.py:471-495`, `stock_lights.py:45-96` |

Item 3 is therefore answered as "the flag is closed" rather than "here is the
approximation"; item 2's Level-3 mapping is answered against the real structure.

A fourth, more consequential finding — **the Level 5 gate is not enforced on the
shares entry path at all** — is developed in item 7. It directly blocks spec §4
("the recommendation must pass the full Level 5 account gate"), because there is
currently no enforced Level 5 on a `buy_shares` to pass.

---

## 1. Juice floor plumbing

There are **two independent floors**, both currently shadow in shares mode, plus
**one dead constant**.

### Floor A — the scan-verdict NET/GROSS juice floor (`juice_floor`)

* **Computed:** `backend/scan_triggers.py:292-327` — `juice_floor_block(net, gross)`.
  Two tiers: hard (`net <= 0`) at `:317-321`, adequacy (`gross < config.JUICE_FLOOR_WK`)
  at `:322-326`.
* **Shadowed:** `scan_triggers.py:314-315` — `if config.LEGACY_LEAP_READONLY: return None`.
  Since `config.LEGACY_LEAP_READONLY` defaults True (`config.py:456`), this function
  **returns `None` unconditionally in production today**. It is not "shadow" in the
  sense of "evaluated and logged but not enforced" — it is short-circuited *before*
  evaluation, so **nothing is computed and nothing is logged**. That is a material
  gap for spec §2's "logged real-data calibration" graduation path.
* **Consumed — exactly one non-test call site:** `backend/metrics/scorecard.py:540-543`,
  appended to `blocks` and folded into the canonical row verdict at `:544`
  (`compose_row_verdict`). Nothing else calls it.
* **Config:** `config.JUICE_FLOOR_WK = 1.5` (`config.py:313`, env `CFM_JUICE_FLOOR_WK`),
  documented `config.py:303-312`.
* **Classification:** `scan_triggers.py:96` maps `juice_floor` to `SAFETY`
  (→ `BLOCKED` on the verdict ladder, `scan_triggers.py:339-340`); label at `:119`;
  trigger text at `:166`.
* **Blocking authority today:** **zero**, by short-circuit.

### Floor B — the Level-5 account-gate juice adequacy check (`juice_adequacy`)

* **Computed:** `backend/account_gate.py:395-419`. Yield = `weekly_extr / denom * 100`
  at `:404-405`, where `denom` is spot in shares mode and LEAP cost in legacy
  (`:403`). Target from `weekly_yield_target_pct()` (`:189-196`).
* **Shadowed:** `account_gate.py:414` — the `blocking` argument is `not shares_mode`.
  In shares mode the check is **evaluated and reported but non-blocking**; the
  detail dict carries `"shadow": shares_mode` (`:417`) and the label is suffixed
  `" — SHADOW, not enforced"` (`:409`, `:412`). This one *is* real shadow: the
  number is computed and surfaced.
* **Consumed:**
  * `account_gate.py:478-487` — `blocking_failures` / `warnings` partition; shadow
    checks can never enter `blocking_failures`.
  * `backend/executor.py:1742-1766` (`_enforce_account_gate`) → raises on
    `blocking_failures` unless `override_reason`. Called at `executor.py:513-514`.
  * `backend/app.py:172` (`evaluate_many` for `/api/scan/ready`) → `:210-212`
    (`gate_blocks(None, account_gate=l5)`) → `scan_triggers.py:282-287` reads
    `blocking_failures` only, so a shadow check is invisible to the overlay.
  * `backend/recommendation_runner.py:138`.
  * `frontend/src/components/ExecuteTab.jsx:150` renders
    `detail.weekly_yield_pct`; `frontend/src/components/ReadyToEnter.jsx:17` maps
    the id to "juice too thin".
* **Blocking authority today:** **zero in shares mode**; still blocking for an
  explicit legacy `position_type`.
* **Not the same thing:** `leap_policy.py:173-205` computes a *third*
  `juice_adequate` for an existing LEAP position's health, read by
  `alerts.py:700` and `recommendation_runner.py:98`. LEAP-only; out of scope.

### The dead constant

`config.SHARES_JUICE_FLOOR_PCT = 1.5` (`config.py:448`, documented `:444-447`)
has **no consumer anywhere in the tree** — verified by repo-wide grep; the only
other hits are prose in `AUDIT_SHARES_PRIMARY_PHASE0_REAUDIT.md:70,221,232,385`,
which already flagged it as un-wired. Phase 1 must decide whether the new
profile-aware floor adopts this name or leaves it dead; leaving two unused
share-denominated floor constants would be worse than either.

### Where a second, profile-aware floor attaches

Three viable attachment points, in preference order:

1. **`scan_triggers.juice_floor_block`** — add a `profile` parameter and replace
   the blanket `return None` at `:314-315` with a per-profile evaluate-then-log
   path returning a non-blocking observation. This is the natural home: it is
   pure over row figures (no account state), so it stays inside the memoized
   market sweep, and it is where `combined_weekly_yield` and `JUICE_BELOW_SLIPPAGE`
   would be computed. Its one caller (`metrics/scorecard.py:540`) has both the row
   yield and (after §1 of the spec) the profile.
2. **`account_gate.py:395-419`** — profile-aware `target` and a second
   `combined_yield` detail field, keeping `blocking=False`. Needed anyway so the
   Execute panel shows the right bar per profile.
3. **`scan_rejection_log`** (`backend/scan_rejection_log.py:1-27`) — the existing
   append-only per-symbol-per-day calibration store under `DATA_DIR`, explicitly
   built for exactly this ("the empirical calibration dataset", `:1`; not in
   state.json, not rebuilt by `recompute_derived`, `:19-22`). **This is the correct
   home for spec §6's "shadow-floor log surface (pass/fail per candidate per day)"** —
   it already has the write cadence (nightly maintenance sweep, `:23`) and the
   idempotent-per-day semantics. Do not build a parallel store.

---

## 2. Entry gate structure (Levels 1–5) and the ETF waiver

### Level map

`backend/screening.py:420-547` (`entry_gate`), stop-on-first-fail at `:538-545`.

| Level | Name | Code | Pass rule |
|---|---|---|---|
| 1 | Market regime green | `screening.py:425-450` | published Genius regime == green (`:448`). The four lights are *sub-checks only* — the level is **not** `_all(checks)` (`:430-431`) |
| 2 | Sector not deteriorating | `:452-469` | `_all` of: RS1M vs SPY not negative, breadth not collapsing, not under distribution (`:460-467`). A **veto**, not a selector (`:452-458`) |
| 3 | Stock lights green | `:471-495` | `row["verdict"] == GREEN` (`:493`) — 4/4 lights **and** no veto. YELLOW never passes (`:475-476`) |
| 3.5 | Structure entrable | `:498-517` | entrability ∈ {READY, CAUTION} (`:510-511`). Explicitly **no `is_etf` branch** (`:502-504`) |
| 4 | Right spot (not extended) | `:519-536` | `right_spot["pass"]` — ATR%, ATR contraction, MA21 extension (`:523-533`) |
| 5 | Account & Juice | `account_gate.py:279-496` | separate module, separate call path — **not** part of `entry_gate` |

### Where RS3M vs Sector actually lives

Not a Level-3 leg. Three sites:

1. **Computed for display / ranking:** `screening.py:296-303` — direct
   `indicators.rs3m(df, sector_df)`, `None` when the row *is* its own sector ETF.
2. **Enforced as a veto:** `stock_lights.evaluate_vetoes` (`stock_lights.py:45-91`),
   veto #1 at `:59-69` (`tripped` iff `applicable and rs_sec < 0`). Any tripped veto
   forces RED (`stock_lights.py:102-113`, applied at `:155-157`), which is what
   makes Level 3 fail. Surfaced as `veto:rs3m_vs_sector` (`:168`).
3. **Enforced as an exit:** `kill_switch.classify` (`kill_switch.py:51-79`) —
   `rs_vs_sector < 0` → hard exit (`:60-63`); `rs_vs_spy < 0` → confirm-on-close
   exit (`:64-67`); the thinning YELLOW leg at `:68-71`. Reason codes at `:105-120`.

There is also a fourth, parallel evaluation in the demoted CFM-suitability lens:
`metrics/scorecard.py:265` (`if not is_etf and rs_sec < T.RS3M_VS_SECTOR_MIN`),
threshold at `metrics/thresholds.py:47`.

### The ETF waiver mechanism — inventory

It is **not one mechanism**. It is six independent `is_etf` branches:

| # | Waived | Site |
|---|---|---|
| 1 | vs-sector veto (stocks only) | `stock_lights.py:61` — `not is_etf` in `applicable` |
| 2 | sector frame withheld from the veto | `screening.py:314-315` — `sector_df=(None if is_etf else sector_df)`; also `stock_lights.py:196-197` |
| 3 | self-comparison for a sector ETF's own row | `screening.py:289`, `:297-298` |
| 4 | kill-switch sector leg for an ETF position | `kill_switch.py:43-47` |
| 5 | **juice-bar relaxation** | `account_gate.weekly_yield_target_pct` (`:189-196`): `if sector_data.is_etf(ticker): return config.ETF_WEEKLY_JUICE_TARGET_PCT` (`config.py:1049` = 1.0) vs the growth bar `CYCLE_RETURN_MIN / CYCLE_WEEKS_MAX * 100` (`:196`) |
| 6 | growth-momentum cautions (MFI / volume ratio / ATR momentum) + the beats-sector AVOID, in the suitability lens | `metrics/scorecard.py:259-286` |

Membership: `sector_data.is_etf` (`sector_data.py:322-326`) = sector-ETF header
in `tickers_by_sector.txt` **or** `config.KNOWN_ETFS` (`config.py:1054`).

### Is the waiver generalizable? — **No. It needs a refactor.**

Assessment, with the specific problems:

* **There is no profile abstraction.** Every branch above re-derives its own
  answer from `sector_data.is_etf(ticker)` at its own call site. There is no
  `profile_for(ticker)` and nothing carries a profile through the gate. Adding
  `DIVIDEND_COMPOUNDER` by copying that pattern means **six more scattered
  branches**, and the spec's "parallel branch, not a modification of existing
  branches" would be violated by construction at sites 1, 2 and 5, which are
  single `if` expressions with no room for a third arm that isn't a rewrite.
* **`is_etf` is a ticker property, not a position property.** The dividend
  profile is a *per-candidate/per-position* discriminator (spec §1 puts
  `income_profile` on positions and scan candidates). These two do not compose:
  `weekly_yield_target_pct(ticker)` (`account_gate.py:189`) takes only a ticker
  and cannot see a profile.
* **The benchmark is hard-wired to the sector ETF.** Every vs-sector site resolves
  its comparison frame through `sector_data.sector_for(ticker)`
  (`sector_data.py:318-319`) — `screening.py:478`, `stock_lights.py:189-197`,
  `kill_switch.py:42-47`, `metrics/scorecard.py:615-637`. **There is no seam for
  substituting a different benchmark frame.** Spec §3's dividend-peer substitution
  requires introducing one.

**Recommended minimal refactor (Phase 1):**

1. New pure module (e.g. `income_profile.py`) owning the enum, `profile_for(ticker,
   position=None)`, and `benchmark_for(profile, ticker)` → the sector ETF for
   `JUICE_ENGINE`, the configured dividend peer for `DIVIDEND_COMPOUNDER`.
2. Thread an optional `benchmark_df` + `profile` through the three pure cores that
   already take frames as arguments — `stock_lights.compute` (`:148-171`),
   `stock_lights.evaluate_vetoes` (`:45-91`), `kill_switch.classify` (`:51-79`).
   All three are already pure over frames/scalars, so this is additive and
   test-isolable; `classify` in particular is explicitly documented as the shared
   pure decision core (`kill_switch.py:53-57`).
3. Make `weekly_yield_target_pct` profile-aware (add a `profile` kwarg; keep the
   `is_etf` arm untouched so ETF behavior is bit-identical).
4. Leave sites 3, 4 and 6 alone — self-comparison and the demoted suitability lens
   are orthogonal to the dividend profile.

**Universe gap:** `SCHD`, `VYM`, `NOBL` are in **neither** `tickers_by_sector.txt`
**nor** `config.KNOWN_ETFS` (`config.py:1054` = `{QQQ, IWM, DIA, SMH, ARKK, XBI,
GDX, XOP, KRE, XHB}`). So the dividend benchmark frame is not prefetched by
`screening._compute_stock_filter` (`:378-384`) or `_compute_sectors` (`:235`) and
`data_handler.get_daily("SCHD")` would be a cold per-request fetch inside the
memoized sweep. Phase 1 must add the configured benchmark to the prefetch lists
at `screening.py:54-56`, `:177`, `:235`, `:382-384` and `metrics/scorecard.py:619`.
Conversely the *candidates* are already covered: KO/PG/KMB/CL are in XLP, JNJ/ABBV
in XLV — `sector_for` resolves them today.

---

## 3. Known approximation interaction

**The flag is closed. There is no difference-approximation path left to inherit.**

Every RS-vs-sector computation in the tree is the direct
`indicators.rs3m(stock_frame, benchmark_frame)` ratio over the same 63-day
lookback (`config.RS3M_LOOKBACK`, `config.py:326`):

* `kill_switch.py:40-47`, with the rationale spelled out at `:21-27`: *"This is
  the true ratio, NOT the vs-SPY difference approximation (rs_vs_spy −
  sector_rs_vs_spy) … `[HARD_CFM_RULE / KILL_SWITCH_RS_SOURCE]`"*.
* `screening.py:296-303`.
* `stock_lights.py:63`.
* `metrics/scorecard.py:210` — *"Sector is the DIRECT rs3m(stock, sector_etf)
  ratio over the same 63-day"*.
* Provenance is recorded on every frozen entry snapshot as
  `rs3m_vs_sector_method: "direct"` (`entry_context.py:278`), deliberately as a
  constant *"so a future change of variant can never be silent"* (`:272-277`), and
  the snapshot schema version notes it (`config.py:1026-1029`,
  `SNAPSHOT_SCHEMA_VERSION = 3` at `:1029`).

**Interaction with a dividend-peer benchmark:** the substitution goes through the
**same direct-ratio path**, because `indicators.rs3m(df, bench_df)` is benchmark-
agnostic — only the second frame changes. So the dividend profile **does not**
inherit an approximation. Per the DO-NOT list nothing is fixed here; two
consequences are recorded instead:

1. **`rs3m_vs_sector_method` must stay honest.** If Phase 1 substitutes the
   benchmark, the frozen snapshot's `"direct"` becomes ambiguous — direct against
   *what*? The field's stated purpose (`entry_context.py:274-277`) is that a
   variant change can never be silent. Phase 1 should record the benchmark symbol
   alongside it (e.g. an additive `rs3m_vs_sector_benchmark`) rather than
   overloading `method`. Additive, so v1/v2/v3 snapshots stay readable.
2. **The `applicable` guard must extend to the new benchmark.** `stock_lights.py:61`
   and `kill_switch.py:43-47` waive the leg when the name *is* its own sector ETF
   (self-comparison computes to exactly 0 and would fire the thinning leg forever
   — `kill_switch.py:28-36`). The identical trap exists for a `DIVIDEND_COMPOUNDER`
   whose ticker *is* the dividend benchmark (SCHD held as a position, compared
   against SCHD). The guard is currently written against `sector_etf`, not against
   "whatever the benchmark is", so it will not catch this.

---

## 4. `compose_verdict`

### Where composition happens

* **The pure core:** `backend/scan_verdict.py:62-88` (`compose_verdict`). Three
  inputs only — regime color, symbol color, `(base_stage, inst_flow)` — placed on
  one severity ladder (`:47`), worst wins (`:78`). Pure; no I/O
  (`scan_verdict.py:11-12`). `is_ready` at `:91-93`.
* **It does not consume the entry gate.** The gate results are folded in *one
  layer up*, in `metrics/scorecard.py:517-552`:
  1. `compose_verdict(...)` → `composed` (`:518-519`);
  2. `scan_triggers.gate_blocks(gate, ext_context=...)` (`:535`) — reads the
     **already-computed** gate for the row (never re-evaluates; `:532-534`);
  3. the juice-floor block appended (`:540-543`);
  4. `scan_triggers.compose_row_verdict(composed, blocks)` (`:544`) — the
     gate-complete verdict, `scan_triggers.py:365-430`;
  5. row fields written at `:545-551`: `verdict`, `verdict_reasons`, `binding`,
     `triggers`, `path_to_ready`, `eligible_days`, `bench`.
* **`gate_blocks`** (`scan_triggers.py:235-289`): Levels 1–4 from the gate dict
  (`:240-280`), Level 5 from `account_gate["blocking_failures"]` only (`:282-287`).
* **Level 5 is a per-request overlay, not part of the memoized sweep:**
  `app.py:172` (`evaluate_many`) → `:208-231`, with `l5_triggers` / `l5_path_to_ready`
  / `l5_eligible_days` on the entry (`:225-228`) and the ready/near-miss split at
  `:233`. Rationale at `metrics/scorecard.py:530-532`.

### What must change so a profile discriminator flows through

Minimal and additive; **none of it touches the `JUICE_ENGINE` decision path**:

1. **`metrics/scorecard.score_ticker`** (`:405-...`) — resolve the profile once
   (`income_profile.profile_for(ticker)`) and write `row["income_profile"]`
   beside `row["is_etf"]` (`:463`). One new row key. No verdict effect.
2. **`scan_triggers.juice_floor_block`** — accept the profile; return the
   observation (non-blocking) rather than the current unconditional `None`. See
   item 1. **Do not append it to `blocks`** for the shadow path: `blocks` feed
   `compose_row_verdict`, which is what gives a block verdict authority
   (`scan_triggers.py:339-340` maps `SAFETY → BLOCKED`). Carry it as a separate
   `row["shadow_floor"]` field instead. This is the single most important
   don't-break-it detail in the whole work item.
3. **`compose_verdict` itself: unchanged.** Its three inputs are regime, symbol
   and structure — none is profile-dependent, and spec §3 waives no trend-quality
   check. Leaving it byte-identical is the cheapest way to satisfy test 2
   (`JUICE_ENGINE` verdicts byte-identical) and test 1 (XLK July 6).
4. **`account_gate.evaluate`** — add a `profile` kwarg (default `JUICE_ENGINE`),
   used only to select the target bar and to add combined-yield detail. Existing
   callers omitting it get today's behavior exactly.
5. **`app.py:212-231`** — pass `income_profile` through onto the scan-ready entry
   so the Execute gate and the badge (spec §6) can read it. Additive key.
6. **Execute path** — `frontend/src/components/ExecuteTab.jsx` and the
   `/api/account-gate` route (`app.py:300-322`) need the profile as a query param
   to render the right bar. `api_account_gate` currently accepts only
   `ticker`/`contracts`/`leap_cost`/`weekly_extrinsic` (`:311-320`).

---

## 5. Dividend handling already present from v20

**Far more exists than the prompt assumes.** Inventory:

### Present

| Capability | Site |
|---|---|
| Per-ticker trailing dividend **yield** (decimal), override → day-cache → provider | `backend/dividends.py:93-116` (`yield_for`), `_normalize` `:32-43`, cache `:46-59`, override `:62-69` |
| Yield **with provenance**, for risk-path logging | `dividends.py:119-129` (`q_with_source`) |
| Next dividend **event** `{ex_date, amount, source}` | `dividends.py:210-237` (`next_dividend`), fetch `:167-207`, per-payment derivation `:151-164` |
| **Cache-only** event read (no fetch storm on a bulk scan) | `dividends.py:240-263` (`cached_dividend`) |
| Cache health for the data-health panel | `dividends.py:132-145` |
| Per-position `dividend` block, seeded by migration | `migrations.py:53-62` (`_v2_to_v3`) |
| Dividend snapshot captured at entry | `account_gate.py:452-456`; stamped onto the position at `executor.py:2891-2898` |
| **Ex-div-inside-cycle** Level-5 warning (non-blocking) | `account_gate.py:458-476` (`ex_div_in_cycle`) |
| Dividend-**adjusted** option greeks (`q` in BSM) | `indicators.py:542-563` (`call_greeks_full`), `:585-...` (`call_greeks`) |
| **Dividend income booking action** end-to-end | `executor.py:36` (`VALID_ACTIONS`), `:502-503` (dispatch), `:1883-1909` (`_dividend_income`) — fields `amount`, `shares`, `per_share`, `ex_date`, `pay_date`, `source` |
| **Derived dividend ledger**, kept out of the juice/theta taxonomy | `logging_handler.py:990-1011` — `state["dividend_ledger"]` with `records` / `by_ticker` / `by_month` / `total` |
| **Broker cash-dividend recognition** + one-click proposals | `transaction_ingest.py:156-160` (`DIVIDEND_TYPES`), `:163-188` (`parse_dividend`), `:191-204` (`dividend_proposals`) |
| Nightly refresh of position dividend data | `maintenance.py` (covered by `test_portfolio_risk.py:205-...`) |
| **Ex-div early-assignment guard** — already implemented | `alerts.py:422-505` (`check_assignment_risk`), registered `:1037`; severity `HIGH` + `HARD_CFM_RULE` rationale `:40`; category `defend` `:94`; per-position read `position_manager.py:212-243` |

### Assessment against the three requested inventories

**(a) Ex-div calendar ingestion — partially present, one hard gap.**
The provider abstraction spec §5 asks for **already exists** (`dividends._fetch_event`,
`:167-207`) with the correct shape: Schwab first, Alpha Vantage fallback,
day-cached, override-first. The gap is precisely the one the spec names:
`dividends.py:175-179` carries an explicit `LIVE_VERIFY` — *"none of these ex-div
key names is confirmed against a live Schwab fundamental payload … The whole
covered-call assignment-risk surface depends on one resolving."* The candidate
keys tried are `nextDivExDate` / `divExDate` / `dividendDate` / `divDate`
(`:180-182`) and `divPayAmount` / `divAmount` / `divFreq` (`:183-191`). Alpha
Vantage's `ExDividendDate` / `DividendPerShare` (`:196-204`) is the only *verified*
path. **Missing:** the internal data contract is implicit in the return dict
rather than written down; there is no fixture-backed stub for offline tests; the
Schwab adapter is a guess rather than a documented TODO. Phase 1 §5's
"contract + stub + documented TODO" is exactly the right shape and mostly a
formalization of what is here.

**(b) Dividend receipt recording — present; the `DIVIDEND_RECEIPT` event of
spec §1 is a rename of the existing `dividend_income` action.** The field set
already matches (`ticker`, `amount`, `shares`, `per_share`, `ex_date`, `pay_date`,
`source`). **Recommendation: do not introduce a second event type.** Adding
`DIVIDEND_RECEIPT` alongside `dividend_income` would fork the derivation at
`logging_handler.py:998` and silently orphan any already-booked dividend from the
accrual ledger. Extend the existing action instead; the spec's intent (a typed,
append-only dividend event) is already satisfied.

**(c) Reconciliation against broker cash — present but not wired to a UI.**
`parse_dividend` + `dividend_proposals` produce `action: "dividend_income"`
proposals with a `proposal_id` and human summary (`transaction_ingest.py:191-204`),
which is exactly the `MANUAL_TRADE_INGEST`-style preview-before-commit flow spec §7
asks for, and it correctly never auto-books (`:193` — `NO_AUTO_REMEDIATION`).
**Missing:** confirmation that `dividend_proposals` is actually called from the
ingestion sweep and surfaced in the diff UI. `state["ingestion"]["proposals"]`
exists (`migrations.py:296-303`); the wiring from `dividend_proposals` into it was
not located, and `DIVIDEND_TYPES` carries its own `LIVE_VERIFY`
(`transaction_ingest.py:156-159`) — the real Schwab `type` value for a cash
dividend is unconfirmed.

### Genuinely missing for this work item

1. `income_profile` discriminator (does not exist in any form).
2. Per-position **accrual ledger** and the `ACCRUAL_CREDIT` / `LOT_ADD_RECOMMENDED`
   / `LOT_ADD_EXECUTED` events.
3. **Combined weekly-equivalent yield** — nothing computes `annual_yield / 52`
   anywhere; `dividends.yield_for` returns an annual decimal and its only consumers
   are the greeks (`q`).
4. Dividend-peer **benchmark** plumbing (item 2).
5. Trailing-annual-yield **provenance for the metric** — `yield_for` returns `0.0`
   for both "non-payer" and "unknown" (`:116`, documented `:13`). For greeks that's
   the safe no-op; for a **displayed** combined yield it silently understates a
   payer whose provider lookup failed. `q_with_source` (`:119-129`) already
   distinguishes them and should be the metric's input, not `yield_for`.

---

## 6. Correction-event layer

### Existing typed correction events

The layer is **narrower than "events"** — there is one correction action plus a
set of income/adjustment actions.

* **The correction action:** `logging_handler.TXN_CORRECTION_ACTION = "txn_correction"`
  (`logging_handler.py:734`).
* **Full `VALID_ACTIONS` set** (`executor.py:28-36`): `buy_leap`, `sell_short`,
  `close_short`, `close_leap`, `roll_short`, `roll_leap`, `open_position_atomic`,
  `close_position_atomic`, `adjustment`, `buy_shares`, `sell_shares`,
  `close_shares_assigned`, `dividend_income`.
* **`adjustment`** (`executor.py:496-499`, `:632-...`) — the reconciliation
  resolution path: an immutable execution plus a position holding correction,
  with **no gate and no price capture** (`:495-496`).
* **`dividend_income`** (`executor.py:502-503`) — same shape: no gate, no price
  capture, no order (`:501-502`).

### Append-only pattern — confirmed, and it is strict

* A correction is *"an APPENDED typed event that carries an economic edit for an
  earlier execution"* (`logging_handler.py:739-741`), **never** an in-place rewrite.
* `_correction_overlay` (`:737-758`) builds `{target_id: edit}` from appended
  records, applied in append order, last-write-wins.
* `derived_executions` (`:761-780`) applies the overlay at **derive time** and
  excludes the `txn_correction` records themselves from the returned trade list
  (`:761-763`, `:775-779`).
* The writer: `executor.py:1290-1338` — *"recorded as an APPENDED `txn_correction`
  event (never an in-place rewrite of …)"* (`:1290-1291`); it composes over the
  already-overlaid view then appends one record per edit (`:1302-1318`).
* Migrations are additive by contract: *"Migrations only ADD structure — they never
  rewrite executions (those are immutable) and never delete user data"*
  (`migrations.py:6-8`), reaffirmed for v20 at `:322-325`.
* Derived state is rebuilt, never edited: `logging_handler.recompute_derived`
  (dividend ledger `:990-1011`, roll ledger `:1013-...`).

### Where the new event types register

Four coordinated edits, all additive:

1. **`executor.VALID_ACTIONS`** (`executor.py:28-36`) — add the new action strings.
2. **`executor.execute` dispatch** — a no-gate/no-price-capture branch beside
   `adjustment` (`:496-499`) and `dividend_income` (`:501-503`). These two are the
   template: both return before `_capture_price` (`:505`), before the Level-5 gate
   (`:513-514`), and before the market-settle window (`:542`). `ACCRUAL_CREDIT`
   and `LOT_ADD_RECOMMENDED` belong there; **`LOT_ADD_EXECUTED` does not** — see
   item 7.
3. **`logging_handler.recompute_derived`** — derive `accrual_ledger` from the
   executions exactly as `dividend_ledger` is derived (`:990-1011`), including the
   `UNDATED` fallback convention (`:1002`).
4. **`migrations._v20_to_v21`** + `MIGRATIONS` dict (`migrations.py:332-352`) —
   seed the empty derived structure so readers never key-error on an un-recomputed
   load, the pattern used by `_v3_to_v4` (`:65-70`) and `_v4_to_v5` (`:72-77`).

### No in-place mutation in the proposed path — confirmed, with one caveat

Verified clean: `_dividend_income` (`executor.py:1883-1909`) reads the position
(`:1890`) purely to derive `shares_ct` and never writes it — *"Never mutates
holdings; the immutable record is the source of truth"* (`:1887-1888`). Deriving
the accrual ledger from executions in `recompute_derived` inherits the same
property.

**Caveat (a real trap):** spec §4's *"Accrued cash never changes covered-call math
until a lot is actually added"* is protected by construction **only if** the
accrual ledger stays in the derived layer and is never written into
`position["shares"]["count"]`. `position_manager.covered_lots`
(`position_manager.py:537-550`) derives coverable lots by integer division of the
share count, and `position_capital` (`:552-565`) sums LEAP cost bases plus
`count × cost_basis_per_share`. If accrued cash ever leaks into either, both
covered-call coverage and the Level-5 capital check silently shift. **Phase 1 must
add an explicit test that a non-zero accrual balance changes neither
`covered_lots` nor `position_capital`** — that is spec test 7 (atomicity), and
these two functions are what it must actually assert against.

---

## 7. Level 5 account gate — and the blocking finding

### Where each check is enforced

All in `backend/account_gate.py:279-496` (`evaluate`):

| Check | id | Lines | Blocking |
|---|---|---|---|
| Cash reserve (post-trade cash ≥ 2×ATR book reserve) | `cash_reserve` | `:329-349` | yes |
| Position count cap | `position_limit` | `:351-356` | yes |
| Deployed-capital cap | `capital_limit` | `:358-365` | yes |
| Round-lot size block | `round_lot_size` | `:367-382` | yes — **but see below** |
| One-per-sector | `sector_concentration` | `:384-393` | yes |
| Weekly juice adequacy | `juice_adequacy` | `:395-419` | **shadow in shares mode** (`:414`) |
| Juice too rich (warning) | `juice_rich` | `:421-432` | no |
| Earnings inside cycle | `earnings_in_cycle` | `:434-447` | yes |
| Ex-div inside cycle (warning) | `ex_div_in_cycle` | `:458-476` | no |

Partition at `:478-479`; result contract at `:480-496`. Bulk variant
`evaluate_many` at `:499-512` (one shared state snapshot, one cash read).

### The entry point a builder-recommended lot add would call

`backend/executor.py:1742-1766` — `_enforce_account_gate(payload, ticker, contracts)`.
It calls `account_gate.evaluate` (`:1750-1754`), stashes the result on
`payload["_account_gate"]` (`:1755`), and raises `ValueError` → HTTP 400 on any
blocking failure unless `override_reason` is supplied (`:1758-1766`).

### **FINDING 7-A (blocking): the Level 5 gate is never run on the shares path.**

`executor.py:513-514`:

```
if action in ("buy_leap", "open_position_atomic"):
    _enforce_account_gate(payload, ticker, contracts)
```

`buy_shares` is **not** in that tuple. It dispatches at `executor.py:1805-1806`
to `_buy_shares` (`:2852-...`), which at `:2869` reads
`gate = payload.get("_account_gate") or {}` — **a key nothing ever sets on this
path.** The consequences, all live today:

* Cash reserve, position limit, capital cap, sector concentration and the
  earnings-in-cycle block are **not enforced** on any shares entry.
* `_buy_shares` falls back to `account_gate.suggested_circuit_breaker(ticker)`
  directly (`:2880-2883`) precisely because the gate dict is empty — the fallback
  is load-bearing, which is why the omission has not surfaced as a crash.
* The `override` record written at `:2870-2872` reads
  `gate.get("blocking_failures", [])` → always `[]`, so an override on a shares
  entry logs **no failed checks**. The override audit trail is empty by construction.
* Note the ordering is otherwise correct: `buy_shares` **is** in
  `FROZEN_BLOCKED_ACTIONS` (`executor.py:44-45`), so the reconciliation freeze is
  enforced (`:492-493`).

**Impact on this work item:** spec §4 requires *"The recommendation must pass the
full Level 5 account gate (capital cap, reserve, position limit, juice adequacy at
the new size) before it is shown as actionable."* **There is currently no enforced
Level 5 on the shares path for a lot add to pass.** Phase 1 must add
`"buy_shares"` to the tuple at `executor.py:513`. That is a behavior change to an
existing path — it will start rejecting shares entries that a low cash balance or
a full sector slot should always have rejected. **It is a fix, not a regression,
but it is out of the literal scope of "dividend profile" and should be
acknowledged explicitly before Phase 1 proceeds.**

### **FINDING 7-B: `round_lot_size` never fires.**

`account_gate.py:373` guards the check with `if position_type == position_types.SHARES`.
`position_type` is a parameter of `evaluate` (`:283`) that **no caller ever
passes** — verified across `app.py:172`, `app.py:314-319`, `executor.py:1750-1754`,
`recommendation_runner.py:138`. It therefore defaults to `None`, `shares_mode`
falls through to `config.LEGACY_LEAP_READONLY` (`:317-320`) for the *sizing* math,
but the round-lot **check is never appended**. The `PER_POSITION_CAP_USD` SIZE-BLOCK
(`config.py:443`) is dead in production. `test_shares_migration.py:207` and `:218`
pass only because they call `evaluate(position_type=...)` explicitly.

**Impact:** spec §4's Level-5 gate for a lot add explicitly includes the per-lot
cap. Phase 1 must pass `position_type` from the shares call sites.

### Where `LOT_ADD_EXECUTED` must route

**Not** through the no-gate branch beside `adjustment`/`dividend_income`. A lot
add buys 100 real shares — it is new risk and must traverse the full existing
path: freeze check (`:492-493`), Level 5 (`:513`, once 7-A is fixed), market-settle
execution window (`execution_gate.execution_window` via `executor.py:542`), and
spread quality (`:543`). The correct implementation is that `LOT_ADD_EXECUTED` is
a **`buy_shares` execution carrying a lot-add provenance stamp**, not a new
action — reusing one code path is also what keeps `position_capital` and
`covered_lots` consistent for free. `LOT_ADD_RECOMMENDED` is pure telemetry and
does belong in the no-gate branch.

---

## 8. Reconciliation & freeze

### How a broker-cash dividend interacts with freeze

* **Freeze verdict:** `reconcile.freeze_status` (`reconcile.py:780-813`) — the book
  is FROZEN when any open position carries `needs_review` (`:797-801`). While
  frozen, recommendation generation is blocked and new-risk submission on the
  diverging position is blocked (`:781-787`). Plus a market-hours staleness
  degrade — a warning, **not** a freeze (`:788-791`, `:815-...`).
* **What freeze blocks:** `executor.FROZEN_BLOCKED_ACTIONS` (`executor.py:38-45`)
  = `{buy_leap, sell_short, roll_short, roll_leap, open_position_atomic, buy_shares}`.
  Enforced at `executor.py:491-493`, **before** the account gate, so a freeze wins
  over a gate rejection (`:490-491`).
* **Closing actions remain excepted — confirmed.** `close_short`, `close_leap`,
  `close_position_atomic` are deliberately absent, documented at `executor.py:39-43`:
  *"a freeze must never trap the operator in a position during a kill-switch event
  — exiting is safe in either state of the world. `adjustment` is the resolution
  path, also allowed."* `sell_shares` and `close_shares_assigned` are likewise
  absent — a shares exit is never frozen.
* **A dividend receipt is not blocked.** `dividend_income` is not in
  `FROZEN_BLOCKED_ACTIONS` and dispatches at `executor.py:502-503`, *after* the
  freeze check at `:493` but on a branch the check does not cover. This is the
  correct semantics: booking income that has already landed in the broker account
  adds no risk. **No freeze-lift logic is needed and none should be written.**

### Implication for the accrual ledger under freeze

Follows from the above and needs stating: the accrual ledger is **derived**, so a
`DIVIDEND_RECEIPT` booked during a freeze correctly increases `accrued_cash` and
may cross the lot threshold while the book is frozen. `LOT_ADD_RECOMMENDED`
(telemetry) is fine to emit. `LOT_ADD_EXECUTED` routes through `buy_shares`
(item 7) and is therefore **automatically blocked by the existing freeze check** —
no new code, provided the routing decision in item 7 is honored. Spec §4's
"blocked-with-reason" surface should read the freeze reason from
`reconcile.freeze_status(state)["reason"]` (`reconcile.py:802-805`) rather than
re-deriving it.

### The "in-flight reconciliation hotfix"

**There is no in-flight hotfix. Nothing is in flight, so there is no overlap.**

* `mcp__github__list_pull_requests(state="open")` → **`[]`**. No open PRs.
* The working tree is clean at `d6f3af8`; only `master` and this branch exist on
  the remote.
* The reconciliation/incident work referenced by the prompt has **already landed**:
  `AUDIT_INCIDENT_HOTFIX.md` + `IMPLEMENTATION_NOTES_INCIDENT_HOTFIX.md` +
  `backend/test_incident_hotfix.py` (18 tests, `:124-364`), and
  `AUDIT_LIFECYCLE_RECONCILIATION.md` + `IMPLEMENTATION_NOTES_LIFECYCLE_RECONCILIATION.md`
  + `backend/test_reconcile_freeze_gate.py` (6 tests, `:43-136`). All green in the
  baseline run.

**Adjacent code this work item touches, for the record:** the stable-diff-id fix
(`reconcile._stable_id`, `:325-334`) exists because per-run `diff_001…` ids
re-numbered each run, dropping acknowledgements and **re-asserting freezes** — a
bug explicitly labeled a *"schema v20 gap"* (`:328-329`). If Phase 1 adds a
dividend-receipt diff class to reconciliation (spec §7), it **must** key its
acks off `_stable_id`, not the per-run id, or it will reintroduce exactly that
bug in a new place. That is the only real coupling.

---

## 9. Day-count consistency

### The normalization sites

| Site | Convention | Citation |
|---|---|---|
| BSM theta | annualized ÷ **365** → per **calendar** day | `indicators.py:542-563`, esp. `:561` (`theta_year / 365.0`); documented `:546-550` |
| LEAP weekly burn | theta/calendar-day × **7** | `indicators.py:566-581`, `:581` |
| Burn projection | `T = dte / 365` | `burn.py:43`; extension steps `weeks × 7` at `:84`, `:205` |
| **Net juice/week — the pinned convention** | one shared **7-calendar-day** week | `burn.py:216-231`, spelled out `:222-228` |
| Realized juice bucketing | ISO calendar week | `logging_handler.bucket_datetime` `:549-555`, `_iso_week` `:558-563` |
| Cycle window (earnings/ex-div) | `CYCLE_WEEKS_MAX × 7` calendar days | `account_gate.py:443` |
| **Weekly short BS pricing** | `T = 5 / 365` — **5 trading days over a 365-calendar-day year** | `account_gate.py:90` |
| LEAP BS pricing | `LEAP_TARGET_DTE / 365` | `account_gate.py:96`, `leap_policy.py:282`, `:298` |
| Dividend yield | **annual decimal**, no weekly normalization anywhere | `dividends.py:93-116` |

### Does the open flag interact? — **Yes, but not where the prompt expects.**

The theta/365-vs-weekly flag is **already resolved and pinned** for the juice
path. `burn.net_juice_per_week` (`:216-231`) states the convention as a named
invariant — `[NET_JUICE_TIME_BASE / HARD_CFM_RULE]` — with both terms on one
7-calendar-day base, and it is locked by
`test_burn.test_net_juice_day_count_convention_is_pinned` (`burn.py:230-231`).
So the extrinsic side of `combined_weekly_yield` inherits a settled convention.

**The real inconsistency is `account_gate.py:90.`** `t_week = 5 / 365.0` prices
the weekly short's Black-Scholes extrinsic over **5 trading days**, while every
consumer of that number treats it as a **7-calendar-day** week: it flows into
`weekly_extrinsic_per_share` (`:130`), `covered_call_yield_pct` (`:138`),
`weekly_yield_pct` (`:140`), and thence into the `juice_adequacy` check (`:404-405`)
and the scanner's ranking. It is a ~2.5% understatement of a weekly figure
(√(7/5) on the vol term) — small, consistent, and pre-existing. **It is out of
scope; documenting it is the point of this item.** Phase 1 must not "fix" it,
because doing so would shift `weekly_yield_pct` on every existing fixture and
break spec test 2 (byte-identical `JUICE_ENGINE` verdicts).

### The convention the combined metric will use

**Stated for the record, to be pinned by spec test 10:**

> `combined_weekly_yield` is expressed **per 7-calendar-day week**, on the same
> time base as `burn.net_juice_per_week` (`[NET_JUICE_TIME_BASE]`).
>
> * **Juice leg:** the existing weekly extrinsic yield, taken **as-is** from
>   `account_gate.juice_estimate` / the live chain. Its internal `5/365` pricing
>   basis (`account_gate.py:90`) is inherited unchanged and **not corrected** here.
> * **Dividend leg:** `trailing_annual_dividend_yield / 52`, i.e. a **52-week
>   year**, *not* `× 7 / 365` (which would give 51.07 weeks). The two differ by
>   ~1.8% of the dividend leg — on a 3% payer that is 0.001%/wk, immaterial to the
>   0.5%/wk floor, but the choice must be pinned so it cannot drift.
> * **Source:** `dividends.q_with_source` (`dividends.py:119-129`), **not**
>   `yield_for` — so an unknown yield is distinguishable from a genuine non-payer
>   (see item 5) and the displayed metric never silently reports a payer as 0.

Rationale for 52 over 365/7: the dividend input is a *quoted annual rate*, and
`/52` is the conventional reading of "weekly equivalent" for a quoted annual
figure. The spec text itself says `annual_yield / 52`. Consistency with the
7-calendar-day juice base is preserved to well within the metric's own precision.

---

## 10. Test surface

### Baseline

`1057 passed, 1 failed` — see the header for the single environment-caused failure.

### Juice floor

| Test | Asserts |
|---|---|
| `test_scan_triggers.py:148-161` `test_juice_floor_block_two_tiers` | hard tier at net ≤ 0, adequacy tier below `JUICE_FLOOR_WK`, `None` above, `None` on missing figures |
| `test_scan_triggers.py:164-171` `test_juice_floor_block_is_shadow_in_shares_mode` | **the shares short-circuit** — both tiers return `None` under `LEGACY_LEAP_READONLY` |
| `test_scan_triggers.py:174-214` | juice floor binds at level 5 over an L4 block; `SAFETY` beats `CALENDAR` |
| `test_account_gate.py:98-115` `test_gate_juice_adequacy_is_shadow_and_sized_on_share_cost` | `shadow: True`, `blocking: False`, sized on **share** cost, absent from `blocking_failures` |
| `test_account_gate.py:195-214` | the same check **blocking** in the legacy path |
| `test_account_gate.py:529-558` | `/api/scan/ready` splits ready vs near-miss on the L5 `blocking_failures` |
| `test_scorecard.py:84`, `:106` | monkeypatch `juice_floor_block → None` to isolate verdict composition |
| `test_universe.py:115-125` | ETF juice bar strictly below the stock bar |

### Entry gate / ETF waiver

`test_cfm.py:1077-1090` (sector ETF has no vs-sector RS, `is_etf` True),
`:1131-1136` (lower ETF RS-vs-SPY bar), `:1138-1157` (stock and ETF get
**identical** lights and right-spot), `:1158-...` (the ETF is scanned alongside
its constituents); `test_kill_switch.py:42-...` (self-comparison waiver);
`test_scorecard.py:278-301` (ETF waives growth-momentum cautions and the
beats-sector AVOID, but **is still caught** by below-MA200 / extension /
below-MA50 / MA50-slope); `:375-...` (sector leg nulled for a sector-ETF
candidate); `test_stock_lights.py:206-...` (identical series → identical verdicts);
`test_structure_classifier.py:205-209` (**structure takes no `is_etf` argument** —
a standing invariant the dividend branch must also respect).

### Verdict composition

`test_scan_verdict.py` (whole file), `test_scorecard.py` (`compute_verdict` +
`score_ticker`), `test_scan_triggers.py:174-214` (`compose_row_verdict`),
`test_shares_migration.py:357` `test_verdict_engine_unchanged_by_migration` — the
**direct precedent for spec test 2**; whatever it does for v20 should be extended,
not duplicated, for v21.

### Shares/dividend surface already under test

`test_shares_migration.py`: `:53` v19→v20 backfill, `:72` absent-type degrades to
legacy, `:178` **`covered_lots` fragment never rounds up** (the atomicity precedent
for spec test 7), `:186` naked-short flag, `:207`/`:218` round-lot size block
(the only callers passing `position_type`), `:233`/`:246` ex-div-in-cycle warn/pass,
`:258` **`dividend_income` is its own ledger, not juice** (the precedent for
"`ACCRUAL_CREDIT` never contaminates juice"), `:279` assignment note shares vs
legacy, `:290` history edit append-only and deterministic.
Also `test_alerts.py` (ASSIGNMENT_RISK), `test_transaction_ingest.py`,
`test_reconcile_freeze_gate.py:43-136`.

### XLK July 6th — **there are two artifacts, and the prompt's "the fixture" is ambiguous**

**(a) The parquet fixture.**
`backend/fixtures/regime/xlk_july6_rollover.parquet`, built by
`fixtures/regime/build_fixtures.py:90-121` (registered `:122`).
Consumed by **exactly one test**: `test_stock_lights.py:84-104`
`test_july6_xlk_rollover_caught_by_both_layers`. Asserts:
* Layer 1 — SAR red **or** momentum red, and `greens < 4` (`:88-91`);
* Layer 2 — `atr_expanding is True`; through the **ETF path** (`is_etf=True`,
  where the vs-sector veto is waived) with `ivr_percentile=95.0` → verdict RED and
  `veto:atr_expanding_high_ivr` in `veto_reasons` (`:96-99`);
* Independence — with a benign IVR (`10.0`) the lights **alone** still deny GREEN
  (`:101-103`).

Note it is **not** in `test_regime_regression.py`'s parametrized well-formedness
list (`:84-85`, which covers only `sustained_green`, `distribution_rollover`,
`v_bottom_whipsaw`).

**(b) The synthetic-frame regression, which is what the DO-NOT list actually means.**
`test_recommendation_engine.py:275-329`
`test_xlk_july6_snapshot_blocking_verdict_and_no_enter`. Frames built inline at
`:285-294` (220 up bars then 30 hard-down, last bar pinned to `2026-07-06` at
`:293`). Asserts:
* `sc.score_ticker("XLK", ...)` → `suitability in ("AVOID", "CAUTION")` (`:313`);
* `engine.evaluate(...)` emits **no `ENTER`** (`:324-326`);
* `engine._entry_blocked(...)` names **at least one blocker** (`:327-329`).
The header (`:275-284`) states the lock is **on the behavior**, not the bars, and
that the frames are a labeled synthetic reconstruction to be replaced if the real
cached frame is ever exported.

**Spec test 1 must pin both.** (a) goes through the ETF waiver path — the exact
mechanism the dividend branch is modeled on — and (b) goes through
`score_ticker` → `compute_verdict` → the recommendation engine, which is the path
a new `income_profile` row key traverses. A change that broke one while leaving
the other green would still be a defect under the spec's own wording.

---

## Open questions for review (Phase 1 blockers)

1. **Schema version.** Confirm the migration is **v20 → v21**, not v13 → v14.
2. **`_enforce_account_gate` on `buy_shares` (Finding 7-A).** This is a live gap
   independent of the dividend sleeve, and spec §4 cannot be satisfied without
   closing it. Fix it inside this work item, or split it out first? Fixing it will
   start rejecting shares entries that today pass ungated.
3. **`position_type` at the gate call sites (Finding 7-B).** Same question for the
   dead `round_lot_size` SIZE-BLOCK.
4. **`DIVIDEND_RECEIPT` vs the existing `dividend_income` action.** Recommendation:
   extend the existing action rather than add a second event type, to avoid forking
   the derivation at `logging_handler.py:998` and orphaning already-booked dividends
   from the accrual ledger. Confirm.
5. **Dividend benchmark in the universe.** `SCHD`/`VYM`/`NOBL` are in neither
   `tickers_by_sector.txt` nor `config.KNOWN_ETFS`. Add the configured benchmark to
   `config.KNOWN_ETFS` and to the five prefetch lists, or fetch it on demand?
6. **Shadow-floor log surface.** Confirm it goes into the existing
   `scan_rejection_log` (`DATA_DIR`, append-only, one record per symbol per day)
   rather than a new store.
7. **`juice_floor_block`'s unconditional `None`.** Spec §2's graduation path needs
   logged calibration data, which requires evaluating the floor without giving it
   authority. Confirm that changing `:314-315` from "short-circuit" to
   "evaluate, log, return non-blocking" is in scope — it is a change to a
   `JUICE_ENGINE` code path, though not to any `JUICE_ENGINE` *verdict*.

---

**HARD STOP.** Awaiting approval before Phase 1.
