"""Adapter: build the scheduler's plain-data inputs from live app state.

``market_scheduler`` is pure and knows nothing about state.json, the scorecard, or
the universe. This module bridges the gap — it reads the cheap, already-computed
signals (open positions, the memoized scorecard, the universe/sector map) and
packs them into ``PortfolioState`` / ``QueueState``. It performs NO provider calls:
it peeks the cached scorecard the same way ``refresh_policy`` does, so building the
queue every scheduler tick is free.

Adjustment vs the spec (see AUDIT.md §7): this codebase has no ranked entry queue
with a per-candidate close forecast. We adapt the existing ready-to-enter data:

  * ``rank``                    = position in the juice-desc order (strongest first);
  * ``gates_passed``            = the scorecard verdict is "GO" (Levels 1-4 hard gate
                                  + CFM suitability);
  * ``slot_opens_within_days``  = 0.0 when a book slot is free right now
                                  (MAX_CFM_POSITIONS - open > 0), else +inf. There is
                                  no close-forecast to say a slot opens "in N days",
                                  so on-deck (Tier 1) activates only when a slot is
                                  actually available — the honest reading. The field
                                  is real data the moment a forecast exists.
"""
from __future__ import annotations

import config
import logging_handler as log
import maintenance
import screening
import sector_data
from market_scheduler import PortfolioState, QueueCandidate, QueueState


def _free_slots(state: dict) -> int:
    """Open book slots right now: the concurrent-position cap minus open positions
    (never negative)."""
    n_open = len(maintenance.open_tickers(state))
    return max(0, config.MAX_CFM_POSITIONS - n_open)


def _cached_scorecard_rows() -> list[dict]:
    """GO/candidate rows from the last memoized scorecard — a cheap peek that never
    triggers a fresh sweep (and ignores an overnight-stale memo), matching the hot-
    refresh candidate path."""
    sc = screening.peek_cached("scorecard:full", max_age=config.HOT_CANDIDATE_MAX_AGE)
    return (sc or {}).get("results", []) if isinstance(sc, dict) else []


def build_portfolio_state(state: dict | None = None) -> PortfolioState:
    """Open positions (Tier 0), the tracked universe (Tier 3 tail) and the sector
    ETFs (rotation names, always at least Tier 3)."""
    state = state if state is not None else log.load_state()
    open_syms = tuple(dict.fromkeys(t.upper() for t in maintenance.open_tickers(state) if t))
    universe = tuple(dict.fromkeys(t.upper() for t in sector_data.all_tickers() if t))
    etfs = tuple(dict.fromkeys(e.upper() for e in sector_data.sector_etfs() if e))
    return PortfolioState(open_symbols=open_syms, tracked_universe=universe, sector_etfs=etfs)


def build_queue_state(state: dict | None = None,
                      rows: list[dict] | None = None) -> QueueState:
    """The ranked entry queue, adapted from the cached scorecard. Candidates are the
    GO rows, ordered by weekly juice (desc) → rank; ``slot_opens_within_days`` is 0
    when a slot is free now, else +inf. Provider-free."""
    state = state if state is not None else log.load_state()
    rows = rows if rows is not None else _cached_scorecard_rows()
    slot_days = 0.0 if _free_slots(state) > 0 else float("inf")

    go = [r for r in rows if r.get("suitability") == "GO" and (r.get("ticker") or "").strip()]
    # Strongest NET juice first (gross minus LEAP burn — never gross); missing
    # juice sorts last. Falls back to gross when net is unavailable so a pricing
    # gap can't drop a name. Ranks are 1-based and dense.
    def _rank_juice(r):
        net = r.get("net_juice_weekly_pct")
        return r.get("juice_weekly_pct") if net is None else net
    go.sort(key=lambda r: (_rank_juice(r) is None, -(_rank_juice(r) or 0.0)))
    seen: set[str] = set()
    candidates: list[QueueCandidate] = []
    for r in go:
        sym = r["ticker"].strip().upper()
        if sym in seen:
            continue
        seen.add(sym)
        candidates.append(QueueCandidate(
            symbol=sym, rank=len(candidates) + 1, gates_passed=True,
            slot_opens_within_days=slot_days))
    return QueueState(candidates=tuple(candidates))


def build(state: dict | None = None) -> tuple[PortfolioState, QueueState]:
    """Both inputs from one state read — the scheduler tick's entry point."""
    state = state if state is not None else log.load_state()
    return build_portfolio_state(state), build_queue_state(state)
