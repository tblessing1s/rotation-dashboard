# AUDIT.md — Weekly Theta Burn & Net Juice Accounting (Phase 0)

**Task:** replace total-extrinsic juice accounting with model-based theta burn over the
*held* window, make **net juice/week** the headline, and rank the entry queue on it.
**Scope of this document:** map every touched surface with `file:line` references and
flag every place the codebase contradicts the implementation prompt, *before* any code
is written.

Every reference is `backend/<file>:line` unless a `frontend/` prefix is shown. This
audit was produced by reading the code, not from memory.

---

## 0. Executive summary — what's really there

- **The wrong accounting the task targets is real and central.** The LEAP's *total
  entry extrinsic* is explicitly modeled as a cost to be paid off by short-call juice,
  cycle-scoped, in `logging_handler.recompute_derived` (§1, `logging_handler.py:399-434`).
  The comment at `:426-427` states it outright: "the net juice is only 'real' income
  once the LEAP extrinsic is paid off." A second notion — *remaining live* extrinsic as
  a runway denominator — lives in `leap_policy.py:124-125`. Both denominate against
  weekly juice; neither keys off a planned exit. **CONFIRMED.**
- **The BS engine is complete and is the only pricer.** `indicators.py` prices calls at
  arbitrary `(T, σ)` via `_bs_call_price` and exposes theta via `call_greeks_full`
  (§2). It takes **DTE in days**, converts `T = dte/365` at the call site, and has **no
  clock abstraction** — time always arrives as caller-supplied DTE. `burn_projection()`
  builds directly on `_bs_call_price`; no second pricer is needed or wanted.
- **`planned_exit_dte` does NOT exist** in the schema (§3). It is a genuine prerequisite
  — a v14 migration is required. **CONFIRMED as absent.**
- **An existing burn already exists but is the WRONG method.** `indicators.leap_weekly_burn`
  (`indicators.py:324`) returns **`−θ_day × 7`** — a *single-point local* theta
  approximation. The spec's HARD_CFM_RULE requires the **two-point model difference**
  `extrinsic(current_dte) − extrinsic(planned_exit_dte)`. These are different numbers.
  `leap_health` already surfaces `net_weekly_maintenance = trailing_juice − leap_weekly_burn`
  (`leap_policy.py:129`) — the right *shape* (juice − burn) with the wrong *burn* and no
  slippage. The new work supersedes the burn term, not the shape. **FLAG.**
- **There is no weekly job.** The only recurring hook is a **nightly** maintenance run
  (`maintenance.nightly_refresh`, gated by `maintenance_due()` in `alert_scheduler.py:143`).
  The weekly mark job must be created and hooked into that same single-writer tick with a
  weekly cadence gate (§5). **FLAG (must create).**
- **Ranking is gross today.** Both the ready-shortlist (`app.py:187`) and the ranked
  queue (`queue_state.py:69`) sort descending on `juice_weekly_pct`, produced from
  `account_gate.juice_estimate` as **gross** `extr_w / leap_cost` (`account_gate.py:93`)
  — no burn subtracted. Switching to net is a single shared-function change feeding both
  sites (§6). **CONFIRMED.**
- **No chart library** (`frontend/package.json` — only react/react-dom). Charts are
  hand-rolled: flex-div bars (`HistoryTab.jsx:104-139`, already a weekly-juice bar chart)
  and inline SVG (`JuiceStand.jsx`). The weekly juice-vs-burn view clones the flex-div
  pattern (§7). **CONFIRMED — do not add a library.**

Nothing in the prompt was found to be factually wrong about the strategy; the two items
that need a design decision from you before I build are marked **DECISION** in §8.

---

## 1. Current juice/week calculation & the total-extrinsic hurdle

### 1a. Juice is computed in four places, four denominators

| Purpose | Location | Formula / denominator |
|---|---|---|
| Realized per-close (source number) | `executor.py:1011-1019` | `net_juice = extrinsic_sold − extrinsic_paid_back`; `×contracts×100`. No time denominator. |
| **Trailing avg weekly juice** (operational per-week) | `logging_handler.py:618-624` | `sum(net_juice per week) / len(weeks)`, capped at `config.JUICE_TRAILING_WEEKS`. Denominator = **completed ISO weeks**. |
| History reporting avg | `history.py:23` | `gross_juice / max(days_held/7, 1)` per closed cycle, averaged. |
| **Screening / entry estimate** (ranking source) | `account_gate.py:52-96` | weekly short extrinsic priced at `T=5/365`, `weekly_yield_pct = extr_w / leap_cost` (`:93`). **Gross.** |

