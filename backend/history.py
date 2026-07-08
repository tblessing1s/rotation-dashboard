"""Closed-cycle history + the juice journal export.

Cycle records are derived in logging_handler.recompute_derived (immutable
executions -> deterministic summaries); this module only aggregates and
formats them for the History tab and the operator's off-system record
(CFM's "juice journal" rule).
"""
from __future__ import annotations

import csv
import io

import config


def aggregates(cycles: list[dict]) -> dict:
    done = [c for c in cycles if c.get("net_return_pct") is not None]
    if not done:
        return {"count": 0, "win_rate": None, "avg_return_pct": None,
                "avg_days_held": None, "avg_juice_per_week": None,
                "avg_roll_drag": None, "target_hit_rate": None}
    wins = [c for c in done if (c.get("net_result") or 0) > 0]
    juice_per_week = [c["gross_juice"] / max(c["days_held"] / 7, 1)
                      for c in done if c.get("days_held")]
    return {
        "count": len(done),
        "win_rate": round(len(wins) / len(done) * 100, 1),
        "avg_return_pct": round(sum(c["net_return_pct"] for c in done) / len(done), 2),
        "avg_days_held": round(sum(c["days_held"] for c in done if c.get("days_held"))
                               / max(sum(1 for c in done if c.get("days_held")), 1), 1),
        "avg_juice_per_week": (round(sum(juice_per_week) / len(juice_per_week), 2)
                               if juice_per_week else None),
        "avg_roll_drag": round(sum(c.get("roll_drag") or 0 for c in done) / len(done), 2),
        "target_hit_rate": round(sum(1 for c in done if c.get("target_met")) / len(done) * 100, 1),
    }


def weekly_juice_chart(state: dict) -> dict:
    """Weekly net juice across the whole book, with the 1-2%/week-of-deployed
    target band (HARD_CFM_RULE) so the chart answers 'am I on pace'."""
    weeks: dict[str, float] = {}
    for w in state.get("theta_ledger", {}).get("weeks", []):
        weeks[w["week"]] = round(weeks.get(w["week"], 0.0) + float(w.get("net_juice") or 0), 2)
    import position_manager
    deployed = position_manager.deployed_capital(state)
    return {
        "weeks": [{"week": k, "net_juice": v} for k, v in sorted(weeks.items())],
        "target_low": round(deployed * config.WEEKLY_JUICE_TARGET_PCT_MIN / 100, 2),
        "target_high": round(deployed * config.WEEKLY_JUICE_TARGET_PCT_MAX / 100, 2),
        "capital_deployed": deployed,
    }


def view(state: dict) -> dict:
    cycles = state.get("cycles", [])
    return {
        "cycles": list(reversed(cycles)),  # newest first for the UI
        "aggregates": aggregates(cycles),
        "weekly_juice": weekly_juice_chart(state),
    }


# ---------------------------------------------------------------------------
# Juice journal export (CSV / markdown)
# ---------------------------------------------------------------------------
_WEEK_COLS = ["week", "ticker", "extrinsic_sold", "extrinsic_paid_back", "net_juice"]
# The cycle columns include the coded exit reason + note and a COMPACT entry-
# context summary (verdict, regime, IV rank, RS3M pair). The full snapshot is
# not in the CSV — it's available per cycle via the /api/history detail.
_CYCLE_COLS = ["id", "ticker", "entry_date", "exit_date", "days_held",
               "capital_deployed", "gross_juice", "roll_count", "roll_net",
               "roll_drag", "leap_pnl", "net_result", "net_return_pct",
               "target_met", "exit_reason", "exit_note",
               "verdict", "regime", "iv_rank", "rs3m_vs_spy", "rs3m_vs_sector"]
_ROLL_COLS = ["roll_id", "ticker", "date", "reason", "from_strike", "to_strike",
              "buyback_cost", "new_premium", "net"]


def _cycle_export_row(c: dict) -> dict:
    """Flatten a cycle for export: its own fields plus the compact entry_summary
    (verdict/regime/iv_rank/rs3m pair) hoisted to top-level column keys."""
    return {**c, **(c.get("entry_summary") or {})}


def juice_journal_csv(state: dict) -> str:
    """Three sections in one CSV: weekly juice ledger, roll ledger, cycles."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["# weekly juice ledger"])
    w.writerow(_WEEK_COLS)
    for row in state.get("theta_ledger", {}).get("weeks", []):
        w.writerow([row.get(c) for c in _WEEK_COLS])
    w.writerow([])
    w.writerow(["# roll ledger"])
    w.writerow(_ROLL_COLS)
    for row in state.get("roll_ledger", {}).get("rolls", []):
        w.writerow([row.get(c) for c in _ROLL_COLS])
    w.writerow([])
    w.writerow(["# closed cycles"])
    w.writerow(_CYCLE_COLS)
    for row in state.get("cycles", []):
        flat = _cycle_export_row(row)
        w.writerow([flat.get(c) for c in _CYCLE_COLS])
    return buf.getvalue()


def _md_table(cols: list[str], rows: list[dict]) -> list[str]:
    out = ["| " + " | ".join(cols) + " |",
           "| " + " | ".join("---" for _ in cols) + " |"]
    for r in rows:
        out.append("| " + " | ".join("" if r.get(c) is None else str(r.get(c))
                                     for c in cols) + " |")
    return out


def juice_journal_markdown(state: dict) -> str:
    agg = aggregates(state.get("cycles", []))
    lines = ["# CFM Juice Journal", "",
             f"- closed cycles: {agg['count']}",
             f"- win rate: {agg['win_rate']}%",
             f"- avg return: {agg['avg_return_pct']}%",
             f"- avg juice/week: {agg['avg_juice_per_week']}",
             f"- avg roll drag: {agg['avg_roll_drag']}", "",
             "## Weekly juice ledger", ""]
    lines += _md_table(_WEEK_COLS, state.get("theta_ledger", {}).get("weeks", []))
    lines += ["", "## Roll ledger", ""]
    lines += _md_table(_ROLL_COLS, state.get("roll_ledger", {}).get("rolls", []))
    lines += ["", "## Closed cycles", ""]
    lines += _md_table(_CYCLE_COLS, [_cycle_export_row(c) for c in state.get("cycles", [])])
    return "\n".join(lines) + "\n"
