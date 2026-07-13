# AUDIT — Order Lifecycle State Machine + Broker Reconciliation

**Scope:** Phase 0 written audit for the spec in
`orderlifecyclereconciliationprompt.md` (§1–§7). Written **before** any code
change, per the spec's "AUDIT FIRST — stop and present." File/line references are
to the tree on branch `claude/next-implementation-4858m3` at audit time.

> **Read-this-first finding.** This codebase is **much further along than the
> spec assumes.** A prior "incident hotfix" effort already built the pure pricing
> validator (`order_pricing.py`), the coded lifecycle state machine
> (`order_lifecycle.py`), the idempotent `client_order_ref`-keyed submission with
> orderId-persist-first and UNKNOWN handling — **but only on the short-call roll
> path** — and a positions-based reconciler with a per-position freeze
> (`reconcile.py`). See `AUDIT_INCIDENT_HOTFIX.md` and
> `IMPLEMENTATION_NOTES_INCIDENT_HOTFIX.md`.
>
> Therefore this audit is precise about the **residual gaps** that remain, not a
> re-description of machinery that already exists. The one genuinely **new,
> unbuilt subsystem is §4 (execution ingestion from the Schwab transactions
> endpoint)** — and it collides head-on with an explicit, operator-confirmed
> design stance in the current reconciler. That collision is the single most
> important thing in this document and is called out as **DECISION 1** below.

---

## 0. TL;DR gap matrix (spec → reality)

| Spec | Requirement | Status | Where |
|---|---|---|---|
| **§1** | Lifecycle state machine w/ coded states, legal transitions, orderId-persist-first, UNKNOWN→status-query, terminal handling, partial-fill UNBALANCED | **PARTIAL** | `order_lifecycle.py` (pure SM ✅); orderId-first + UNKNOWN only on roll path; two parallel state vocabularies |
| **§2** | Idempotent backend submission keyed by `client_order_ref`; frontend POST-once-then-poll; cancel w/ fill-during-cancel race | **PARTIAL** | Roll path fully ✅; single-leg / atomic-open / atomic-exit / leap-roll **not** idempotent |
| **§3** | Pure pre-submit validation: tick, net sign, quote freshness/one-sided, leg structure | **MOSTLY DONE** | `order_pricing.py` ✅ (roll legs); not wired into non-roll paths; leg-structure check thin |
| **§4** | Ingest executions from Schwab **transactions** endpoint; dedupe by txn id; `source: broker_manual` out-of-band; multi-leg orderId linking | **MISSING** (and philosophically blocked) | No `get_transactions`; reconcile is positions-only; **DECISION 1** |
| **§5** | Position reconciliation + freeze; block recommendations **and** orders; minutes-based stale; interval scheduler | **PARTIAL** | Positions reconcile + freeze ✅; freeze blocks **orders only, not recommendations**; stale is **hours** not minutes; no during-market interval scheduler |
| **§6** | Unbalanced-position detection → highest alert + freeze + exposure description; never auto-remediate | **MOSTLY DONE** | `PARTIAL_FILL_CANCELED` + `ROLL_LEG_IMBALANCE` + `SHORT_STOCK_DETECTED` all freeze ✅; exposure-direction wording could be sharpened |
| **§7** | Order-status panel w/ orderId/timestamps/reasons + **manual** cancel button; reconcile heartbeat + "Reconcile now"; divergence/freeze panel; `broker_manual` badges | **PARTIAL** | Heartbeat + "Reconcile now" + divergence panel ✅; **no standing order-lifecycle panel**; cancel is **auto/time-based, no manual button**; no `broker_manual` badge (nothing to badge yet) |
| **Config** | `RECONCILE_INTERVAL_MINUTES`, `RECONCILE_STALE_MINUTES`, `NO_AUTO_REMEDIATION`, `INGESTION_IS_GROUND_TRUTH` | **MISSING** | `config.py` has the other six constants already |

---

## 1. Incident root-cause (confirmed — already fixed for the roll path)

The four incident failures were root-caused and fixed in the prior hotfix. This
audit **confirms** those findings; nothing here contradicts them.

