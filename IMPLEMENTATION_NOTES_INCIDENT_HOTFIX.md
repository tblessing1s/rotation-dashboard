# IMPLEMENTATION NOTES — Incident Defect Hotfix (Roll-Order Path)

Companion to `AUDIT_INCIDENT_HOTFIX.md`. What was built, which D1 candidates actually
applied, what still needs live verification, and what the forthcoming
order-lifecycle-reconciliation system should inherit or revisit.

Scope was held to the **roll (`roll_short`) path**, backend-side quote validation, per
the two approval decisions. No live Schwab call is made in implementation or tests.
Until this ships green and the operator re-enables it, order submission stays
advisory-only.

## Confirmed root causes (what actually reproduced the incident)

- **D1 — price.** Confirmed candidates: **(a)** off-tick — `round(net, 2)` snapped to
  cents, not the option tick; **(c)** bad-quote — `... or 0` coercion turned a
  missing/one-sided quote into a zero or **direction-flipped** net that was submitted;
  **(d)** float serialization — mid math ran in binary float. Candidate **(b)** (sign
  convention) was latent: `build_roll_order` was internally self-consistent, but nothing
  asserted the constructed direction against the operator's intent, so (c) could flip a
  credit to a debit silently.
- **D2 — false failure.** Confirmed: success was defined as "2xx **and** a parseable
  `Location` header." A header-less 2xx ack raised `"…returned no order id"`; a POST
  timeout after the order reached Schwab raised too. Both unwound to `app.py`'s
  catch-all → HTTP 500 → the frontend's red "failed" toast. No UNKNOWN state existed.
- **D3 — refresh/retry.** Confirmed: no idempotency key anywhere; the atomic-roll path
  never called `_guard_resubmit` (and `roll_short ∉ _LOCKED_INTENTS`). A lost response
  (D2) + a manual retry, or a double-click, placed a second roll.
- **D4 — orderId.** Confirmed: persisted only *after* a clean id-extraction, so every D2
  failure branch lost it. No pre-submission durable record existed.

## What was built

### F1 — price construction hardening (`order_pricing.py`, new pure module)
- `round_to_tick(price, tick)` + `tick_for_price` + a config tick table
  (`OPTION_TICK_*`). All order prices are `Decimal`; `format_price` serializes an exact
  2-dp string (no `2.35000001`).
- `net_credit_debit(buyback, new_premium)` is the **single source of direction** —
  returns `(abs_price, NET_CREDIT|NET_DEBIT)`. `build_roll_order` now takes the derived
  `order_type` explicitly and **asserts** it matches the price sign (a contradiction is
  an `AssertionError`, never a submission).
- `validate_roll_quotes(...) -> [] | [reasons]` is a pure pre-submit gate: both legs must
  have a two-sided, nonzero, non-crossed quote, fresh within
  `QUOTE_MAX_AGE_FOR_ORDER_SECONDS`. Reasons name the specific leg and problem.
- **Wiring (decision: backend re-reads & validates):** `_place_live_roll` now re-reads
  both legs' quotes via `client.get_quotes`, validates them, and recomputes the net from
  the re-read mids. A bad/stale quote **refuses to construct** (`ValueError`, never a
  submission). If the operator-staged mids imply a direction, it is asserted against the
  re-read direction — a flip (quotes moved under the ticket) refuses rather than submits.

### F2 / F4 — truthful response + orderId-first (`schwab_api.py`, `executor.py`, `logging_handler.py`)
- New durable store `state["order_submissions"]` keyed by `client_order_ref`
  (`save/update/get/list_order_submission`), co-located with `pending_orders`. Written
  **before** the broker call (status `SUBMITTING`).
- New `SchwabClient.submit_order` returns a **structured outcome** instead of raising:
  `accepted` (2xx; `order_id` from Location, `None` if absent), `rejected` (explicit
  400/422 with the body as a **verbatim** reason), or `unknown` (timeout / network / auth
  / 5xx — the order may be live). This is the D2 hinge: rejection ≠ no-confirmation.
- `_place_live_roll` writes the orderId onto the durable record **first**
  (`ORDERID_PERSIST_FIRST`), then the pending record. A 2xx with no id → **UNKNOWN**
  (never failed) + alert; timeout/5xx → **UNKNOWN** + alert; explicit rejection → the
  verbatim reason. The frontend renders UNKNOWN as "confirming with broker…", a rejection
  as its reason, and **never** "failed" for anything Schwab didn't explicitly reject.
- Minimal, **manual** status check: `executor.submission_status(ref)` /
  `GET /api/order-submission-status?ref=…` — re-polls a known orderId and syncs the
  record; for an id-less UNKNOWN it recovers the id by **recent-orders match**
  (`client.list_orders` + `_match_recent_order` on the leg symbols), bounded by
  `UNKNOWN_STATUS_MAX_ATTEMPTS` / `UNKNOWN_STATUS_RETRY_SECONDS`. **No submission is ever
  auto-retried** — only the read-back that resolves an UNKNOWN.
- Cancel-by-orderId is unchanged (already broker-first, fill-during-cancel aware); the
  submission record is synced to the settled outcome via `_sync_roll_submission`.

### F3 — idempotent submission decoupled from page lifecycle
- `_place_live_roll` **requires** a `client_order_ref` and short-circuits on a repeat:
  any existing record for the ref returns its truthful state **without re-submitting**.
  A refresh/retry storm on one ref places exactly one order (test D3).
