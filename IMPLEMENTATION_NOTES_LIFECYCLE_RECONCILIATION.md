# IMPLEMENTATION NOTES — Order Lifecycle + Broker Reconciliation

Companion to `AUDIT_LIFECYCLE_RECONCILIATION.md` (Phase 0). Records what shipped,
the two operator decisions taken, the Schwab schema fields that still need
verification against captured live responses, which `PROPOSED_DEFAULT`s to tune,
and the precisely-scoped work deliberately left for a follow-up.

**No live Schwab call is made in implementation or tests.** All new logic is
exercised offline against scripted transaction feeds and mocked clients.

## Test status

Full `backend/` suite: **808 passed** (790 pre-existing baseline + 18 new/adjusted),
fully offline. New files: `test_transaction_ingest.py` (11), `test_reconcile_freeze_gate.py`
(6), plus `_leg_imbalance_exposure` cases in `test_atomic_roll.py`. Adjusted:
`test_recommendation_settle.py` (schema v18→v19). Frontend builds clean (`vite build`).
Run: `cd backend && python -m pytest -q`.

The environment needs `pandas numpy requests pyarrow flask flask-cors cffi
cryptography` installed to run the suite; `http-ece` (webpush) fails to build here
but is not on any path these tests touch.

---

## Decisions taken (the audit's open questions)

The Phase-0 audit surfaced two blocking decisions; the interactive question tool
was unavailable, so I proceeded with the audit's **recommended** options and made
them explicit and reversible:

- **DECISION 1 → Option B, "ingest-to-confirm."** §4 wanted out-of-band broker
  trades auto-ingested, which contradicts `reconcile.py`'s operator-confirmed "NO
  adopt-external-trade flow." Resolution: build the transactions endpoint + dedupe;
  **matched** fills confirm app orders automatically (pure upside, no philosophy
  change); **unmatched** broker trades surface as one-click adoption **proposals**
  with economics from the broker record. This satisfies the incident's real need
  (getting the manual ToS roll into state) **without** reversing the no-silent-adopt
  safety stance, and honors `NO_AUTO_REMEDIATION`. Full auto-ingest (Option A)
  remains available by having the interval job call adopt on each proposal — a
  one-line change gated on operator sign-off.
- **DECISION 2 → keep the two-store split; unify the read, not the stores.** The
  existing coded lifecycle (`order_events`) and submission-record statuses
  (`order_submissions`) both stand; no risky migration of live order stores was
  done this pass. Recommended follow-up: one `order_status_view(ref_or_id)` read
  path (see below).

---

## What shipped

### §4 — Execution ingestion (the reconciliation core)
- `schwab_api.get_transactions(account_hash, start_date, end_date, types)` — the
  ground-truth feed. **LIVE_VERIFY** (see below); parses defensively and fails
  closed.
- `transaction_ingest.py` (new) — pure core + thin fetch wrapper:
  `parse_transaction`/`parse_feed` → `group_by_order` (links multi-leg by shared
  Schwab orderId so a manual roll ingests as ONE roll) → `build_report` (dedupe by
  transaction id, classify matched `source: app` vs out-of-band `source:
  broker_manual`). `run_ingestion` persists the dedupe ledger + surfaces open
  proposals. Re-running is idempotent.
- `executor.adopt_broker_trade(proposal_id)` — human-gated adoption that books a
  proposal through the SAME builders app fills use (`_build_leg` / roll linkage),
  tagging each execution `source: broker_manual` + `transaction_id` +
  `broker_order_id`, then `recompute_derived` rebuilds ledgers/positions. No
  derived value is patched directly.
- State: `ingested_transactions` (dedupe ledger) + `ingestion` (last summary +
  open proposals), seeded by additive migration **v18→v19**; new execution fields
  `source`/`transaction_id`/`broker_order_id` are additive + nullable.
- API: `GET/POST /api/ingestion`, `POST /api/ingestion/adopt`.

### §5 — Freeze gating + minutes staleness + interval scheduler
- `reconcile.freeze_status(state)` — the global freeze verdict (frozen tickers +
  reviews + reason) and `is_reconcile_stale_minutes` (market-hours degrade vs
  `RECONCILE_STALE_MINUTES`).
- `recommendation_runner.run` now **blocks recommendation generation entirely
  while the book is frozen** (any position `needs_review`) — closing the §5 gap
  that recommendations ignored the freeze. Order submission on a diverging
  position stays blocked by the existing per-position `_enforce_not_frozen`.
- `alert_scheduler._maybe_interval_reconcile` — during market hours (+ a
  post-close tail) runs reconcile **and** ingestion on the
  `RECONCILE_INTERVAL_MINUTES` cadence, giving the minutes-staleness clock a
  cadence to measure against.
- API: `GET /api/reconcile/freeze-status`.

### §6 — Unbalanced exposure direction
- `_leg_imbalance_exposure` names the direction: `orphaned_new_short` (an extra
  short leg is live → potentially NAKED — the one urgent case) vs
  `orphaned_buyback` (under-written, safe). Surfaced on the review payload + the
  cancel/imbalance API result.

### §7 — UI (partial)
- `DataHealth` ingestion panel: lists out-of-band proposals with a **broker_manual
  badge**, per-leg summaries, exposure text, and one-click **Adopt**; plus an
  "Ingest now" trigger. `api.js` gains `ingestion`/`runIngestion`/
  `adoptBrokerTrade`/`freezeStatus`.