`trailing_avg_weekly_juice` (stamped per position at `logging_handler.py:623`) is the
figure `leap_health` and the frontend treat as "juice/wk". It is the correct **juice**
input to `net_juice_per_week`; only the **burn** term is being replaced.

### 1b. Total extrinsic as a payback target / income hurdle — CONFIRMED

`recompute_derived` builds a per-ticker **payback meter** — `logging_handler.py:399-424`:
```python
at_entry  = float(cycle_target.get(ticker, leap.get("extrinsic_at_entry") or 0))
collected = float(cycle_collected.get(ticker, 0.0))
remaining = max(at_entry - collected, 0.0)
payback[ticker] = { "leap_extrinsic_at_entry": ..., "collected_to_date": ...,
                    "remaining_to_payback": round(remaining, 2),
                    "pct_complete": round(collected / at_entry * 100, 1) if at_entry else 0 }
```
and a **book-wide income hurdle** — `logging_handler.py:426-433`:
```python
state["theta_ledger"]["extrinsic_summary"] = {
    ..., "net_income": round(agg_collected - agg_at_entry, 2),
    "income_positive": agg_at_entry > 0 and agg_remaining <= 0 }
```
Consumed downstream as a hurdle/countdown:
- `option_chain.py:624-645` — `weeks_to_income_positive = ceil(extrinsic_to_cover / weekly_juice)`,
  where `extrinsic_to_cover = remaining_to_payback` for a held LEAP (`:628`).
- `leap_policy.py:124-125` — `leap_extrinsic_weeks_remaining = extrinsic_remaining / trailing_juice`.
- `alerts.py:517-583` — `check_capital_burn` / `check_juice_inadequate` fire off these.

This is precisely the "total extrinsic ≈ 3× the true cost" error the spec describes:
the whole entry extrinsic is treated as a cost, when only the extrinsic consumed
195→~135 DTE is truly spent (the rest is recovered on the LEAP sale, minus slippage).

**Scope note (see §8 DECISION-1):** `extrinsic_payback` is derived from immutable
executions and is asserted by the test suite (`test_cfm.py:518,523,758`). It will **not**
be ripped out — it stays as a secondary "capital recovery" view. The *headline* and the
*income-positive framing* move to net-juice/coverage; the two coexist.

---

## 2. Black-Scholes engine — public interface

**Module: `indicators.py` (~lines 221-397).** Pure `math` BSM with continuous dividend
yield `q`. Thin re-export wrapper in `portfolio_risk.py:25-38`.

**Pricers (T in YEARS, σ as a decimal):**
- `_bs_call_price(S, K, T, r, sigma, q=0.0) -> float` — `indicators.py:241`. **The primitive
  `burn_projection()` builds on.**
- `_bs_put_price(...)` — `indicators.py:271` (put-IV substitution path).
- `_d1(...)` — `:229`; `_norm_cdf/_norm_pdf` — `:221/:225`.

**Public greeks / IV:**
- `bs_call_delta(S,K,T,r,sigma,q)` — `:233`.
- `implied_vol_call(price,S,K,T,r,q)` — `:247` (bisection, robust deep-ITM).
- `implied_vol_put(price,S,K,T,r,q)` — `:277` (recovers skew-aware σ for an ITM call
  from its same-strike OTM put).
- `call_greeks_full(S,K,T,r,sigma,q) -> (delta, theta_per_calendar_day, vega)` — `:302`.
- `call_greeks(S,K,dte,mark,reported_iv=None,r,q) -> (delta, iv_pct)` — `:344`. **Takes
  DTE in days**, `T=(dte)/365` at `:356`.
- `leap_weekly_burn(S,K,dte,mark_ps,contracts,q) -> $/week` — `:324`. **`−θ_day×7×contracts×100`.
  Single-point local theta — NOT the two-point method the spec mandates.** (FLAG.)

**Extrinsic decomposition:**
- `calculate_extrinsic(bid,ask,strike,underlying) = mid − max(S−K,0)`, clamped ≥0 — `:375`.
  ⚠️ This is **market** extrinsic (from bid/ask). `burn_projection()` must use **model**
  extrinsic = `_bs_call_price(...) − max(S−K,0)` so both DTE points share one σ and spot.
