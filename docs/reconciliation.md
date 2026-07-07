# Position reconciliation — does `state.json` match Schwab?

Every guardrail in this app (kill switch, coverage, payback meter, capital burn,
alerts) computes off `state.json`. Reconciliation is the one check that verifies
`state.json` matches what the brokerage account **actually holds**. It is the
last major safety layer.

**Design assumption (confirmed by the operator): all trading on this account
goes through this app.** So any divergence between Schwab and state is an
*anomaly* — assignment, expiry, a partial fill, a corporate action, or a bug —
never a "legitimate external trade." There is no adopt-external-trade flow. The
response to a divergence is always the same: **freeze the position, alert, and
let the operator resolve it.** The reconciler never rewrites `state.json` from
broker data — it detects and suggests; only you commit truth to the record.

---

## When it runs

- **Pre-market (the important one).** The first morning scheduler slot runs
  reconciliation before the alert pass — assignments materialize overnight, and
  pre-market is when you can act calmly.
- **Nightly.** The maintenance job runs it again.
- **On demand.** Settings tab → Data health → **Reconcile now**, or
  `POST /api/reconcile`.

It runs whenever Schwab is connected — a **read-only** account call, so
`CFM_LIVE_TRADING` is not required. In **paper mode** it runs report-only: only
positions established by live-transmitted orders are checked (paper positions
don't exist at the broker). In **demo mode** it reconciles the demo book against
a synthetic broker fixture so the UI can be previewed.

If the Schwab positions call **fails**, the run records a failure and generates
**no diffs** — a failed call never masquerades as an empty account. A valid
"zero positions" response (an all-cash account) is different and is reconciled
normally.

---

## CLEAN vs DIRTY

- **CLEAN** — every state-expected holding matches the broker (and any diffs are
  the benign expired-worthless kind). Nothing to do.
- **DIRTY** — at least one non-benign divergence. The affected position is
  **frozen** (`NEEDS REVIEW` badge on the Positions tab) and a `RECONCILE_DIRTY`
  alert fires.
- **FAILED** — the broker fetch failed; no diffs. If this persists,
  `RECONCILE_STALE` fires (the safety check has gone silent).

### Diff classifications

| Classification | Meaning | Typical cause |
|---|---|---|
| `MATCH` | Same instrument, same quantity | — (no diff recorded) |
| `MISSING_AT_BROKER` | State expects it, broker doesn't have it | Assignment, expiry, exercise, corporate action |
| `UNEXPECTED_AT_BROKER` | Broker holds it, state doesn't | A fill that committed differently than logged, corporate-action replacement symbol |
| `QUANTITY_MISMATCH` | Both have it, counts differ | Partial fill, partial assignment |
| `SHORT_STOCK_DETECTED` | Broker holds **short stock** against an open LEAP | **Assignment happened** — highest severity, time-sensitive |
| `EXPIRED_WORTHLESS_PENDING` | A short past expiry, underlying closed **below** strike | Benign weekly expiry — not a freeze |

**The expiry carve-out.** Before flagging a missing short as a problem, the
reconciler checks the underlying's cached close on the expiry day:

- close **below** strike → `EXPIRED_WORTHLESS_PENDING` (benign; a one-click "book
  at $0" suggestion, no freeze, no scary alert).
- close **at/above** strike → `MISSING_AT_BROKER` with `assignment_suspected`
  (expect a paired `SHORT_STOCK_DETECTED` or missing shares).
- no cached close for that day → `MISSING_AT_BROKER` (never silently benign).
- an **expired LEAP** (long call) is always `MISSING_AT_BROKER` — never benign.

---

## What a freeze does — and doesn't — block

While a position is frozen (`needs_review`):

- **Blocked (HTTP 409):** new-risk actions — `buy_leap`, `sell_short`,
  `roll_short`, `roll_leap`. No override. The 409 is distinct from the 400
  gate-rejection and carries the diff summary in the body.
- **Always allowed:** closing actions — `close_position_atomic` (atomic exit),
  `close_short`, `close_leap` (still subject to the existing naked-short guard).
  A freeze must never trap you in a position during a kill-switch event. This is
  deliberate: a freeze protects against *acting on wrong state*, but *exiting is
  safe* in either state of the world.
- **Metrics keep computing.** Payback, burn, and coverage still render, marked
  *"state unverified"* rather than hidden — suppressing them would blind you at
  exactly the wrong moment. The portfolio risk card shows a frozen-count context.

---

## Resolving a diff

Open the frozen position on the Positions tab and expand its **Reconciliation
review** panel. Each diff offers the appropriate action.

### 1. Expired worthless → one click

For `EXPIRED_WORTHLESS_PENDING`, click **Book expiry at $0.00**. This writes a
`close_short` execution at $0.00 premium with reason `expired_worthless`,
timestamped to the expiry date, and clears the diff. (`POST
/api/reconcile/resolve-expiry`.) You keep the full premium as juice — that's what
a worthless expiry means.

### 2. Everything else → a compensating adjustment

For any other diff, record a **compensating execution** — a new execution type
`adjustment` with a required typed reason. It flows through `recompute_derived()`
like any other execution, preserving append-only immutability: **history is
never edited, only corrected forward.** Fill in:

- **leg** — EQUITY or OPTION (and the strike, for an option),
- **qty Δ (signed)** — the change to apply to that leg's signed quantity
  (e.g. `+5` to close out an assigned 5-lot short, `-500` to book short stock),
- **reason** — required, logged onto the immutable record.

The adjustment links to the diff (`linked_diff_id`), marks it resolved, and lifts
the freeze once the position's diffs are all resolved.

### 3. Not actually a problem → acknowledge

If you've determined a diff is a non-issue (e.g. a corporate-action replacement
symbol you've verified), click **Acknowledge** and type a reason. The typed
`ack_reason` is logged onto the reconciliation record and the freeze lifts once
everything is resolved or acknowledged. (`POST /api/reconcile/acknowledge`.)

There is **no auto-correction.** The reconciler detects, freezes, and suggests;
you commit the truth.

---

## The short-stock playbook — why you never exercise the LEAP

`SHORT_STOCK_DETECTED` is the assignment-happened event and the highest-severity
diff. The short call was assigned, so the broker now holds **short stock** —
margin-consuming and time-sensitive — while your **LEAP** still sits there long.

The instinct is to exercise the LEAP to deliver the shares and "close it out."
**Do not.**

> **Assignment likely occurred. Do NOT exercise the LEAP to cover — buy back the
> short stock or close the position. Exercising forfeits all remaining LEAP
> extrinsic.**

The LEAP is a deep-ITM call carrying real time value. Exercising it throws that
extrinsic away for nothing. The correct move is to **buy back the short stock**
(returning the position to its intended shape) or **close the position** through
the normal exit — both of which preserve the LEAP's remaining value. After you've
acted at the broker, record a compensating `adjustment` (leg EQUITY, the signed
share delta, a reason) to bring `state.json` back in line, which clears the diff
and lifts the freeze.

`SHORT_STOCK_DETECTED` fires its own CRITICAL alert with its own dedup
fingerprint, so it escalates even when `RECONCILE_DIRTY` already fired for the
same ticker.

---

## Alerts

- `reconcile_dirty` (HIGH) — one alert per frozen ticker, with per-diff one-liners.
- `short_stock_detected` (CRITICAL) — separate, highest severity, the playbook copy above.
- `reconcile_stale` (MEDIUM) — reconciliation hasn't run successfully within
  `RECONCILE_STALE_HOURS` (default 36) while Schwab is connected and positions are
  open. Silence is itself a failure signal.

---

## Config thresholds

| Constant | Default | Provenance | Meaning |
|---|---|---|---|
| `RECONCILE_HISTORY_MAX` | 30 | PROPOSED_DEFAULT | Past reconciliation reports retained in `state.reconciliation.history`. |
| `RECONCILE_STALE_HOURS` | 36 | PROPOSED_DEFAULT | Max age of the last successful run before `reconcile_stale` fires. |

Schema: reconciliation is a v7 additive migration (a `reconciliation` store, a
per-position `needs_review` flag, and an explicit `live_transmitted` flag on every
execution). A pre-migration snapshot is taken automatically before it runs.