- Frontend: `RollModal` generates the ref once when the roll is staged
  (`crypto.randomUUID`), persists it in `sessionStorage` (so a mid-flight reload resumes
  the same ref, not a new order), includes it in the payload, and retires it only on a
  confirmed terminal outcome. The submit button already disabled on first press; the new
  guarantee is that render/reload can't reach Schwab because (a) no effect submits, and
  (b) the backend collapses duplicates on the ref. `orderFlow.js` now treats a lost
  response for a ref-keyed order as "confirming…" (poll `submissionStatus`), not "failed".

## Config added (all provenance-tagged, shared names the lifecycle prompt uses)
```
QUOTE_MAX_AGE_FOR_ORDER_SECONDS = 60   # PROPOSED_DEFAULT
UNKNOWN_STATUS_RETRY_SECONDS    = 10   # PROPOSED_DEFAULT
UNKNOWN_STATUS_MAX_ATTEMPTS     = 6    # PROPOSED_DEFAULT
ORDERID_PERSIST_FIRST           = True # HARD_CFM_RULE
NO_FAILURE_WITHOUT_VERIFICATION = True # HARD_CFM_RULE
OPTION_TICK_BREAKPOINT = 3.00 / OPTION_TICK_BELOW = 0.01 / OPTION_TICK_ABOVE = 0.05
                                       # PROPOSED_DEFAULT / LIVE_VERIFY
```

## Schwab schema fields still needing live verification (LIVE_VERIFY)
No raw incident response bodies were captured in the repo, so these assumptions are
stubbed behind the `SchwabClient` interface and must be confirmed against a live account
before this path is trusted unsupervised:
1. **Order-id location.** Assumed only in the `Location` response header. If Schwab also
   (or instead) returns the id in the 2xx JSON body, read it there too — that alone would
   turn many of today's UNKNOWNs into immediate WORKING.
2. **Explicit-rejection status codes.** `submit_order` treats **400/422** as explicit
   rejections and everything else (401/403/408/429/5xx/network) as UNKNOWN. Confirm the
   real set of order-rejection codes and that auth/rate errors never carry an order id.
3. **`list_orders` contract.** Query params (`fromEnteredTime`/`toEnteredTime`,
   maxResults, ISO-8601 format) and the response array shape are unconfirmed. A wrong
   assumption degrades safely (no match → stays UNKNOWN), never a false positive.
4. **Recent-orders match key.** Currently matches on the two leg OCC symbols. Confirm the
   symbol format Schwab echoes in `orderLegCollection` and consider tightening the match
   with the entered-time window and net price once the field names are known.
5. **Tick table.** $0.05 at/above $3.00 is the conservative venue minimum; penny-pilot
   names quote finer. Confirm per-symbol increments and, importantly, the **NET
   (complex-order) increment** — the current code tick-rounds the net by its own
   magnitude, which is a documented assumption, not a verified rule.
6. **Per-leg partial-fill quantity fields** (inherited from the existing roll lifecycle)
   remain LIVE_VERIFY.

## For the lifecycle-reconciliation build to inherit or revisit
- **Adopt unchanged:** `order_pricing.py` (pure), the `order_submissions` store keyed by
  `client_order_ref`, `submit_order`'s structured outcome, and `submission_status`'s
  UNKNOWN-recovery shape. These were built to be the lifecycle system's pre-submit
  validator, client-ref index, and truthful ack handler.
- **Revisit / extend (deliberately out of scope here):**
  - Extend the `client_order_ref` idempotency and F1 validation to the other live paths
    (single-leg, atomic open/close, leap roll). Only the roll path was hardened.
  - A background poller/reconciler that walks `order_submissions` still in
    SUBMITTING/UNKNOWN and resolves them without an operator click (this hotfix only added
    the *manual* check + startup reconcile of `pending_orders`).
  - Fold `order_submissions` into the append-only order-event log rather than a mutable
    map, once the lifecycle log is the source of truth.
  - Race remediation on cancel-vs-fill beyond surfacing the truth (explicitly not built).
  - `_guard_resubmit` still keys ENTRY intents only; the roll path is now guarded by the
    ref instead. The lifecycle system should unify these into one gate.

## state.json impact
No phantom executions or partial records from the incident (the false-failure branch
raised before any write). No migration needed. New keys added at runtime:
`order_submissions` (this hotfix). Existing `pending_orders`/`order_events`/`order_locks`
/`order_receipts` are untouched in shape.

## Test status
- New `test_incident_hotfix.py` (15 tests) covers the prompt's tests 1–5 plus pure-
  function guards: orderId-persisted-before-fault, id-less-ack→UNKNOWN→WORKING recovery,
  lost-response→UNKNOWN, D1 matrix (a) off-tick / (b) exact-decimal / (c) direction-
  contradiction assertion / (d) one-sided-refuse / (e) stale-refuse, D3 refresh-storm→one
  order, verbatim rejection with no auto-retry, `submit_order` HTTP-outcome mapping, and
  cancel-races-a-fill→FILLED. Test 6 (real captured bodies) is N/A — none exist.
- `test_atomic_roll.py` and `test_position_mgmt.py` roll fixtures updated to the new
  contract (ref + re-read quotes + `submit_order`); all pass.
- Full suite: **766 passed, 24 failed.** All 24 failures are a pre-existing environment
  issue — `pyo3_runtime.PanicException: No module named '_cffi_backend'` from
  `cryptography` (reached via the alert path) and a `numpy`/regime `ImportError` — present
  identically on the clean tree and unrelated to the order path. Verified: none reference
  the order-submission, pricing, or roll code. On a machine with the native crypto
  backend installed these pass; they are not introduced or fixable by this hotfix.