- **D1 — false "premium" / price.** `executor.py:1646` (old) built the net with
  `round(new_premium - buyback, 2)` and `... or 0` coercion. Confirmed causes:
  **(a)** off-tick rounding (whole cents, not the option's $0.05 tick ≥ $3.00);
  **(c)** a missing/one-sided quote coerced to `0`, which could **flip** a credit
  to a debit; **(d)** binary-float mid math. **Fixed** by `order_pricing.py`
  (`round_to_tick`, `net_credit_debit` as the single source of direction,
  `assert_direction`, `format_price`, `validate_roll_quotes`).
- **D2 — the inverted picture ("failed" while working at Schwab).** Old success =
  "2xx **and** a parseable `Location` header"; a header-less 2xx or a
  post-send timeout raised → `app.py` catch-all → HTTP 500 → red "failed" toast.
  **Fixed** by `schwab_api.submit_order` (`schwab_api.py:422-460`) returning a
  structured `accepted | rejected | unknown` outcome, and `_place_live_roll`
  mapping ambiguity to **UNKNOWN**, never "failed".
- **D3 — refresh/retry duplicate risk.** No idempotency key existed. **Fixed** by
  `client_order_ref` minted client-side in `sessionStorage`
  (`RollModal.jsx:26-62`) and the backend short-circuit
  (`executor.py:1732-1734`): a repeat ref returns the existing record without
  re-submitting. Regression test `test_incident_hotfix.py:256`.
- **D4 — orderId discarded.** The orderId **was** received (Location header) but
  persisted only after a clean extraction, so every failure branch lost it.
  **Fixed** by `ORDERID_PERSIST_FIRST`: durable `order_submissions` record written
  **before** the broker call (`executor.py:1775`) and the orderId written onto it
  **first** on ack (`executor.py:1792`) via atomic whole-file rewrite
  (`logging_handler.py:164-198`). Regression test `test_incident_hotfix.py:124`.

**Phantom records:** the D2 false-failure branch **raised before any state
write**, so **no phantom executions exist** from the incident
(`IMPLEMENTATION_NOTES_INCIDENT_HOTFIX.md:135-139`). The manual ToS roll that
actually executed is **not** in `state.json` — that divergence is real and is
exactly the §4 out-of-band case (see DECISION 1). **§4's "phantom cleanup" is
therefore N/A for cleanup, but the underlying divergence is live.**

---

## 2. Order submission path (spec Phase-0 items 1–3)

**Two generations of live-order code coexist.**

- **Hardened path — short-call roll only:** `executor._place_live_roll`
  (`executor.py:1704-1828`) → `schwab_api.submit_order`. Has: required
  `client_order_ref` + idempotency short-circuit (`:1723-1734`); re-read + validate
  both leg quotes (`order_pricing.validate_roll_quotes`, `:1751`); single-source
  net + direction (`net_credit_debit`, `:1762`) with `assert_direction` (`:1766`);
  durable record **before** the call (`:1775`); orderId-**first** on ack (`:1792`);
  UNKNOWN on id-less ack / timeout / 5xx (`:1803-1827`); manual UNKNOWN-recovery
  by recent-orders match (`submission_status`, `:1850-1921`).

- **Legacy paths — everything else:** single-leg `_place_live` (`:796`), atomic
  open `_place_live_open` (`:2224`), atomic exit `_place_live_exit` (`:2419`),
  LEAP roll `_place_live_leap_roll` (`:2601`) all call `schwab_api.place_order`
  (`schwab_api.py:399`), which **raises** on non-2xx and on a header-less 2xx
  ("returned no order id", `:412` / callers `:815,:2259,:2458,:2625`). These
  paths:
  1. write **no durable record before the call** — a crash between a good ack and
     `save_pending_order` loses the orderId (startup reconcile only re-polls
     `pending_orders`, which was never written — `executor.py:1265`);
  2. read/persist the orderId **after** the ack, not first (a real D4-shaped
     window);
  3. treat a **live-but-headerless 2xx as an error** (a latent D2 for these
     paths).

- **Idempotency coverage.** `client_order_ref` idempotency exists **only** on the
  roll path. The per-intent resubmit gate `_guard_resubmit` (`executor.py:280`) →
  `order_lifecycle.check_resubmit` covers only `_LOCKED_INTENTS =
  {buy_leap, sell_short, open_position_atomic}` (`executor.py:266`) — **exit and
  LEAP-roll intents are ungated**, protected only by the pending-order scan,
  which is bypassed when the pending record was never written (point 1).

- **Frontend coupling (item 3) — well guarded now.** Submission is user-gesture
  driven, never in a `useEffect`. `RollModal.execute()` (`RollModal.jsx:144`) →
  `orderFlow.js:79 api.execute(payload)` (POST once) → then **polls**
  `api.orderStatus` (`orderFlow.js:115-131`) and `api.submissionStatus`
  (`:36-63`). The `client_order_ref` is minted once and kept in `sessionStorage`
  across remounts (`RollModal.jsx:26-62`), retired only on a confirmed terminal
  outcome — a mid-flight refresh reuses the **same** ref. UNKNOWN renders
  "confirming with broker…", rejection renders Schwab's verbatim reason, and the
  UI never collapses ambiguity to "failed" (`orderFlow.js:48-96`).
  **Gap:** cancellation is an **automatic time-based** call after a no-fill window
  (`orderFlow.js:139`); there is **no standing order-lifecycle panel and no manual
  per-order Cancel button** (§7).

---

## 3. orderId & transaction-id handling (Phase-0 item 4)

- **Schwab orderId** is parsed from the `Location` header in both clients
  (`place_order` `schwab_api.py:408-410`; `submit_order` `:452-454`). **LIVE_VERIFY:
  whether Schwab also returns the id in the 2xx body is unconfirmed** — if it
  does, many current UNKNOWNs would resolve immediately to WORKING
  (`IMPLEMENTATION_NOTES_INCIDENT_HOTFIX.md:99-101`).
- Persisted (roll path, first) on `order_submissions[client_order_ref].order_id`
  (`logging_handler.py:333-378`) and on `pending_orders[orderId]`,
  `order_events[].order_id`, `order_locks["TICKER:intent"].order_id`, and
  `order_receipts[].order_id` (which links `order_id → execution_ids` at fill
  time, `executor.py:1041-1048`).
- **Schwab transaction IDs appear nowhere** in the codebase. Executions carry
  **no** `transaction_id`. This is the missing dedupe key for §4.

---

## 4. state.json execution schema + correction mechanism (Phase-0 item 5)

- **Append-only, versioned.** `executions` is appended, never mutated
  (`logging_handler.py:265-280`). Migrations `migrations.py` only ADD;
  `CURRENT_VERSION = 18` (`migrations.py:20`), each step snapshotted before apply.
- **Execution record fields** (appended by `append_execution`,
  `logging_handler.py:265`): `id` (`exec_NNN`), `date`, `live_transmitted`
  (bool|None), plus per-action economics — `buy_leap`
  (`execution_price/execution_total/extrinsic_captured/stock_price/expiration`),
  `sell_short` (`premium_per_share/premium_total/entry_extrinsic_per_share`),
  `close_short`
  (`close_price_per_share/close_total/extrinsic_paid_back/net_juice`), `roll_*`
  linkage (`roll_group_id`, `roll_leg`, `roll_reference_net_mid`), and
  `mode`/`price_source`/`fill_assumption`/`quoted_mid_per_share`.
- **No `source` discriminator** (app vs broker_manual). Provenance today = `mode`
  (`"live"`/`"logged"`) + derived `live_transmitted`. **§4 needs a new `source`
  field** (`app` / `broker_manual`) and a `transaction_id`.
- **No `order_id` on execution records** — the link is indirect via
  `order_receipts`. §4 matching wants the orderId on the ingested execution.
- **Correction primitive (append-only) EXISTS:** the **linked compensating
  adjustment**. `action:"adjustment"` (`executor.py:569-576`) with a signed
  `quantity_delta`, a **required typed `reason`**, and `linked_diff_id` back to
  the reconcile diff; and `resolve_expiry` (`:616-621`) which books a short at
  $0 "corrected forward." No in-place edit/delete/void anywhere. **This is the
  mechanism §4's phantom-cleanup asks for — it already exists and is tested**
  (`test_reconcile.py:291,324,331`). Since the incident wrote no phantoms, the
  §4 phantom task reduces to: *if* ingestion ever needs to correct a prior
  record, append a compensating adjustment; never edit.

---

## 5. Existing reconciliation + the central design collision (Phase-0 item 6)

`reconcile.py` (680 lines) is a **positions-only** reconciler:

- Pulls `get_accounts(positions=True)` **only** (`reconcile.py:557-562`); parses
  `securitiesAccount.positions` (`:156-169`), primary account only. **No
  transactions endpoint, no orders endpoint** for reconciliation.
- Pure core `reconcile(broker_view, expected_view, ...)` (`:324-397`) classifies
  every instrument into exactly one of `MATCH / MISSING_AT_BROKER /
  UNEXPECTED_AT_BROKER / QUANTITY_MISMATCH / SHORT_STOCK_DETECTED
  (highest sev) / EXPIRED_WORTHLESS_PENDING (benign)`. Quantities are **signed**
  (long−short), so a long/short flip is caught.
- Freeze = per-position **`needs_review`** flag + `review` block, set by
  `reevaluate_freezes` (`:615-647`). No global `RECONCILE_FREEZE` state.

> ### DECISION 1 — the collision that gates §4
>
> `reconcile.py:7-11` states the **operator-confirmed** design philosophy
> verbatim:
>
> > "ALL trading on this account goes through this app. Any divergence between
> > Schwab and state is therefore an anomaly … never a 'legitimate external
> > trade'. **There is NO adopt-external-trade flow.** The correct response to a
> > diff is: freeze the position, alert, human resolves."
>
> **§4 asks for the exact opposite:** automatically **ingest** out-of-band broker
> executions (the manual ToS roll) as ground-truth executions tagged
> `source: broker_manual`, "never hand-entered," and reconcile them into state.
>
> These cannot both stand. This is a philosophy reversal on a **live-money**
> system and must be an explicit operator decision before any §4 code is written.
> Three coherent options (my recommendation is **B**):
>
> - **(A) Full auto-ingest (literal §4).** Transactions endpoint → dedupe by txn
>   id → matched fills complete lifecycle records (`source: app`) → **unmatched
>   broker executions auto-append as `source: broker_manual`** with all economics
>   from the broker. Maximum automation; **reverses** the "no adopt" stance;
>   an ingestion bug can silently write wrong positions into the source of truth.
> - **(B) Ingest-to-confirm, human-adopts (recommended).** Build the transactions
>   endpoint and dedupe. **Matched** fills auto-complete app lifecycle records and
>   confirm executions (this is pure upside, no philosophy change). **Unmatched**
>   broker executions are surfaced in the divergence/freeze panel as a **proposed
>   `broker_manual` adoption** with the broker economics pre-filled; the operator
>   clicks **once** to append it (which internally uses the existing
>   compensating-adjustment / append primitive). Preserves "nothing hand-entered"
>   (economics come from the broker record, not the human) **and** keeps
>   human-in-the-loop, honoring `NO_AUTO_REMEDIATION`. This is the smallest change
>   that satisfies the incident's real need (get the ToS roll into state) without
>   reversing the safety stance.
> - **(C) Detect-and-freeze only.** Add the transactions endpoint purely to
>   improve *matching/confirmation* of app orders and to *detect* out-of-band
>   activity earlier, but still **freeze + human-resolves** with no adoption path
>   at all. Least code; leaves the operator re-typing the ToS roll as an
>   `adjustment` (status quo), which is the workflow the incident showed is
>   error-prone.

---

## 6. Recommendation / action gating (Phase-0 item 7) + freeze semantics (§5)

- **Order submission is gated** by the freeze: `_enforce_not_frozen`
  (`executor.py:493-498`) raises `PositionFrozenError` (HTTP 409) for
  `FROZEN_BLOCKED_ACTIONS = {buy_leap, sell_short, roll_short, roll_leap,
  open_position_atomic}` (`:36-37`); closing/adjustment deliberately allowed.
- **Recommendations are NOT gated.** `recommendation_engine.py` /
  `recommendation_runner.py` never read `needs_review` (confirmed by grep). §5
  requires the freeze to **also** block recommendation generation. **Gap.**
- **Stale detection is HOURS, not minutes.** `RECONCILE_STALE_HOURS = 36`
  (`config.py:986`); `check_reconcile_stale` (`alerts.py:848`) fires a MEDIUM
  `RECONCILE_STALE` alert. §5 wants `RECONCILE_STALE_MINUTES = 45` during market
  hours. **Note the tension:** with today's **once-daily** reconcile cadence, a
  45-minute stale threshold would read "stale" almost always — a minutes-based
  threshold only makes sense **together with** the §4/§5 interval scheduler
  (below). Ship them together.
- **No during-market interval scheduler.** Reconcile runs once pre-market
  (`alert_scheduler.py:248`), nightly (`maintenance.py:212`), on-demand
  (`POST /api/reconcile`), and non-persisting after fills (`fill_verify.py:191`).
  `RECONCILE_INTERVAL_MINUTES` **does not exist**. §4/§5 want every N minutes
  during market hours + once after close.

---

## 7. Unbalanced-position handling (§6) — mostly done

- Partial-fill-then-cancel on a multi-leg roll → `PARTIAL_FILL_CANCELED`
  (`order_lifecycle.py:123`), `_freeze_for_partial_fill_cancel`
  (`executor.py:1207`): sets `needs_review`, trips `DELTA_COVERAGE_CHECK`, fires
  CRITICAL `ORDER_PARTIAL_FILL_CANCELED`, never auto-fixes.
- Leg-imbalanced roll fill → `ROLL_LEG_IMBALANCE` (CRITICAL, `executor.py:2012`),
  freeze, no execution written (`test_atomic_roll.py:252`).
- Fill-during-cancel → `FILLED_DURING_CANCEL` + CRITICAL alert
  (`executor.py:1178`).
- Assignment (uncovered short stock) → `SHORT_STOCK_DETECTED` (highest reconcile
  severity, `reconcile.py:373-379`).
- **Residual:** the spec wants the panel to state **covered vs uncovered
  direction** explicitly (orphaned buyback vs orphaned new short). Current copy
  names the imbalance but the **exposure-direction sentence** could be sharpened
  in the freeze `review` payload. Minor.

---

## 8. Config constants — status

Already present & correctly provenance-tagged (`config.py:620-682`):
`MAX_RESUBMIT_ATTEMPTS=3`, `NO_RESUBMIT_BEFORE_TERMINAL=True`,
`QUOTE_MAX_AGE_FOR_ORDER_SECONDS=60`, `UNKNOWN_STATUS_RETRY_SECONDS=10`,
`UNKNOWN_STATUS_MAX_ATTEMPTS=6`, `ORDERID_PERSIST_FIRST=True`,
`NO_FAILURE_WITHOUT_VERIFICATION=True`, `OPTION_TICK_*`.

**Missing — to add:**
`RECONCILE_INTERVAL_MINUTES=15`, `RECONCILE_STALE_MINUTES=45`,
`NO_AUTO_REMEDIATION=True`, `INGESTION_IS_GROUND_TRUTH=True`. (Keep
`RECONCILE_STALE_HOURS` for the existing daily alert or migrate it — see §6
tension.)

---

## 9. Testing surface

- ~758 test functions across 53 `backend/test_*.py` files (well past the spec's
  "330+"). Relevant: `test_order_lifecycle.py` (17), `test_reconcile.py` (35),
  `test_incident_hotfix.py` (15), `test_order_fidelity.py` (11),
  `test_atomic_roll.py` (15), `test_atomic_open.py` (5).
- **No shared `conftest.py`/fixtures.** Each file defines its own inline fake
  client. The richest scripted-replay mock is `test_incident_hotfix.py:81
  FakeClient`: `get_quotes`, `submit_order` (attempt-keyed scripted outcomes),
  `cancel_order`, `list_orders`, `get_order` (pops a scripted queue to replay
  WORKING→terminal).
- **Gap for §4 tests:** no mock can replay **`get_transactions`** (the method
  doesn't exist) and there is no client-level `get_positions` mock (reconcile
  tests monkeypatch the fetch function / use a demo fixture). Building §4 requires
  extending a mock client to script transaction feeds. Recommend a **shared
  `conftest.py` `MockSchwabClient`** consolidating the inline fakes so the §4
  ingestion + idempotency + dedupe tests (spec tests 6, 9) have one replayable
  surface.
- **Env caveat (pre-existing):** ~24 suite failures come from a missing native
  `_cffi_backend` (cryptography) + a numpy/regime import on this box, unrelated to
  the order path (`IMPLEMENTATION_NOTES_INCIDENT_HOTFIX.md:150-155`). `pytest`
  is not currently installed in this environment — must be installed to run the
  suite. I will verify the order/reconcile subset runs green before/after.

---

## 10. Terminology reconciliation (spec state names vs existing)

The spec's diagram uses `PENDING_SUBMIT → SUBMITTED → ACKED → WORKING → {…}` with
`UNKNOWN` and `UNBALANCED`. The existing machine
(`order_lifecycle.py`) uses `SUBMITTED → WORKING → {FILLED | CANCEL_REQUESTED →
PENDING_CANCEL → {CANCELED | FILLED_DURING_CANCEL | PARTIAL_FILL_CANCELED} |
REJECTED | EXPIRED}` plus `LOCKED_UNKNOWN`, and a **separate** submission-record
status vocabulary (`SUB_SUBMITTING / SUB_WORKING / SUB_UNKNOWN`) on
`order_submissions`. Mapping:

| Spec state | Existing equivalent |
|---|---|
| PENDING_SUBMIT | `order_submissions` record written, status `SUB_SUBMITTING`, pre-call |
| SUBMITTED / ACKED | `SUB_SUBMITTING`→`SUB_WORKING` on ack; `order_events` opens at `SUBMITTED` |
| WORKING | `WORKING` |
| UNKNOWN | `SUB_UNKNOWN` (submission ambiguous) **and/or** `LOCKED_UNKNOWN` (orphan working order) |
| FILLED / CANCELED / REJECTED / EXPIRED | same |
| PARTIALLY_FILLED → … | partial-fill cursor on `pending_orders.filled`; terminal `PARTIAL_FILL_CANCELED` |
| UNBALANCED | `PARTIAL_FILL_CANCELED` / `ROLL_LEG_IMBALANCE` freeze |

**The existing model is richer** (it distinguishes fill-during-cancel). I
recommend **keeping the existing coded states** and mapping the spec's
`ACKED/PENDING_SUBMIT` onto the submission-record statuses rather than renaming —
but the two vocabularies (`order_events` lifecycle vs `order_submissions` status)
**should be unified** into one source of truth, as the hotfix notes already
flagged (`IMPLEMENTATION_NOTES_INCIDENT_HOTFIX.md:129-133`). **DECISION 2:** unify
now, or keep the two-store split and just document the mapping? (Recommend:
unify the *read* path — one `order_status_view(ref_or_id)` — without a risky
migration of the stores this pass.)

---

## 11. Proposed implementation plan (post-approval; spec's ordering)

1. **Config + shared mock.** Add the 4 missing constants; add
   `backend/conftest.py` with a `MockSchwabClient` (scriptable
   `get_order/place_order/submit_order/cancel_order/list_orders/get_quotes/
   get_transactions/get_accounts`). *No behavior change; unblocks §4 tests.*
2. **Extend §2/§3 to the legacy paths.** Route single-leg / atomic-open /
   atomic-exit / leap-roll through the `submit_order` structured outcome +
   `client_order_ref` idempotency + orderId-persist-first + UNKNOWN, reusing the
   roll path's proven shape. Close the header-less-2xx→error latent D2. Tests
   mirror `test_incident_hotfix.py` per path.
3. **§4 ingestion (gated on DECISION 1).** `schwab_api.get_transactions`
   (behind the interface, LIVE_VERIFY schema); pure
   `ingest_transactions(feed, state)`: dedupe by `transaction_id`, match to
   lifecycle by orderId (`source: app`, complete the record), link multi-leg by
   shared orderId; unmatched → per DECISION 1 (recommend B: surface as proposed
   `broker_manual` adoption). Idempotent re-run. Tests = spec 6(a)(b)(c), 9.
4. **§5 gating + scheduler.** Block recommendations on freeze (add a
   reconcile-freeze check in the recommendation runner); add the
   `RECONCILE_INTERVAL_MINUTES` market-hours scheduler + post-close run; wire
   `RECONCILE_STALE_MINUTES` to a market-hours degrade state. Tests = spec 7.
5. **§6 wording.** Sharpen the exposure-direction sentence in the freeze payload.
6. **§7 UI.** Standing order-lifecycle panel (orderId, timestamps, verbatim
   reasons, **manual Cancel button** on `api.cancelOrder`); `broker_manual` badge
   in position/history views; divergence-panel adoption control (if DECISION 1 =
   B).

**Nothing above is started.** Per the spec, I am stopping here to present this
audit and resolve **DECISION 1** (and DECISION 2) before writing code.
