# Implementation Notes — Market-Settle Execution Gate (Time-of-Day Order Discipline)

Feature branch `claude/market-settle-execution-gate-pivwu8`, on top of `fb244c0`.
Companion to `AUDIT_MARKET_SETTLE_GATE.md` (Phase 0). App **v2.6.0**, state schema
**v17 → v18**. All new logic is offline-testable with a mocked clock; the full
suite is green (**775 passed**).

---

## What changed

### New modules
- **`backend/session.py`** — the intraday session model (prerequisite; the old
  `market_calendar` is date-only). For any injected `now`: is-open,
  minutes-since-open, minutes-until-close, early-close half-days, next-session-open.
  Eastern-time, DST-correct via `zoneinfo`. Pure. (`test_session.py`, 15 tests.)
- **`backend/execution_gate.py`** — the pure `execution_window()` gate + the
  gap-emergency unlock, the `NO_MARKET_ORDERS_AT_OPEN` forward invariant, the
  independent `spread_quality()` check, and `classify_action()` (executor action +
  `roll_reason` → the gate's action vocabulary). No I/O; deterministic.
  (`test_execution_gate.py`, 38 tests.)
- **`backend/spread_monitor.py`** — trailing per-contract bid-ask spread baseline,
  fed from quotes already fetched (no new polling); "no baseline" until
  `SPREAD_BASELINE_MIN_SAMPLES` samples exist (never fabricated).
- **`backend/recommendation_settle.py`** — the PENDING_SETTLE lifecycle
  (stage / mark / pending / due / expiry / pre-approve), pure given `state` + `now`.
  (`test_recommendation_settle.py`, 15 tests.)

### Wiring
- **`backend/executor.py`** — `execute(payload, now=None)` now runs the gate at the
  single shared placement chokepoint, before any order is staged/transmitted:
  `_enforce_execution_window` (blocks with `ExecutionWindowError`, tags
  `emergency_path` on the record) + `_enforce_spread_quality` (wide-spread
  `SpreadAckRequiredError`). Cancels (separate `cancel_order` path) and
  `adjustment` are never gated — `CANCEL_NEVER_GATED` is structural.
- **`backend/recommendation_runner.py`** — `run(now=None)`; a `release_pending`
  pass that re-validates each due PENDING_SETTLE rec by re-running the pure engine
  with no open recs; window-aware dual-timezone push copy (the alert always fires).
  (`test_execution_gate_wiring.py`, 10 tests.)
- **`backend/app.py`** — `ExecutionWindowError` / `SpreadAckRequiredError` → HTTP 409
  with `executable_at`; new `POST /api/recommendations/preapprove`; `pending_settle`
  + `gate_enforced` surfaced on `GET /api/recommendations`.
- **`backend/migrations.py`** — schema **v18** (`_v17_to_v18`) seeds `spread_baselines`
  and a `market_settle_gate_since` marker. The per-record `settle` block is additive
  and nullable (no per-record rewrite).
- **`backend/config.py`** — the constant block (provenance-tagged) +
  `market_settle_gate_enabled()` (env `CFM_MARKET_SETTLE_GATE`) + `OPERATOR_TZ`.
- **Frontend** (`PositionTracker.jsx`, `api.js`) — a PENDING_SETTLE rec renders a
  countdown ("Staged — executable 10:00 ET (in 18m)") and swaps Execute for a
  **Pre-approve** toggle. Builds clean.

---

## Key assumptions & decisions (with rationale)

1. **Rollout behind an ops flag, off by default.** `CFM_MARKET_SETTLE_GATE`
   governs *enforcement*; the gate *verdict* is always computed and surfaced
   (staging/countdown work regardless). This mirrors the existing `CFM_LIVE_TRADING`
   philosophy — a live-execution-timing change gets a deliberate opt-in — and keeps
   the 30+ existing wall-clock-time live-execute tests green (they run whenever CI
   fires, including when the market is closed). **To activate in production, set
   `CFM_MARKET_SETTLE_GATE=1`.**

2. **Gap-emergency is fail-closed / gap-size-only (per the approved decision).**
   The pure `_gap_emergency_unlocked` models Design §3 faithfully (gap-OR-range-break
   AND two-sided-prints AND limit-order, every unknown input → not satisfied). In
   the executor/runner *wiring*, only the overnight-gap-vs-ATR leg is computed from
   real data; the **opening-range-low break is unavailable → passed `False`**, and
   **two-sided-print duration is proxied by elapsed session time** (a conservative
   lower bar for a liquid optionable name) pending tick-level tracking. Net: an
   emergency unlocks on a genuine ≥2·ATR adverse gap ≥5 min after the open; a
   *filling* gap never unlocks. See "Follow-ups" for the real intraday tracker.

3. **The gate's action vocabulary is derived, not native.** `classify_action`
   maps executor actions + `roll_reason` to ENTRY/ROLL_SHORT/ROLL_LEAP/DEFENSE/
   EXIT_KILL/CANCEL. **DEFENSE is a `roll_short` with `roll_reason=="defend"`**;
   an absent/unknown reason falls back to the stricter ROLL_SHORT (never
   emergency-eligible) — a routine roll can never borrow the emergency unlock.

4. **`NO_MARKET_ORDERS_AT_OPEN` is a forward invariant.** No market order is ever
   built today (all LIMIT / NET_DEBIT / NET_CREDIT), so "emergencies as limit
   orders" is already true by construction; the rule refuses a *future* market path
   inside the settle window.

5. **PENDING_SETTLE is additive, never a mutation of the immutable claim.** Only a
   nullable `settle` block (status + `executable_at` + an append-only `events` log)
   is written; `emitted_at` / `action_type` / `proposed_ticket` / `valid_until` are
   never touched. Derived trust resolutions are orthogonal to the settle lifecycle.

6. **Auto-submit of pre-approved recs goes through an injected `submit_fn` seam.**
   The release pass makes the full re-validate → RELEASED/SELF_CANCELED/EXPIRED
   decision and, when pre-approved AND the trigger re-validates, calls `submit_fn`.
   Wiring that submitter to a real ticket→`execute()` adapter is intentionally
   deferred (automation→execution is not yet wired per the audit); tests exercise
   the seam with a fake submitter. This keeps a fragile live adapter from mis-firing
   while delivering the complete, tested lifecycle and the re-validation guarantee.

7. **Close-blackout & self-cancel reuse the confirmed-close convention.** Release
   re-validation re-runs the pure engine, which already encodes the `below`/
   `confirmed` (settled close AND live price) two-step — so a defense whose stock
   recovered above the strike self-cancels, no duplicated logic.

---

## PROPOSED_DEFAULT constants most needing calibration

Ranked by how much a live emergency-path review should move them:

1. **`GAP_EMERGENCY_ATR_MULT = 2.0`** — the single most consequential knob: it alone
   decides whether a pre-settle DEFENSE/EXIT unlocks (given the fail-closed wiring).
   Too low → the settle discipline leaks; too high → a real crash can't be defended
   until 10:00. Calibrate from the tagged `emergency_path` executions.
2. **`EMERGENCY_MIN_PRINT_MINUTES = 5`** — currently satisfied by the elapsed-session
   proxy; revisit once the real two-sided-print tracker lands (it may want to key off
   actual quote continuity, not clock time).
3. **`MARKET_SETTLE_MINUTES = 30`** — the core blackout length; every deferral's
   `executable_at` keys off it. Watch the spread/IV-settle data to confirm 30 min.
4. **`SPREAD_QUALITY_MULT = 2.0` / `SPREAD_BASELINE_MIN_SAMPLES = 5`** — tune from the
   trailing-spread distribution once baselines accumulate (deep-ITM LEAPs run wide).
5. **`CLOSE_BLACKOUT_MINUTES = 15`**, **`ENTRY_EARLIEST_MINUTES = 60`**,
   **`OPENING_RANGE_MINUTES = 15`** — lower priority; the first two are policy, the
   third only bites once the opening-range tracker exists.

Every `emergency_path: true` execution is tagged on the record and surfaced for
post-hoc review — each should be rare enough to warrant it.

---

## Phase 0 findings that altered the plan

- **No intraday session model / no early-close support** → built `session.py` with a
  half-day table (the close blackout keys off the *actual* close).
- **Gap-emergency intraday inputs absent from the data layer** → fail-closed
  gap-size-only wiring (decision above), pure rule kept complete for when data lands.
- **Action taxonomy not native; DEFENSE has no order path of its own** → the
  `classify_action` derivation, with the stricter-fallback safety.
- **No market orders exist** → market-order block implemented as a forward invariant,
  not a change to any order builder.
- **Recommendation records immutable, no `executable_at`/status** → additive `settle`
  block + a derived pending view + a runner-adjacent release pass.
- **Executor clock implicit (`log.utcnow`)** → `execute(now=...)` / `run(now=...)`
  injection so the gate is testable end-to-end.

---

## Follow-ups (documented, out of first cut)

1. **Intraday opening-range + two-sided-print tracker** — piggyback the existing
   quote cadence (no new polling) to replace the elapsed-session print proxy and
   enable the opening-range-continuation unlock leg. Activating it is a wiring change
   in `_build_gap_context` / `_gap_from_market`; the pure gate already supports it.
2. **Real pre-approved auto-submit adapter** — a `proposed_ticket` → `execute()`
   payload builder wired into the release pass's `submit_fn` (behind the automation
   switch), so a pre-approved rec transmits through all existing gates on release.
