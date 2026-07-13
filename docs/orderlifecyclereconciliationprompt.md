# Claude Code Implementation Prompt: Order Lifecycle State Machine + Broker Reconciliation

## Context

You are working on the CFM/rotation-dashboard app: Python/Flask backend, React/Vite/Tailwind frontend, Fly.io deployment. It manages a live options-income strategy (deep-ITM LEAPs + weekly short calls) against a real Schwab account. Key facts:

- `state.json` is the immutable, append-only single source of truth for executions; all ledgers and metrics are derived from it via recompute. Nothing is hand-entered into state. Schema changes require a migration and version bump.
- Schwab Trader API is the broker (order placement, order status, transactions, positions). Alpha Vantage is market-data fallback only — it plays no role here.
- Paper trading by default; `CFM_LIVE_TRADING=1` gates real orders.
- Test suite is 330+ offline tests. All new logic must be fully testable offline with mocked Schwab responses. **No live broker calls during implementation or in tests.**

## The incident driving this work (real, occurred on a live account)

A roll order (buy-to-close short call + sell-to-open new short call) was submitted from the app:

1. The app reported the submission as **failed** ("premium wasn't getting set properly"). In reality the order was **accepted and working at Schwab**. The app's picture of reality inverted the broker's.
2. The user, seeing "failed," refreshed the browser repeatedly to retry — meaning order submission was coupled to a page-level request cycle, and each refresh was a potential duplicate submission.
3. The app had no handle on the working order (orderId not captured/persisted), so it could not cancel it. The user had to cancel manually at Schwab.
4. The roll was ultimately executed manually in thinkorSwim. The app has no knowledge of that execution; `state.json` is now divergent from the real account, and may additionally contain phantom records from the failed attempts.

This incident defines the requirements. The deliverable is a system in which each of the four failures above is structurally impossible, plus an ingestion path so out-of-band trades (executed in ToS or anywhere else) reconcile back into the app automatically.

## Phase 0 — AUDIT FIRST (written report before any code)

1. **Order submission path:** every code path that constructs and submits a Schwab order. For each: how the request is built (esp. multi-leg/roll construction, orderType, price field), how the response is parsed, what is persisted from the acknowledgment, and what the frontend does on success/failure/timeout.
2. **Root-cause the incident:** find where a successful Schwab acknowledgment could be parsed as a failure. Identify the premium/price construction bug (check: tick-increment rounding, NET_CREDIT/NET_DEBIT sign convention, mid computed from missing/one-sided quotes, float serialization). Determine whether the Schwab response containing the orderId was received and discarded.
3. **Frontend coupling:** confirm whether submission fires from a page-level request that re-fires on reload; identify every place a retry can originate.
4. **Existing orderId handling:** whether any Schwab orderId is currently captured or persisted anywhere; whether Schwab transaction IDs appear anywhere.
5. **`state.json` execution records:** current schema for executions; whether the failed attempts wrote phantom records; what a correcting mechanism would look like under the append-only convention.
6. **Existing reconciliation:** any current position-comparison against Schwab positions/transactions endpoints, however partial.
7. **Recommendation/action gating:** where recommendations are generated, so a reconciliation freeze can block them.

Deliver as `AUDIT.md` with file/line references, including a specific root-cause writeup of the incident (use the captured logs/response bodies if present in the repo or provided). **Stop and present before implementing.**

## Design Requirements

### 1. Order lifecycle state machine

Every order the app submits gets a persistent lifecycle record with an app-generated `client_order_ref` (idempotency key) created **before** submission:

```
PENDING_SUBMIT → SUBMITTED → ACKED → WORKING → {FILLED | PARTIALLY_FILLED → ... | CANCELED | REJECTED | EXPIRED}
                     ↓ (no/unparseable response)
                  UNKNOWN → (status query by client_order_ref / orderId) → resolved state
```

- **HARD_CFM_RULE — orderId persistence is first:** on any acknowledgment, the Schwab orderId is persisted to the lifecycle record *before* any other processing of the response. If parsing the rest of the response fails, the orderId must already be on disk.
- **HARD_CFM_RULE — no assertion of failure without verification:** a submission that gets no response, a timeout, or an unparseable response transitions to UNKNOWN, which immediately triggers a status query against Schwab (by orderId if held, else by querying recent orders for the account and matching on `client_order_ref`/legs/time). The UI never displays "failed" from UNKNOWN — it displays "confirming with broker…" until resolved. REJECTED is only shown when Schwab explicitly said so.
- Terminal-state handling: FILLED produces execution records (see §4); CANCELED/REJECTED/EXPIRED close the lifecycle record with the broker's reason string captured verbatim.
- Partial fills: record filled quantity per leg; a partially-filled-then-canceled multi-leg order is flagged **UNBALANCED** (see §6).

### 2. Idempotent, backend-owned submission

