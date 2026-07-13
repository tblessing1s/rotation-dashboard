# AUDIT — Incident Defect Hotfix: Roll-Order Path

**Scope:** tactical root-cause of the four defects (D1–D4) from the live roll incident.
Written before any code change, per Phase 0. File/line references are to the tree at
audit time (branch `claude/fixes-needed-r3432c`).

**Read-this-first finding:** this codebase is materially further along than the hotfix
prompt assumes. A full order lifecycle already exists — `order_lifecycle.py` (coded
states + legal transitions), pending-order persistence, per-intent resubmit locks,
`_roll_order_status` (whole-unit partial fills, leg-imbalance freeze, rejection
fallback), `cancel_order` (broker-first, fill-during-cancel aware), and startup
reconciliation. **Much of F2/F4 is already implemented for the happy path.** This audit
is therefore precise about the *residual* gaps that actually reproduce the incident,
rather than re-describing machinery that already exists. The fixes must slot into the
existing lifecycle, not fork a parallel one.

---

## Incident recap (mapped to code)

A roll (`action: "roll_short"`) was submitted repeatedly from the app. At least one
attempt was accepted and working at Schwab while the app showed failure; the app held
no orderId; the user refreshed/retried, risking duplicate submissions; the working order
had to be cancelled manually at Schwab. The four defects below fully explain that
sequence.

---

## D1 — Premium/price construction

**Where the roll net price is built:** `executor.py:1644-1647` in `_place_live_roll`:

```python
buyback     = float(payload.get("close_price_per_share") or 0)   # 1644
new_premium = float(payload.get("premium_per_share")   or 0)     # 1645
net = round(new_premium - buyback, 2)                            # 1646
order = schwab_api.build_roll_order(contracts, close_symbol, open_symbol, net)  # 1647
```

`build_roll_order` (`schwab_api.py:508-540`) then chooses `orderType` from the sign of
`net` and serializes the price as `f"{abs(float(net_price)):.2f}"` (`schwab_api.py:524`).
The per-leg mids (`close_price_per_share`, `premium_per_share`) are computed on the
**frontend** (`RollModal.jsx:96-112`) and arrive in the payload — the backend does no
independent quote read or validation for the roll price.

**Which candidate root causes actually apply:**

- **(a) Tick-increment conformity — APPLIES.** `round(net, 2)` snaps to whole cents, not
  to the option's valid tick. Options priced ≥ $3.00 that are *not* in the penny pilot
  trade in **$0.05** increments; a net like `2.37` or `2.43` is a valid two-decimal
  number but an **off-tick** limit that Schwab can reject or silently reprice. There is
  no `round_to_tick`, and the same cent-rounding is in `build_single_leg_order`
  (`schwab_api.py:467`) and `build_net_order` (`schwab_api.py:496`).
- **(b) NET_CREDIT/NET_DEBIT sign convention — PARTIALLY APPLIES (latent).**
  `build_roll_order` derives `orderType` from `sign(net)` internally, so the ticket is
  self-consistent. But nothing asserts the derived direction matches the operator's
  *intent*: if the frontend sends a malformed pair (see (c)), the sign flips silently and
  a roll the operator staged as a credit is transmitted as a **NET_DEBIT**. There is no
  single place that computes direction from the legs and asserts it.
- **(c) Missing/zero/one-sided/stale quote — APPLIES, and is the most dangerous.** Each
  input is coerced with `... or 0`. If `premium_per_share` is missing/None (one-sided or
  stale quote on the sell-to-open leg), `new_premium = 0` and `net = -buyback` → a
  **NET_DEBIT of the buyback price** is transmitted from a bad quote. If both are missing,
  `net = 0.00` → a zero-priced limit. No two-sided / nonzero / freshness check exists
  anywhere on the order-construction path. This matches the user-visible symptom
  ("the premium wasn't getting set properly").
- **(d) Float-vs-Decimal serialization — APPLIES (contributing).** `net` is a binary
  float; `round(x, 2)` then `%.2f` mostly masks artifacts, but mid math upstream
  (`(bid+ask)/2`, e.g. `schwab_api.py:626`) can yield values like `2.35000000001` that
  should be tick-rounded with `Decimal`, not float `round`. Not the primary cause, but
  the fix (Decimal throughout) closes it for free.

