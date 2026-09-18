# Phase 0 Audit — Store Migration (state.json → a database)

**Scope.** Whether and how to move `state.json` off a whole-file-rewrite JSON
document onto a real datastore, without breaking any of: the append-only
execution log, `recompute_derived()`'s wholesale-rebuild contract, the
per-account book layout, demo mode, the order journal (which lives OUTSIDE
state.json on purpose), or the nightly/pre-migration backup and restore path.

**This is an audit only. No code was changed.** Every claim carries a
`file:line` citation against branch `claude/lucid-cori-ysj81c` at its current
head (`669b0e4`). Where the repo doesn't settle a question it is marked
**UNKNOWN**. **Hard stop after this document — no implementation until the
owner approves.**

---

## TL;DR

1. **`state.json` is not one undifferentiated blob — it's four different
   consistency regimes wearing one file.** (a) an append-only, immutable log
   (`executions`, `recommendations`, `recommendation_overrides`,
   `coverage_miss_acks`, `order_events`) that is only ever appended to; (b)
   pure derivations rebuilt wholesale from (a) on every write
   (`theta_ledger`, `extrinsic_payback`, `roll_ledger`, `cycles`,
   `dividend_ledger`, `accrual_ledger`, `put_ledger`, `order_state`,
   `payback_reconciliation`, the trust layer) via
   `logging_handler.recompute_derived()` (`backend/logging_handler.py:945-1476`);
   (c) small keyed maps that are read-modify-written in place
   (`pending_orders`, `order_locks`, `order_submissions`, `alerts`,
   `metadata`, `payouts`, `reconciliation`, `ingestion`); and (d)
   `positions`, which is neither append-only nor purely derived — it is
   **imperatively mutated** by per-action `apply()` closures in `executor.py`
   and only *some* of its fields (`leap_dte`, `trailing_avg_weekly_juice`,
   per-leg `dte`) are touched by `recompute_derived`. A store migration that
   treats `positions` as replayable from the log will be wrong; the prior
   Phase 0 audit for the shares migration flagged this exact gap
   (`AUDIT_SHARES_PRIMARY_MIGRATION_PHASE0.md:29-37`) and it is still true today.

2. **The whole-file read-modify-write is not always funneled through
   `mutate_state`.** `logging_handler.mutate_state` (`logging_handler.py:277-294`)
   exists precisely to prevent lost updates across a slow fetch, but several
   production writers do their own `load_state()` → mutate → `save_state()`
   under a *different* lock: `alerts._run_locked` / `alerts.record_event`
   (`backend/alerts.py:1545-1573`, `1588-1622`, guarded by `alerts._run_lock`,
   `alerts.py:1542`, not `logging_handler._lock`). This is safe today only
   because `logging_handler._lock` is a plain in-process `RLock` and every
   writer eventually calls `load_state()`/`save_state()`, which both take it —
   but it means "goes through mutate" is a convention, not an enforced
   interface, and a repository seam has to decide whether to fold these into
   `mutate_state` or keep them as a second, equally-legitimate entry point.

3. **The append-only log has an overlay mechanism, not just a filter.**
   `derived_executions()` (`logging_handler.py:913-942`) drops
   `reversed_by`/`reverses_execution_id`/`excluded` executions (as expected)
   but ALSO applies `txn_correction` records as a field-level overlay onto the
   execution they correct (`_correction_overlay`, `logging_handler.py:897-910`)
   — the replacement for the old in-place History-tab edit
   (`TXN_CORRECTION_ACTION`, `logging_handler.py:894`). Any store design that
   makes `executions` a literal immutable table must reproduce this overlay at
   read time, in application code or a view — it cannot be "just" an
   append-only table with no read-side transform.

4. **The order journal is deliberately NOT in the same store as everything
   else, and that separation is the point, not an accident to fix.**
   `orders[.<account>].jsonl` (`logging_handler.py:452-544`) exists *because*
   `state.json` is a whole-file rewrite: a stale copy saved over a fresh one,
   a restore, or a repair can all lose a fill's captured price, and the
   journal survives all three because it's appended, never rewritten,
   recovered by broker order id (`logging_handler.py:456-462`). Any store
   migration must keep this file (or its per-account, external-recovery
   property) genuinely independent of the primary store's transaction
   boundary — collapsing it into the same SQLite file removes the exact
   property it exists for (a corrupt/rolled-back main store still lets you
   recover the placement price).

5. **Roughly 30 non-test backend modules call `load_state`/`save_state`/
   `mutate_state` (§1), and 9 touch `active_state_path`/`config.STATE_PATH`/
   `config.DEMO_STATE_PATH` directly (§1.4).** `executor.py` alone accounts
   for 93 of the call sites (`grep -c` count, §1.1) — it is the de facto
   writer for almost everything except alerts/payouts/webpush/reconciliation
   self-mutation. A repository-seam interface only has to cover the
   *operations* those ~30 modules perform, not each call site individually —
   and the operations are a short, enumerable list (§4).

