# AUDIT — Entry Order Types + Broker-Side Cancel Propagation (Phase 1)

**Task:** (1) make the ENTRY a single atomic multi-leg diagonal like the roll,
and (2) make cancels broker-first with an async-confirm lifecycle, a
resubmission gate, and startup reconciliation.

**Scope of this document:** map the *actual* entry and cancel paths before
writing any code, and flag every place the implementation prompt's assumptions
are contradicted by what is already in the tree. Every reference is
`backend/<file>:<line>`, read from the code, not from memory. **No code was
changed to produce this report.**

The repo is at **VERSION 2.4.0** (the prompt says 2.2.0). The atomic-roll work
(`AUDIT_ATOMIC_ROLLS.md`) landed and merged, and a substantial chunk of the
atomic-open and broker-first-cancel machinery landed with it.

---

## 0. Executive summary — the two headline findings

**Headline A — the atomic entry already exists.** The prompt's premise for
problem #1 (*"Entry may be using sequential single-leg orders instead of a
single atomic multi-leg diagonal"*) is **largely REFUTED**. The
`open_position_atomic` action already builds **ONE two-leg NET_DEBIT diagonal**
(BUY_TO_OPEN LEAP + SELL_TO_OPEN weekly) on a single ticket via
`schwab_api.build_net_order`, parks it as one pending order, and commits both
legs on fill (`executor._place_live_open`, `executor.py:1517`). The UI **defaults
a fresh entry to this atomic action** whenever a weekly is selectable
(`OptionChainModal.jsx:78`). **What survives of the finding:** the app still
*also* exposes standalone single-leg `buy_leap` and `sell_short` actions that
each place an independent single-leg order (`executor._place_live`,
`executor.py:492`), and the atomic-open payload has a **strategy-type
inconsistency** with the roll (§2). So this is a *hardening + consistency*
change, not a green-field build.

**Headline B — broker-side cancel already exists too.** The prompt's premise for
problem #2 (*"Cancels are local-only … never sends the cancel to Schwab … the
order remains WORKING at the broker"*) is **REFUTED**. `executor.cancel_order`
(`executor.py:727`) already sends `DELETE …/orders/{orderId}` to Schwab
(`schwab_api.cancel_order`, `schwab_api.py:410`) **before** clearing local state,
reconciles a fill-during-cancel race, and — since it is asynchronous — polls the
order to a terminal state via `_confirm_cancel` (`executor.py:775`) before
reporting `canceled`. The orderId **is** captured from the POST `Location` header
and persisted (`schwab_api.py:401`, `save_pending_order`). So cancel propagation,
order-id persistence, and fill-during-cancel are **already done**.

**So what is this task actually?** It is closing the spec gaps the merged
implementation does not yet cover. The real, verified gaps are enumerated in §8;
the load-bearing ones are: **no resubmission gate / per-position order lock**,
**no startup reconciliation of non-terminal orders**, **order lifecycle is
implicit ad-hoc status strings, not an explicit state machine and not an
append-only event log**, **the atomic OPEN has no partial-fill / leg-imbalance
handling** (the roll path does), **no `PARTIAL_FILL_CANCELED` state**, and **none
of the Phase-2 config constants exist** (the two that do are hardcoded in
`executor.py`, un-tagged).

---

## 1. Entry order construction (Q1)

### 1.1 Two entry code paths exist
`executor.execute` (`executor.py:129`) dispatches by `action`:
- **`open_position_atomic`** → `_open_position_atomic` (`executor.py:1444`) →
  live: `_place_live_open` (`executor.py:1517`); paper/logged: `_commit_open`
  (`executor.py:1464`).
- **`buy_leap`** / **`sell_short`** (single leg) → `_place_live`
  (`executor.py:492`, live) or `_commit` (paper). Each is **one independent
  single-leg LIMIT DAY order** (`build_single_leg_order`, `schwab_api.py:460`).

