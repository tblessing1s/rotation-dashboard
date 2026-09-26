"""Coverage gaps — the stretches when owned 100-share lots had NO call on them.

A roll that is legged (close fills, then the replacement is sold later), an
entry that buys the shares before the first call is sold, and an exit that buys
back the last call before the shares are sold all leave the base leg naked-long
for however long the gap lasts. Whatever the stock does in that window lands in
P&L as raw, unhedged stock exposure — it is not juice and nothing offsets it.

This module MEASURES those windows from the immutable execution log. It has NO
authority (see CLAUDE.md, shadow mode): it never blocks, gates or sizes
anything, and it is never appended to the verdict ``blocks`` list. It exists so
the realized P&L the operator sees can be split into "juice I collected" vs
"stock moves while I was uncovered", and so an open gap is visible (and alerted)
while it is still open.

Model
-----
Walk one ticker's derived executions in fill-time order, tracking owned shares
and open short contracts. Only FULL lots are coverable (a fragment is never
coverable — the same rule as ``position_manager.delta_coverage``), so

    uncovered_shares = max(floor(shares / 100) - short_contracts, 0) * 100

Every time that number changes, the running window closes and (if non-zero) a
new one opens. A window's gap P&L is ``uncovered_shares * (end_px - start_px)``
using the stock price stamped on the fills that bound it (``stock_price``, the
fill-time capture — see executor._stamp_fill_spot). A window whose bounding
fills lack a price is kept but reported unpriced, never guessed.

Kinds (what the operator should have done differently):
  * ``entry``  — opened by a share purchase; close shares + first call together.
  * ``roll``   — opened by a call buy-back, closed by a call sale; roll as ONE
                 two-leg ticket (the app's atomic roll) so there is no window.
  * ``exit``   — closed by a share sale; decide exit-vs-recover before buying
                 back the last call, and sell the shares right behind it.
  * ``expiry`` — opened by a call expiring; the replacement goes on at the open.
  * ``other``  — anything else (adoption corrections, partials).
"""
from __future__ import annotations

from datetime import datetime, timezone

import config
import logging_handler as log

SHARES_PER_LOT = config.SHARES_PER_LOT

_SHARE_ADD = {"buy_shares"}
_SHARE_REDUCE = {"sell_shares", "close_shares_assigned"}
_SHORT_OPEN = {"sell_short"}
_SHORT_CLOSE = {"close_short", "close_shares_assigned"}
_RELEVANT = _SHARE_ADD | _SHARE_REDUCE | _SHORT_OPEN | _SHORT_CLOSE


def _parse_time(value) -> datetime | None:
    """ISO timestamps with Z / +0000 / +00:00 offsets, or a bare YYYY-MM-DD
    (treated as midnight UTC — an adopted fill with only a date)."""
    if not value:
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":" and s[-4:].isdigit():
        s = s[:-2] + ":" + s[-2:]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fill_time(e: dict) -> datetime | None:
    """When an execution actually traded: the broker fill time when stamped,
    else its booked ``date``."""
    return _parse_time(e.get("fill_time")) or _parse_time(e.get("date"))


def _price(e: dict) -> float | None:
    for k in ("stock_price_at_fill", "stock_price"):
        v = e.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    if e.get("action") in _SHARE_ADD | {"sell_shares"} and e.get("price_per_share") is not None:
        return float(e["price_per_share"])
    return None


def _share_qty(e: dict) -> int:
    q = e.get("qty")
    if q is None and e.get("action") == "close_shares_assigned":
        q = int(e.get("contracts") or 0) * SHARES_PER_LOT
    return int(q or 0)


def _is_expiry(e: dict) -> bool:
    return (e.get("reason") == "expired_worthless"
            or e.get("exit_reason") == "expired_worthless")


def _kind(start_e: dict, end_e: dict | None) -> str:
    sa = start_e.get("action")
    ea = (end_e or {}).get("action")
    if sa in _SHARE_ADD:
        return "entry"
    if ea in _SHARE_REDUCE:
        return "exit"
    if sa == "close_short" and _is_expiry(start_e):
        return "expiry"
    if sa == "close_short" and (ea in _SHORT_OPEN or end_e is None):
        return "roll"
    return "other"


def _ref(e: dict) -> dict:
    return {"id": e.get("id"), "action": e.get("action"), "strike": e.get("strike"),
            "time": (fill_time(e).isoformat() if fill_time(e) else None),
            "stock_price": _price(e)}


