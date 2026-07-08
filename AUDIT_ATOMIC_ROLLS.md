# AUDIT — Atomic Spread Roll Orders (Phase 0)

**Task:** Make the live short-call roll a single Schwab multi-leg NET order
(buy-to-close old short + sell-to-open new short) that fills as a unit or not at
all. **Scope of this document:** map the *actual* roll path before writing code,
and flag every place the implementation prompt's assumptions are contradicted by
what is already in the tree.

Every reference is `backend/<file>:<line>`. This audit was produced by reading
the code, not from memory.

---

## 0. Executive summary — the headline finding

**The atomic spread roll is already implemented and merged.** The prompt's
central premise — *"every live action is a single-leg DAY LIMIT order"* and *"a
live roll executes as two independent single-leg orders"* — is **REFUTED**. The
live roll path already:

- builds ONE two-leg NET_CREDIT/NET_DEBIT DAY order
  (`schwab_api.build_roll_order`, `schwab_api.py:500`),
- parks it as a single `pending_orders` entry (`executor._place_live_roll`,
  `executor.py:878`),
- commits **two** linked executions (`close_short` + `sell_short`) tagged with a
  shared `roll_id` only on confirmed fill, overlaying the real per-leg fills
  (`executor._commit_roll_from_pending` → `_commit_roll`, `executor.py:504`/`948`),
- auto-cancels the whole spread with no execution trace (`executor.cancel_order`,
  `executor.py:571`).

`fill_verify.py` already verifies multi-leg rolls per-leg against Schwab
(`_broker_legs`, `_verify_receipt`, `fill_verify.py:29`/`87`). The same atomic
machinery exists for the atomic open (`_place_live_open`, `executor.py:1089`)
and atomic exit / LEAP roll (`_commit_exit_from_pending`,
`_commit_leap_roll_from_pending`).

So this task is **not** a green-field build. It is **closing the spec gaps** the
merged implementation does not yet cover. Those gaps are real and are enumerated
in §8.

---

## 1. Current roll path (R-audit Q1)

### 1.1 Dispatch
`executor.execute` (`executor.py:125`) validates the action, enforces the
reconciliation freeze for `roll_short` (it is in `FROZEN_BLOCKED_ACTIONS`,
`executor.py:28`), captures the underlying price, then dispatches
`roll_short` → `_roll_short` (`executor.py:175`). `mode` is `"live"` iff
`live_transmit()` (env/flag gate AND not demo, `executor.py:71`).

### 1.2 `_roll_short` (`executor.py:858`)
Requires `from_strike` + `to_strike` (raises `ValueError` otherwise). Branches:
- **live + `schwab_api.configured()`** → `_place_live_roll` (atomic).
- **otherwise (paper, or live w/o Schwab)** → `_commit_roll` (books both legs
  immediately).

### 1.3 Live: `_place_live_roll` (`executor.py:878`)
- `_assert_transmit_allowed("roll_short")` (kill-switch/live gate, `executor.py:84`).
- Resolves `close_symbol`/`open_symbol` from `{from,to}_option_symbol` or builds
  the OCC symbol from `{from,to}_expiration` + strike (`occ_option_symbol`,
  `schwab_api.py:445`).
- **Net limit** = `round(premium_per_share − close_price_per_share, 2)` — new
  short credit minus buy-back cost. Positive ⇒ NET_CREDIT, negative ⇒ NET_DEBIT
  (`build_roll_order` chooses by sign, `schwab_api.py:507`).
- `client.place_order(...)`; on missing `orderId` raises `SchwabError`.
- Parks ONE `pending_orders[order_id]` record with `kind:"roll_short"`, the full
  payload, both symbols, and `net_limit` (`log.save_pending_order`,
  `executor.py:906`). Returns `status:"working"`.

### 1.4 Poll / commit: `order_status` (`executor.py:518`)
Polls Schwab; on `FILLED`, dispatches by `kind` → `_commit_roll_from_pending`
(`executor.py:530`), pops the pending record, and writes an `order_receipt`
(`_capture_order_receipt`, `executor.py:550`). On
`CANCELED/REJECTED/EXPIRED` it pops the pending record and returns a terminal
status. Anything else ⇒ `working`.

