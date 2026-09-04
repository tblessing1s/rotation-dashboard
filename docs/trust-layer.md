# Recommendation Trust Layer (v2.6, state schema v17)

**There is no automation. Nothing in this version places, modifies, or cancels
an order on its own.** The trust layer exists to *earn* automation the honest
way: the app commits to explicit recommendations BEFORE you act, then measures
— from immutable records only — how often you and it agree, and whether every
live order lifecycle behaved exactly as specified. "Automation eligible" on the
scoreboard is a display-only readout of that evidence, and while post-fill
reconciliation is `NOT_YET_IMPLEMENTED`, **no action type can be eligible**.

## What you'll see

**ENTER recommendations** clear two more gates than the scan's verdict: one
100-share lot must be **≤ the dry powder** deployable right now (the tighter
of the capital-cap headroom and cash above the defensive reserve — the same
figure the Overview's barrel shows; inactive only while operating cash was
never configured), and the weekly juice is judged on one **full
Friday-to-Friday week** (`JUICE_WEEK_CALENDAR_DAYS`) so every name is compared
on the same basis whatever weekday the scan runs. The proposed first call is
sold at the **earliest full-week expiration** (≥ `FULL_WEEK_MIN_SESSIONS`
sessions; a Tuesday entry skips this Friday's partial week), priced at its
own DTE; the card shows lot vs dry powder, the full-week %, and the first
call's expiration, DTE and %-to-expiry.

**Recommendation cards** on each position (Positions tab). Every scheduled
alert slot (08:30 → 16:15 ET) runs a recommendation pass, and so does an
**event**: during market hours the tiered quote poller (2-minute Tier 0
cadence) hands each cycle's prints to `event_runner`, which runs the engine
at once when a roll-family signal flips true for a name on the fresh quote
(75% buyback rule, extrinsic-captured threshold, the dividend and plain
assignment-risk variants — the same `enrich_short` signals the engine reads),
when a date-driven trigger flips for a name holding a short (an earnings
report entering the `EARNINGS_WARN_DAYS` window, read from the same
cache-only `earnings.cached_earnings` the snapshot uses — so a date landed by
the nightly or hot refresh raises the card on the next poll, not the next
slot), or the poller escalates (a defense level breached, SPY / a held sector
moving hard). Edges only, never "still
true"; one run per name per `EVENT_RUN_COOLDOWN_SECONDS` (15 min) and never
two inside `EVENT_RUN_MIN_GAP_SECONDS` (2 min); a restart primes silently.
Each open short call rides its underlying's batched quote on every poll (one
request either way), so its live mark is as fresh as the stock's: the 75% rule
and the extrinsic-captured threshold react to the option's own price intraday,
the engine's snapshot freezes the same marks (`short_marks`), and the Positions
view is served from that cache instead of quoting on its own. A mark older
than `OPTION_MARK_MAX_AGE_SECONDS` is ignored and the stored entry mark is used,
exactly as before.
The scheduled slots remain the floor, and close-only rules (kill switch,
circuit breaker) read the same daily bars an intraday slot would. The
Overview's "engine ran" line says what triggered the last pass.
`CFM_EVENT_RUNS=0` turns event runs off. For each open
position it either emits an actionable recommendation — EXIT, DEFEND, ROLL_OUT
— with a concrete proposed ticket (legs, strikes, net limit, minimum
acceptable net credit, max slippage), or an explicit **ALL_CLEAR**. ROLL_OUT
fires on the weekly cadence, the 75% rule, an earnings window, and
`ROLL_EXTRINSIC_CAPTURED`: once `ROLL_EXTRINSIC_CAPTURED_PCT` (80%) of the
extrinsic sold at entry is banked, at any remaining DTE, the engine proposes
rolling out to the next weekly to sell fresh juice (roll reason
`extrinsic-captured`). Silence is
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

**Moves you make yourself resolve the open recommendation** — you never have to
dismiss a card for something you already did. Matching connects an operator
MOVE to the engine's call on that position, wherever the move came from:

| You did it… | Execution carries | Resolution `source` |
|---|---|---|
| from the card's **Execute** | `source_rec_id` | `engine_card` |
| by hand in the app (roll modal / order ticket) | nothing special | `app_manual` |
| by hand **at Schwab**, adopted from the transaction feed | `source: broker_manual`, `roll_reason: broker_manual_roll` | `broker_manual` |

- **A roll is a roll.** A roll of any reason (or a broker-adopted roll, which has
  no reason) matches the open roll-family recommendation — ROLL_OUT, ROLL_DOWN
  or DEFEND — on that position. An exact action-type match is preferred; a
  family match records the difference as `deltas.action_delta`
  (e.g. `DEFEND->ROLL_OUT`) and counts on the scoreboard as `matched_diverged`.
- **Acting differently is an override, not a miss.** If the engine said EXIT
  and you rolled (or said ROLL and you exited), the recommendation resolves as
  `OVERRIDDEN` with the derived reason `ACTED_DIFFERENTLY` — you disagreed with
  your hands instead of the Dismiss button. It counts against precision like
  any override; it is never a coverage miss, because the engine did commit.
  The reason is derived-only: the dismiss endpoint refuses it.
- **A recommendation withdrawn by an ALL_CLEAR still matches a move made
  before the all-clear.** A roll done at Schwab is adopted only after the next
  pass has seen the new short and cleared the position; the fill predates the
  all-clear, so it is the recommendation's match, not a miss. A move made
  AFTER the all-clear is late, and a miss.
- Your explicit dismissal always wins over a derived override.
- Each position card shows how its last engine call was closed out ("engine
  called DEFEND · you rolled at Schwab to 176, +0.5 vs proposed") for two weeks.

**The Trust Scoreboard** (Settings tab), per action type:

| Metric | Question it answers | Math (all derived in `recompute_derived`) |
|---|---|---|
| **Coverage** | When I acted, had the engine already committed? | matched ÷ (matched + coverage misses) |
| **Precision** | When the engine committed, did I agree? | matched ÷ (matched + overridden); `matched_by_source` splits matches by card / app by hand / Schwab by hand, `matched_diverged` counts roll-family matches of a different type |
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

## Acknowledging a coverage miss

A miss cannot be dismissed — it is derived from the immutable execution and
recommendation logs, and the zero-misses graduation rule is code, not config.
What you *can* do is classify it. **Acknowledge** on the scoreboard's miss list
records an append-only `coverage_miss_acks` entry keyed on the miss's execution
ids, with a coded reason:

- `OPERATOR_DISCRETION` — you acted outside the rules on purpose (the engine
  was right to stay silent; the disagreement is yours).
- `ENGINE_MISSED` — a rule should have fired; treat it as an engine defect.
- `RULE_GAP` — nothing in the rule set covers what you did; a candidate rule.
- `OTHER` — typed note required.

An acknowledged miss **still counts**: the coverage rate and the graduation gate
read it exactly as before. It stops re-paging through `TRUST_COVERAGE_MISS`,
reads as acknowledged on the board, and gives the calibration record a reason
instead of a blank. First acknowledgement wins; there is no un-acknowledge.

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
