# Phase 0 Audit — Entry-Context Snapshots + Coded Exit Reasons

All references are `backend/<file>:<line>` against the tree at branch
`claude/entry-context-exit-reasons-f1tn3h`.

## Key finding: the skeleton already exists

`recompute_derived` already **reads** two not-yet-populated fields when it
derives the closed-cycle records:

- `logging_handler.py:533` — `"exit_reason": e.get("exit_reason") or "discretionary"`
- `logging_handler.py:534` — `"entry_snapshot": entry.get("entry_snapshot")`

and the executor already writes primitive versions of both:

- `executor.py:613` `_entry_snapshot(ticker)` → one scorecard row, stored on the
  buy_leap execution at `executor.py:703`.
- `executor.py:52` `EXIT_REASONS` — a **free-text** set
  (`"target hit"`, `"kill switch"`, …), validated at `executor.py:773`.

This task **upgrades that skeleton**: a rich `entry_context` (superset of the
scorecard row) and a **coded** exit-reason enum. The architecture — snapshot &
reason live on the immutable executions, cycles are derived — is already the
right one and satisfies R2/Q5 for free.

---

## 1. Position & cycle schema; where a cycle is created/closed

**Position** (`_default_state` at `logging_handler.py:39`; built by executor
`apply()` closures): keys observed —
`ticker`, `status` (`active`/`closed`), `entry_date`, `leap` (alias of
`leap_legs[0]`), `leap_legs[]` (each: `strike, contracts, cost_basis,
current_bid, intrinsic, extrinsic, entry_date, dte, expiration,
extrinsic_at_entry, extrinsic_collected_to_date`), `short_calls[]`, `shares`,
`circuit_breaker` (`price, source, set_at, entry_price`), `dividend`,
`needs_review`, `review`, `delta_history[]`, plus derived `leap_dte`,
`trailing_avg_weekly_juice`. **No `entry_context` today.**

**Cycle** (built at `logging_handler.py:515-536`): `id, ticker, entry_date,
exit_date, days_held, capital_deployed, gross_juice, roll_count, roll_net,
roll_drag, leap_pnl, net_result, net_return_pct, target_range_pct, target_met,
exit_reason, entry_snapshot, wash_sale`.

Cycles are **fully derived** — `recompute_derived` rebuilds the entire `cycles`
list from executions on every write (`logging_handler.py:484-537`). A cycle is
"created" when a `buy_leap` opens `open_cycle[ticker]`
(`logging_handler.py:489`) and "closed"/emitted when a `close_leap` fires
(`logging_handler.py:502`, `del open_cycle[t]` at `:537`). **There is no
separate persisted cycle store to mutate** — the copy in R3 must be a *derived*
copy from the executions, keyed off the buy_leap (`entry_context`) and close_leap
(`exit_reason`) records.

> Quirk (pre-existing, must not change per "Do NOT change cycle logic"): a LEAP
> roll (`_commit_leap_roll`, `executor.py:1391`) logs `close_leap`+`buy_leap`
> sharing a `leap_roll_id`. The cycle loop closes the cycle on *any* close_leap
> (`:502` does not test `leap_roll_id`), so a LEAP roll ends one derived cycle
> and starts another. Documented, left intact.

## 2. Entry flow — what is available at execution time (R4 classification)

Opening path: `execute` (`executor.py:125`) → `_enforce_account_gate`
(`:346`, stashes the full Level-5 result on `payload["_account_gate"]` at
`:359`) → `_buy_leap` (`:640`) or `_open_position_atomic`→`_commit_open`
(`:1036`, which calls `_buy_leap` then `_sell_short`).

At the moment the opening execution is appended, these are **in memory or one
cached read away** (→ record the value):

| Field group | Source | Cost |
|---|---|---|
| Scorecard scalars + verdict | `scorecard([ticker])` (`metrics/scorecard.py:466`, `score_ticker` `:363`) | cached bars |
| Entry-gate L1–L4 detail (regime, sector, stock, consolidation) | `screening.entry_gate(ticker)` (`screening.py:357`) — regime/sectors memoized (`:169/:220`) | cached/memoized |
| Regime status + SPY breadth + VIX | gate L1 `detail` = `regime()` (`screening.py:200`) | memoized |
| Sector ETF, RS3M, breadth, ATR direction | gate L2 `detail` = `sectors()[etf]` (`screening.py:238`) | memoized |
| Stock RS3M vs SPY/Sector, ATR%, consolidation, price | gate L3/L4 `detail` (`screening.py:297`) | cached bars |
| ATR value, RSI, %>MA21 | `indicators.atr/rsi` (`indicators.py:71/55`), scorecard row | cached bars |
| IV rank + percentile | `iv_history.iv_rank(ticker)` (`iv_history.py:92`) — local JSON | no network |
| Account-gate per-check detail + override | `payload["_account_gate"]` (`:359`) already computed | free |
| Execution intent (posture, strike, expiry, LEAP strike/DTE) | `payload` | free |
| Data staleness (provider + fetched_at) | `data_cache.symbol_staleness(ticker)` (`data_cache.py:142`) | in-process |