### 1.2 The live atomic entry is a single NET_DEBIT diagonal
`_place_live_open` (`executor.py:1517`):
- Resolves `leap_symbol` + `short_symbol` (from supplied symbols or built via
  `occ_option_symbol`, `executor.py:1527`).
- `net_ps = round(short_ps − leap_ps, 2)` — short credit minus LEAP debit, a
  **negative** number ⇒ `build_net_order` emits **NET_DEBIT** at that magnitude
  (`executor.py:1541`, `schwab_api.py:484`).
- `legs = [("BUY_TO_OPEN", leap_symbol, contracts), ("SELL_TO_OPEN",
  short_symbol, contracts)]` → **one** `client.place_order` → parks **one**
  `pending_orders[order_id]` with `kind:"open"`, both symbols, and `net_limit`
  (`executor.py:1548`). Returns `status:"working"`.
- On fill, `_commit_open_from_pending` (`executor.py:1558`) overlays real per-leg
  fills (`_leg_fills` by `legId`→symbol, `executor.py:1586`) onto the LEAP's
  `execution_price` and the short's premium, then `_commit_open` books **exactly
  two** executions linked by a shared `open_id` (`executor.py:1482`).

**Exact JSON payload shape sent to Schwab for a live atomic entry** (from
`build_net_order`, `schwab_api.py:478`; a NET_DEBIT because short − LEAP < 0):

```json
{
  "orderType": "NET_DEBIT",
  "session": "NORMAL",
  "price": "4.30",
  "duration": "DAY",
  "orderStrategyType": "SINGLE",
  "complexOrderStrategyType": "CUSTOM",
  "orderLegCollection": [
    {"instruction": "BUY_TO_OPEN",  "quantity": 1,
     "instrument": {"symbol": "AAPL  270115C00180000", "assetType": "OPTION"}},
    {"instruction": "SELL_TO_OPEN", "quantity": 1,
     "instrument": {"symbol": "AAPL  260117C00250000", "assetType": "OPTION"}}
  ]
}
```

**Verdict on Q1:** entry is already a single atomic multi-leg order **when the
operator uses `open_position_atomic`** (the UI default). The residual issues are
(a) the legged `buy_leap`+`sell_short` path still exists and is legging-risky if
used for a fresh entry, and (b) the strategy-type inconsistency in §2.

---

## 2. All order types in use (Q2)

| Action | Builder | orderType | session | duration | orderStrategyType | complexOrderStrategyType | legs |
|---|---|---|---|---|---|---|---|
| `buy_leap` (single) | `build_single_leg_order` `schwab_api.py:460` | LIMIT | NORMAL | DAY | SINGLE | — | BUY_TO_OPEN ×1 |
| `sell_short` (single) | `build_single_leg_order` | LIMIT | NORMAL | DAY | SINGLE | — | SELL_TO_OPEN ×1 |
| `close_short` (single) | `build_single_leg_order` | LIMIT | NORMAL | DAY | SINGLE | — | BUY_TO_CLOSE ×1 |
| `close_leap` (single) | `build_single_leg_order` | LIMIT | NORMAL | DAY | SINGLE | — | SELL_TO_CLOSE ×1 |
| **`open_position_atomic`** | `build_net_order` `schwab_api.py:478` | NET_DEBIT | NORMAL | DAY | SINGLE | **`CUSTOM` (hardcoded)** | BUY_TO_OPEN + SELL_TO_OPEN |
| `roll_short` (atomic) | `build_roll_order` `schwab_api.py:500` | NET_CREDIT/NET_DEBIT by sign | NORMAL | **`config.ROLL_ORDER_DURATION`** | SINGLE | **`config.ROLL_COMPLEX_STRATEGY_TYPE`** | BUY_TO_CLOSE + SELL_TO_OPEN |
| `close_position_atomic` (exit) | `build_net_order` | NET_CREDIT/NET_DEBIT by sign | NORMAL | DAY | SINGLE | **`CUSTOM` (hardcoded)** | SELL_TO_CLOSE LEAP + BUY_TO_CLOSE short(s) |
| `roll_leap` (atomic) | `build_net_order` | NET_* by sign | NORMAL | DAY | SINGLE | **`CUSTOM` (hardcoded)** | close old LEAP + open new LEAP |

