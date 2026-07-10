# Phase 1 Audit — Recommendation Engine + Trust Scoreboard + Execution Fidelity Ledger

All references are `backend/<file>:<line>` (or `frontend/src/...`) against branch
`claude/recommendation-trust-layer-kngqnk` at v2.5.0, state schema v16. This is a
written audit only — no file other than this report was created or modified.

**Executive summary.** The architecture is already pointed the right way: executions
are immutable and append-only, every ledger derives in `recompute_derived()`, entry
context is frozen at trade time, exits carry coded reasons, and order transitions are
an append-only `order_events` log replayed into a derived `order_state`. What does
NOT exist anywhere is a *decision*: every evaluator in the codebase emits a
**condition** plus prose advice ("EXIT ... immediately", "roll it down/out"), never a
concrete proposed ticket. "No action" is not representable as a record. Nearly every
evaluator is impure (reads providers/clock/state directly), with one outright
write-side-effect inside a gate. The pre-migration snapshot mechanism required for
the v17 migration exists and is tested. The largest genuine gaps for the fidelity
ledger are: no ticket-level min-credit / max-slippage bound field, an unconfirmed
`pending_cancel` escape path, no partial-fill orphan detection on the atomic-open
expiry path, and no post-fill buying-power diff. The "XLK July 6th snapshot" named
in the test plan does not exist in the repo and must be sourced in Phase 2.

---

## 1.1 Inventory of decision-shaped logic

Legend — **Output**: CONDITION (a fact) vs DECISION (a specific actionable ticket).
**Snapshotted**: are the evaluator's inputs frozen anywhere today.

### 1.1.1 Kill switch — `kill_switch.py`

| | |
|---|---|
| Functions | `_rs_pair(ticker)` `:18-48`, `evaluate(ticker)` `:51-83`, `evaluate_all(state)` `:86-92`, `exit_reason_code(evaluation)` `:95-110` |
| Output | **CONDITION.** `{status: green/yellow/red, alert, suggested_action, rs3m_vs_spy, rs3m_vs_sector}`. `suggested_action` is prose. Both rules present: RS3M-vs-Sector negative → red/"exit immediately" and RS3M-vs-SPY negative → red/"exit within 1–2 days" (confirmed close — the 16:15 post-close alert slot exists precisely to evaluate it on the official close). `exit_reason_code` maps red → `KILL_SWITCH_SECTOR` / `KILL_SWITCH_SPY`. Advisory by declared invariant (`exit_reasons.py:4-10`). |
| Purity | **Impure.** Takes only `ticker`; internally reads `data_handler.get_daily` (SPY, ticker, sector ETF) `:38-46`, `sector_data.sector_for` `:42`, `indicators.rs3m` `:40,47`, and `earnings.next_earnings` `:69` (provider). No injected snapshot or clock. |
| Snapshotted | Not at evaluation time. The same RS features are frozen only at *entry* (`entry_context` stock section, `entry_context.py:255-280`) and at *close* (`exit_metrics`, `:378-407`). Intraday RS inputs are refreshed at most `REFRESH_KILLSWITCH_PER_DAY`=3 times/session (`tier_poll.py:138-168`) — relevant to timeliness measurement. |
| Callers | `alerts.check_kill_switch` `alerts.py:146-160`; `GET /api/kill-switch` `app.py:674`; overview join `app.py:725`; accumulation guard `position_manager.py:342`. |
| Constants | `STOCK_RS_VS_SECTOR_MIN`=0.0 `config.py:213`, `STOCK_RS_VS_SPY_MIN`=5.0 `:206` (kill line at 0), HARD_CFM_RULE. Sector leg waived (None) when ticker is its own sector ETF. |

### 1.1.2 Circuit breaker / line in the sand — `circuit_breaker.py`

| | |
|---|---|
| Functions | `evaluate(position, df=None)` `:39-131` (4 conditions: ≥15% drawdown, 3 closes < MA50, 1 close < MA200, operator manual line, plus an "approaching" soft band), `evaluate_all(state)` `:134-140`, `exit_reason_code` `:153-163` |
| Output | **CONDITION.** `{status, tripped, tripped_conditions[ids], approaching, headline, suggested_action(prose)}`. Per-condition coded exit reasons already exist (`CB_DRAWDOWN_15`, `CB_MA50_3CLOSE`, `CB_MA200_CLOSE`, `CB_MANUAL_LINE`). |
| Purity | **Nearly pure.** Pure over `(position, df)` when `df` is supplied; defaults to `data_handler.get_daily` `:48` when not. The best-shaped evaluator in the codebase for the Phase 2 refactor. |
| Snapshotted | **Yes — reference inputs.** `position.circuit_breaker = {price, source, set_at, entry_price}` written at entry (`executor.py:1179-1182`), mirrored to the execution (`:1183`), re-stamped on LEAP roll (`:2134`). The *evaluation-time* market inputs (closes, MAs) are not snapshotted. |
| Callers | `alerts.check_circuit_breaker` `alerts.py:322-341`; `position_manager.enrich_position` `:331-332`. |
| Constants | `CIRCUIT_BREAKER_DROP_PCT`=0.15, `MA_FAST`=50/`MA_FAST_CLOSES`=3, `MA_SLOW`=200 `config.py:852-855`; suggested line `max(MA50, entry − 2×ATR)` via `CIRCUIT_BREAKER_ATR_MULT`=2.0 `:841`. |

### 1.1.3 Whipsaw guard — `position_manager.whipsaw_status` `:217-262`