**Would require a NEW provider call → record `null` + `missing_reason`:** a live
option-chain greek for the LEAP *entry delta* (not in state; the payload carries
target delta at most), and any datum whose `data_cache` record is **stale beyond
its tier max-age** (`data_cache.get_with_staleness` `:78`) → nulled with
`missing_reason: "stale"`. Capture is wrapped section-by-section in try/except and
uses read-only/memoized reads, so it never triggers a fresh fetch and never
blocks (SNAPSHOT_NEVER_BLOCKS_EXECUTION).

## 3. Exit paths — what closes a position, what each writes today

**No exit is automated.** `kill_switch.evaluate` (`kill_switch.py:45`) and
`circuit_breaker.evaluate` (`circuit_breaker.py:39`) are **advisory** — they
return `status/alert/suggested_action`/tripped-condition ids and never close.
Every close is operator-driven through `executor.execute`:

| Close path | Code | Writes today |
|---|---|---|
| `close_leap` (single-leg) | `_close_leap` `executor.py:745` | `exit_reason` from payload, validated vs `EXIT_REASONS` (`:773`) |
| `close_position_atomic` (normal full exit) | `_build_exit_legs` `:1192` → `_commit_exit` `:1229` | `exit_reason` from payload → LEAP close (`:1204`) |
| LEAP roll close (not a real exit) | `_commit_leap_roll` `:1391` | hard-codes `exit_reason:"discretionary"` (`:1402`), carries `leap_roll_id` |

The trigger→code mapping (set at the point the trigger fires, surfaced by the
advisory evaluators / alert engine `alerts.py:34-54`):

| Trigger (real code path) | Coded exit reason |
|---|---|
| `kill_switch`: `rs3m_vs_sector < 0` (`kill_switch.py:50`) | `KILL_SWITCH_SECTOR` |
| `kill_switch`: `rs3m_vs_spy < 0` (`kill_switch.py:54`) | `KILL_SWITCH_SPY` |
| `circuit_breaker` `drawdown` (`circuit_breaker.py:56`) | `CB_DRAWDOWN_15` |
| `circuit_breaker` `ma_fast` (`:66`) | `CB_MA50_3CLOSE` |
| `circuit_breaker` `ma_slow` (`:77`) | `CB_MA200_CLOSE` |
| `circuit_breaker` `manual_line` (`:86`) | `CB_MANUAL_LINE` |
| `alerts.check_whipsaw_exit` / `position_manager.whipsaw_status` (`alerts.py:258`) | `WHIPSAW_BREAKER` |
| `alerts` `DELTA_UNCOVERED` (`alerts.py:37`) | `DELTA_COVERAGE` |
| `alerts` `EARNINGS_WINDOW` (`alerts.py:43`) | `EARNINGS_WINDOW` |
| reconciliation freeze `RECONCILE_DIRTY` (`alerts.py:53`) | `RECONCILIATION` |
| cycle return ≥ target | `TARGET_REACHED` |
| operator manual close (typed note required) | `OPERATOR_DISCRETION` |
| migration backfill only, never at close time | `LEGACY_UNRECORDED` |

**Final enum** = the 13 members above (`CB_MANUAL_LINE` added over the prompt's
list because the circuit breaker genuinely has a 4th operator-line condition).

## 4. Calibration expectations

`calibration.py` today replays the scorecard over cached OHLCV and pairs it with
forward returns (`collect_rows` `:46`); it **never reads cycles or entry
context**. R6 adds a loader yielding `(entry_context, exit_reason,
cycle_outcome_metrics)` per closed cycle, skipping `entry_context is None`
(legacy) cycles with a skip count.

## 5. recompute_derived boundary (R2/Q5)

`recompute_derived` (`logging_handler.py:311`) rebuilds `theta_ledger`,
`extrinsic_payback`, `roll_ledger`, `cycles`, and a few per-position derived
scalars (`leap_dte`, `trailing_avg_weekly_juice`, `extrinsic_collected_to_date`
at `:585-608`). It **never** writes `entry_context`/`exit_reason` onto positions
or executions — those are raw record. Guarantee: (a) source of truth is the
**immutable execution** (`entry_context` on the buy_leap, `exit_reason`/
`exit_note`/exit-metrics on the close_leap) which recompute only *reads*; (b)
the position's `entry_context` copy is written once in the `apply()` closure and
recompute's position loop touches only the named derived scalars — an explicit
test asserts a full rebuild leaves every snapshot byte-identical (R2).