No standalone "defensive" order type — a defend is a `roll_short` with
`roll_reason:"defend"` (same payload).

**Inconsistency flagged (F-1):** `build_net_order` **hardcodes
`complexOrderStrategyType:"CUSTOM"` and `duration:"DAY"`** (`schwab_api.py:489`,
`491`), whereas the roll routes both through config
(`ROLL_COMPLEX_STRATEGY_TYPE`, `ROLL_ORDER_DURATION`, `config.py:515`,`528`). The
atomic **entry** should honor the same provenance-tagged constants (a diagonal —
different strike *and* expiry — is exactly the case where Schwab may want
`DIAGONAL` vs `CUSTOM`, the `[LIVE-VERIFY]` item). This is a Phase-2 A item.

---

## 3. Current cancel path (Q3) — premise REFUTED

Full path from "not filled within timeout" → terminal:

1. The **UI** drives the timeout, not the backend: the frontend places the order
   (`/api/execute`), polls `/api/order-status` (`order_status`, `executor.py:664`),
   and on operator/timeout drops it via `/api/order-cancel` (`cancel_order`).
   There is **no backend `ORDER_FILL_TIMEOUT_SEC` timer** — the "timeout" is a
   client concern today (gap F-6).
2. `cancel_order` (`executor.py:727`) is **broker-first**:
   - Re-reads the broker status. If **FILLED** → `order_status` settles it as a
     fill (never lost). If already **CANCELED/REJECTED/EXPIRED** → pop the stale
     pending record.
   - Otherwise still working → `client.cancel_order(...)` = **`DELETE
     …/accounts/{hash}/orders/{orderId}`** (`schwab_api.py:410-419`, accepts
     200/201/204).
   - Then `_confirm_cancel` (`executor.py:775`) **polls** the order for up to
     `CANCEL_CONFIRM_TIMEOUT_S=2.5s` every `CANCEL_CONFIRM_POLL_S=0.4s`
     (`executor.py:723`), settling a fill-during-cancel and only popping the
     pending record on a confirmed terminal state; otherwise returns
     `pending_cancel` and **keeps** the record.

**A Schwab cancel endpoint call absolutely exists** (`schwab_api.py:412`), it is
sent **before** local state is cleared, and the local record is **never** dropped
without a broker-confirmed terminal state. The prompt's "cancels are local-only"
premise is **REFUTED**. Tests already cover the clean cancel, the
fill-before-cancel race, and the not-confirmed `pending_cancel` case
(`test_cfm.py:752`,`772`,`801`).

**What is missing vs Phase 2 B:** the confirm window is a **hardcoded 2.5s
wall-clock** in `executor.py`, not the provenance-tagged
`CANCEL_POLL_INTERVAL_SEC` / `CANCEL_POLL_MAX_ATTEMPTS` config the spec wants, and
it is **not a mocked clock** (real `time.sleep`/`time.monotonic`,
`executor.py:784-787`) — so the bounded-retry/backoff policy and its offline
testability are gaps (F-5).

---

## 4. Order ID persistence (Q4) — confirmed present

`place_order` (`schwab_api.py:392`) parses the **`Location` header** on the 201
into `orderId` (`schwab_api.py:401-403`). Every live placer persists it:
`pending_orders[order_id] = {...}` via `save_pending_order`
(`logging_handler.py:272`) into `state.json` (open `executor.py:1548`, single-leg
`:523`, roll/exit/leap-roll similarly). If Schwab returns no id the placer raises
`SchwabError` and nothing is parked (`executor.py:1546`). **Cancel propagation is
therefore possible today and is used.** This is **not** finding #1 — it already
works.