`_commit_roll_from_pending` (`executor.py:504`) extracts per-leg fills via
`_roll_leg_fills` (matches `executionLegs.legId` → `orderLegCollection` symbol,
`executor.py:924`), overlays `close_price_per_share` / `premium_per_share` onto
the payload (**falling back to the staged estimate when a leg price is
absent**), then calls `_commit_roll`.

### 1.5 Booking: `_commit_roll` (`executor.py:948`)
Builds a `close_short` execution (`_close_short`, `executor.py:815`) and a
`sell_short` execution (`_sell_short`, `executor.py:793`), stamps both with
`mode`, `price_source`, `roll_leg` (`"close"`/`"open"`), `roll_id`
(`_next_roll_id`, `executor.py:852`), and `roll_reason`. Appends both
(`log.append_execution`), applies both position mutations once onto fresh state,
`recompute_derived`, `save_state`.

### 1.6 Execution-record count & derived-ledger expectations
- **One roll ⇒ exactly two executions** (`close_short` + `sell_short`), same
  today and in every mode.
- `_close_short` computes `extrinsic_sold`, `extrinsic_paid_back`, `net_juice`,
  `net_juice_total` — the **theta ledger** and **extrinsic payback** are rebuilt
  from these fields in `recompute_derived`.
- The **roll ledger** keys off `roll_id` + `roll_leg` + `roll_reason`
  (`_next_roll_id` counts `close_short` execs that carry a `roll_id`).
- Every field the ledgers need lives on the immutable execution, so a legacy
  legged pair and an atomic pair are **already** replay-identical *provided both
  legs carry the same `roll_id`/`roll_leg`* — which `_commit_roll` guarantees for
  both paper and live. (Proven by new test in §Deliverables.)

### 1.7 Auto-cancel (invariant preserved)
The UI polls `order_status`; an unfilled order is dropped by `cancel_order`
(`executor.py:571`), which reconciles against the broker first (commits a
late fill rather than losing it; clears a stale terminal record) and only pops
the pending record after Schwab confirms the cancel. **No execution is written
for a canceled spread** — invariant already holds.

---

## 2. Schwab client capabilities (R-audit Q2)

`schwab_api.py` already supports everything R1 needs:
- `place_order` / `get_order` / `cancel_order` / `preview_order`
  (`schwab_api.py:392`/`407`/`410`/`381`).
- `build_single_leg_order` — LIMIT DAY single leg (`schwab_api.py:460`).
- `build_net_order` — generic multi-leg NET_CREDIT/NET_DEBIT DAY, CUSTOM
  (`schwab_api.py:478`); used by atomic open/exit/LEAP-roll.
- `build_roll_order` — the two-leg roll: `orderType` NET_CREDIT/NET_DEBIT by
  sign, `session` NORMAL, `duration` DAY, `orderStrategyType` SINGLE,
  `complexOrderStrategyType` **CUSTOM**, legs `BUY_TO_CLOSE` + `SELL_TO_OPEN`
  with matching quantities (`schwab_api.py:500`).

**Field-verification flags (do NOT rely on in production until confirmed live):**
- `complexOrderStrategyType` is hardcoded `"CUSTOM"`. For a same-underlying,
  different-strike/expiry call pair Schwab also documents `DIAGONAL` (diff
  expiry) / `VERTICAL` (same expiry). **CUSTOM is the safe superset** and is what
  the atomic open/exit already use in production-intent code — but the exact enum
  Schwab's spread-approval logic wants is a **[LIVE-VERIFY]** item. This audit
  makes it a config constant rather than guessing (§8, `ROLL_COMPLEX_STRATEGY_TYPE`).