- Order submission is a backend operation keyed by `client_order_ref`. Re-invoking submission with the same ref (double-click, browser refresh, network retry) is a no-op that returns the existing lifecycle record.
- The frontend never submits directly on page load or render. It POSTs an intent once, then **polls/subscribes to lifecycle status**. Refreshing the page shows current status; it cannot re-fire submission.
- Cancel requests go through the same pattern: cancel-by-orderId from the persisted lifecycle record, with a CANCEL_PENDING state, and — per the known race — a post-cancel status check that detects fill-during-cancel and routes to §6 rather than assuming the cancel won.

### 3. Pre-submit validation (fixes the original premium bug class)

A pure validation function runs before any order is constructed for submission. It must reject, with a specific reason, any order where:

- The limit price does not conform to the option's tick increment (round to valid increment explicitly; never submit float artifacts — construct prices as Decimal, serialize per Schwab's expected format).
- Net price sign/type mismatch: a net-credit roll must carry NET_CREDIT with positive price; NET_DEBIT likewise. The validator recomputes expected direction from the legs and rejects contradictions.
- Any leg's quote inputs are missing, one-sided, zero, or stale beyond tolerance — **refuse to construct, loudly**, rather than submit a malformed price. (Integrates with staleness conventions if present; else a fetched-at age check.)
- Leg structure is inconsistent (quantities unmatched for a roll, wrong position effect, symbol mismatch).

Validation failures are app-side rejections displayed with the specific reason — they never reach Schwab and never enter the lifecycle beyond PENDING_SUBMIT.

### 4. Execution ingestion from Schwab transactions (the reconciliation core)

A reconciliation job (on-demand button + scheduled every RECONCILE_INTERVAL_MINUTES during market hours + once after close) pulls the Schwab transactions endpoint and ingests executions as the ground truth:

- **Dedupe by Schwab transaction ID**, persisted with every ingested execution. Re-running reconciliation is always safe and idempotent.
- **Matching:** each broker execution is matched to an app lifecycle record by orderId when possible. Matched executions confirm/complete the lifecycle record and produce `state.json` execution entries tagged `source: app`.
- **Out-of-band detection:** broker executions with no matching lifecycle record (e.g., the manual ToS roll) are ingested as executions tagged `source: broker_manual`, with all economic fields taken from the broker record (fills, prices, fees, timestamps — never hand-entered). Multi-leg out-of-band orders sharing a Schwab orderId are linked as one logical action so a manual roll ingests as a roll, not two unrelated trades.
- **Derived-only principle preserved:** ingestion appends execution records; every ledger, position, and metric then recomputes from state as usual. No derived value is patched directly.
- **Phantom cleanup:** if the audit finds phantom execution records from the incident's failed attempts, write a one-time migration/correction consistent with the append-only convention (compensating entries or a flagged invalidation — follow whatever correction pattern exists; if none exists, propose one in the audit and implement it as a general mechanism, since this will recur).

### 5. Position reconciliation + freeze semantics

After each ingestion run, compare app-derived positions against Schwab's positions endpoint, instrument by instrument, contract counts and share counts:

- **Match:** record a reconciliation heartbeat (timestamp shown in the UI — "last reconciled N minutes ago").
- **Mismatch:** enter **RECONCILE_FREEZE**: all recommendations and any order-submission capability are blocked, the UI shows a prominent divergence panel listing exactly which instruments/quantities differ, and the resolution path is another ingestion run (transactions may lag) followed by manual review. **HARD_CFM_RULE — no auto-remediation:** the app never generates orders to "fix" a divergence; it surfaces and freezes.
- A stale reconciliation (older than RECONCILE_STALE_MINUTES during market hours) degrades to a warning state on all action-capable panels.

### 6. Unbalanced-position handling

A partially-filled multi-leg order, or a fill-during-cancel, can leave an unbalanced position (e.g., a bought-back short without its replacement sold, or worst case an uncovered short). When lifecycle or reconciliation detects leg-count asymmetry against the intended structure:

- Raise the highest-severity alert state in the app (this is the one genuinely urgent condition the system can produce).
- Enter RECONCILE_FREEZE with a panel stating precisely which leg is unpaired and what the exposure is (covered vs uncovered direction).
- **Never auto-remediate.** Present the situation; the human acts at the broker.

### 7. UI (minimal, follows existing patterns)

- Order status panel driven by lifecycle records: current state, orderId, timestamps, broker reason strings verbatim, and a cancel button wired to §2's cancel path.
- Reconciliation heartbeat + on-demand "Reconcile now" button.
- Divergence/freeze panel per §5–6.
- Ingested `broker_manual` executions visibly badged in position/history views so out-of-band trades are distinguishable at a glance.

## Config Constants (single config module, provenance-tagged)