Caveat (F-7): if the app **crashes between `place_order` returning and
`save_pending_order` committing**, the broker holds a WORKING order the app has no
record of. There is no post-hoc "open orders" sweep against Schwab to recover it
(ties into the startup-reconciliation gap, §6/F-4).

---

## 5. Retry / resubmit path (Q5) — the real gap

**Nothing today enforces a per-position resubmission gate.** What *partially*
constrains re-submission:
- **Reconciliation freeze:** new-risk actions (`buy_leap`, `sell_short`,
  `roll_short`, `roll_leap`, `open_position_atomic`) are blocked on a
  `needs_review` position (`FROZEN_BLOCKED_ACTIONS`, `executor.py:30`;
  `_enforce_not_frozen`). But an unfilled/pending order does **not** freeze the
  position, so this does not gate resubmission.
- **Broker-side collision:** Schwab would reject a second order that collides —
  but that is the broker saving us, not an app invariant, and it does not cover
  a not-yet-canceled order that the app *thinks* is gone.

There is **no `pending_orders`-aware guard** in `execute()`: a caller can place a
second `open_position_atomic` for the same ticker while the first is still
`working` or `pending_cancel`. There is **no per-position order lock persisted in
`state.json`**, **no `MAX_RESUBMIT_ATTEMPTS`**, and **no
`NO_RESUBMIT_BEFORE_TERMINAL` invariant** (grep: these names exist nowhere in
`backend/`). This is the load-bearing Phase-2 B rule 5 / 5-lock work (F-3).

---

## 6. Partial fill handling (Q6) — honestly, mostly unhandled for the ENTRY

- **Atomic OPEN partial fill:** `order_status` commits an open **only on
  `raw=="FILLED"`** (`executor.py:678-681`); any `filledQuantity>0` while still
  WORKING returns a bare `{"status":"working"}` (`executor.py:695`). There is
  **no whole-unit partial commit and no leg-imbalance detection for opens** —
  unlike the roll, which has a full `_roll_order_status` lifecycle (whole-unit
  partials, leg-imbalance → `_freeze_for_leg_imbalance`, rejection fallback,
  `executor.py:618-661`). So a two-leg entry that fills one leg and not the other
  is **not** detected as an unbalanced (naked-ish) position today. This is a real
  finding (F-2).
- **Partial fill DURING cancel:** `cancel_order`/`_confirm_cancel` settle a full
  **FILLED**-during-cancel (`executor.py:759`,`792`) but do **not** distinguish
  `filledQuantity>0 AND terminal` → there is **no `PARTIAL_FILL_CANCELED` coded
  state**, no delta-coverage trip, no defensive-review flag on that branch (F-2).
- **Single-leg partial:** `_commit_from_pending` books the whole `contracts` at
  the average fill; a partial-quantity single-leg fill is not modeled.

Verdict: "nothing handles the entry partial / partial-on-cancel case" is the
honest finding. The roll path is the template to generalize.

---

## 7. Order lifecycle representation & append-only invariant (cross-cutting)

- `pending_orders` is a **mutable dict keyed by order_id**
  (`logging_handler.py:272-300`): `save`/`get`/`pop`. State transitions **mutate
  or delete** this dict; they are **not** appended as immutable events.
- `recompute_derived` (`logging_handler.py:315`) rebuilds **only** the
  theta_ledger + extrinsic_payback from `executions`/`positions`. It does **not**
  derive any order state — order state is the live `pending_orders` dict, not a
  replay of an event log.
- The only append-only order artifact is `order_receipts` (`save_order_receipt`,
  `logging_handler.py:279`) — capped at 200, written **on fill only**, for
  `fill_verify.py`. It is not a transition log (no SUBMITTED→WORKING→… events,
  no prior/new state, no raw broker status per transition).