- Per-leg fill price reporting: the code reads
  `orderActivityCollection[].executionLegs[].price` keyed by `legId`
  (`_roll_leg_fills`). Whether Schwab always populates per-leg `price` on a
  complex fill (vs only a net) is a **[LIVE-VERIFY]** item → drives the
  proportional-to-mid allocation fallback in §8.
- Partial-fill fields (`filledQuantity` / `remainingQuantity`) on a working
  complex order are a **[LIVE-VERIFY]** item → drives R3.

No new Schwab fields are invented in this change.

---

## 3. Fill verification (R-audit Q3)

`fill_verify.py` **already** handles multi-leg orders: `_broker_legs` maps each
`executionLegs` leg to its `orderLegCollection` leg by `legId`
(`fill_verify.py:29`); `_verify_receipt` pairs each committed execution to its
broker leg by instruction+strike and checks per-leg price drift to the cent
(`fill_verify.py:87`). Receipts carry `kind:"roll_short"` already.

**Gap:** it verifies *per-leg* prices but never verifies the **net** fill against
a **reference net mid** (mid_new_short − mid_old_short). `slippage.py` is where
the reference-mid comparison lives, and `slippage._fill_slippage` explicitly
**returns None for rolls** (`slippage.py:49`, "rolls, pre-capture executions")
because roll legs don't carry `quoted_mid_per_share`. R5 asks for **net** roll
slippage → new code (§8).

---

## 4. Slippage model (R-audit Q4) — prompt assumption partially REFUTED

The prompt says *"a paper roll currently pays the haircut twice (once per leg)."*
**Reality:** paper fills are booked at the **quoted mid with NO haircut applied
to the immutable ledger** (`slippage.py` header; `_commit` sets
`fill_assumption:"mid"`, `quoted_mid_per_share = limit`, `executor.py:394`). The
haircut is a **report-only caveat** (`slippage.report`, `slippage.py:74`); the
only "×2" is the *illustrative* `roundtrip_haircut_pct = effective*2` line
(`slippage.py:107`), which is a display factor, not a ledger deduction.

So there is **no two-haircut ledger cost to remove**. What R4 actually maps to:
paper roll legs currently don't participate in the slippage report at all (they
lack `quoted_mid_per_share`), so a paper roll's economics are *mid on both legs*.
Making paper "one net crossing" is therefore about the **slippage report**
(measure one net figure per roll, not per leg) + documenting the
`PAPER_ROLL_HAIRCUT_CROSSINGS=1` intent — not about changing booked ledger
prices. The changelog will state this precisely (R4 note).

---

## 5. Pending-order representation (R-audit Q5)

`pending_orders` is a **free-form dict keyed by order_id** (`log.save_pending_order`
/ `get_pending_order` / `pop_pending_order`, `logging_handler.py:266+`). A roll
record already stores two symbols + `net_limit` + `kind`. **It already
represents a two-leg order** — no structural change is required. The additive
change is a `roll_group_id` on the *executions* (the spec's term for the existing
`roll_id`) and an `allocation_method` marker (§8), plus a forward-only migration
that backfills `roll_group_id = roll_id` on historical roll executions.

---

## 6. UI touchpoints (R-audit Q6)

Order flow is API-driven; the frontend calls `/api/execute` then polls
`/api/order-status` and drops unfilled orders via `/api/order-cancel`
(`app.py:339`/`356`/`367`). The roll returns already differ for live
(`status:"working"`, `option_symbols:[...]`, `net_limit`) vs paper
(`status:"filled"`, `executions:[...]`, `net_credit`). No component assumes a
single leg for the roll — the RollModal deep link is unchanged
(`alerts._ROLL_ACTIONS`). **New UI surface** from this change is the R6 rejection
→ "leg this manually?" confirmation, returned in the `order_status` body for the
frontend to render (no forced fallback).

---

## 7. Failure / rejection paths (R-audit Q7)

- **Schwab rejects at placement:** `place_order` raises `SchwabError`
  (`schwab_api.py:405`); `/api/execute` surfaces it as a 500 error body
  (`app.py:352`). Nothing is parked, nothing booked.
