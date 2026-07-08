"""Paper-fill slippage — measure how far live fills land from the quoted mid.

Paper fills are booked at the quoted MIDPOINT, but deep-ITM options rarely fill
at mid: every paper cycle's juice is optimistic by ~half the spread, twice a
week, and that bias compounds through the payback meter and into the calibration
harness's threshold tuning. This module turns real fills into a measured haircut.

Each live-transmitted execution carries the reference mid captured at order time
(``quoted_mid_per_share``, the placement limit) and its actual fill price. The
adverse slippage per fill is the fraction of the mid we gave up — signed by side
(paying above mid on a buy, receiving below mid on a sell). Until
``SLIPPAGE_MIN_FILLS`` live fills exist the realized number isn't trustworthy, so
paper results carry a mid-fill caveat and the ``ASSUMED_SLIPPAGE_PCT`` default;
past that bar the measured slippage supersedes the assumption.

Read-only and pure over state — no provider calls, works offline / in demo.
"""
from __future__ import annotations

import config

# BUY legs pay the ask (adverse = fill above mid); SELL legs hit the bid (adverse
# = fill below mid). Mirrors executor.INSTRUCTION's open/close-to-buy/sell split.
_BUY_ACTIONS = {"buy_leap", "close_short"}
_SELL_ACTIONS = {"sell_short", "close_leap"}


def _recorded_per_share(e: dict) -> float | None:
    """The per-share option price we logged, normalized across the leap
    (per-contract dollars) and short (per-share) storage conventions. Mirrors
    fill_verify._recorded_per_share so both read fills the same way."""
    action = e.get("action")
    try:
        if action == "buy_leap":
            return float(e.get("execution_price") or 0) / 100.0
        if action == "close_leap":
            return float(e.get("close_price") or 0) / 100.0
        if action == "sell_short":
            return float(e.get("premium_per_share") or 0)
        if action == "close_short":
            return float(e.get("close_price_per_share") or 0)
    except (TypeError, ValueError):
        return None
    return None


def _fill_slippage(e: dict) -> dict | None:
    """Adverse slippage for one live fill as a % of the reference mid, or None
    when the fill lacks a usable mid/price (rolls, pre-capture executions)."""
    if e.get("live_transmitted") is not True:
        return None
    action = e.get("action")
    mid = e.get("quoted_mid_per_share")
    rec = _recorded_per_share(e)
    if mid is None or rec is None:
        return None
    try:
        mid = float(mid)
    except (TypeError, ValueError):
        return None
    if mid <= 0:
        return None
    if action in _BUY_ACTIONS:
        frac = (rec - mid) / mid          # paid above mid = positive (adverse)
    elif action in _SELL_ACTIONS:
        frac = (mid - rec) / mid          # received below mid = positive (adverse)
    else:
        return None
    return {"execution_id": e.get("id"), "action": action, "ticker": e.get("ticker"),
            "quoted_mid": round(mid, 4), "fill": round(rec, 4),
            "slippage_pct": round(frac * 100, 3)}


def _roll_net_slippage(state: dict) -> list[dict]:
    """Net slippage per atomic roll (R5): the adverse deviation of the realized
    NET fill from the reference NET mid captured at ticket time (mid(new short) −
    mid(old short)). A higher net is always better (more credit / less debit), so
    adverse = (reference − realized) / |reference|, positive = worse for us. One
    entry per roll_group (both legs carry the same net fields); live rolls only."""
    seen: dict[str, dict] = {}
    for e in state.get("executions", []):
        gid = e.get("roll_group_id")
        if not gid or gid in seen:
            continue
        if e.get("live_transmitted") is not True:
            continue
        ref = e.get("roll_reference_net_mid")
        net = e.get("roll_net_fill")
        if ref is None or net is None:
            continue
        try:
            ref = float(ref)
            net = float(net)
        except (TypeError, ValueError):
            continue
        if abs(ref) < 1e-9:
            continue
        adverse = (ref - net) / abs(ref)
        seen[gid] = {
            "roll_group_id": gid, "ticker": e.get("ticker"),
            "reference_net_mid": round(ref, 4), "net_fill": round(net, 4),
            "net_slippage_pct": round(adverse * 100, 3),
            "alloc_method": e.get("roll_alloc_method"),
        }
    return list(seen.values())


def roll_report(state: dict) -> dict:
    """Net roll-slippage summary — one net crossing per roll, not two per-leg
    crossings (PAPER_ROLL_HAIRCUT_CROSSINGS=1). Live rolls only."""
    rolls = _roll_net_slippage(state)
    n = len(rolls)
    mean = round(sum(r["net_slippage_pct"] for r in rolls) / n, 3) if n else None
    return {"live_rolls": n, "mean_net_slippage_pct": mean, "recent_rolls": rolls[-20:]}


def report(state: dict) -> dict:
    """Realized-vs-assumed slippage summary for the paper-fill caveat + haircut.

    ``effective_slippage_pct`` is the measured mean once ``SLIPPAGE_MIN_FILLS``
    live fills exist, else the assumed default; ``mid_fill_caveat`` is True while
    the assumption is still in force (paper results should say so)."""
    fills = [s for s in (_fill_slippage(e) for e in state.get("executions", [])) if s]
    n = len(fills)
    sufficient = n >= config.SLIPPAGE_MIN_FILLS
    measured = round(sum(f["slippage_pct"] for f in fills) / n, 3) if n else None
    assumed = round(config.ASSUMED_SLIPPAGE_PCT * 100, 3)
    effective = measured if sufficient else assumed

    by_action: dict[str, dict] = {}
    for f in fills:
        agg = by_action.setdefault(f["action"], {"n": 0, "sum": 0.0})
        agg["n"] += 1
        agg["sum"] += f["slippage_pct"]
    by_action = {a: {"n": v["n"], "mean_slippage_pct": round(v["sum"] / v["n"], 3)}
                 for a, v in by_action.items()}

    return {
        "live_fills": n,
        "min_fills": config.SLIPPAGE_MIN_FILLS,
        "sufficient": sufficient,
        "measured_slippage_pct": measured,
        "assumed_slippage_pct": assumed,
        "effective_slippage_pct": round(effective, 3),
        "source": "measured" if sufficient else "assumed",
        # Paper juice is a two-leg round trip (sell short, then buy it back), so
        # realized results run ~2× the per-leg haircut of premium below the
        # mid-fill figure — an illustrative factor for the caveat, not applied to
        # the immutable ledger.
        "roundtrip_haircut_pct": round(effective * 2, 3),
        "mid_fill_caveat": not sufficient,
        "by_action": by_action,
        "recent_fills": fills[-20:],
        # Net roll slippage (one net crossing per atomic roll) is measured
        # separately from the per-leg figures above — a roll pays ONE net crossing,
        # not two per-leg crossings (PAPER_ROLL_HAIRCUT_CROSSINGS=1).
        "roll_net": roll_report(state),
    }