**Root cause (D1):** the roll net limit is `round(new_premium − buyback, 2)` over
frontend-supplied, unvalidated per-leg prices — no tick rounding, no Decimal, no
two-sided/nonzero/freshness gate, and no independent direction assertion. A bad or
one-sided quote produces a malformed, wrong-direction, or zero net limit that is
submitted rather than refused.

---

## D2 — False failure on a successful acknowledgment

**Order-id extraction:** `schwab_api.py:400-403` in `place_order`:

```python
if resp.status_code in (200, 201):
    location = resp.headers.get("Location") or resp.headers.get("location") or ""
    order_id = location.rstrip("/").rsplit("/", 1)[-1] if location else None
    return {"orderId": order_id, "location": location}
```

**Two paths turn a real acceptance into a displayed failure:**

1. **2xx with no/odd `Location` header.** The order is accepted at Schwab, but
   `order_id = None`. Back in `_place_live_roll` (`executor.py:1649-1651`):

   ```python
   order_id = placed.get("orderId")
   if not order_id:
       raise schwab_api.SchwabError("Schwab accepted the roll but returned no order id")
   ```

   The `raise` fires **before** `log.save_pending_order(...)` (`executor.py:1653`). The
   identical pattern is in the single-leg path (`executor.py:813-815` → `:827`).

2. **Timeout / connection drop after the request reached Schwab.** `place_order` uses
   `requests.post(..., timeout=30)` (`schwab_api.py:395-399`). A read timeout or reset
   *after* Schwab accepted the order raises, and the order is live but unacknowledged
   locally. (The frontend `fetch` also has its own 60s abort — `api.js:11,23-27` — which
   produces the same "lost response, order maybe live" condition one layer up.)

**Where it becomes "failed":** every exception unwinds to the catch-all at
`app.py:505-506` → `_err(e)` (`app.py:46-47`) → **HTTP 500 `{"error": ...}`**. The
frontend renders any thrown/500 response as a red **"{label} failed"** toast
(`orderFlow.js:33-36`). Nothing distinguishes "Schwab explicitly rejected" from "we lost
the response" — both display as failure.

**Enumerated response shapes the parser assumes** (all in `place_order`,
`schwab_api.py:392-405`): success is *only* `status ∈ {200,201}` **and** a parseable
`Location` header; everything else (including a 2xx body-only ack, or a 2xx with the id
in the JSON body rather than the header) is treated as failure or id-less. The multi-leg
ack shape is **not** covered by a captured fixture (see §"Artifacts").

**Root cause (D2):** success is defined as "2xx **and** a parseable `Location` header,"
and any deviation — missing header, timeout, or unparseable body — raises and is rendered
as an explicit failure. There is no UNKNOWN/"confirming with broker" state at the
placement boundary; the code can only say "working" or "failed."

---

## D3 — Refresh-as-retry

**Frontend mechanics (confirmed):** there is no `<form>` around the execute buttons; the
POST is a `fetch` (`api.js:95`, via `orderFlow.js:32`); the `?action/?ticker` deep link is
query-stripped on consume (`App.jsx:96-98,106-108`); the service worker is a no-op
pass-through (`public/sw.js:21`); and no `useEffect` calls `execute` (all on-mount effects
are GET reads). **So a literal browser refresh does not natively re-POST an order.**

**The actual re-fire vector is retry-as-duplicate, and it is unguarded:**

- `orderFlow.js:33-36` reports a lost/timed-out response as a hard **"failed"** even
  though the backend may have executed the roll. The operator, told it failed, clicks
  Roll again → a **second** buy-to-close + sell-to-open.
- The submit-button `busy` guard is **per-instance React state** (`RollModal.jsx:122,128`;
  `OptionChainModal.jsx:204,210`) — lost on modal unmount / navigation / refresh, so it
  does not protect across reloads. A fast double-click through the `LiveOrderConfirm`
  dialog can also double-fire before `busy` re-renders (`tradeMode.jsx:119`).
- **No idempotency key exists anywhere.** No `client_order_ref` / `idempotency` / request
  id in any payload (`api.js:95`, `RollModal.jsx:96-112`, `OptionChainModal.jsx:128-181`)
  or on the backend.