def _close_window(start_e, end_e, uncovered, end_time=None, end_px=None) -> dict:
    t0 = fill_time(start_e)
    t1 = end_time if end_e is None else fill_time(end_e)
    p0 = _price(start_e)
    p1 = end_px if end_e is None else _price(end_e)
    seconds = (t1 - t0).total_seconds() if (t0 and t1) else None
    pnl = round(uncovered * (p1 - p0), 2) if (p0 is not None and p1 is not None) else None
    return {
        "kind": _kind(start_e, end_e),
        "open": end_e is None,
        "uncovered_shares": uncovered,
        "start": _ref(start_e),
        "end": _ref(end_e) if end_e is not None else None,
        "start_time": t0.isoformat() if t0 else None,
        "end_time": t1.isoformat() if t1 else None,
        "seconds": round(seconds) if seconds is not None else None,
        "start_price": p0, "end_price": p1,
        "move_per_share": round(p1 - p0, 4) if (p0 is not None and p1 is not None) else None,
        "gap_pnl": pnl,
    }


def windows_for(executions: list[dict], now: datetime | None = None,
                live_price: float | None = None) -> list[dict]:
    """Every uncovered window in one ticker's executions, oldest first. The last
    window is ``open`` when lots are uncovered right now; it is marked to
    ``live_price`` at ``now`` when given (unpriced otherwise)."""
    rows = [e for e in executions if e.get("action") in _RELEVANT and fill_time(e)]
    # Stable sort: two legs of one atomic roll share a fill time and keep log order.
    rows.sort(key=fill_time)
    shares = 0
    shorts = 0
    uncovered = 0
    start = None
    out: list[dict] = []
    for e in rows:
        a = e.get("action")
        if a in _SHARE_ADD:
            shares += _share_qty(e)
        if a in _SHARE_REDUCE:
            shares = max(shares - _share_qty(e), 0)
        if a in _SHORT_OPEN:
            shorts += int(e.get("contracts") or 0)
        if a in _SHORT_CLOSE:
            shorts = max(shorts - int(e.get("contracts") or 0), 0)
        now_uncovered = max(shares // SHARES_PER_LOT - shorts, 0) * SHARES_PER_LOT
        if now_uncovered == uncovered:
            continue
        if uncovered and start is not None:
            w = _close_window(start, e, uncovered)
            if w["seconds"] is None or w["seconds"] > 0 or (w["move_per_share"] or 0) != 0:
                out.append(w)
        uncovered = now_uncovered
        start = e if uncovered else None
    if uncovered and start is not None:
        out.append(_close_window(start, None, uncovered,
                                 end_time=now or datetime.now(timezone.utc),
                                 end_px=live_price))
    return out


def summarize(windows: list[dict]) -> dict:
    by_kind: dict[str, dict] = {}
    total = 0.0
    priced = 0
    seconds = 0
    for w in windows:
        k = by_kind.setdefault(w["kind"], {"count": 0, "gap_pnl": 0.0, "seconds": 0})
        k["count"] += 1
        if w["gap_pnl"] is not None:
            k["gap_pnl"] = round(k["gap_pnl"] + w["gap_pnl"], 2)
            total += w["gap_pnl"]
            priced += 1
        if w["seconds"]:
            k["seconds"] += w["seconds"]
            seconds += w["seconds"]
    open_w = next((w for w in reversed(windows) if w["open"]), None)
    return {
        "authority": "none",
        "count": len(windows),
        "priced_count": priced,
        "unpriced_count": len(windows) - priced,
        "gap_pnl": round(total, 2),
        "seconds": seconds,
        "by_kind": by_kind,
        "open": open_w,
    }


def for_ticker(state: dict, ticker: str, now: datetime | None = None,
               live_price: float | None = None, since: str | None = None) -> dict:
    """Windows + summary for one ticker. ``since`` (ISO) keeps only windows that
    started at/after it — e.g. the current position's entry date, so an earlier,
    closed cycle on the same name doesn't pad the current card."""
    t = (ticker or "").upper()
    execs = [e for e in log.derived_executions(state)
             if (e.get("ticker") or "").upper() == t]
    ws = windows_for(execs, now=now, live_price=live_price)
    if since:
        cutoff = _parse_time(since)
        if cutoff:
            ws = [w for w in ws if (_parse_time(w["start_time"]) or cutoff) >= cutoff]
    return {"ticker": t, "windows": ws, "summary": summarize(ws)}


def book(state: dict, now: datetime | None = None) -> dict:
    """Every ticker that has ever held shares, plus a book-wide total."""
    tickers = sorted({(e.get("ticker") or "").upper() for e in log.derived_executions(state)
                      if e.get("action") in _SHARE_ADD and e.get("ticker")})
    per = {t: for_ticker(state, t, now=now) for t in tickers}
    total = round(sum(v["summary"]["gap_pnl"] for v in per.values()), 2)
    return {"authority": "none", "gap_pnl": total, "tickers": per}