### Config (all provenance-tagged)
`RECONCILE_INTERVAL_MINUTES=15`, `RECONCILE_STALE_MINUTES=45`,
`NO_AUTO_REMEDIATION=True` (HARD), `INGESTION_IS_GROUND_TRUTH=True` (HARD),
`INGESTION_LOOKBACK_DAYS=7`. The incident-hotfix block (`QUOTE_MAX_AGE_*`,
`UNKNOWN_STATUS_*`, `ORDERID_PERSIST_FIRST`, `NO_FAILURE_WITHOUT_VERIFICATION`,
`OPTION_TICK_*`) is adopted unchanged.

---

## Schwab schema fields needing live verification (LIVE_VERIFY)

The transactions endpoint has **no captured real response in the repo**, so these
are assumptions stubbed behind `SchwabClient.get_transactions` and
`transaction_ingest`. Confirm against a live account before trusting ingestion
unsupervised. The parser **fails closed** (an unparsed transaction lands in the
report's `errors`, never silently ingested), so a wrong assumption degrades safely.

1. **Endpoint + query params.** `/accounts/{hash}/transactions` with
   `startDate`/`endDate` (ISO-8601) and a `types=TRADE` filter — names/format
   unconfirmed.
2. **Stable transaction id.** Assumed `activityId` (falls back to
   `transactionId`/`id`). This is the dedupe key — the single most important field
   to confirm.
3. **Order link.** Assumed `orderId` on the TRADE transaction. This is what
   distinguishes a matched app fill from an out-of-band trade and links multi-leg
   rolls. If Schwab nests it elsewhere, matching degrades to "everything looks
   out-of-band" (safe — surfaces as proposals, never a false auto-book).
4. **transferItems shape.** `instrument.{symbol,assetType,putCall,underlyingSymbol,
   strikePrice,expirationDate}`, `amount` (signed qty), `price`, `cost`,
   `positionEffect` (OPENING/CLOSING), `feeType`. The OCC `symbol` is trusted over
   the loose fields when present.
5. **Fee attribution.** Per-leg vs per-transaction fee rows — the current fee sum
   is best-effort and should be reconciled against `netAmount` once real bodies
   exist.
6. **Underlying price at trade time.** NOT in the transaction (it's a market
   datum), so the intrinsic/extrinsic split on an adopted trade uses a cached close
   for the trade day when available, else degrades to all-extrinsic — never
   hand-entered. Confirm this is acceptable or capture the underlying mark at
   adoption time.

---

## PROPOSED_DEFAULTs to tune

- `RECONCILE_INTERVAL_MINUTES=15` / `RECONCILE_STALE_MINUTES=45` — the stale
  threshold must stay > the interval; tune once real cadence/latency is observed.
- `INGESTION_LOOKBACK_DAYS=7` — wide enough for late-reporting fills, narrow enough
  to keep the dedupe set small.

---

## Deliberately deferred (precise plans for a follow-up)

These are extensions of already-hardened machinery; the incident's own path (the
short-call roll) is fully hardened already, so none of these reproduce the
incident. Left out of this pass to avoid rushing risky changes to live-money code
without operator sign-off.

1. **§2/§3 — extend idempotency + validation to the legacy order paths.** Today
   only `_place_live_roll` uses the structured `submit_order` + `client_order_ref`
   idempotency + orderId-persist-first + UNKNOWN handling. The single-leg
   (`_place_live`), atomic-open (`_place_live_open`), atomic-exit
   (`_place_live_exit`), and LEAP-roll (`_place_live_leap_roll`) paths still call
   `place_order`, which raises on a header-less 2xx (a latent D2 for those order
   types) and persists the orderId only after the ack (a D4 window). **Plan:**
   route all four through a shared `_submit_and_record(ref, order, kind)` helper
   that (a) writes a durable `order_submissions` record before the call, (b) calls
   `submit_order`, (c) writes the orderId first on ack, (d) maps timeout/5xx/
   header-less-2xx to UNKNOWN. Requires updating `test_atomic_open.py`,
   `test_order_lifecycle.py`, and their inline mocks to the structured-outcome
   contract. Medium-risk, ~1 focused change per path.
2. **§7 — standing order-lifecycle panel + MANUAL cancel button.** Cancellation is
   currently automatic/time-based (`orderFlow.js`); there is no always-on panel
   listing live orders (orderId, timestamps, verbatim broker reason) with an
   operator Cancel control. **Plan:** add `GET /api/orders/live` (a projection over
   `pending_orders` + `order_submissions` + the derived order state) and an
   `OrderLifecyclePanel` component wired to the existing `api.cancelOrder`.
3. **§7 — broker_manual badge in the history/positions views.** The ingestion panel
   badges proposals + adopted trades, but the history view is cycle-aggregated and
   doesn't carry execution `source`. **Plan:** thread `source` through the history
   payload (`history.py`) and badge cycles/legs that contain a `broker_manual`
   execution.
4. **DECISION 2 unification** — one `order_status_view(ref_or_id)` read path over
   the two order stores (no store migration).

## state.json impact

Additive only. New keys `ingested_transactions` / `ingestion` (v18→v19 migration);
new nullable execution fields `source` / `transaction_id` / `broker_order_id`. No
execution is ever mutated; adoption and corrections are new append-only records
(the existing compensating-adjustment convention). The incident wrote no phantom
executions (the false-failure raised before any write), so no phantom-cleanup
migration was needed — the mechanism (`action: "adjustment"` with `linked_diff_id`)
already exists if a future correction is required.