- **Output**: CONDITION. `{tripped, defensive_rolls, roll_drag, drag_pct, rolls_trip, drag_trip, reasons[]}` — trips on ≥`WHIPSAW_DEFEND_ROLLS`(3) defend rolls in `WHIPSAW_WINDOW_WEEKS`(4) OR drag ≥ `WHIPSAW_DRAG_PCT`(5%) of position capital (`config.py:747-749`; concept HARD_CFM_RULE, numbers PROPOSED_DEFAULT). Recommends EXIT via alert text only.
- **Purity**: **cleanest of all** — docstring says "Pure — no market data"; `position`, `rolls`, `today` are parameters; only impurity is the `date.today()` default `:226` (already injectable — tests inject).
- **Snapshotted**: **yes** — reads the persisted, derived `state.roll_ledger.rolls` (each `{ticker, date, reason, net}`) filtered by `reason=="defend"` and the position's `entry_date`.
- **Callers**: `alerts.check_whipsaw_exit` `alerts.py:298-319`; `enrich_position` `:326`; surfaced on `executor.defend_recommendation` `:2277-2290` (the defend ticket itself says "exit instead").

### 1.1.4 Delta coverage floor — `alerts.check_delta_uncovered` `alerts.py:163-217`

- **Output**: CONDITION. Two sub-checks: weakest LEAP leg delta < `LEAP_DELTA_FLOOR`=0.50 (`:195`; `config.py:405-407`, HARD_CFM_RULE) and short total delta > long total delta (`:208`). Prose action.
- **Purity**: **impure** — recomputes greeks live each run: `data_handler.get_daily` via `_last_close` `:168`, `dividends.q_with_source` `:174` (provider), `indicators.call_greeks` per leg `:186,204`.
- **Snapshotted**: no. Delta values are recomputed; `entry_context` deliberately excludes `leap_delta` from tracked fields (`entry_context.py:31-33`). `delta_history` (persisted, nightly-appended) feeds velocity, not this floor.

### 1.1.5 Defend / roll-down trigger + regime × posture strike table

- **Trigger (condition)**: `alerts.check_defend_position` `alerts.py:243-295` — two-gate: last daily close below short strike AND live price below strike (`:258,263,268`). Emits `DEFEND_POSITION` with a suggested roll-down strike from `strike_policy.suggest_strike` `:274`. **Impure**: `data_handler.live_price` `:263` (live quote), `get_daily`+`atr` `:265`, `screening.regime()` `:273`.
- **Nearest thing to a DECISION in the codebase**: `executor.defend_recommendation(ticker)` `executor.py:2219-2302` returns `recommended_strike` (from `strike_policy`), `recommended_dte` (same week if it has time, else 5 `:2264`), `new_premium_per_share`, `new_extrinsic_per_share`, `net_total`, `cost_basis_effect`, embedded whipsaw status. Still **not a ticket**: `"source": "estimate"` `:2301`, no legs/limit/min-credit; the staged roll re-prices from the live chain. **Impure**: `log.load_state` `:2231`, `get_daily`/`live_price` `:2235,2246`, `screening.regime` `:2257`, BS pricing `:2268`.
- **Strike table SSOT**: `strike_policy.py` — `suggest_strike(price, atr_value, regime_status, posture=None)` `:47-52` is **pure over its args**; the impurity is `get_posture()` (reads state.json `:23`) when `posture` is omitted via `table_entry` `:41`. `set_posture` writes state `:32-34`. `STRIKE_TABLE` `config.py:329-333` (HARD_CFM_RULE header `:317-328`). All strike surfaces already route through it: entry chain (`option_chain.py:554`), roll picker (`:229,268`), defend engine, roll suggestion, `DEFEND_POSITION` alert — the single-source requirement is already satisfied server-side.
- **Known divergence (documented, deliberately unresolved)**: `config.py:337-352` — the live `STRIKE_TABLE` scheme differs from the documented canonical `STRIKE_ATR_MULT_GREEN`=1.5 / `STRIKE_ATR_MULT_YELLOW`=2.0 (both HARD_CFM_RULE); reconciling is a scoped follow-up. The recommendation engine must consume `STRIKE_TABLE` (the live SSOT) and will inherit this divergence — it must not resolve it.

### 1.1.6 75–80% roll guideline + roll-out default

- `alerts.check_buyback_75` `alerts.py:220-240`: decayed ≥ `BUYBACK_DECAY_PCT`(0.75) with DTE > `BUYBACK_MIN_DTE`(2) (`config.py:400-403`, HARD_CFM_RULE). CONDITION. There is **no separate 80% constant** — the band collapses to the single 0.75.
- `position_manager.enrich_short` sets `roll_now` `:162-163` (same rule; drives the badge + alert).
- **Roll-out default**: `option_chain.roll_options(ticker)` `option_chain.py:193-322` — current short = nearest-dated open leg `:237`; candidate expirations to `ROLL_MAX_DTE`=45 `:275`, weekly boundaries `:281`; default strike = policy suggestion `:285`; earnings-week override to `suggest_earnings_strike` `:297`. CONDITION/data-prep. The **UI consumes the server-flagged suggestion** (`RollModal.jsx:44-46`) — it does not compute its own default strike (good; see §1.5 for what it does compute).

### 1.1.7 Entry gate verdicts (Levels 1–5)