**Backend has no dedupe for the roll path either.** The resubmit gate `_guard_resubmit`
(`executor.py:279-306`) only covers `_LOCKED_INTENTS = {"buy_leap", "sell_short",
"open_position_atomic"}` (`executor.py:265`) — **`roll_short` is not in that set, and
`_place_live_roll` never calls `_guard_resubmit`** (contrast `_place_live` at
`executor.py:799` and `_place_live_open` at `executor.py:1976`). Even the belt-and-braces
"pending order already exists for this ticker+action" check lives *inside*
`_guard_resubmit` (`executor.py:304-306`), so it never runs for a roll. And in the D2
false-failure case `save_pending_order` was never reached, so there is no pending record
to detect regardless.

**Root cause (D3):** submission is not idempotent. There is no client-generated order
reference, the roll path has no server-side resubmit/pending guard at all, and the
frontend's only guard is per-instance component state. A lost response (D2) plus a manual
retry — or a double-click — produces duplicate live rolls with nothing on either side to
collapse them to one.

---

## D4 — orderId not captured/persisted

**Happy path already persists it:** on a clean 2xx-with-Location, `_place_live_roll`
saves `order_id` into `state["pending_orders"]` via `log.save_pending_order`
(`executor.py:1653-1660`; storage at `logging_handler.py:296-300`), and the poll/settle
lifecycle (`order_status` → `_roll_order_status`, `executor.py:923-1003`) and
`cancel_order` (`executor.py:1046-1099`) both key off it. So D4 is **not** a blanket
"orderId never captured" defect in the current code.

**The residual gap is exactly the D2 failure cases:** when the ack reaches the app but
lacks a `Location` header, or the response is lost to a timeout, the code raises
**before** any persistence. No orderId, no pending record, no receipt → cancel-from-app
is structurally impossible and startup reconciliation has nothing to poll. Confirming §5
of the prompt: in the incident the ack (or its absence) was **swallowed by D2** — the
orderId either sat only in a header the code didn't read, or in a response the timeout
discarded — and D4 is the persistence half of that same bug.

**Root cause (D4):** orderId persistence happens *after* successful id-extraction and
dict construction, so every D2 failure branch loses it. There is no pre-submission
durable record keyed by an app-generated reference, which is the only structure that
survives a header-less ack or a timeout.

---

## state.json impact (prompt Phase 0 item 6)

**Did the failed attempts write anything?** For the D2 false-failure branch: **no.** The
`raise` precedes `save_pending_order`, and executions are only committed on a confirmed
fill (`_commit_roll_from_pending`, via `order_status`). So a spuriously-"failed" roll
leaves **no** phantom execution and **no** pending record in `state.json` — the orphan
lived only at Schwab. There is therefore **no derived-metric corruption** from the
incident to remediate here; cleanup of any real orphan is a reconciliation-prompt concern.
One caveat worth flagging: because the roll path skips `_guard_resubmit`, a *successful*
first roll followed by a retry (before the first is polled) could write a **second**
pending record and, on fill, a second execution — that is a live duplication risk, not a
past-corruption cleanup, and F3 closes it.

**Proposed home for the durable order record.** The existing durable store is
`state["pending_orders"]` (keyed by Schwab `order_id`) inside `state.json`, written
atomically under a lock (`logging_handler.py:164-198,296-300`). The hotfix needs a record
that exists **before** an order_id is known, keyed by the app-generated
`client_order_ref`. Recommended: a sibling map `state["order_submissions"]`
(client_order_ref → {ref, ticker, action, status, order_id, broker_reason, request,
timestamps}), written pre-submission and updated in place. This keeps operational order
records in the same atomic store the lifecycle already trusts, is the natural key for F3
idempotency, and is a structure the forthcoming lifecycle-reconciliation system can adopt
unchanged (it becomes the client-ref index over the order log). It stays out of
`executions`/`positions`, so no derived metric is touched. (An entirely separate JSON file
is also acceptable per the prompt; co-locating in the already-atomic `state.json` under a
new top-level key is lower-risk and consistent with `pending_orders`/`order_events`/
`order_locks`/`order_receipts` all living there.)

---

## Captured incident artifacts