## 6. Migration pattern

`migrations.py`: `CURRENT_VERSION` (`:20`), a numbered `_vN_to_vN+1(state)` that
only **adds** structure (never rewrites executions), registered in `MIGRATIONS`
(`:171`); `migrate` (`:185`) snapshots the pre-migration file to `backups/` and
**aborts** (`MigrationAbortedError`) if the snapshot can't be written; `load_state`
recomputes derived after any migration (`logging_handler.py:122-126`). New:
`_v12_to_v13` sets `position.setdefault("entry_context", None)`; legacy cycles
get `exit_reason:"LEGACY_UNRECORDED"` from the *derivation* rule (a close_leap
with no coded reason), triggered by the post-migration recompute. No fabricated
backfill (R5).

---

## Implementation plan

1. **config.py** — `SNAPSHOT_SCHEMA_VERSION=1`, `SNAPSHOT_NEVER_BLOCKS_EXECUTION=True`,
   `SNAPSHOT_NULL_FIELD_ALERT_FRACTION=0.25`, provenance-tagged.
2. **exit_reasons.py** (new) — `ExitReason` coded constants, `ALL`, `AUTOMATED`,
   `NOTE_REQUIRED={OPERATOR_DISCRETION}`, `LEGACY_UNRECORDED`, `normalize()`,
   `requires_note()`.
3. **entry_context.py** (new) — `capture(ticker, payload, account_gate, now=None)`:
   section-by-section, network-free, best-effort, null-with-`missing_reason`,
   `null_field_fraction`, `SNAPSHOT_SCHEMA_VERSION`.
4. **executor.py** — `_buy_leap`/`_commit_open` write `entry_context` on the
   execution **and** the position; fire the >25%-null low-severity alert.
   `_close_leap` + `execute()` validate the coded reason and enforce the typed
   note for `OPERATOR_DISCRETION`; store `exit_reason`+`exit_note`+exit metrics.
5. **logging_handler.py** — cycle derivation: coded `exit_reason`
   (LEGACY_UNRECORDED fallback), `exit_note`, `exit_metrics`, `entry_context`,
   compact `entry_summary`. Immutability preserved.
6. **kill_switch.py / circuit_breaker.py** — `exit_reason_code(evaluation)`
   mappers so the trigger names its own code.
7. **migrations.py** — `_v12_to_v13`, bump `CURRENT_VERSION=13`.
8. **calibration.py** — `load_closed_cycles(state)` → `(tuples, skipped)`.
9. **history.py / app.py** — CSV summary columns + `/api/history` detail exposes
   full `entry_context`.
10. **frontend** — History tab shows exit reason + entry summary chips.
11. **Tests** — offline: completeness, immutability, one-per-exit-path, missing
    data, migration, calibration loader, export.
12. **VERSION + changelog note.**

---

## Changelog (v2.2.0 — state schema v13)

**Entry-context snapshots + coded exit reasons.** From this version forward,
**every closed cycle is calibration-usable**: the opening `buy_leap` freezes an
immutable `entry_context` snapshot (scorecard verdict + all metrics, regime,
sector, stock, IV rank, entry-gate L1–4 + account-gate detail, execution intent,
and per-field data-quality/staleness) onto the execution and the position, and
every close records a **coded** `exit_reason` (`exit_reasons.ExitReason`) plus an
optional typed `exit_note` and exit-time counterpart metrics.

- Snapshot capture never blocks or delays a trade and never makes a network
  call; unavailable/stale fields are recorded `null` with a `missing_reason`,
  and a >25%-null entry fires a LOW-severity `SNAPSHOT_DATA_QUALITY` alert.
- `OPERATOR_DISCRETION` requires a typed note; automated triggers
  (kill switch, circuit breaker, whipsaw, delta, earnings, reconciliation,
  target) each map to their own code via `exit_reason_code()`.
- New consumers: `calibration.load_closed_cycles()` yields
  `(entry_context, exit_reason, outcome)` tuples (legacy cycles skipped +
  counted); the juice-journal CSV gains exit-reason/note + a compact entry
  summary; the full snapshot is exposed per cycle via `/api/history`.

**Permanent boundary:** cycles closed **before** v2.2.0 carry
`entry_context: null` and `exit_reason: "LEGACY_UNRECORDED"` (migration
`_v12_to_v13`). They are **not** backfilled — reconstructing entry snapshots
from cached bars would be fabricated training data, worse than missing data.