- **L1–L4**: `screening.entry_gate(ticker)` `screening.py:367-457`. Aggregation is **contiguous-prefix / stop-on-first-fail** (`cleared_level` = consecutive passes from L1, `:449-454`), verdict binary `READY TO ENTER`/`WAIT` `:455`. Note: true **worst-signal-wins** aggregation lives in the *scorecard* verdict (`metrics/scorecard.py::compute_verdict` — AVOID beats CAUTION beats GO), which is what `/api/scan/ready` gates on. The prompt's "worst-signal-wins" maps to the scorecard verdict, not `entry_gate`; Phase 2's `GATE_ALL_PASS`/blocked verdict must name which aggregation is authoritative (proposal: scorecard verdict for stock merit + L1 regime hard-block + L5 blocking set, matching what `executor.execute` actually enforces today).
- **Output**: CONDITION (eligibility). No proposed ticket — no strike/expiry/contracts.
- **Purity**: `screening.py` is **deeply impure**: provider reads throughout (`data_handler.prefetch/get_many/get_daily/latest_quote` `:53,176-193,234-239,333-334,411-412`), `datetime.now` `:85,203`, module-level TTL memo caches keyed on `time.time()` `:22-25,142-161`, detached scan threads mutating module state `:80-113`. `entry_gate` takes only `ticker`.
- **L5**: `account_gate.evaluate(...)` `account_gate.py:203-352`. Worst-signal on the blocking subset (any blocking failure fails; `pass = not blocking_failures` `:344`). Enforced server-side in `executor._enforce_account_gate` `executor.py:488-512`; blocking failure → HTTP 400 unless typed `override_reason`, logged on the immutable execution `:1160-1165`.
- **L5 purity — one outright violation**: `resolve_operating_cash` `:123-147` performs a **live Schwab cash call** (`:141`) and **writes state** (`log.save_state(state)` `:146`) inside gate evaluation. Also `log.load_state()` fallback `:219`, `earnings.next_earnings` `:319`, `dividends.next_dividend` `:333` (providers).
- **Snapshotted**: **yes, at entry** — the full L1–L4 + L5 detail plus override is frozen in `entry_context.gates` (`entry_context.py:207-219,314-339`), which **reuses `payload["_account_gate"]`, never re-evaluates** (`:314-330`). This is the exact snapshot pattern §2.1 asks to generalize.

### 1.1.8 Earnings-window warnings — `earnings.py`

- `next_earnings` `:196-219` (override → day-cache → AV+Schwab cross-check with `conflict` flag), `cached_earnings` `:180-193` (never fetches). Output `{date, days_until, warning, stale, conflict, ...}`; `warning` = 0 ≤ days_until ≤ `EARNINGS_WARN_DAYS`(7) `:150`. CONDITION.
- **Impure**: `date.today()` `:36`, providers `:77,102`, file cache `:46-59`, `log.load_state` in override `:65`, `time.time()` `:137,214`.
- Consumers: L5 `earnings_in_cycle`, `alerts.check_earnings_window` `:349`, `check_earnings_date_stale` `:373`.

### 1.1.9 Dividend / assignment-risk escalation — `dividends.py` + `alerts.check_assignment_risk`

- `dividends.py` is **data-only** (yield q with provenance `q_with_source` `:119-129`; next ex-date/amount `next_dividend` `:205-232`); no verdict. Impure (providers, file caches, `time.time`, state reads for overrides).
- The escalation logic is `alerts.check_assignment_risk` `alerts.py:421-503`: base trigger = ITM short extrinsic < `ASSIGNMENT_EXTRINSIC_FLOOR`(0.10); escalation = extrinsic < upcoming dividend before ex-div; q-aware BS extrinsic fallback when no quote (`_model_extrinsic_for_assignment` `:395-418`). CONDITION. Impure: `datetime.now(ET).date()` `:429`, `get_daily` `:432,407`, provider q `:415`.
- **Snapshotted**: the *dividend event* is snapshotted per position (`position.dividend`, synced nightly); the extrinsic evaluation is live.

### 1.1.10 Anti-zombie window slide + juice adequacy on open positions

- **Anti-zombie**: `burn._effective_exit_dte` `burn.py:72-85` (held past `PLANNED_EXIT_DTE`=135 → slide the exit window forward `EXTENSION_STEP_WEEKS`, flag `extended`); applied in `burn_projection` `:144-147`; extension-cost ladder in `leap_policy.leap_health` `:156-162`. Pure math over args; `leap_health` around it is impure (`get_daily`, `datetime.now` via `_leap_dte` `:63-66`).
- **Juice adequacy**: `leap_policy.leap_health` `:171-183` (trailing weekly juice % vs `account_gate.weekly_yield_target_pct`) → `alerts.check_juice_inadequate` `:683-718`; the extreme case is `check_capital_burn` `:646-680`. CONDITIONs.
- `leap_policy.roll_policy(leap_dte, extrinsic_weeks_remaining)` `:32-47` is **already pure** ("pure, no market data").

### 1.1.11 Alert engine — full enumeration and classification

`ALERT_TYPES` registry `alerts.py:33-64` — 30 types. Every alert carries a `message`
(condition), a **free-text** `action` string, structured `data`, and for some a
**deep-link `action_url`** (`:86-101`) that pre-stages the UI (roll modal / focus /
payouts tab). **No alert carries a structured executable ticket.** Classification:

