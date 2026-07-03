# LEAP lifecycle — looking after the deployed capital

In a poor-man's covered call, the **LEAP is your capital**. The weekly short side
already tells you when to roll (75% rule), when to defend, and tracks roll drag.
This is the matching discipline for the long leg: when to roll the LEAP, whether
each position is still paying for its own decay, and an early warning before the
delta floor. Nothing here trades for you — it recommends and prepares the numbers;
you transmit.

Everything below shows up on the **Positions** tab in a compact "LEAP health"
strip per position: `DTE · extrinsic (≈ weeks of juice) · maint. · Δ`, plus a
`ROLL LEAP DUE` badge when a roll is recommended.

## When the app tells you to roll the LEAP

A `ROLL LEAP DUE` badge (and a `LEAP_ROLL_DUE` alert) appears when **either**:

- **DTE < 90** (`LEAP_ROLL_DTE_FLOOR`). Below ~90 days the long leg's own theta
  steepens — it stops behaving like a calm stock proxy and starts bleeding time
  value like a shorter-dated option. Roll before that.
- **Extrinsic runway < 4 weeks** (`LEAP_MIN_EXTRINSIC_WEEKS`). "Runway" is the
  LEAP's remaining extrinsic (time value) divided by your recent average weekly
  juice for that position. When the leg has less than about a month of juice left
  in it, its decay is about to outrun what the shorts collect against it.

Tap the badge to see the **roll-cost estimate**: the suggested replacement LEAP
(~0.90 delta, ~180 DTE), the estimated **net debit**, and a **reserve check** —
whether paying that debit still leaves enough cash for the 2×ATR defensive reserve
across the whole book. If it would breach the reserve, the roll needs an
`override_reason` (exactly like an entry that breaks the Level 5 gate). Estimates
use the live chain when available, otherwise Black-Scholes at the ticker's trailing
realized volatility; the staged roll re-prices from the live chain before you send.

The LEAP roll is transmitted as **one two-leg net ticket** (sell-to-close the old
LEAP + buy-to-open the new one) so it can't leg out.

## What the maintenance number means (juice vs. burn)

`maint.` on the strip is **net weekly maintenance = trailing weekly juice −
weekly LEAP burn**:

- **trailing weekly juice** — your average net juice (extrinsic sold − paid back)
  over the last 4 completed weeks for that position.
- **weekly LEAP burn** — the LEAP's extrinsic decay per week, computed as the
  option's Black-Scholes theta (not a straight line — theta accelerates as
  expiry nears, which is the whole point).

Green (`+`) means the position is **self-funding**: the shorts are collecting more
than the long is bleeding. Red (`−`) means it's **burning** — the flywheel is
running backwards. If maintenance stays negative for **2 consecutive weeks**
(`MAINTENANCE_NEGATIVE_WEEKS`) you get a `CAPITAL_BURN` alert. The usual fix is to
roll the LEAP deeper/longer (resetting the decay curve) or to reassess whether the
juice is still there.

## Delta velocity — the early warning

The existing rule exits/repairs when the LEAP delta falls below **0.50** (it stops
tracking the stock). That fires late. `DELTA_VELOCITY` is an earlier, rate-based
warning: it flags when the LEAP delta has dropped more than **0.08 over 5 sessions**
while still above the 0.50 floor. The strip shows the delta with a small trend
arrow (▼ falling / ▲ recovering). It's a heads-up, not a directive — it points you
at the kill-switch and circuit-breaker panels to make the call. Once delta is below
0.50 the existing floor alert owns it (the two never double-fire).

The daily delta is snapshotted by the nightly job, so this warms up over the first
week after a deploy (it starts empty and needs 5+ sessions of history).

## How payback continuity works across a LEAP roll

The extrinsic-payback meter tracks how much of a LEAP's entry extrinsic your
collected juice has paid back. A **LEAP roll is one continuous position**, not a
fresh start, so across a roll:

- the **juice you've already collected carries over**, and
- the **new LEAP's entry extrinsic is added** to the outstanding payback target.

A roll is recorded as linked `close_leap` + `buy_leap` executions sharing a
`leap_roll_id`, which is how the derived layer tells a roll apart from a true
**exit + re-entry**. If you fully exit a name (a `close_leap` with no linked
re-entry) and later open it again, that's a new cycle — the meter resets and does
**not** carry the old juice. This keeps the "am I in profit mode yet" number
honest whether you roll the long leg or start over.

## Safety rail: no naked shorts

You can't close the LEAP by itself while a short is still open against it — that
would leave a naked short call. A single-leg `close_leap` in that situation is
**rejected outright** (no override). To exit a full position use **Close position
(atomic)**: it sells-to-close the LEAP and buys-to-close the short on one net
ticket, which is the default action offered by the kill-switch and
circuit-breaker alerts — legging out by hand is most expensive exactly when those
fire. Single-leg closes remain available for legitimate cases (the short already
expired or was closed, or a shares-only trim).

## The thresholds (all tunable — `PROPOSED_DEFAULT`)

| Config | Default | Meaning |
| --- | --- | --- |
| `LEAP_ROLL_DTE_FLOOR` | 90 | Roll the LEAP under this DTE. |
| `LEAP_MIN_EXTRINSIC_WEEKS` | 4 | Roll when extrinsic runway is below this many weeks of juice. |
| `JUICE_TRAILING_WEEKS` | 4 | Window for average weekly juice. |
| `MAINTENANCE_NEGATIVE_WEEKS` | 2 | Consecutive burning weeks before `CAPITAL_BURN`. |
| `DELTA_HISTORY_DAYS` | 30 | Days of daily LEAP delta retained. |
| `DELTA_VELOCITY_DROP` | 0.08 | Delta drop over the window that triggers the warning. |
| `DELTA_VELOCITY_WINDOW` | 5 | Sessions the drop is measured over. |

None of these are CFM "hard rules" — they're sensible starting points pending
calibration against your own closed cycles.