- **Rejected after working:** `order_status` returns
  `{status:"rejected", raw_status:"REJECTED"}` and pops the pending record
  (`executor.py:543`). **Gap (R6):** no reason surfaced, no operator-confirmed
  legged fallback offered.
- **Token expired mid-flow:** `_auth_headers` refreshes; a dead refresh token
  raises `SchwabError` with `_ACCT_HINT`. The pending record **stays** (only
  popped on a definite terminal status or confirmed cancel), so the order isn't
  lost — the next poll retries. `TOKEN_EXPIRY` alert covers re-auth.
- **Leg-imbalanced fill** (one leg fills, the other doesn't, on a spread): the
  current code only acts on `FILLED` and would then read both legs. **Gap (R3):**
  a leg-imbalance report must **freeze + alert, write no execution** — not
  auto-correct.

---

## 8. Gaps to close (what this change actually implements)

| # | Requirement | Status in tree | Work |
|---|---|---|---|
| G1 | R7 feature flag `ATOMIC_ROLLS_ENABLED` | absent | add config constant; `_roll_short` honours it; when off ⇒ legacy legged live path |
| G2 | R1 strategy type as config constant, [LIVE-VERIFY] | hardcoded `CUSTOM` | `ROLL_COMPLEX_STRATEGY_TYPE` + `ROLL_ORDER_DURATION` config; `build_roll_order` reads them |
| G3 | R2 `roll_group_id` on both legs | uses `roll_id` | stamp `roll_group_id` (= `roll_id`); migration backfill |
| G4 | R2 per-leg allocation + proportional-to-mid fallback, marked | overlays per-leg or staged estimate, unmarked | `allocation_method` on records; proportional-to-mid when broker per-leg absent |
| G5 | R3 partial fill (whole spread units) | only full FILLED handled | commit filled units, keep remainder pending |
| G6 | R3 leg-imbalance ⇒ freeze + alert, no execution | absent | detect imbalance; freeze position; `ROLL_LEG_IMBALANCE` alert; write nothing |
| G7 | R5 net roll slippage | rolls excluded from slippage | net reference-mid capture + net slippage report per roll_group |
| G8 | R6 explicit rejection fallback (never silent) | bare `rejected` | surface reason + offer legged path behind explicit `confirm_leg_manually` |
| G9 | R4 paper one-net-crossing | mid on both legs (report-only) | net-crossing accounting in slippage report + changelog note |
| G10 | migration | `CURRENT_VERSION=11` | v11→v12 additive; backfill `roll_group_id`; snapshot-before-migration already enforced |

## 9. LEAP-roll reuse note (scope guard)

The atomic LEAP roll (`_roll_leap`/`_commit_leap_roll_from_pending`,
`executor.py:1361`/`1486`) already uses `build_net_order` and the same
pending→poll→commit lifecycle. The R3 leg-imbalance freeze and R4 net-slippage
helpers are written generically enough to reuse for the LEAP roll later, but
**this change touches the short-call roll only** (per "Do NOT touch LEAP roll
paths"). No LEAP-roll behavior changes.

## 10. Config constants (provenance-tagged)

| Constant | Value | Provenance |
|---|---|---|
| `ATOMIC_ROLLS_ENABLED` | `True` | [PROPOSED-DEFAULT] feature flag |
| `ROLL_ORDER_DURATION` | `"DAY"` | [HARD-CFM-RULE] unfilled = canceled, no trace |
| `ROLL_NET_PRICE_SOURCE` | `"reference_net_mid"` | [HARD-CFM-RULE] consistent w/ fill_verify |
| `ROLL_COMPLEX_STRATEGY_TYPE` | `"CUSTOM"` | [PROPOSED-DEFAULT] **[LIVE-VERIFY]** DIAGONAL vs CUSTOM |
| `ROLL_LEG_IMBALANCE_ACTION` | `"freeze"` | [HARD-CFM-RULE] never auto-correct |
| `PAPER_ROLL_HAIRCUT_CROSSINGS` | `1` | [PROPOSED-DEFAULT] single net crossing |