So Phase-2 B rule 7 (*every transition is an append-only event with timestamp,
orderId, prior state, new state, raw broker status; `recompute_derived` derives
current order state*) is **entirely unbuilt** — order state is imperative, not
derived (F-8). The state machine of Phase-2 B (SUBMITTED → WORKING → {FILLED |
CANCEL_REQUESTED → PENDING_CANCEL → …}) exists only as **implicit ad-hoc status
strings** returned by `order_status`/`cancel_order` (`working`, `filled`,
`canceled`, `rejected`, `pending_cancel`, `partially_filled`), never as a named,
enforced state enum (F-9).

**Startup reconciliation (rule 6):** app startup runs only `log.startup_check()`
(durability temp-file cleanup + eager load, `app.py:1308`) and the alert
scheduler. `reconcile.py` reconciles **positions** against the broker but **never
re-polls non-terminal `pending_orders`** (grep in `reconcile.py`: no
`pending_order`/`order_status` reference). A crash with a WORKING order in
`pending_orders` leaves it un-reconciled until someone happens to poll it, and no
gate stops new activity on that position meanwhile (F-4).

---

## 8. Gaps to close (what Phase 2 actually implements)

| # | Requirement (Phase-2 ref) | Status in tree | Work |
|---|---|---|---|
| F-1 | A: entry strategy type / duration as config (like roll) | `build_net_order` hardcodes `CUSTOM`/`DAY` | route atomic-open (and reuse) through provenance-tagged `ENTRY_COMPLEX_STRATEGY_TYPE`/duration constants; `[LIVE-VERIFY]` DIAGONAL vs CUSTOM |
| F-2 | B rules 3–4: entry partial fill + leg-imbalance + `PARTIAL_FILL_CANCELED` | roll has it; **open does not**; no partial-on-cancel state | generalize `_roll_order_status` leg-imbalance/whole-unit logic to opens; add `PARTIAL_FILL_CANCELED` coded state → delta-coverage trip + alert, **flag only** |
| F-3 | B rule 5: resubmission gate + per-position lock in `state.json` | **absent** | persist a per-position-intent order lock; block resubmit until prior order broker-terminal + reconciled; `NO_RESUBMIT_BEFORE_TERMINAL` named invariant; `MAX_RESUBMIT_ATTEMPTS` |
| F-4 | B rule 6: startup reconciliation of non-terminal orders | **absent** | on start, re-poll every non-terminal `pending_orders` entry to a broker verdict before allowing new activity for that position |
| F-5 | B rule 2 + C: cancel poll as config + bounded backoff + mockable clock | hardcoded 2.5s/0.4s real-clock in `executor.py` | `CANCEL_POLL_INTERVAL_SEC`/`CANCEL_POLL_MAX_ATTEMPTS` in `config.py`; inject clock/sleep for offline tests |
| F-6 | C: `ORDER_FILL_TIMEOUT_SEC` (no-fill → initiate cancel) | client-driven, no backend constant | provenance-tagged constant + the code that consults it |
| F-7 | Crash between place & persist orphans a broker order | no recovery sweep | covered by F-4 startup sweep (open-orders reconcile), plus persist intent **before** place where feasible |
| F-8 | B rule 7: transitions as append-only events; state derived | mutable dict; only fill receipts appended | append `order_events`; have `recompute_derived` derive current order state from the log |
| F-9 | B: explicit named state machine | implicit status strings | encode the state enum + legal transitions as pure functions (testable) |
| F-10 | D: config constants provenance-tagged, offline tests for every branch | `CANCEL_CONFIRM_*` un-tagged in `executor.py`; no fixture tests for the new branches | move/add tagged constants; fixture-driven mocked-Schwab + mocked-clock tests (the 10 cases in the prompt) |
| F-11 | A: legged `buy_leap`+`sell_short` fresh-entry still legging-risky | both single-leg actions remain | leave the actions (scale-in/leg-repair use them) but ensure a *fresh two-leg entry* routes atomic; document the intent |

## 9. Reuse map (do NOT duplicate)

- **Order construction:** `build_net_order` (`schwab_api.py:478`) is the shared
  multi-leg builder — extend it (or a thin entry wrapper) for the strategy-type
  constant rather than duplicating. `occ_option_symbol`, `_leg_fills`,
  `_commit_open` are all reusable.