| Type (severity, provenance) | Evaluator | Carries proposed action? |
|---|---|---|
| KILL_SWITCH_SECTOR (CRIT, HARD) | `check_kill_switch` `:146` | prose "exit immediately" + focus link |
| KILL_SWITCH_SPY (CRIT, HARD) | same | prose "exit 1–2 days" + focus link |
| CIRCUIT_BREAKER (CRIT, HARD) | `check_circuit_breaker` `:322` | prose `suggested_action` `:339` |
| DELTA_UNCOVERED (HIGH, HARD) | `check_delta_uncovered` `:163` | prose ("roll down/out or exit") |
| DEFEND_POSITION (HIGH, HARD) | `check_defend_position` `:243` | **numeric**: suggested roll-down strike from `strike_policy` + roll deep-link |
| WHIPSAW_EXIT (CRIT, HARD) | `check_whipsaw_exit` `:298` | prose "EXIT, not another defend" |
| ASSIGNMENT_RISK (HIGH, HARD) | `check_assignment_risk` `:421` | prose + roll deep-link |
| BUYBACK_75 (MED, HARD) | `check_buyback_75` `:220` | roll deep-link with `reason=75%-rule` |
| EARNINGS_WINDOW (MED, HARD) | `check_earnings_window` `:344` | prose + roll deep-link (earnings strike) |
| EXPIRY_FRIDAY (MED, HARD) | `check_expiry_friday` `:550` | roll deep-link |
| JUICE_INADEQUATE (MED, HARD) | `check_juice_inadequate` `:683` | prose reassess/redeploy |
| LEAP_ROLL_DUE (HIGH, PROP) | `check_leap_roll_due` `:616` | **numeric**: est. net debit `:636-642` |
| CAPITAL_BURN (HIGH, PROP) | `check_capital_burn` `:646` | prose |
| DELTA_VELOCITY (MED, PROP) | `check_delta_velocity` `:721` | prose |
| EARNINGS_DATE_STALE (MED, PROP) | `check_earnings_date_stale` `:361` | prose (data hygiene) |
| TOKEN_EXPIRY (HIGH, PROP) | `check_token_expiry` `:566` | prose (ops) |
| DATA_STALE (MED, PROP) | `check_data_stale` `:583` | prose (ops) |
| BOOK_CORRELATION (MED, PROP) | `check_book_correlation` `:811` | prose |
| SHORT_STOCK_DETECTED (CRIT, HARD) | `check_short_stock_detected` `:767` | prose |
| RECONCILE_DIRTY (HIGH, HARD) | `check_reconcile_dirty` `:788` | prose |
| RECONCILE_STALE (MED, PROP) | `check_reconcile_stale` `:865` | prose (ops) |
| ROLL_LEG_IMBALANCE (CRIT, HARD) | `check_roll_leg_imbalance` `:843` | prose (frozen, review) |
| ORDER_FILLED_DURING_CANCEL (CRIT, HARD) | raised from executor `:981` | prose |
| ORDER_PARTIAL_FILL_CANCELED (CRIT, HARD) | raised from executor `:995` | prose |
| ORDER_STATE_UNKNOWN (CRIT, HARD) | raised from executor `:1046` | prose |
| ORDER_RESUBMIT_EXHAUSTED (HIGH, PROP) | raised from executor | prose |
| SNAPSHOT_DATA_QUALITY (LOW, PROP) | one-off `record_event` from `executor.py:1102` | none |
| REGIME_CHANGE (MED, HARD) | `check_regime_change` `:905` | prose |
| PAYOUT_READY (MED, PROP) | `check_payout_ready` `:939` | payouts-tab link |
| EXTRINSIC_ABOVE_ENTRY (LOW, PROP) | `check_extrinsic_above_entry` `:506` | prose |

**Verdict: all 30 are condition-only.** Five roll-type alerts (`_ROLL_ACTIONS`
`:71-77`) come closest to actions via deep-links that pre-stage the roll modal with
the policy strike, but the "ticket" is still assembled in the UI at tap time.

**Alert storage is the wrong substrate for recommendations**: records are
**mutable** (active-set entries have `last_seen`/`data` mutated in place
`:1086-1089`, `acknowledge` flips a flag `:1121-1136`), deduped by fingerprint,
auto-resolved when the condition clears `:1097-1108`, and the log is **capped** at
`ALERT_LOG_MAX`=500 (`:1046,1110`). Recommendations require a new append-only
immutable collection.