- `_augment(contract, underlying)` attaches `mark/intrinsic/extrinsic` — `:389`.
- Live LEAP intrinsic/extrinsic split (bid-based) — `leap_policy.py:115-123`, with a
  `below_intrinsic` flag + floor-at-0 (the `low_extrinsic_flag` precedent).

**Put-IV substitution path (ITM calls) — `option_chain.py:330-365` (`_augment_call_greeks`):**
IV precedence for an ITM call: same-strike **put's reported IV** (`schwab_api.parse_put_iv`,
`schwab_api.py:583`) → else **σ implied from the put's mark** via `implied_vol_put`
(`option_chain.py:355`) → else the call's own `volatility`. Dividend `q` from
`dividends.yield_for(ticker)`. This is the σ that must feed `burn_projection()` — the
mark job resolves it here and passes the plain value in (keeping the pure function I/O-free).

**Clock:** none inside the engine. DTE is `daysToExpiration` from Schwab
(`schwab_api.py:570`). `burn_projection()` therefore takes explicit `current_dte` /
`planned_exit_dte` ints; the `clock` arg in the spec signature is only needed at the
mark-job boundary to derive `current_dte` from the stored `expiration`. (Minor deviation
noted in §8.)

---

## 3. Position storage schema

Positions are a plain list under `state["positions"]` (`logging_handler.py:48`), appended
by `executor._ensure_position` (`executor.py:105-123`). Position shell — `executor.py:109-121`:
`ticker, sector, entry_date, status, leap, leap_legs, shares, short_calls, kill_switch,
thesis, delta_history`. Migrations add `circuit_breaker/dividend` (v3), `needs_review/review`
(v7), `entry_context` (v13).

**LEAP legs** (multi-tranche since v10; `leap` mirrors `leap_legs[0]`, re-aliased each
load at `logging_handler.py:74-86`). A leg — `executor.py:888-895`:
`strike, contracts, cost_basis, current_bid, intrinsic, extrinsic, entry_date,
dte (entry snapshot, default LEAP_TARGET_DTE=180), expiration, extrinsic_at_entry,
extrinsic_collected_to_date`.

Field mapping the spec asks for:
- Entry date → position `entry_date` (`executor.py:900`) + leg `entry_date` (`:892`).
- LEAP entry price → leg `cost_basis` (`:889`); strike → leg `strike`; expiration → leg
  `expiration` (`:893`).
- Live DTE → derived position-level `leap_dte` = `expiration − today`, fallback to entry
  snapshot, at `logging_handler.py:607-617`.

**`planned_exit_dte` / planned hold length — ABSENT.** A full search
(`planned|exit_dte|target_dte|hold_length|holding_period|hold_weeks`) found only
`config.LEAP_TARGET_DTE=180` (a strategy-wide *entry* target, not a per-position exit
plan) and prose comments (`account_gate.py:299`). **Prerequisite confirmed — v14 migration
required (§4).**

---

## 4. Migrations & schema version

All in `migrations.py`. `CURRENT_VERSION = 13` (`:20`). Pattern: `_vN_to_vN+1(state)->state`,
additive only, registered in `MIGRATIONS` (`:199-212`); `migrate()` snapshots-then-walks,
bumping `schema_version` per step (`:242-249`); invoked in `load_state`
(`logging_handler.py:121-125`). New files stamp at `CURRENT_VERSION` (`logging_handler.py:41`).

**Planned migration — `_v13_to_v14` (bump to 14):** for each position (and each
`leap_leg`, if we key exit off the primary leg — primary leg is sufficient),
`p.setdefault("planned_exit_dte", config.PLANNED_EXIT_DTE)`. Additive, seeds the default
onto existing positions — the exact shape of `_v12_to_v13` (`migrations.py:186-196`).
Old-state fixture must load clean (test §9 in the plan).

---

## 5. Weekly marks / snapshots / derived-data convention

**No weekly / end-of-week / roll-cadence scheduler exists.** The only recurring job is
**nightly** `maintenance.nightly_refresh` (`maintenance.py:83`), gated once-per-day by
`maintenance_due()` (`alert_scheduler.py:143-146`) and driven from the single in-process
daemon tick (`alert_scheduler.py:160-166`). It already appends per-position **`delta_history`**
snapshots (`maintenance.py:36-64`) and per-ticker IV points — the precedent for a
periodic per-position mark. Single-writer constraint: state.json is on one Fly volume /
one machine, so the weekly mark job **hooks into this tick with a weekly gate**, it does
not spawn a second scheduler.