```
RECONCILE_INTERVAL_MINUTES = 15        # PROPOSED_DEFAULT
RECONCILE_STALE_MINUTES = 45           # PROPOSED_DEFAULT
UNKNOWN_STATUS_RETRY_SECONDS = 10      # PROPOSED_DEFAULT (backoff base for status queries)
UNKNOWN_STATUS_MAX_ATTEMPTS = 6        # PROPOSED_DEFAULT (then persistent UNKNOWN banner, keep querying slower)
QUOTE_MAX_AGE_FOR_ORDER_SECONDS = 60   # PROPOSED_DEFAULT (pre-submit staleness tolerance)
ORDERID_PERSIST_FIRST = True           # HARD_CFM_RULE
NO_FAILURE_WITHOUT_VERIFICATION = True # HARD_CFM_RULE (UNKNOWN → status query, never "failed" display)
NO_AUTO_REMEDIATION = True             # HARD_CFM_RULE (divergence/unbalanced → freeze + surface only)
INGESTION_IS_GROUND_TRUTH = True       # HARD_CFM_RULE (broker transactions define executions; nothing hand-entered)
```

## Testing Requirements (offline only, mocked Schwab client throughout)

Build a mock Schwab client that can replay scripted response sequences. Required cases:

1. **The incident, end to end (regression fixture):** submission → valid ack with orderId → response parse raises mid-processing → orderId is nonetheless persisted (assert on-disk before the exception surfaces) → state is UNKNOWN → status query resolves WORKING → app shows working order with cancel available. The old behavior (display "failed", no orderId) must be impossible to reproduce.
2. Idempotency: same `client_order_ref` submitted 5× (simulated refresh storm) → exactly one Schwab submission on the mock, one lifecycle record.
3. Pre-submit validation: tick-violation price rejected with reason; NET_CREDIT/NET_DEBIT sign contradiction rejected; one-sided/stale quote → refuse-to-construct; each never reaches the mock client.
4. UNKNOWN resolution matrix: timeout→WORKING, timeout→FILLED, timeout→REJECTED, and timeout→not-found (resolves via recent-orders match on client_order_ref).
5. Cancel race: cancel issued, mock reports fill occurred first → lifecycle lands FILLED, cancel marked lost, no false CANCELED state; partial-fill-then-cancel on a two-leg roll → UNBALANCED flag + freeze + correct exposure description (test both directions: orphaned buyback vs orphaned new short).
6. Ingestion: scripted transactions feed containing (a) fills matching an app order → lifecycle completes, executions appended `source: app`; (b) a manual two-leg roll sharing an orderId with no lifecycle record → ingested as one linked `broker_manual` action; (c) duplicate transaction IDs across two runs → second run is a no-op (idempotency).
7. Position reconciliation: matching positions → heartbeat; injected mismatch → RECONCILE_FREEZE, recommendations blocked (assert the gating), divergence panel payload correct; mismatch that resolves on next ingestion (lagging transaction) → freeze lifts.
8. Phantom correction: fixture state containing incident-style phantom executions → correction mechanism produces derived ledgers matching broker truth, original records preserved per append-only convention.
9. Derived-recompute integrity: after any ingestion sequence, full recompute from state equals incremental results (no drift between paths).

## What NOT to do

- Do NOT make any live Schwab call during implementation or tests.
- Do NOT auto-remediate divergences or unbalanced positions — freeze and surface only.
- Do NOT hand-enter or synthesize execution economics — broker transaction records are the only source for ingested executions.
- Do NOT let the frontend submit or resubmit orders from page lifecycle events.
- Do NOT display "failed"/"rejected" for any order whose state was not explicitly confirmed by a Schwab response or status query.
- Do NOT guess at Schwab API fields — where the audit finds ambiguity in response schemas, mark it, stub the parse behind an interface, and flag for verification against captured real responses.
- Do NOT modify `state.json` derived values directly; executions in, recompute out.
- Do NOT break the append-only convention for the phantom cleanup — corrections are new records, not edits.
- Do NOT remove or bypass existing tests; all 330+ must pass.

## Deliverables

1. `AUDIT.md` with incident root-cause (present before implementation).
2. Lifecycle state machine + persistence, with orderId-first acknowledgment handling.
3. Idempotent backend submission + status-driven frontend + cancel path with race detection.
4. Pre-submit validation module (pure functions).
5. Transactions ingestion + matching + `broker_manual` out-of-band path + dedupe.
6. Position reconciliation + freeze/unbalanced semantics + recommendation gating.
7. Phantom-correction mechanism applied to incident records (if audit confirms them).
8. UI panels per §7.
9. Full offline test suite per above, including the incident regression fixture.
10. `IMPLEMENTATION_NOTES.md`: root cause confirmed, assumptions, Schwab schema fields that need verification against captured live responses, and which PROPOSED_DEFAULTs need tuning.

Work in this order: Audit → present → lifecycle + validation pure functions + tests → submission/cancel idempotency → ingestion → reconciliation/freeze → phantom correction → UI. Present findings at each boundary if anything contradicts this spec. This system is the gate everything else waits behind: no order-capable feature graduates past supervised use until these tests exist and pass.