- **Cancel lifecycle:** `cancel_order` + `_confirm_cancel` are the broker-first
  spine — extend them to bounded/config'd polling and the partial-on-cancel
  state, don't rewrite.
- **Leg-imbalance / whole-unit partials:** `_roll_order_status`,
  `_roll_leg_filled_qty`, `_freeze_for_leg_imbalance` are the template to
  generalize to opens (F-2).
- **Alerts:** `alerts.record_event(...)` (`executor.py:823` pattern) is the alert
  engine; the delta-coverage floor is `LEAP_DELTA_FLOOR=0.50`
  (`option_chain.py:374`, alerts at `alerts.py:182`,`631`).
- **Fill audit:** `fill_verify.py` + `order_receipts` already diff live fills
  against Schwab — the new event log complements it, not replaces it.

## 10. Config constants to add (provenance-tagged) — proposal for Phase 2 C

| Constant | Proposed value | Provenance |
|---|---|---|
| `ORDER_FILL_TIMEOUT_SEC` | `45` | PROPOSED_DEFAULT — no-fill wait before initiating cancel |
| `CANCEL_POLL_INTERVAL_SEC` | `0.4` | PROPOSED_DEFAULT — matches current `CANCEL_CONFIRM_POLL_S` |
| `CANCEL_POLL_MAX_ATTEMPTS` | `6` | PROPOSED_DEFAULT — ~2.5s window at 0.4s, matches today |
| `MAX_RESUBMIT_ATTEMPTS` | `3` | PROPOSED_DEFAULT — per position intent per session |
| `REPRICE_ON_RETRY` | `"none"` | PROPOSED_DEFAULT — never silently chase price; comment the tradeoff |
| `ENTRY_COMPLEX_STRATEGY_TYPE` | `"CUSTOM"` | PROPOSED_DEFAULT / **[LIVE-VERIFY]** DIAGONAL vs CUSTOM |
| `NO_RESUBMIT_BEFORE_TERMINAL` | `True` | HARD_CFM_RULE — named invariant, checked in code |

---

## Phase 2 — IMPLEMENTED

Approval was given. Phase 2 landed in the same branch (see CHANGELOG.md, "Order
lifecycle: entry order type + broker-side cancel/retry state machine"). Summary of
how each finding was closed:

| # | Resolution |
|---|---|
| F-1 | `build_net_order` takes `complex_strategy_type`/`duration`; entry routes `ENTRY_COMPLEX_STRATEGY_TYPE`/`ENTRY_ORDER_DURATION` (exit/LEAP-roll defaults unchanged) |
| F-2 | `PARTIAL_FILL_CANCELED` + fill-during-cancel handled on the cancel path: freeze + delta-coverage review + CRITICAL alert; never auto-fix |
| F-3 | Per-position-intent lock in `state.json` + `order_lifecycle.check_resubmit` (`NO_RESUBMIT_BEFORE_TERMINAL`, `MAX_RESUBMIT_ATTEMPTS`); surfaced as HTTP 409 |
| F-4/F-7 | `executor.reconcile_pending_orders_on_startup()` wired into app startup; unreachable orders hard-lock (`LOCKED_UNKNOWN`) |
| F-5/F-6 | Cancel poll bounded by `CANCEL_POLL_*` (interval 0 in tests = mocked clock); `ORDER_FILL_TIMEOUT_SEC` added |
| F-8/F-9 | Append-only `order_events`; `recompute_derived` derives `order_state`; named coded states in `order_lifecycle.py` |
| F-10 | `backend/test_order_lifecycle.py` — pure state machine + the 10 required branches, all offline |
| F-11 | **Decision taken:** legged `buy_leap`/`sell_short` actions kept (scale-in / leg repair); a lone long LEAP is not a naked-short risk and the naked-short guard already exists — no new hard refusal added; atomic routing for fresh entry stays the UI default |