**Two persistence conventions (hard split):**
1. **Derived-from-executions → inside state.json, rebuilt by `recompute_derived` every
   write** (`logging_handler.py:311-625`; called at `:253` and on load `:125`):
   `theta_ledger`, `extrinsic_payback`, `roll_ledger`, `cycles`, per-position `leap_dte`
   / `trailing_avg_weekly_juice`. Migrations only seed empty shells (`migrations.py:63-75`).
2. **Market telemetry / not-recomputable → separate files under `config.DATA_DIR`:**
   `iv_history.json` ("market data, not a trading record, so it stays out" —
   `iv_history.py:14,27`), `dividends_cache.json`, `data_budget.json`, parquet OHLCV
   cache. The offline calibration harness (`calibration.py`) is read-only telemetry that
   writes only a markdown report.

The nightly `delta_history` series (inside state.json, appended, not recomputable) is the
lone in-state per-position snapshot precedent.

**→ See §8 DECISION-2 for where the weekly burn marks should live.**

---

## 6. Scorecard / entry-queue juice consumption

**Two ranking sites, both descending on `juice_weekly_pct`:**
1. Ready shortlist — `app.py:187`: `ready.sort(key=lambda r: r.get("juice_weekly_pct") or 0, reverse=True)`.
2. Ranked queue — `queue_state.py:69`: `go.sort(key=lambda r: (r.get("juice_weekly_pct") is None, -(r.get("juice_weekly_pct") or 0.0)))`;
   assigns 1-based `rank` (`:78`), which `market_scheduler.py:145` then inherits.

**Field source:** `metrics/scorecard.py:411` — `row["juice_weekly_pct"] = est["weekly_yield_pct"]`,
where `est = account_gate.juice_estimate(ticker, df)` (`:407`). Formula = **gross**
`extr_w / leap_cost * 100` (`account_gate.py:93`); the LEAP weekly burn is **not**
subtracted. Same gross figure drives the Level-5 juice-adequacy gate (`account_gate.py:272-284`).

**Switch to net (single shared function, two call sites — spec §6):** add a **net** weekly
figure computed once as a pure function using a *hypothetical entry* (LEAP at
`LEAP_ENTRY_DTE_DEFAULT`, exit at `PLANNED_EXIT_DTE`) — net = gross juice − model
burn/week (with slippage). Call it from `juice_estimate`/`scorecard.py` (queue) **and**
from the position view so both produce identical net values for identical inputs
(single-source-of-truth test §8 in the plan). Sites to move together so the gate never
disagrees with the ranking: `account_gate.py:93` (add net field), `metrics/scorecard.py:407-414`,
`app.py:187`, `queue_state.py:69`, and the gate at `account_gate.py:272-284`. Display
consumers that then show net automatically: `Scorecard.jsx:24-34`, `ReadyToEnter.jsx:59,63`.

---

## 7. Frontend surfaces

