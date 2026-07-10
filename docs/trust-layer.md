# Recommendation Trust Layer (v2.6, state schema v17)

**There is no automation. Nothing in this version places, modifies, or cancels
an order on its own.** The trust layer exists to *earn* automation the honest
way: the app commits to explicit recommendations BEFORE you act, then measures
— from immutable records only — how often you and it agree, and whether every
live order lifecycle behaved exactly as specified. "Automation eligible" on the
scoreboard is a display-only readout of that evidence, and while post-fill
reconciliation is `NOT_YET_IMPLEMENTED`, **no action type can be eligible**.

## What you'll see

**Recommendation cards** on each position (Positions tab). Every scheduled
alert slot (08:30 → 16:15 ET) also runs a recommendation pass. For each open
position it either emits an actionable recommendation — EXIT, DEFEND, ROLL_OUT
— with a concrete proposed ticket (legs, strikes, net limit, minimum
acceptable net credit, max slippage), or an explicit **ALL_CLEAR**. Silence is
not a valid output: if you act and no recommendation existed, that becomes a
**coverage miss**, the loudest failure on the scoreboard.

- **Execute** stages the proposed ticket into the normal execute flow (the
  same roll modal / close flow you already use — the engine and the modal read
  the same `strike_policy`, so they cannot disagree about the suggested
  strike). The execution then carries `source_rec_id` so matching is exact.
- **Dismiss** requires a coded reason: `DISAGREE_TIMING`, `DISAGREE_STRIKE`,
  `DISAGREE_ACTION`, `EXTERNAL_INFO`, `DISCIPLINE_LAPSE`, or `OTHER` (typed
  note required). These feed the precision metric — dismissing honestly is how
  the engine learns where it's wrong.
- Recommendations expire (`valid_until`). A stale recommendation never matches
  a later action — acting late counts as a miss, on purpose.

**The Trust Scoreboard** (Settings tab), per action type:

| Metric | Question it answers | Math (all derived in `recompute_derived`) |
|---|---|---|
| **Coverage** | When I acted, had the engine already committed? | matched ÷ (matched + coverage misses) |
| **Precision** | When the engine committed, did I agree? | matched ÷ (matched + overridden) |
| **Timeliness** | How long after the condition turned true did it commit? | emission lag per rec; "late after action" flags |
| **Fidelity** | Did live order lifecycles behave exactly as specified? | per-ticket pass rate (below) |
| **Graduation** | Is this action type automation-eligible? | ALL criteria below over the trailing window |

**Fidelity checks** per order ticket (paper tickets are graded too, flagged
paper): `LIFECYCLE_LEGAL` (every observed state transition legal per the order
state machine and the cancel/retry rules), `SLIPPAGE_IN_BOUND` (fill within
the max-slippage bound the ticket priced), `NO_ORPHAN_LEG` (both legs of a
two-leg ticket filled, or neither — the fill-during-cancel race is detected),
`CANCEL_CONFIRMED_DEAD` (every cancel confirmed terminal at Schwab, not merely
requested), `RECONCILED_CLEAN` (**NOT_YET_IMPLEMENTED** — it will never
silently pass; the post-fill broker reconciliation diff is a separate work
item). Failures page you through the normal alert channels.

## Graduation criteria (per action type, trailing window)

- ≥ `GRAD_MIN_LIVE_CYCLES` (10) live matched instances — paper doesn't count.
- Window length `GRAD_MIN_WEEKS`: ROLL_OUT 8, ROLL_DOWN/DEFEND 16, EXIT 26
  weeks. **ENTER is never auto-eligible in this iteration.**
- Coverage misses in window = 0 — *hard requirement, not tunable*.
- Override rate ≤ `GRAD_MAX_OVERRIDE_RATE` (0.10), with zero unresolved
  `DISAGREE_ACTION` overrides.
- Fidelity pass rate = 100% for the ticket type — *hard requirement*.
- Reconciliation green throughout — *hard requirement*; `NOT_YET_IMPLEMENTED`
  blocks everything, and the scoreboard says so by name.

Tunable numbers are `PROPOSED_DEFAULT` in `backend/config.py`; the hard
requirements are code, not config.

## What is deliberately OUT of coverage scope

These operator actions never synthesize a coverage miss (and never match):
mechanical LEAP rolls (`roll_leap` / `leap_roll_id` pairs), the roll legs of a
kill-switch exit (the EXIT itself is matched via the LEAP close), scale-in
adds (`leap_add`), standalone single-leg repairs, and reconciliation
`adjustment` records. Executions from before the trust layer activated
(`metadata.trust_layer_since`) are likewise excluded — they predate the engine
and would all read as misses.

## Mechanics worth knowing

- Recommendations and overrides are **append-only and immutable**; every
  score is re-derived from them on every write. Nothing on the scoreboard can
  be hand-edited.
- One dominant recommendation per position per pass (exit triggers beat
  defends, defends beat rolls); everything else that fired is preserved in the
  record's `input_snapshot.secondary_triggers`.
- A re-evaluation that changes its mind **supersedes** the open record (the
  old one becomes unmatchable); one that agrees emits nothing — the open
  record is the claim, so a restart never duplicates it.
- The engine is a pure function over a frozen market snapshot + injected
  clock (`recommendation_engine.evaluate`). The scheduled pass and any future
  automation call the *same* function — that sameness is what makes this
  evidence transferable, and it is enforced by the offline test suite
  (including the XLK July-6th no-enter regression lock and the AAPL laggard
  kill-switch case).
- Manual pass: `POST /api/recommendations/run` or the button on the
  scoreboard panel. Scheduler toggle: `CFM_RECOMMENDATIONS=0`.
