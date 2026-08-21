# Decision — Remove RS3M-vs-Sector completely

**Date:** 2026-08-21
**Provenance:** `TRAVIS_EXTENSION`
**Status:** implemented
**Audit:** `AUDIT_REMOVE_SECTOR_RS_PHASE0.md` (Phase 0, same branch)

This record exists because part of what follows is a **deliberate loosening of a
safety mechanism**. A future reader must not read the absence of the sector
kill-switch trigger as an oversight. It was removed on purpose, with the
trade-off enumerated below and accepted.

---

## What was removed

**Everything keyed off a stock's relative strength against its sector ETF.**

1. **The entry veto.** `stock_lights.evaluate_vetoes` veto 1 (`rs3m_vs_sector < 0`,
   stocks only) — the RED-forcing veto that failed Level 3 of the entry gate.
2. **The kill-switch exit-now trigger.** `kill_switch.classify`'s first branch
   (`rs_vs_sector < 0` → RED, "EXIT immediately"), its `KILL_SWITCH_SECTOR` exit
   reason emission, its `KILL_RS_SECTOR` recommendation trigger, and its
   `KILL_SWITCH_SECTOR` alert.
3. **The kill-switch YELLOW "thinning" leg's sector half**
   (`rs_vs_sector < STOCK_RS_VS_SECTOR_MIN + 2`).
4. **The suitability AVOID rule** (`rs3m_vs_sector` negative → AVOID), which
   gated the recommendation pool, the internal queue and the intraday
   hot-refresh set.
5. **The RS1M-vs-sector ranking key.** Candidate ordering within GREENs now uses
   RS1M-vs-SPY for every name, stock and ETF alike.
6. **The two-speed RS-vs-Sector shadow** — the scan table's `RS` column, its
   contribution to the shadow SCORE, and the `TURNING` WATCH annotation.
7. All display, telemetry and config surfaces for the above.

**RS3M-vs-SPY is untouched** — the kill switch's confirmed-close exit (negative →
exit within 1–2 days) behaves identically, on the same inputs, with the same
threshold. The two-speed RS **vs SPY** shadow readout also remains.

## Why

**The benchmark is not a peer group.** A cap-weighted sector ETF (XLK and
friends) is dominated by a handful of mega-caps. Measuring a stock against it
does not answer "is this name leading its peers"; it largely answers "is this
name keeping up with the three largest companies in its sector". The comparison
was judged to carry no reliable meaning in this system.

A **rules-based industry peer-basket benchmark** is planned as the replacement
for sector-relative logic. It is explicitly **not** part of this change, and
nothing here should be read as a decision about its design.

### A rationale that was considered and rejected as factually wrong

The removal was originally motivated in part by the belief that the sector figure
was an *approximation* — the difference of two RS-vs-SPY values rather than a
direct comparison. **This is not true and is deliberately not part of this
record.** Every site already computed `indicators.rs3m(stock, peer)`, the true
63-day ratio. The codebase had migrated off the difference approximation earlier,
on purpose, tagging the result `[HARD_CFM_RULE / KILL_SWITCH_RS_SOURCE]`, and
`SNAPSHOT_SCHEMA_VERSION = 3` exists precisely to mark that migration. The
invalid-benchmark rationale above stands on its own and is the whole basis for
this decision.

---

## The safety trade-off, accepted

The sector trigger fired in cases the SPY trigger does not, or fires later. Those
cases now produce **no exit signal**. Enumerated so the cost is on record:

**A. Sector-RED fired, SPY never does — authority lost outright.**
A name lagging its sector while still beating SPY (`rs_vs_sector < 0`,
`rs_vs_spy >= 0`) was an immediate exit. It is now GREEN, or YELLOW only if
`rs_vs_spy < 5`. **At `rs_vs_spy >= 5` it reads fully green and holds.** A name
rolling over inside a strong sector is the archetype, and this is the largest
piece of the surface given up.

**B. Sector-RED fired first — timing lost.**
Where both eventually go negative, the sector leg usually crossed first: a stock
tends to underperform its own sector before it underperforms the broad market.
That exit now waits for the SPY crossing, which is both later and a downgrade
from "exit now" to "exit within 1–2 days".

**C. The YELLOW warning, and share adds.**
With `0 <= rs_vs_sector < 2` and `rs_vs_spy >= 5` the position read YELLOW
("thinning toward the kill line"). It now reads GREEN. Because
`position_manager.can_add_shares` blocks adds on red **or** yellow, the system
will now permit adding to a position it previously refused.

There is no way to keep a sector-based YELLOW once the sector figure is gone, and
raising the SPY YELLOW threshold to compensate would violate the requirement that
the SPY leg be behaviourally identical. So this consequence follows necessarily
from (2) and (3), and is accepted rather than mitigated.

**D. Recommendations.** `KILL_RS_SECTOR` exit recommendations are no longer
generated.

Unchanged: every SPY-RED case still fires, on the same inputs, in the same way.

---

## Compatibility — historical records are readable and untouched

The execution log is append-only and immutable. **Nothing was mutated or
stripped**, and no migration was written.

* `ExitReason.KILL_SWITCH_SECTOR` and `TriggerRule.KILL_RS_SECTOR` are
  **RETIRED, NOT DELETED.** Historical closes and recommendation records carry
  those exact strings, and the History tab and the CSV export read them back.
  They remain valid for READS and are removed only from the emitting paths and
  from `exit_reasons.CLOSE_TIME`, so no new record can be stamped with either.
  This follows the existing `LEGACY_UNRECORDED` pattern.
* `recompute_derived` reaches the entry snapshot only through
  `entry_context.summary`, a pure `.get()` read that ignores unknown keys, so old
  snapshots carrying sector fields recompute correctly and silently.
* `SNAPSHOT_SCHEMA_VERSION` 3 → 4: new snapshots stop carrying
  `rs3m_vs_sector`, `rs1m_vs_sector`, `rs3m_vs_sector_method` and
  `rs3m_vs_sector_benchmark`. v1/v2/v3 snapshots stay valid and readable by their
  own version tag.
* `scan_rejection_log` schema 3 → 4: new records stop carrying the vs-sector
  two-speed RS fields. Older records keep theirs.

## Consequences worth knowing

* **There is no relative-strength entry veto any more.** RS3M-vs-SPY was never
  one — the "beats SPY" gate leg had been removed earlier, leaving
  `config.rs_vs_spy_min()` with no production caller. The sector veto was the
  only RS check at entry. Entry now rests on the market regime, sector
  deterioration, the SYM four-light vote with its ATR/IVR and MA200 vetoes,
  structure entrability, the right spot, and the account overlay.
* **The shadow SCORE is recomposed.** Dropping the RS component takes the quality
  weights from 8.5 to 7.0; they are renormalized, so SCORE stays 0–10 but the
  remaining five components carry more of it. SCORE has no authority, so this
  changes no decision — but a score is not comparable across this change.
  *If the RS component is wanted back, the natural move is to re-point it at the
  already-computed vs-SPY state rather than reinstate a sector benchmark.*
* **Candidate ordering changed.** RS1M-vs-SPY now ranks every name; previously
  stocks ranked on RS1M-vs-sector and only ETFs on RS1M-vs-SPY.