**No chart library** (`frontend/package.json` deps = react/react-dom only). Idioms:
- **Flex-div bars** — `HistoryTab.jsx:104-139` (`WeeklyJuiceChart`, already "weekly net
  juice vs target"): maps `weeks[]` to `flex-1` divs, height `Math.abs(net)/max*100`,
  color per bar. **Clone this for the weekly juice-vs-burn view** (two series: full-opacity
  realized weeks, lighter projected weeks).
- **Inline SVG** — `JuiceStand.jsx:130-210/225-304` ("Pure SVG — no chart lib").
- Bar primitives: `Meter` (`ui.jsx:97-104`), `SqueezeBar` (`JuiceStand.jsx:336-347`).

**Per-position juice today:** `JuiceStand.jsx:507-508` reads
`lh.trailing_avg_weekly_juice` / `lh.leap_weekly_burn`, renders `juice …/wk · burn …/wk`
(`:575-579`). `PositionTracker.jsx` `LeapHealth` strip renders juice-yield % vs target
(`:129-136`) and `net_weekly_maintenance` `+…/wk` (`:144`). **The new three-metric panel
(Juice/wk · Burn/wk · Net/wk) + coverage meter slots alongside these**, matching the
`Card`/`Stat` primitives (`ui.jsx:50-75`); grid `sm:grid-cols-3` (cf. `Overview.jsx:136`).

**Portfolio rollup:** Overview KPI band reads server `theta.totals`
(`Overview.jsx:214,234-244`) — "Net juice · week" = `money(juice.this_week)`. The rollup
must sum **net** juice (spec §6): add an aggregate net figure server-side and point this
card at it.

**Staleness badge:** canonical `StaleBadge` (`ui.jsx:32-43`, amber pill, renders `null`
when fresh). Consumers: `ReadyToEnter.jsx:74-79`, `Scorecard.jsx` refresh amber-vs-emerald
(`:90-119`), earnings `:19-33`. Backend age flows via `data_cache.stale_blocks_go`
(`app.py:177-182`). **Burn figures get a `StaleBadge` when the inputs (spot/IV `fetched_at`)
are stale.**

---

## 8. Contradictions & decisions to confirm before building

**FLAG-1 — an existing `leap_weekly_burn` will be superseded, not reused.** `indicators.py:324`
returns `−θ×7` (single-point). It is consumed by `leap_health` (`leap_policy.py:126`),
`JuiceStand.jsx:508`, `PositionTracker.jsx`. I will add `burn_projection()` (two-point
model diff) and route `leap_health` / the frontend through it. `leap_weekly_burn` stays
in the module (other call sites / tests) but is no longer the headline burn. Nothing
contradicts the spec here — just noting the overlap so I don't fork two "burn" concepts.

**FLAG-2 — two legacy "extrinsic-as-cost" notions remain (payback meter + remaining
runway).** Both are execution-derived and test-asserted. I keep them intact (capital-recovery
view) and add the corrected net-juice/coverage as the headline. Removing them would break
`test_cfm.py` (spec: all 330+ must pass).

**DECISION-1 — legacy income-hurdle framing.** The prompt says "net juice is the headline"
and "portfolio rollup sums net juice, not gross." I will (a) add net-juice everywhere as
primary, and (b) keep `extrinsic_payback` / `theta_ledger.extrinsic_summary` as a
secondary "capital returned on LEAP exit" readout rather than deleting them (they're
derived + tested). **Confirm** you're happy with coexistence rather than a hard removal of
the old hurdle.

**DECISION-2 — where weekly burn marks live.** Marks capture live spot/IV at a point in
time, so they are **not** recomputable from executions — they're telemetry. Two precedents:
`delta_history` (in-state, per-position, appended nightly) vs `iv_history.json` (separate
`DATA_DIR` file, "market data stays out of state.json"). The divergence-tracking purpose
(a live BS-engine verification harness) is squarely calibration/telemetry territory. **My
recommendation: a separate `DATA_DIR/burn_marks.json` store mirroring `iv_history.py`** —
keeps the append-only execution record clean (spec: "do not pollute…"), matches the
market-telemetry convention, and decouples weekly telemetry appends from the
recompute+backup-snapshot cost of a state.json write. The `planned_exit_dte` field itself
still goes in state.json (it *is* position state, not telemetry) via the v14 migration.
**Confirm** DATA_DIR store vs a `delta_history`-style in-state series.

**Minor — `burn_projection(clock)` signature.** The pure math needs only explicit DTE
ints (no clock). I'll keep a `clock`-shaped boundary at the mark job (to derive
`current_dte` from `expiration`) and can accept an optional `clock` on the function for
signature fidelity; it won't be consulted when `current_dte` is passed. No behavioral
impact.

**No external Python deps required** — everything builds on `indicators.py` + stdlib
`math`. Frontend adds no library.

---

## 9. Implementation order (unchanged from the plan, now grounded)

1. Config block (`config.py`, after the CFM-mechanics section, provenance-tagged).
2. Pure functions: `burn_projection()`, `extension_cost()`, `net_juice_per_week()` /
   `coverage_ratio()` — new module (e.g. `burn.py`) + tests, all built on `_bs_call_price`.
3. v14 migration (`planned_exit_dte`).
4. Weekly mark job + realized/projected series + divergence (hooked into the nightly tick
   with a weekly gate; storage per DECISION-2).
5. Queue/scorecard → net-juice via the shared function (`account_gate.py`,
   `metrics/scorecard.py`, `app.py:187`, `queue_state.py:69`).
6. Frontend: three-metric panel + coverage meter + weekly juice-vs-burn bars +
   hold-extension readout + staleness badge (all reusing existing primitives).
7. Full offline test pass (existing 330+ green + new cases from the plan's list).

**Stopping here per the spec ("Stop and present the audit before implementing").** Awaiting
confirmation on DECISION-1 and DECISION-2 before writing code.