6. **Thirteen-plus side stores already live outside `state.json` under
   `DATA_DIR`, and every one of them already states its own consistency
   contract in its module docstring** (§2). They fall into two families: (a)
   externally-recomputable telemetry that intentionally is NOT rebuilt by
   `recompute_derived` and carries zero decision authority
   (`iv_history`, `regime_history`, `scan_rejection_log`, `burn_marks`,
   `structure_labels`, `symbol_genius_history`, `scan_diff_log`,
   `data_budget`, `gate_telemetry`, `candidate_universe`,
   `csp_dry_powder_log`, `juice_capacity`) — these can stay as files
   indefinitely, a store migration owes them nothing; and (b) durable
   operational record that a store migration DOES have to reason about
   (`accounts.json`, the order journal, `schwab_token*.json`,
   `.session_secret`, `.vapid_keys.json`, `live_trading.json`, `mode.json`).

7. **Durability today is entirely file-level (atomic temp-file + `fsync` +
   `os.replace` + directory `fsync`, `logging_handler._atomic_write`,
   `logging_handler.py:185-219`) plus a single in-process `RLock`
   (`logging_handler.py:26`) — there is no cross-process lock, and the docs
   say so explicitly** (`docs/recovery.md:38-39`, `scripts/restore_state.py:14-16`).
   `fly.toml:7-10` states the single-writer/single-machine constraint in so
   many words: one Fly volume, one machine, by design. Any SQLite backend
   inherits this constraint for free (SQLite's own writer serialization
   matches it); Postgres on Fly would be the first time this app runs against
   a service that isn't pinned to the same machine as its writer, which is a
   bigger architectural change than the prompt's framing ("Postgres on Fly")
   suggests — see §6.

8. **The demo-mode / per-account matrix is multiplicative, not additive: 2
   (live×demo) × up to `MAX_ACCOUNTS=12` (`accounts.py:49`) possible state
   files, selected by two independent switches** — `config.demo_enabled()`
   (`config.py:45-54`, ONE global flag in `mode.json`, not per-account) and
   `accounts.active_id()` (`accounts.py:229-235`, per-request/per-job
   contextvar). `accounts.state_path()` (`accounts.py:287-296`) is the single
   function that resolves both into a path. A store migration must keep this
   resolution function's *signature* (account, demo) → identity, whatever the
   identity becomes (file path today, a database name/schema/keyspace
   tomorrow) — see §3.

9. **`recompute_derived` and the order journal are the two consumers whose
   invariants are hardest to keep, and for different reasons.**
   `recompute_derived` (§5.1) must run to completion and OVERWRITE every
   derived key on every state-changing write — it is not incremental, it is
   not append-only, and a database that tries to make it incremental (a
   trigger, a materialized view maintained row-by-row) will silently diverge
   from "recompute from scratch" the first time an execution is corrected,
   reversed, or migrated. The order journal (§5.2) must stay append-only,
   crash-safe, and recoverable **independently of whether `state.json`/its
   replacement is even readable** — that is its entire reason for existing.

10. **Recommendation: (a) SQLite per account on the existing volume**, not
    (b) one file with an account column or (c) Postgres. See §6 for the full
    tradeoff; in short, (b) reintroduces the exact blast-radius risk
    `accounts.py`'s own docstring says one-file-per-account was built to
    avoid (`accounts.py:1-9`: "never one file with an account column... mixing
    two books in one log would make every derived number a blend of accounts
    that no migration could unpick later"), and (c) trades a durability model
    that already matches the single-writer/single-machine constraint for one
    that doesn't, for no capability this app currently needs (there is no
    multi-machine write path today, and none is proposed).

---

## §1. Every module that reads or writes state

### 1.1 State I/O call sites (`load_state` / `save_state` / `mutate_state`)

`grep -lE '\b(load_state|save_state|mutate_state)\s*\(' backend/**/*.py` matches
**77 files total**; excluding `test_*.py` leaves **30 non-test modules**
(`backend/{account_gate,accounts,alert_scheduler,alerts,app,circuit_breaker,
csp_dry_powder,daytrade/budget,dividends,earnings,executor,fill_verify,
leap_policy,logging_handler,maintenance,metrics/scorecard,option_chain,
payouts,queue_state,recommendation_auto_execute,recommendation_runner,
reconcile,refresh_policy,screening,seed_demo_data,strike_policy,tier_poll,
transaction_ingest,webpush,weeklies}.py`). Per-file call counts (excluding
tests), for scale:

| Module | calls | Module | calls |
|---|---|---|---|
| `executor.py` | 93 | `maintenance.py` | 10 |
| `logging_handler.py` (definitions) | 34 | `payouts.py` | 9 |
| `app.py` | 29 | `webpush.py` | 6 |
| `alerts.py` | 13 | `seed_demo_data.py` | 5 |
| `recommendation_runner.py` | 11 | everything else | 1–3 each |

`executor.py` (5,696 lines) is the de facto single writer for
executions/positions/pending-orders/order-events/order-locks/order-submissions;
`app.py` (2,915 lines) is almost entirely reads for API responses plus a
handful of admin-mutation endpoints (`reset_book`, metadata patch,
`append_recommendation_override`, `append_coverage_miss_ack`).

### 1.2 Classification