**Scheduler**: in-process daemon thread, 30 s tick (`alert_scheduler.py:33,248-250`);
ET slots = anchors 08:30/10:00/12:30/15:30/**16:15** + post-open gap slots
09:40/09:50 (`config.py:438-483`); each slot fires once/day, weekdays only,
**holidays not modelled** (`_market_hours` `:76-82`). Evaluators run guarded (one
failure never sinks the pass `:1014-1020`). This is the natural host for the
scheduled recommendation evaluation pass.

### 1.1.12 "No action" representability — **not representable today**

- Green verdicts (`kill_switch`/`circuit_breaker` "Hold —..."`, `whipsaw tripped:False`, `roll_due:False`) are transient computed values, never persisted.
- The alert engine persists only *fired* conditions. The closest artifacts are `alerts.last_run` `{at, evaluated, fired, resolved}` `:1111-1113` (a pass ran; not per-position) and auto-`resolved` records (only for conditions that previously fired).
- There is **no first-class per-position "evaluated, all clear" record** — the §2.1 `NO_ACTION`/`ALL_CLEAR` requirement is entirely new structure.

---

## 1.2 Execution / order lifecycle inventory

### 1.2.1 The order state machine

Single source of truth: `order_lifecycle.py` — **fully pure, no I/O** (`:1-26`).
States (`:29-39`): `SUBMITTED, WORKING, CANCEL_REQUESTED, PENDING_CANCEL, FILLED,
CANCELED, REJECTED, EXPIRED, FILLED_DURING_CANCEL, PARTIAL_FILL_CANCELED,
LOCKED_UNKNOWN`. Legal transitions (declared graph `:14-24`):

```
SUBMITTED → WORKING → { FILLED
                      | CANCEL_REQUESTED → PENDING_CANCEL → { CANCELED
                                                            | FILLED_DURING_CANCEL
                                                            | PARTIAL_FILL_CANCELED }
                      | REJECTED | EXPIRED }
plus LOCKED_UNKNOWN — non-terminal hard lock, operator-cleared only
```

Sets (`:42-53`): `TERMINAL` = {FILLED, CANCELED, REJECTED, EXPIRED,
FILLED_DURING_CANCEL, PARTIAL_FILL_CANCELED}; `RESUBMIT_OK_STATES` = {CANCELED,
REJECTED, EXPIRED, FILLED}; `REVIEW_BLOCKING` = {FILLED_DURING_CANCEL,
PARTIAL_FILL_CANCELED, LOCKED_UNKNOWN}. Mapping fn `map_broker_status(raw,
filled_qty, ordered_qty, cancel_requested)` `:60-96`; gate `check_resubmit(lock,
max_attempts)` `:99-124`.

Transitions are *emitted* in `executor.py`: placement `_record_placement` `:159-177`
(SUBMITTED→WORKING); poll settle `order_status` `:798-832`; cancel path `:906`
(CANCEL_REQUESTED), `:981` (FILLED_DURING_CANCEL), `:995` (PARTIAL_FILL_CANCELED),
`:1003` (clean terminal), `:1046` (LOCKED_UNKNOWN). The short-roll has a parallel
lifecycle (`_roll_order_status` `:752-795`: whole-unit partials booked
incrementally, leg-imbalance freeze `:1588-1616`, rejection fallback `:726-749`).

### 1.2.2 The seven hard cancel-and-retry rules (locations)

1. **Broker-first cancel** — local pending record cleared only once confirmed gone at broker (`executor.py:876-880`).
2. **2xx ack not trusted; confirm a terminal state** — `CANCEL_REQUESTED` → bounded DELETE retries → `_confirm_cancel` polls to a deadline (`:885-886, 931-954`; `CANCEL_POLL_INTERVAL_SEC`=0.4 / `CANCEL_POLL_MAX_ATTEMPTS`=6 `config.py:585-586`).
3. **Partial fill / leg imbalance → freeze, never book a one-legged fill** (`:959, 989-996, 1592`; `ROLL_LEG_IMBALANCE_ACTION="freeze"` HARD_CFM_RULE `config.py:539-542`).
4. **The app flags; it never auto-fixes** (`:914, 993, 1013`).
5. **No resubmit before terminal; hard-lock on unknown** — `NO_RESUBMIT_BEFORE_TERMINAL=True` HARD_CFM_RULE `config.py:603-608`, `MAX_RESUBMIT_ATTEMPTS`=3 `:588-593`; enforced by `_guard_resubmit` `executor.py:129-156` at the head of every live placer; `ResubmitLockedError` → HTTP 409.
6. **Startup reconciliation before new activity** — `reconcile_pending_orders_on_startup` `:1054-1082`, wired at boot `app.py:1320-1323`; unreachable order → hard lock.
7. **Every transition is an append-only event; current state is a pure replay** — `append_order_event` `logging_handler.py:321-338`; derivation `recompute_derived` `:776-793`.

Numbering caveat: `AUDIT_ATOMIC_ROLLS.md` uses a *separate* R1–R6 sequence for the
roll (net-mid reference, per-leg allocation, partials, leg-imbalance, net-slippage,
rejection-fallback) — "R5" in `fill_verify.py:140`/`slippage.py` means net-slippage,
not resubmit.

### 1.2.3 Slippage assumptions and the assumed fill price

- Config: `ASSUMED_SLIPPAGE_PCT`=0.05 PROPOSED_DEFAULT `config.py:498-500`; `SLIPPAGE_MIN_FILLS`=5 `:501-503`. Below 5 live fills, the assumption + mid-fill caveat applies; past it, measured supersedes (`slippage.py:117-151`).
- **Recorded per execution**: `fill_assumption` ("mid" paper / "broker" live) and `quoted_mid_per_share` = the placement limit (`executor.py:561-572`; live single-leg recovered at commit `:693`). Rolls record the **net** reference: `roll_reference_net_mid` (mid(new)−mid(old)) + realized `roll_net_fill` on both legs (`:1665-1685`).
- Math: signed per-leg adverse-% of mid by side (`slippage.py:24-25,47-71`); one net crossing per `roll_group_id` (`:74-105`). Only `live_transmitted is True` fills count.
- **What does NOT exist: a per-ticket max-slippage bound.** The limit price *is* the net mid (`ROLL_NET_PRICE_SOURCE="reference_net_mid"` HARD_CFM_RULE `config.py:525-528`), `REPRICE_ON_RETRY="none"` (never chases). There is no "minimum acceptable net credit" or "max slippage vs mid" field on any ticket — a limit order enforces the bound implicitly (fill ≥ limit for credit), but the *assumed* bound the net-juice math used is not stamped as a ticket-level constraint the fidelity grader can grade against. `SLIPPAGE_IN_BOUND` (§2.4) needs this field added to the proposed ticket (Phase 2, on the *new* recommendation records — not by mutating pending-order handling).

### 1.2.4 Post-fill verification — exists vs missing

**Exists:**
- `fill_verify.py` (read-only; `POST /api/verify-fills`): broker status == FILLED `:100-103`; per-leg fill price matches recorded to the cent (`PRICE_TOLERANCE`=0.01 `:26`, `:114-131`); positions reconcile (`reconcile.run_reconciliation(persist=False)` `:190-197`); informational roll net drift `:137-163`. Skipped-broker is flagged, never a vacuous pass `:184-207`. Input: `order_receipts` (order id ↔ committed execution ids, cap 200, fills only, `logging_handler.py:288-297`).
- `reconcile.py`: broker-vs-state **positions only** — `MATCH / MISSING_AT_BROKER / UNEXPECTED_AT_BROKER / QUANTITY_MISMATCH / SHORT_STOCK_DETECTED / EXPIRED_WORTHLESS_PENDING` (`:29-37`; core pure fn `:324-397`); freezes (`needs_review`), never auto-corrects; failed fetch → failure report, never an empty-account illusion `:483-496`. Runs pre-market / nightly / on-demand.
- **Fill-during-cancel race**: handled — `FILLED_DURING_CANCEL` + CRITICAL alert (`executor.py:899-901, 974-987`).

**Does NOT exist (explicit):**
- **No standalone confirmed-dead check after cancel.** `_confirm_cancel`'s window can close non-terminal → returns `{"status": "pending_cancel"}` and keeps the pending record (`executor.py:952-954`), relying on a later `order_status` poll or the next startup reconcile. Nothing *guarantees* a later confirmation during the same session → `CANCEL_CONFIRMED_DEAD` (§2.4) has real work to do.
- **No partial-fill orphan detection on the atomic-open expiry path.** Verified: `order_status`'s `CANCELED/REJECTED/EXPIRED` branch (`executor.py:826-831`) pops and settles with **no `_order_filled_qty` check** — a two-leg entry that partially fills then EXPIRES at day-end (reached via poll, not via `cancel_order`) settles EXPIRED with no imbalance freeze. The cancel path catches it (`_finalize_cancel_terminal` `:989-996`); the poll path does not. (Matches `AUDIT_ENTRY_CANCEL.md` F-2 — Phase 2 of that work closed the cancel-path half only.) The roll path detects imbalance on both paths.
- **No post-fill buying-power / cash diff.** `schwab_api._account_cash` `:425-439` is read only by the account gate for sizing; nothing compares expected vs actual cash/buying-power around a fill. `RECONCILED_CLEAN`'s buying-power leg is `NOT_YET_IMPLEMENTED` territory per §2.4.

### 1.2.5 Execution records in `state.json`

Single writer: `logging_handler.append_execution` `:250-268` (assigns `id`, `date`,
backfills `live_transmitted`; appends; `recompute_derived`; `save_state`). **No
update/delete of an execution exists anywhere in `backend/`** — corrections are
forward-only `adjustment` executions (`executor.py:388-416`;
`docs/reconciliation.md:104-128`).

Fields — common: `id, date, live_transmitted, ticker, action, mode, price_source,
fill_assumption, quoted_mid_per_share`. Per action: `buy_leap` adds `strike,
contracts, execution_price, execution_total, extrinsic_captured, stock_price,
expiration, leap_add?, override?, circuit_breaker_price, entry_context` (frozen);
`sell_short`: `strike, contracts, premium_per_share, premium_total, stock_price,
entry_extrinsic_per_share`; `close_short`: `+ close_price_per_share, close_total,
extrinsic_sold, extrinsic_paid_back, net_juice, net_juice_total`; `close_leap`:
`+ close_price, close_total, cost_basis, realized_pnl, extrinsic_remaining,
exit_reason (coded), exit_note, exit_metrics, legs_remaining`; atomic-open legs:
`open_id, open_leg`; roll legs: `roll_leg, roll_id, roll_group_id, roll_reason
(scheduled | 75%-rule | defend | earnings | kill-switch-exit), roll_alloc_method,
roll_reference_net_mid, roll_net_fill`; LEAP-roll legs: `leap_roll_id`;
`adjustment`: `instrument, instrument_type, strike, quantity_delta, price, reason
(typed, required), linked_diff_id, mode`.

Paper/live flagging is triple-redundant: `mode`, `live_transmitted` (True/False/
None-legacy), `fill_assumption` — the §2.4 "paper tickets, flagged as paper"
requirement is already satisfiable from existing fields.

### 1.2.6 Order events — the replayable journal (and its caps)

`state.order_events` (schema v16) — each event: `order_id, ticker, action, intent,
prior_state, new_state, raw_status, attempt/seq, at`; **append-only, capped at
1000** (`logging_handler.py:341`); `recompute_derived` replays last-event-wins into
derived `state.order_state` `:776-793`. `order_receipts` (cap 200, fills only) is
the durable fill trail. **Implication for §2.4:** fidelity records must be derived
and *persisted* (append-only) close to lifecycle-terminal time; a grader that only
ever replays `order_events` from scratch would silently lose graded history once
the 1000-event window rolls. The caps themselves must not be changed (existing
collections untouched); the fidelity derivation just cannot assume infinite replay.

### 1.2.7 Ticket data structure

Live JSON built in `schwab_api.py`: single-leg `build_single_leg_order` `:460-475`;
multi-leg `build_net_order(legs, net_price, complex_strategy_type, duration)`
`:478-505` — `orderType` = `NET_CREDIT` if net ≥ 0 else `NET_DEBIT` (sign-decided
`:492-494`), `price` = abs(net), legs = `{instruction, quantity,
instrument:{symbol: OCC 21-char, assetType: OPTION}}`; short-roll
`build_roll_order` `:508-540` (BUY_TO_CLOSE + SELL_TO_OPEN). Pending record
(`state.pending_orders[order_id]`): `kind, payload, ticker, action, contracts,
stock_price, price_source, account_hash, leg symbols, net_limit/limit_price,
placed_at`. Entry uses `ENTRY_COMPLEX_STRATEGY_TYPE`/`ENTRY_ORDER_DURATION`
(`CUSTOM`/`DAY`). **No min-acceptable-credit and no max-slippage field** (§1.2.3).

### 1.2.8 Purity notes on the executor

`order_lifecycle.py`, `slippage.py`, and `reconcile.reconcile` (with injected
`today`) are pure. Impurity concentrates in `executor.py`: broker client fetched
directly in every placer/poller (`:629, 806, 896, 1457, 1804, 1999, 2177`);
`data_handler.latest_quote` at execute time `:233`; `log.utcnow()` timestamps
throughout; and **`_confirm_cancel` reads `time.monotonic()` + `time.sleep()`
directly** (`:939-942`) — the one bounded-retry window that is not fully mockable
(tests neutralize via `CANCEL_POLL_INTERVAL_SEC=0` only). Fidelity fixtures (§2.7
cases b/c) will need lifecycle *event* fixtures rather than driving
`_confirm_cancel` in real time — which the append-only `order_events` design
already supports.

---

## 1.3 Gap analysis per decision type

What §2.1's `Recommendation` record needs vs what exists. Common to ALL types:
(a) no `rec_id`/emission record of any kind; (b) no `proposed_ticket` structure
(legs/limit/min-credit/max-slippage) anywhere pre-execution; (c) no `valid_until`
semantics (alert auto-resolve is condition-driven, not validity-driven); (d) no
`input_snapshot` at evaluation time (the pattern exists but only fires at
*execution* time via `entry_context`); (e) no supersession concept.

| Decision type | Condition source today | Missing for a full Recommendation | Purity refactor needed |
|---|---|---|---|
| **ENTER** | scorecard verdict + `entry_gate` L1–4 + `account_gate` L5 (`/api/scan/ready` composes them) | Ticket: LEAP strike/expiry from `LEAP_TARGET_DELTA`/`LEAP_TARGET_DTE` + short strike from `strike_policy` + NET_DEBIT limit + contracts sizing — all pieces exist (`account_gate._leap_strike_for_delta`, `option_chain.option_chain`, `executor._place_live_open`) but are never assembled pre-operator. Trigger `GATE_ALL_PASS` needs the authoritative aggregation named (§1.1.7) | `screening.py` (providers, clock, TTL caches, threads), `account_gate.resolve_operating_cash` (live cash + **state write**), earnings/dividends provider reads |
| **ROLL_OUT** | `BUYBACK_75` alert + `EXPIRY_FRIDAY` + `roll_options` data-prep | Concrete ticket: both legs + NET limit + min-acceptable-credit (field doesn't exist). `roll_options` already computes strike/expiration candidates + suggestion — closest to done | `option_chain.roll_options` reads chain provider + `get_posture()` state read; clock for DTE |
| **ROLL_DOWN / DEFEND** | `DEFEND_POSITION` alert + `executor.defend_recommendation` (strike + DTE + est. net) | Ticket-ification of `defend_recommendation` (legs/limit/min-credit); **definition needed**: today's `roll_reason` enum has a single `defend` — the prompt distinguishes ROLL_DOWN vs DEFEND; matching cannot distinguish them from executions without a definition (proposal: treat them as one action type `DEFEND` with the strike-delta recorded, or extend `roll_reason` — flagged for decision) | `defend_recommendation`: `load_state`, `get_daily`, `live_price`, `regime()`; `check_defend_position`: same + live quote |
| **EXIT** | kill switch / circuit breaker / whipsaw / delta coverage / juice — five advisory evaluators with coded exit reasons already mapped | Ticket: `_build_exit_legs` `executor.py:1938-1940` already builds the two-leg exit; needs pre-operator assembly + limit/min-credit. Trigger enum largely maps 1:1 onto `exit_reasons` codes | `kill_switch.evaluate` (providers, no injected snapshot), `circuit_breaker.evaluate` (df default), `check_delta_uncovered` (greeks live), whipsaw (`date.today` default only) |
| **NO_ACTION / ALL_CLEAR** | **not representable** (§1.1.12) | Entirely new: explicit per-position per-pass record | n/a — new pure function output |

**Action-type coverage holes to decide in Phase 2 scope:** real operator actions
that fit *no* proposed `action_type`: LEAP roll (`roll_leap`, mechanical, exit code
`LEAP_ROLL`), scale-in `buy_leap` (leap_add), leg-repair `sell_short`/`close_short`
singles, `adjustment` executions (reconciliation-driven), and payout finalization.
Unless explicitly scoped, each would synthesize a spurious `COVERAGE_MISS`.
Proposal: matching applies to {ENTER, ROLL_OUT, DEFEND(≡ROLL_DOWN), EXIT}; LEAP
rolls, adjustments, and payout ops are excluded from coverage by rule and listed as
such in the scoreboard doc.

**Trigger-rule enum vs audit findings:** the proposed set is sound; the audit
suggests these refinements: `CIRCUIT_BREAKER` should carry the tripped-condition id
in the snapshot (4 distinct coded conditions exist: `CB_DRAWDOWN_15`,
`CB_MA50_3CLOSE`, `CB_MA200_CLOSE`, `CB_MANUAL_LINE`) or be split to mirror
`exit_reasons`; `DIVIDEND_ASSIGNMENT_RISK` should be understood as the
*extrinsic-floor* trigger with dividend escalation (that is how
`check_assignment_risk` actually works); `ROLL_SCHEDULED_WEEKLY` maps to today's
`EXPIRY_FRIDAY` + `roll_reason="scheduled"`; `JUICE_HURDLE_FAIL` maps to
`JUICE_INADEQUATE`/`CAPITAL_BURN` (two distinct conditions — consider two codes);
`DTE_PLANNED_EXIT` has no alert today (planned-exit window lives in
`burn`/`leap_policy`) — it is a new trigger, cheap to add from `planned_exit_dte`.

---

## 1.4 Schema impact

- **Current version: 16** (`migrations.py:20`); v13 added `entry_context` + coded exit reasons, v16 added `order_events` + `order_locks`.
- **Required migration**: one new `_v16_to_v17` adding empty `recommendations: []`, `recommendation_resolutions: []` (derived — see note), `order_fidelity: []` and registering in `MIGRATIONS` (`:238-254`). Migrations are add-only, idempotent (setdefault-style), version bumped per step, unknown-gap-safe; post-migration `load_state` recomputes + persists once (`logging_handler.py:135-139`).
- **Pre-migration snapshot: exists and is tested.** `migrate()` calls `backups.snapshot_before_migration(state_path, from_v, to_v, state=...)` *before* the loop and raises `MigrationAbortedError` if the snapshot cannot be written — on-disk file left at the original version (`migrations.py:271-281`). Naming: `backups/pre-migration-v{from}-to-v{to}-{YYYYMMDD-HHMMSS}.json`; copies on-disk bytes under lock; **exempt from the 30-file rotation** (`backups.py:114,134-151`). Covered by `test_durability.py:157-186`.
- **Derivation home**: `recompute_derived` (`logging_handler.py:446-794`) runs after every `append_execution`/`append_order_event`, on migrated load, and at every executor commit — exactly the hook §2.2/§2.3 need. Resolutions + scoreboard become new derived keys; `recommendations` and `order_fidelity` are appended raw records (recommendations immutable-once-written like executions; fidelity persisted at terminal-transition time because `order_events` caps at 1000 — §1.2.6).
- **A design distinction to settle in Phase 2**: the prompt calls `recommendation_resolutions` a collection while also requiring "matching runs inside `recompute_derived()` ... pure derivations". If resolutions are *fully* derivable (EXECUTED_MATCHED / EXPIRED / COVERAGE_MISS are), they should be a derived key like `cycles`; the one member that is raw record, not derivation, is **OVERRIDDEN** (an operator tap with a coded reason — analogous to `exit_reason` landing on an execution). Proposal: overrides are appended as small immutable *override records*; `recommendation_resolutions` is then 100% derived from executions + recommendations + override records. This keeps the no-hand-entered-scores invariant airtight.

---

## 1.5 Risks and conflicts with the same-code-path invariant

**Backend:**
1. **`account_gate.resolve_operating_cash` writes state and calls Schwab inside gate evaluation** (`account_gate.py:141-146`) — direct violation of the pure-evaluation requirement; must be lifted to the caller/snapshot layer in Phase 2.
2. **Entry-gate aggregation ambiguity** (§1.1.7): "worst-signal-wins" is the scorecard verdict; `entry_gate` is stop-on-first-fail; the executor enforces L5 + regime-RED only. The recommendation engine must codify one authoritative ENTER verdict or the trust evidence will grade against a rule the operator wasn't actually shown.
3. **`STRIKE_TABLE` vs documented canonical multiples divergence** (`config.py:337-352`) — deliberate, scoped elsewhere. The engine must consume `STRIKE_TABLE` as-is; reconciling it inside this work would violate "do not modify any HARD_CFM_RULE constant".
4. **Alert storage is mutable/capped/auto-resolving** — cannot host recommendation records (§1.1.11); also `valid_until` semantics differ from condition-cleared auto-resolve and the two must not be conflated when both fire on the same underlying condition.
5. **`DEFEND` vs `ROLL_DOWN` indistinguishable in executions** — single `roll_reason="defend"`; matching needs a definition (§1.3).
6. **Timeliness measurement is cadence-bounded**: slot-based scheduler (5 anchors + gap slots), kill-switch RS refresh ≤ 3×/day, holidays absent from `_market_hours` — "condition first true in the data" must be defined against the evaluation cadence actually available, or every recommendation will look late by construction.
7. **`_confirm_cancel` uses a real clock** (`executor.py:939-942`) — fidelity tests must drive `order_events` fixtures rather than the live cancel loop.
8. **`order_events` cap 1000 / `order_receipts` cap 200** — fidelity derivation must persist verdicts near terminal time, not re-replay history forever (§1.2.6).
9. **No ticket-level min-credit / max-slippage field exists** — `SLIPPAGE_IN_BOUND` needs the bound recorded on the *recommendation's* proposed ticket, and the grader compares the realized fill (`quoted_mid_per_share`/`roll_net_fill` already recorded) against it.
10. **Atomic-open expiry partial gap** (`executor.py:826-831`) — `NO_ORPHAN_LEG` can and should *detect* this passively from `order_events` + fill quantities; fixing the lifecycle behavior itself is out of scope per §2.8 ("no behavioral change to order placement").

**Frontend (UI/backend disagreement candidates):**
11. **`tradeMode.jsx describeOrder` (`:58-87`) self-declared duplicate of `executor._limit_price`** — the live-confirm dialog recomputes the limit client-side; a backend change diverges silently. Recommendation cards must render `proposed_ticket` fields verbatim, never recompute.
12. **Roll modal computes displayed net credit locally** (`RollModal.jsx:86-88`: `mark×100×qty` arithmetic) — strike/expiry defaults are server-driven (good: server-flagged `suggested` from `/api/roll-options`), but the credit the operator sees is client math with no backend validation against a proposed minimum.
13. **75% threshold re-encoded in 5 frontend places** (`JuiceStand.jsx:135`; `PositionTracker.jsx:326,394,410` + tooltip `:338`) rather than server flags; `DTE≤2` similarly (`PositionTracker.jsx:358`, `Overview.jsx:58`, `JuiceStand.jsx:604`, `ProcessRibbon.jsx:419`).
14. **`buildActionItems` (`Overview.jsx:31-81`) classifies severities client-side**; `canAdd` AND-gate duplicated in `PortfolioRisk.jsx:71-74` and `PositionTracker.jsx:532-538`; client-invented constants with no backend SSOT (0.5 lean band, IV-rank 50/25, coverage `||3` fallback, `net >= -1` epsilon).
15. Alert deep-links (`?action=roll&...`) open a **pre-staged modal whose ticket is assembled at tap time** — under the trust layer, the tap must route through the recommendation's frozen `proposed_ticket` instead, or UI-time re-pricing will register as strike/credit deltas that the engine never proposed.

**Test-plan risk:** the **"XLK July 6th snapshot" fixture does not exist in the
repo** (searched: no XLK-dated fixture; `fixtures/regime/` holds three synthetic
SPY shapes). Phase 2 must source it — either from the operator's live parquet
cache/state or as a constructed labeled fixture reviewed by the operator — before
the regression-lock test can be honest.

---

## Proposals requested by the prompt

- **Scoreboard placement (§2.3)**: Settings tab, as a full panel alongside Data
  Health (it is diagnostics, read at review time, and Settings already hosts the
  alert engine controls) — with the two *loud* items (coverage misses, fidelity
  failures) also surfaced as counts in the Overview action-items digest and as
  CRITICAL/HIGH alerts through the existing engine. Open-recommendation counts go
  in the Overview digest per §2.6.
- **Recommendation evaluation host**: a new pass inside the existing
  `alert_scheduler` slot cadence (same slots, incl. 16:15 post-close for the
  confirmed-close kill switch), taking one frozen market snapshot per pass and
  calling the pure engine — mirroring how `alerts.run()` is hosted today.
- **Resolution storage**: overrides as immutable operator records; everything else
  derived (§1.4).

---

**HARD STOP.** This is the end of Phase 1. No code has been written and no existing
file modified. Phase 2 (schema v17, pure evaluation module, matching/scoreboard
derivations, fidelity ledger, UI, tests) awaits explicit approval.