3. **Backstop assertion in `_assert_transmit_allowed`** — the primary gate in
   `execute()` covers every current placement path (all funnel through it); a
   deep backstop is only needed if a future path bypasses `execute()`.
4. **UI spread-acknowledge affordance** — the API returns `spread_ack_required`
   (409) with the dollar estimate; the roll/exit ticket UI should add the explicit
   acknowledge checkbox (the backend already enforces it).

---

## Test coverage added (78 new tests)

`test_session.py` (15), `test_execution_gate.py` (38), `test_execution_gate_wiring.py`
(10), `test_recommendation_settle.py` (15). The Design-doc matrix is covered:
per-action verdicts across pre-open / 9:31 / 9:45 / 10:00 / 10:29 / 10:30 / midday /
3:44 / 3:46 / post-close; early-close blackout shift; CANCEL in every session state;
gap-emergency unlock + market-order refusal + never-for-entry + print/range legs;
market-order block; PENDING_SETTLE lifecycle incl. release re-validation, self-cancel
on a filled gap, expiry, and pre-approved auto-submit; spread quality (wide/ack/
emergency/no-baseline); DST boundary; migration v18. Two pre-existing wall-clock
issues in `test_trust_derive` were repaired (a fixture time-bomb and a hardcoded
schema-version assertion) — both unrelated to this feature.