**Append to an immutable record** (writer function, never a positional
update): `logging_handler.append_execution` (`:384-402`, called only from
`executor.py`), `append_recommendations` (`:628-647`, `recommendation_runner.py`
only), `append_recommendation_override` (`:650-663`, `app.py` only),
`append_coverage_miss_ack` (`:666-679`, `app.py` only), `append_order_event`
(`:598-615`, `executor.py` only). All five are one-writer-module functions —
the fan-out this migration has to preserve is narrow, not wide.

**Derived rebuild (wholesale, not incremental):** everything inside
`recompute_derived` (`logging_handler.py:945-1476`) — see §5.1. Called from
every append-writer above plus `migrate()` on schema upgrade
(`logging_handler.py:171-175`) and fresh-store initialization (`load_state`,
`:139-144`).

**In-place mutation of a mutable section**, i.e. `state[key]` read,
positionally edited, written back — NOT append-only:
- `pending_orders` — `save_pending_order`/`get_pending_order`/
  `pop_pending_order`/`list_pending_orders` (`logging_handler.py:415-449`),
  called from `executor.py` (place/poll/cancel) and read by `app.py` for
  status.
- `order_locks` — `get_order_lock`/`save_order_lock` (`:682-690`),
  `executor.py` only (the resubmission gate).
- `order_submissions` — `save_order_submission`/`update_order_submission`/
  `get_order_submission`/`list_order_submissions` (`:557-592`),
  `executor.py` only (F3 idempotency key, written BEFORE the broker call).
- `order_receipts` — `save_order_receipt` (`:422-431`), capped list,
  `executor.py` at fill time, read by `fill_verify.py`.
- `alerts` (`active`/`log`/`settings`/`push_subscriptions`) — mutated
  in-place by `alerts._run_locked`/`alerts.record_event`
  (`backend/alerts.py:1545-1622`) and `webpush.subscribe`/`unsubscribe`
  (`backend/webpush.py:147` and neighboring lines) — **not** via
  `logging_handler.mutate_state`; see TL;DR #2.
- `metadata` — patched from many sites: `app.py:2164` (operator PATCH),
  `account_gate.py:281` (operating-cash sync), `strike_policy.py:33`
  (posture stamp), `recommendation_auto_execute.py:53` and
  `circuit_breaker.py:287` (per-feature permission sub-keys), plus
  `logging_handler.save_state` itself stamping `last_updated`
  (`logging_handler.py:272`).
- `payouts.records` — `payouts.py:422,474` (mark-paid / un-mark), `app.py`
  as the HTTP entry point.
- `reconciliation` — `reconcile.py:772,794` (report write + ack), read
  everywhere a freeze check happens.
- `ingestion.proposals`/`ingestion.last` — `transaction_ingest.py:694`,
  `executor.py:1167,1226` (adopt/dismiss a proposal).

**Read-only:** the large majority of `app.py`'s 29 call sites (API GET
handlers), plus `metrics/scorecard.py`, `option_chain.py`, `leap_policy.py`,
`dividends.py`, `earnings.py`, `queue_state.py`, `screening.py`,
`refresh_policy.py`, `daytrade/budget.py`, `fill_verify.py`,
`csp_dry_powder.py`, `weeklies.py`, `tier_poll.py` — these call `load_state()`
to compute a view and never call `save_state`/`mutate_state`.

**`positions` — the one section that is neither of the above.** It is
imperatively edited by per-action closures inside `executor.py` (e.g. a
`buy_shares`/`sell_short`/`close_short` handler appends/removes a leg dict on
the position in place) and is NOT reconstructed by `recompute_derived` from
`executions` — only specific derived *fields* on each position
(`leap_dte`, `trailing_avg_weekly_juice`, per-short-call `dte`,
`extrinsic_collected_to_date` on the LEAP leg) are recomputed there
(`logging_handler.py:1390-1446`, `1114-1116`). This is the same gap the
shares-migration Phase 0 audit flagged as its single most important
architectural finding (`AUDIT_SHARES_PRIMARY_MIGRATION_PHASE0.md:29-37,
275-281`) and it is unchanged today: "replay from genesis reproduces
`positions`" is not a true statement about this codebase, only "replay from
genesis reproduces the derived ledgers" is.

### 1.3 The `mutate_state`-bypass finding (detail for TL;DR #2)

`logging_handler.mutate_state(fn)` (`:277-294`) is documented as "the only
safe way to persist a change computed across something slow" — its docstring
even names the exact bug it exists to prevent (a fill committed by the order
poll disappearing because an interval reconciliation's slow Schwab fetch
started before it and saved a stale copy after). Yet:

- `alerts._run_locked` (`alerts.py:1588-1622`, `run()` wrapper `:1576-1585`)
  and `alerts.record_event` (`:1545-1573`) each do `log.load_state()` →
  mutate `state["alerts"]` → `log.save_state(state)` directly, serialized by
  their OWN lock `alerts._run_lock = threading.Lock()` (`alerts.py:1542`),
  not `logging_handler._lock`.
- `webpush.py:147` and its neighbors (subscribe/unsubscribe) follow the same
  pattern without going through `mutate_state`.