Searched for saved raw Schwab responses / incident captures (`*incident*`, `*capture*`,
`*response*`, order fixtures under `backend/fixtures/`): **none found.** The only fixtures
are `backend/fixtures/regime/`. Therefore the multi-leg ack and the explicit-rejection
bodies used in tests 1 and 4 must be **synthesized behind the mock client** and every
Schwab response-schema assumption they encode (Location-header format, order-id-in-body
vs header, rejection reason field name, per-leg fill quantity fields) is tagged
**LIVE_VERIFY** for confirmation against a real capture. No response body is invented as
fact; unknown shapes are stubbed behind the existing `SchwabClient` interface.

---

## Config constants to add (shared module, provenance-tagged)

Per the prompt, in `config.py` alongside the existing roll/cancel block
(`config.py:551-644`):

```
QUOTE_MAX_AGE_FOR_ORDER_SECONDS = 60   # PROPOSED_DEFAULT
UNKNOWN_STATUS_RETRY_SECONDS    = 10   # PROPOSED_DEFAULT
UNKNOWN_STATUS_MAX_ATTEMPTS     = 6    # PROPOSED_DEFAULT
ORDERID_PERSIST_FIRST           = True # HARD_CFM_RULE
NO_FAILURE_WITHOUT_VERIFICATION = True # HARD_CFM_RULE
```

(The option tick table is new; it will be tagged `PROPOSED_DEFAULT` / `LIVE_VERIFY`
against Schwab's published increment rules — $0.05 for options priced ≥ $3.00, $0.01 for
< $3.00, with penny-pilot names as a documented assumption to verify.)

---

## Root-cause summary (one line each)

- **D1:** roll net limit = `round(new_premium − buyback, 2)` over unvalidated frontend
  mids — no tick rounding, no Decimal, no two-sided/nonzero/fresh-quote gate, no
  direction assertion. (`executor.py:1644-1647`, `schwab_api.py:508-540`)
- **D2:** success is defined as "2xx **and** parseable `Location` header"; missing header /
  timeout / unparseable body raises and renders as an explicit "failed" — no UNKNOWN
  state. (`schwab_api.py:400-403`, `executor.py:1649-1651`, `app.py:505-506`,
  `orderFlow.js:33-36`)
- **D3:** submission is non-idempotent — no `client_order_ref`, the roll path skips
  `_guard_resubmit` entirely, and the only frontend guard is per-instance state; a lost
  response + retry (or double-click) duplicates the roll. (`executor.py:265,279-306,1625`,
  `orderFlow.js:33-36`)
- **D4:** orderId is persisted only *after* successful extraction, so every D2 failure
  branch loses it; there is no pre-submission durable record keyed by an app-generated
  reference. (`executor.py:1649-1660`)

---

## Proposed fix surface (for approval — no code written yet)

- **F1 (D1):** new pure `order_pricing.py` (or additions to `schwab_api.py`) with
  `round_to_tick(price, tick) -> Decimal`, a tick table, a `net_roll_price(legs) ->
  (Decimal, orderType)` that computes direction once and asserts it, and a pure
  `validate_order_quotes(legs, quotes, now) -> [] | [reasons]` (two-sided, nonzero, age ≤
  `QUOTE_MAX_AGE_FOR_ORDER_SECONDS`). Wire into `_place_live_roll` (and reuse in the atomic
  entry/exit builders). A direction↔orderType contradiction is an assertion failure, not a
  submission.
- **F2 (D2/D4):** pre-submission `client_order_ref` + durable `order_submissions` record
  written **before** `place_order`; on any response, persist orderId **first**; map
  no-response/timeout/unparseable → **UNKNOWN** ("confirming with broker…"), never
  "failed"; display an explicit failure only when Schwab returned a rejection (persist its
  reason verbatim); add a bounded manual "check status" (by orderId, else recent-orders
  match) and keep the existing cancel-by-orderId + post-cancel re-query.
- **F3 (D3):** make `/api/execute` idempotent on `client_order_ref` (repeat ref → return
  existing record, no re-submit); frontend generates the ref at staging, POSTs once, then
  only reads status; a refresh mid-flight lands on a status view.

**Stopping here for review before implementing, per Phase 0.**
