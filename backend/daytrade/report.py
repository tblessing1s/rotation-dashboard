"""Day-trade performance report — per account, read-only.

Flattens the account's trade logs (``daytrade/store.py``) into one row per
trade and rolls them up by Total / Year / Month / Week / Day, then renders
the result as JSON, CSV, or a Markdown pack sized to paste into a chat for
feedback on the trades. Pure recompute from the trade logs on every call
(same trade-off as ``daytrade.trial``): no second ledger to drift.

Trades are bucketed by the trading DAY they opened (the log's own
``YYYY-MM-DD`` key) — day trades close the same session, so that is also the
day they settle. Weeks are ISO weeks labelled ``YYYY-Www`` (Monday start).
Realized P&L includes banked partial exits of a still-open trade; win/loss/
flat is judged on CLOSED trades only.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime

from daytrade import store

PERIODS = ("total", "year", "month", "week", "day")

TRADE_COLS = ["date", "symbol", "direction", "status", "entry_at", "entry_price", "size",
              "notional", "exit_kinds", "closed_at", "hold_minutes", "realized_pnl",
              "realized_r", "trade_id"]
SUMMARY_COLS = ["period", "trades", "closed", "open", "wins", "losses", "flat", "win_rate",
                "net_pnl", "net_r", "avg_r", "avg_win", "avg_loss", "profit_factor",
                "best_trade", "worst_trade", "avg_notional"]


def _minutes(a: str | None, b: str | None):
    try:
        return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60, 1)
    except (TypeError, ValueError):
        return None


def trade_rows(account_id: str, since: str | None = None, until: str | None = None) -> list[dict]:
    """One flat row per trade, oldest first. ``since``/``until`` are inclusive
    ``YYYY-MM-DD`` bounds on the trade date."""
    rows = []
    for day, day_trades in store.iter_all_trades(account_id):
        if (since and day < since) or (until and day > until):
            continue
        for t in day_trades.values():
            entry = t.get("entry") or {}
            exits = t.get("exits") or []
            price, size = entry.get("price"), entry.get("size")
            rows.append({
                "date": day, "symbol": t.get("symbol"), "direction": t.get("direction"),
                "status": t.get("status"), "entry_at": entry.get("at"),
                "entry_price": price, "size": size,
                "notional": round(price * size, 2) if price and size else None,
                "exit_kinds": ">".join(e.get("kind", "") for e in exits),
                "closed_at": t.get("closed_at"),
                "hold_minutes": _minutes(entry.get("at"), t.get("closed_at")),
                "realized_pnl": round(t.get("realized_pnl") or 0.0, 2),
                "realized_r": t.get("realized_r"),
                "trade_id": t.get("trade_id"),
            })
    rows.sort(key=lambda r: (r["date"], str(r["entry_at"] or ""), str(r["trade_id"] or "")))
    return rows


def period_key(day: str, period: str) -> str:
    if period == "total":
        return "Total"
    if period == "year":
        return day[:4]
    if period == "month":
        return day[:7]
    if period == "week":
        iso = date.fromisoformat(day).isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    return day


def _summarize(label: str, rows: list[dict]) -> dict:
    closed = [r for r in rows if r["status"] == "closed"]
    wins = [r["realized_pnl"] for r in closed if r["realized_pnl"] > 0]
    losses = [r["realized_pnl"] for r in closed if r["realized_pnl"] < 0]
    rs = [r["realized_r"] for r in closed if r["realized_r"] is not None]
    pnls = [r["realized_pnl"] for r in rows]
    notionals = [r["notional"] for r in rows if r["notional"]]
    gross_loss = -sum(losses)
    return {
        "period": label, "trades": len(rows), "closed": len(closed),
        "open": len(rows) - len(closed), "wins": len(wins), "losses": len(losses),
        "flat": len(closed) - len(wins) - len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "net_pnl": round(sum(pnls), 2), "net_r": round(sum(rs), 4),
        "avg_r": round(sum(rs) / len(rs), 4) if rs else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss else None,
        "best_trade": max(pnls) if pnls else None,
        "worst_trade": min(pnls) if pnls else None,
        "avg_notional": round(sum(notionals) / len(notionals), 2) if notionals else None,
    }


def summaries(rows: list[dict], period: str) -> list[dict]:
    """Roll ``rows`` up by ``period`` — oldest first (Total is one row)."""
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(period_key(r["date"], period), []).append(r)
    return [_summarize(k, buckets[k]) for k in sorted(buckets)]


def build_report(account_id: str, since: str | None = None, until: str | None = None) -> dict:
    rows = trade_rows(account_id, since, until)
    return {
        "account_id": account_id, "since": since, "until": until,
        "total": _summarize("Total", rows) if rows else None,
        **{p: summaries(rows, p) for p in PERIODS[1:]},
        "trades": rows,
    }


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _csv(cols: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in cols])
    return buf.getvalue()


def _md_table(cols: list[str], rows: list[dict]) -> str:
    def fmt(v):
        return "" if v is None else str(v).replace("|", "/")
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(fmt(r.get(c)) for c in cols) + " |" for r in rows]
    return "\n".join(lines)


def to_csv(report: dict, view: str) -> str:
    if view == "trades":
        return _csv(TRADE_COLS, report["trades"])
    rows = [report["total"]] if view == "total" and report["total"] else report.get(view, [])
    return _csv(SUMMARY_COLS, rows)


def to_markdown(report: dict) -> str:
    """A self-contained pack for pasting into a chat: what it is, how to read
    it, every rollup, then the trade-by-trade list."""
    span = f"{report['since'] or 'start'} to {report['until'] or 'latest'}"
    out = [
        f"# Day-trade paper-trading report — account `{report['account_id']}` ({span})",
        "",
        "Paper-mode trades from a rules-based intraday strategy (5-min bars, entry on a "
        "volume-confirmed breakout, stop = ATR-based, half off at +1R, then a loose trailing stop on the "
        "rest that tightens at +2R, time cutoff). R = multiple of the initial per-share risk; P&L is realized "
        "dollars. Win rate / wins / losses count closed trades only. Please review the "
        "results and the individual trades for patterns, rule-following, and what to improve.",
        "",
        "## Total", "",
    ]
    out.append(_md_table(SUMMARY_COLS, [report["total"]]) if report["total"] else "_No trades yet._")
    for title, key in (("Yearly", "year"), ("Monthly", "month"), ("Weekly", "week"),
                       ("Daily", "day")):
        out += ["", f"## {title}", "", _md_table(SUMMARY_COLS, report[key]) if report[key]
                else "_No trades._"]
    out += ["", "## Trades", "", _md_table(TRADE_COLS, report["trades"]) if report["trades"]
            else "_No trades._", ""]
    return "\n".join(out)


def to_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True, default=str)