This is not a live bug — `logging_handler._lock` is a **process-wide**
`RLock` (`logging_handler.py:26`) and both `load_state`/`save_state`
acquire it (`:136`, `:271`), so `alerts._run_lock` only adds a *second,
redundant* layer of serialization for alerts' own read-modify-write, it
doesn't create a race. But it means the invariant "every mutation goes
through one funnel" is a convention enforced by every writer separately
calling `load_state()`/`save_state()` (which happen to share a lock), not an
interface. A repository-seam `Store.mutate()` must decide whether
`alerts.py`/`webpush.py` are folded into it (changing their call sites) or
whether the seam explicitly exposes `load`/`save` as first-class operations
alongside `mutate`, matching what these callers actually do today. The
prompt's operation list already includes plain `load`/`save`, which covers
this without a rewrite.

### 1.4 State PATH access (as opposed to state I/O)

`grep -lE 'active_state_path|config\.STATE_PATH|config\.DEMO_STATE_PATH'`
matches 22 files; excluding tests leaves **9 non-test modules**:
`accounts.py` (defines `state_path`/`active_state_path`, `:287-300`),
`app.py` (multi-account admin endpoints + `/api/admin/*`), `backups.py`
(`account_for_path`, `:114-124`; nightly/pre-migration snapshot targets),
`config.py` (defines `STATE_PATH`/`DEMO_STATE_PATH`, delegates
`active_state_path()` to `accounts.py`, `:131-141`), `executor.py`
(`order_journal_path` resolution indirectly via `logging_handler`),
`logging_handler.py` (`load_state`, `_write`, `order_journal_path`,
`_store_paths` for the orphan-temp-file sweep, `:227-238`), `schwab_api.py`
(token path is account-scoped the same way, `:66-70`, NOT state path but the
same resolution pattern), `seed_demo_data.py` (seeds a specific account's
demo file), `conftest.py` (test fixture wiring). This is the count the
prompt's "~57" figure is presumably approximating when the broader
`STATE_PATH`-adjacent grep (including every test file) is taken — the
non-test surface is small and already centralized through
`accounts.state_path()`.

---

## §2. Side stores under `DATA_DIR` outside `state.json`

| Store | Path | Writer(s) | Consistency contract with `state.json` |
|---|---|---|---|
| Account registry | `accounts.json` (`accounts.py:82`) | `accounts._write_registry` (`:186-199`) | **Must move with the account concept, not with any one book.** It is the index of which state files/identities exist; a store migration that changes book identity (file → DB row) must update this registry's shape, not its meaning. |
| Order journal | `orders[.<account>].jsonl` (`logging_handler.py:463-470`) | `append_order_journal` (`:473-493`), `executor.py` | **Must survive independently of the primary store**, by design (TL;DR #4). Append-only, one line per event, recovered by broker order id regardless of what happened to state.json since. A store migration must NOT fold this into the same transactional file/db as the primary store. |
| Burn marks | `burn_marks.json` (`burn_marks.py:31`) | nightly job | Derived telemetry, recomputable, zero authority (`burn_marks.py:17-20` — mirrors `iv_history`/`scan_rejection_log`). **Can stay a file.** |
| Candidate universe | `candidate_universe.json` (`candidate_universe.py:27`) | weekly screen | Shadow artifact, single weekly writer, append-only change log (`candidate_universe.py:1-15`). **Can stay a file.** |
| Data budget | `data_budget.json` (`data_budget.py:32`) | every provider call | Per-day counters reset at the ET day boundary (`data_budget.py:1-13`). **Can stay a file** — it's not even meant to survive past a day boundary conceptually. |
| Dividends cache | `dividends_cache.json` (`dividends.py:28`) | 24h TTL fetch | Point-in-time cache, no history (`dividends.py:149-160` per the juice-capacity audit, §0.3 there). **Can stay a file.** |
| Earnings cache | `earnings_cache.json` (`earnings.py:30`) | per-ticker fetch | Same shape as dividends cache. **Can stay a file.** |
| IV history | `iv_history.json` (`iv_history.py:27`) | option-chain view + nightly | Derived, day-idempotent, capped, zero authority (`iv_history.py:1-16`). **Can stay a file.** |
| Juice capacity | `juice_capacity.json` (`juice_capacity.py:72`) | scan sweep | SHADOW, zero authority, never in the `blocks` list (`juice_capacity.py:19-27`). **Can stay a file.** |
| Live-trading toggle | `live_trading.json` (`config.py:74`) | UI toggle | Global (not per-account), overridable by `CFM_LIVE_TRADING` env (`config.py:68-98`). **Operational config, not a book — stays a file**, but note it is a GLOBAL switch across every account's book (§3 caveat). |
| Demo-mode flag | `mode.json` (`config.py:40`) | UI toggle | Global (not per-account) boolean, in-process cached (`config.py:42-54`). **Stays a file**; same global-vs-per-account caveat as above. |
| Regime history | `regime_history.json` (`regime_history.py:31`) | nightly + backfill | Derived from cached SPY bars, day-idempotent, capped (`regime_history.py:1-19`). **Can stay a file.** |
| Scan diff log | `scan_diff_log.json` (`scan_diff_log.py:25`) | nightly diff | Append-only events + one last-write-wins snapshot field (`scan_diff_log.py:1-13`). **Can stay a file.** |
| Scan rejection log | `scan_rejection_log.json` (`scan_rejection_log.py:33`) | nightly sweep | Append-per-scan-run, idempotent per `scan_id`, 180-distinct-date retention (per the juice-capacity audit §0.1, `scan_rejection_log.py:373-380`). **Can stay a file.** |
| Structure labels | `structure_labels.json` (`structure_labels.py:39`) | operator UI | Manual annotation, joined to a scan record by `scan_id`, never edits the record it labels (`structure_labels.py:1-20`). **Can stay a file.** |
| Symbol Genius history | `symbol_genius_history.json` (`symbol_genius_history.py:28`) | nightly | Derived, day-idempotent (`symbol_genius_history.py:1-16`). **Can stay a file.** |
| Universe | `universe.json` (`config.py:23`) | operator edits | Editable ticker roster, seeded once from the repo file (`config.py:20-23`). **Can stay a file.** |
| Day-trade universe | `daytrade_universe.json` (`config.py:30`) | operator edits | Independent roster, seeded once, diverges freely from `universe.json` (`config.py:24-29`). **Can stay a file.** |
| CSP dry-powder log | `csp_dry_powder_log/YYYY-MM-DD.json` (`csp_dry_powder.py:82`) | daily sweep | One file per scan day, explicitly "no shared state" with the main CSP path (`csp_dry_powder.py:1-27`). **Can stay a file.** |
| Day-trade log dir | `daytrade_log/` (`daytrade/settings.py:24`, `daytrade/__init__.py:5`) | day-trade sleeve | Side-channel, own `enabled.json` per account id — NOT the accounts registry (`daytrade/settings.py:8-11`). **Can stay files**, but note it has its OWN per-account keying scheme, parallel to but distinct from `accounts.py`'s — a store migration should not assume "per account" always means the same key shape. |
| Gate telemetry log | `gate_telemetry_log/YYYY-MM-DD.json` (`gate_telemetry.py:109`) | scan sweep | Read-only observability, zero authority (`gate_telemetry.py:16-28`). **Can stay a file.** |
| Parquet cache (+ `cache_demo`) | `cache/`, `cache_demo/` (`config.py:19,39`) | data_handler fetch | Pure cache of provider bars, disposable, rebuildable from providers. **Can stay files**, and arguably should never be database rows (large, columnar, rebuildable). |
| Backups | `backups/`, per-account subdirectory (`backups.py:37-48`) | nightly + pre-migration | Point-in-time copies of the primary store, taken under `logging_handler._lock` (`backups.py:61-67`). **Must move with the store's own backup mechanism** — a SQLite backend needs its own snapshot method (the sqlite3 backup API), not a copy of a JSON file; see §6/Phase 2 in the companion cutover prompt. |
| Schwab OAuth tokens | `schwab_token.json`, `schwab_token.account-<id>.json` (`schwab_api.py:66-70`) | OAuth flow | Credential, not a record; deleted (not archived) on disconnect (`accounts.py:513-533`). **Stays a file** — a credential store, not trading data. |
| Session secret | `.session_secret` (`auth.py:35`) | first boot | One-time-generated secret. **Stays a file.** |
| VAPID keys | `.vapid_keys.json` (`webpush.py:60`) | first boot | Same as session secret. **Stays a file.** |

**Summary of §2.** Of the ~24 side stores, **21 are pure telemetry/config
that intentionally sit outside `recompute_derived`'s authority and can stay
JSON files indefinitely** — migrating them buys nothing and the modules'
own docstrings already say so. **Three are operationally load-bearing and
need explicit handling in any store migration: the order journal (must stay
independent — TL;DR #4), the account registry (must be updated in lockstep
with however book identity changes — §3), and the backups mechanism (needs a
format-appropriate snapshot method, not a file copy — §6).**

---

## §3. Per-account and demo-mode layout that must survive

`accounts.py`'s own docstring (`:1-28`) already states the design constraint
a store migration must not violate: **one state file per account, never one
file with an account column** — because `executions` is append-only and
every derived ledger is a wholesale rebuild from it, so mixing two books'
logs would make every derived number "a blend of accounts that no migration
could unpick later" (`accounts.py:6-9`). This is a direct argument against
option (b) in the prompt (one SQLite file with an account column) — see §6.

**Resolution chain that must survive, whatever the storage identity becomes:**
`accounts.active_id()` (contextvar override, else the registry's persisted
choice, `accounts.py:229-235`) × `config.demo_enabled()` (global boolean,
`config.py:45-54`) → `accounts.state_path(account_id, demo)` (`:287-296`) →
`accounts.active_state_path()` (`:299-300`) → `config.active_state_path()`
(`:131-141`, the module every other module imports). **The demo flag is
GLOBAL, not per-account** — flipping it in `mode.json` switches *every*
account's book to its `state.demo[.<id>].json` sibling simultaneously; there
is no way today to have account A live and account B in demo at once. A
store migration must preserve this coupling (or make it a deliberate,
called-out behavior change) rather than accidentally making demo mode
per-account through a database schema that has a natural per-row
demo-vs-live column.

**Naming convention that call sites depend on:** `_sibling()`
(`accounts.py:280-284`) turns `/data/state.json` + `ira` into
`/data/state.ira.json`, and the demo base the same way
(`/data/state.demo.json` → `/data/state.demo.ira.json`). `order_journal_path`
(`logging_handler.py:463-470`) derives the journal filename from the STATE
filename by the same string surgery (`state` → `orders`), so the journal's
naming is coupled to the state file's naming, not independently configured.
Any identity scheme that replaces file paths (a SQLite filename, a table
prefix, a schema name) needs an equivalent single function serving the same
role `accounts.state_path()` plays today, and the order journal's path
derivation needs to be repointed at whatever that function returns rather
than string-surgery on a `.json` filename.

**Registry mutation surface that must keep working:** `accounts.create`
(`:334-367`, lazy — the state file is NOT created at registration, only on
first load), `accounts.delete` (`:430-468`, refuses on a book with
executions unless `purge`, and even then RENAMES the file aside, never
unlinks it), `accounts.all_state_paths()` (`:303-312`, used by the orphan-temp-file
sweep and would need a DB-equivalent enumeration — "every account's live and
demo store" — for whatever the store backend needs to sweep/checkpoint at
startup).

---

## §4. Durability guarantees today, and what each must map to in a database

| Guarantee today | Where | What it must become |
|---|---|---|
| **Atomic write** (temp file same dir → `fsync` → `os.replace` → dir `fsync`) | `logging_handler._atomic_write` (`:185-219`) | A committed SQLite transaction (WAL mode gives you this for free — a crash mid-transaction leaves the last COMMITted state, never a half-written row). Postgres: same, transactionally, but now over a network round-trip the app has never had to reason about failing mid-op. |
| **Refuse-to-reinitialize on corrupt file** | `StateCorruptError` (`logging_handler.py:29-32`, raised at `:159-162`) | A SQLite file that fails `PRAGMA integrity_check` (or fails to open at all) must refuse to start the same way — there is no "silently create a fresh empty DB" fallback today and there must not be one after. |
| **Process lock (single-writer, in-process)** | `logging_handler._lock = threading.RLock()` (`:26`) | SQLite: WAL mode already serializes writers at the file level; the in-process RLock still matters for the read-then-write logic in `mutate_state`, so it should be KEPT even with SQLite (SQLite's own locking prevents corruption, not lost updates across a slow fetch — that's an application-level race the RLock prevents, not a storage-engine concern). Postgres: the RLock stops protecting anything across machines; every `mutate_state`-shaped operation must become a database transaction with the right isolation level, or the lost-update bug the docstring describes (`logging_handler.py:281-288`) comes back in a multi-machine deployment — which is exactly the scenario Postgres invites and SQLite doesn't. |
| **Nightly rotating backups** | `backups.make_nightly_backup`/`rotate` (`backups.py:127-154`), 30 kept (`docs/recovery.md:19`) | SQLite: the sqlite3 backup API (a proper hot-backup, not a raw file copy of a WAL-mode file, which can be inconsistent) — the prompt's Phase 2 spec already calls for this (`backups.py` "snapshots the SQLite file using the sqlite3 backup API under the same lock"). Postgres: `pg_dump`/`pg_basebackup`, a genuinely different mechanism and a genuinely different restore story. |
| **Pre-migration snapshots** | `backups.snapshot_before_migration` (`:160-177`), called from `migrations.migrate` (`migrations.py:454-466`), kept forever | Same shape: a snapshot before any schema migration, SQL migration or JSON-chain. Must abort the migration (not proceed) if the snapshot can't be taken — `MigrationAbortedError` (`migrations.py:23-25`) is the pattern to keep. |
| **Off-machine copy** | `backups.send_offmachine_copy` — S3/Tigris or email attachment (`backups.py:263-280`, `docs/recovery.md:89-117`) | Unchanged in spirit — ship the backup artifact (whatever format it now is) off the Fly volume. A SQLite file backup is a single file, same as today; a Postgres `pg_dump` is also a single file — this guarantee is format-agnostic. |
| **Restore CLI** | `scripts/restore_state.py` (writes the current file aside as `.pre-restore.<ts>` before overwriting, requires `--yes`, requires the app be stopped manually — no cross-process lock, `restore_state.py:14-16`) | Must accept whatever the new backup format is (`scripts/restore_state.py` in the prompt's Phase 2 spec: "accepts either format"). The "stop the app first" caveat gets MORE important, not less, with SQLite (a live WAL file mid-restore is worse than a live JSON file mid-restore) and becomes a genuinely different operational story with Postgres (there is no single file to "restore" — it's a service-level operation). |

**The single-writer/single-machine constraint is explicit and load-bearing.**
`fly.toml:7-10`: *"Single-writer store: run exactly ONE machine (`fly scale
count 1`). A volume attaches to only one machine, so 2 machines would need 2
volumes and the data would diverge."* This is not an accident of the current
implementation — the whole `logging_handler._lock` / `mutate_state` design
assumes one process, one machine. Any option that doesn't also assume that
(Postgres, in the abstract) is solving a problem this app doesn't have and
importing operational surface (connection pooling, network partitions,
a service that can be down independently of the app) that it currently has
zero code to handle.

---

## §5. The two consumers with the strongest invariants

### 5.1 `recompute_derived` — wholesale rebuild, not incremental

`logging_handler.recompute_derived(state)` (`:945-1476`) is called after
**every** append (`append_execution:400`, `append_order_event:613`,
`append_recommendations:645`, `append_recommendation_override:661`,
`append_coverage_miss_ack:677`) and after a schema migration
(`load_state:174`). Each call:

1. Computes `derived_executions(state)` (`:913-942`) — filters
   reversed/excluded executions AND applies the `txn_correction` overlay
   (TL;DR #3) — **from scratch, over the full history**, every time.
2. Rebuilds, entirely from that filtered/overlaid list plus (for a few
   fields) the current `positions` array: `theta_ledger` (`:959-1018`),
   `extrinsic_payback` (`:1083-1123`, explicitly SKIPPED for `SHARES`
   positions — `:1094`), `payback_reconciliation` (`:1125-1138`, itself
   running `validate_payback` over the same filtered list, `:815-891`),
   `theta_ledger.extrinsic_summary` (`:1140-1148`), `dividend_ledger`
   (`:1154-1171`), `accrual_ledger` (delegated to `accrual.derive`,
   `:1177-1178`), `put_ledger` (`:1197-1225`), `roll_ledger` (`:1227-1262`),
   `cycles` (`:1264-1384`, including wash-sale flagging over the same
   execution list), a handful of per-position fields (`:1390-1446`),
   `order_state` (rebuilt from `order_events`, `:1452-1465`), and the trust
   layer (delegated to `trust_derive.recompute`, guarded so a derivation bug
   there can't block the append, `:1467-1476`).
3. Every one of those assignments is `state[key] = <freshly computed dict>`
   — **there is no partial update anywhere in this function.** A single new
   execution triggers a full re-walk of the entire (filtered) execution
   history for theta/payback/dividend/accrual/put/roll/cycle ledgers alike.

**Implication for a database backend.** This function's contract is "given
the full execution history (with the correction overlay applied) and the
current positions, replace these ten-plus derived keys with their from-scratch
recomputation." A SQL schema that tries to make any of these incremental
(a trigger that updates a running total instead of replaying) will drift
the moment a `txn_correction`, `adoption_reversal`, or schema migration
changes what "the full history" means — which is precisely the scenario
these ledgers exist to get right. **The correct database-native mapping is:
keep `recompute_derived` as application logic that runs a `SELECT` over the
executions table (respecting the same overlay/reversal filters) and
overwrites the derived tables/documents wholesale inside one transaction** —
not a set of maintained aggregates. This matches the audit's earlier
finding that `positions` (unlike the ledgers) does NOT replay cleanly from
genesis (§1.2) — a store migration must not accidentally "fix" that by
making positions a derived view either, since the imperative mutation is
the current (if imperfect) source of truth for it.

### 5.2 The order journal — append-only, outside the primary store, recovered by id

Already covered in TL;DR #4 and §2's table. The specific operations a store
interface needs: `append_order_journal(entry)` (best-effort, swallows its own
exceptions — `logging_handler.py:473-493` — a journal failure must never
block an order that is about to be sent or a fill that has already been
booked), `order_journal_entries(limit=None)` (linear scan of the account's
`.jsonl`, skips malformed lines, `:496-512`), and `order_journal_lookup(order_id)`
(merges every entry for one broker order id, later events overlay earlier
ones so a fill's spot wins over the placement's, `:515-544`). All three are
called from `executor.py` and `transaction_ingest.py` only. **A store
migration should give the journal its own table/store with the SAME
independence property it has today (recoverable when the primary store
isn't), not necessarily the same file format** — the Phase 1 seam prompt is
right to keep it byte-for-byte identical for now and defer the "should the
journal also move into SQLite" question to a later phase, because moving it
into the SAME transactional boundary as the primary store is the one change
that would remove the property it exists for.

---

## §6. Recommendation: (a) SQLite per account, on the existing volume

### What each option buys and costs

**(a) SQLite per account on the existing volume — recommended.**
- **Buys:** matches the existing single-writer/single-machine constraint
  exactly (`fly.toml:7-10`) — no new operational surface. WAL mode gives
  real crash-safety (superset of today's atomic-rename guarantee) and real
  transactions (the `mutate_state` read-modify-write becomes an actual
  isolated transaction instead of an RLock + a full-file rewrite). Keeps the
  one-file-per-account isolation `accounts.py` was explicitly designed
  around (`accounts.py:1-9`) — a bad migration or corruption on one book
  can't touch another's. The sqlite3 backup API gives a real hot-backup
  primitive the current "copy the file" approach doesn't have for a live
  WAL file. Requires no new service, no new credentials, no new network
  dependency — `sqlite3` is stdlib.
- **Costs:** still a single point of failure per machine (unchanged from
  today — this is the volume's existing risk, not a new one). SQL schema
  design/migration discipline is new relative to "just add a key to a
  dict." Cross-account rollups (`accounts.summary()`, `accounts.py:673-693`)
  become N separate connections instead of N file reads — a small, bounded
  cost (`MAX_ACCOUNTS=12`).

**(b) One SQLite file with an account column.**
- **Buys:** marginally simpler cross-account queries/rollups; one file to
  back up instead of up to 12×2.
- **Costs:** directly reintroduces the risk `accounts.py`'s own design
  rationale calls out — a bug in a migration, a bad `recompute_derived`-
  equivalent rebuild, or a WAL corruption event now blast-radiuses across
  every account's book at once, where today (and under option (a)) it's
  contained to one file. It also fights the grain of every derived-ledger
  computation in `logging_handler.py`, all of which are written as "the
  whole store, no account filter" — every one of them would need a
  `WHERE account_id = ?` retrofitted, multiplying the surface area of the
  Phase 1/2 work for no capability the app is asking for.

**(c) Postgres on Fly.**
- **Buys:** if this app ever needs multiple writers on multiple machines,
  or wants managed replication/point-in-time recovery as a *service*
  feature rather than a cron job, Postgres is where that capability lives.
- **Costs:** breaks the single-writer/single-machine assumption baked into
  `logging_handler._lock` and every `mutate_state` caller — not because
  Postgres can't be used single-writer (it can), but because *choosing*
  Postgres for an app that has exactly one machine and one process is paying
  for a capability (network-attached, independently-scalable storage) this
  app has no other code path that needs, while giving up the "everything is
  a local file, `fly volume` durability is the whole story" operational
  simplicity the rest of the deployment (`fly.toml`, `docs/recovery.md`)
  is built around. It also means the backup/restore story (§4's last two
  rows) becomes qualitatively different — `pg_dump`/PITR instead of "copy a
  file" — for a workload (one operator's trading book) that has never
  needed that.

**Given the single-writer, single-machine constraint fly.toml states
explicitly, (a) is the only option that doesn't either reintroduce a
blast-radius risk the current design deliberately avoids (b) or trade a
matched operational model for a mismatched one in exchange for a capability
nothing in this app currently exercises (c).**

### Phased plan

**Phase 1 — repository seam, JSON backend unchanged.** A `backend/store.py`
`Store` interface covering exactly the operation list §1.2/§1.3/§5 derived:
`load`, `save`, `mutate` (read-modify-write under the lock — the catch-all
for anything not individually named, including what `alerts.py`/`webpush.py`
do today), `append_execution`, `append_recommendations`,
`append_recommendation_override`, `append_coverage_miss_ack`,
`append_order_event`, order-journal `append`/`lookup`/`entries`,
pending-order `get`/`save`/`pop`/`list`, order-lock `get`/`save`,
order-submission `get`/`save`/`update`/`list`, and a backup-snapshot hook.
`JsonStore` is the current code moved, not rewritten — same paths, same
`_atomic_write`, same lock, same journal format. `logging_handler` keeps its
public names as thin delegates so the ~30 call sites in §1.1 don't change.
Byte-identical output is a testable property (replay the demo execution log
through old and new paths, diff the result) — see the Phase 1 prompt already
drafted for this.

**Phase 2 — SQLite backend behind `CFM_STORE=sqlite`, default off, with a
`CFM_STORE=json+verify` shadow-verification mode** that writes both stores
and diffs the SQLite-materialized state against the JSON state on every
load, alerting on mismatch through the existing `alerts.py` (`DATA_STORE_MISMATCH`)
without ever changing what the app serves — this is the exact "zero
authority until reviewed" pattern the shadow-mode features in
`CLAUDE.md` already establish (`shadow_floor`, `chart_structure`,
`juice_capacity`); this migration should use the same trust posture rather
than inventing a new one. Concurrency and crash tests (kill mid-transaction,
confirm the file opens clean) are the SQLite-specific tests the JSON backend
never needed and this phase must add.

**Phase 3 — cutover, after a real production soak.** SQLite becomes the
default; `state.json` is renamed `state.json.migrated` and kept for one
release as the rollback path; `CLAUDE.md`'s "state.json is the single source
of truth" line is updated to name the SQLite file (per account) instead,
while "prefer fixing derivation over editing state" is kept verbatim — that
principle doesn't change with the storage engine.

### HARD STOPs

- **Do not fold the order journal into the same SQLite file/transaction as
  the primary store** until a separate, explicit decision is made and
  documented — its whole value is being recoverable when the primary store
  isn't (§5.2, TL;DR #4).
- **Do not make `recompute_derived`'s outputs incrementally-maintained
  database structures** (triggers, materialized views updated row-by-row)
  — they must remain "recompute from the full filtered/overlaid execution
  history, overwrite wholesale, inside one transaction" (§5.1).
- **Do not choose option (b) or (c)** without the owner explicitly accepting,
  respectively, the cross-account blast-radius regression or the
  single-writer-assumption break this audit identifies.
- **Do not flip `CFM_STORE`'s default** in Phase 2, and do not proceed to
  Phase 3 without a documented production soak period with zero
  `DATA_STORE_MISMATCH` alerts (the precondition the Phase 3 prompt already
  states).
- **Do not touch `executor.py`'s internals** in Phase 1 — the seam is a
  storage-layer change; `executor.py`'s ~93 call sites should see identical
  `logging_handler` function signatures throughout Phase 1.

---

## Acceptance (Phase 0)

This report is delivered for owner review. **No code changed.** Implementation
(Phase 1) begins only on explicit approval. All citations are `file:line`
against `669b0e4`.
