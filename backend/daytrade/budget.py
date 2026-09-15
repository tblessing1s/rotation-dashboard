"""Day-trade sleeve budget — per account.

Sizing off a REAL number: that account's own dry-powder deploy capacity
(``position_manager.capital_summary(state)["deployable"]``) — the same "how
much MORE capital could I put to work right now" figure CFM's own capital
panel shows (the tighter of the deployed-capital cap and cash above the
defensive reserve). This is exactly the number the brief means by "smaller
amounts of capital that don't fit the Cash Flow Machine": whatever CFM
CAN'T deploy right now is what's available here — and since an account only
trades day-trade at all when ``daytrade.settings.is_enabled()`` says so, its
OWN dry powder is the honest number, never another book's.

Read fresh on every call (no caching) — deployable capital moves as CFM
enters/exits positions, and this only feeds a PAPER fill (still no real
order; see config.daytrade_mode() / daytrade/adapters.py), so there's no
cost to reading it live every time the scheduler runs the signal engine.

FALLBACK: any failure to read it (Schwab not connected, a state read error)
falls back to the static config.DAYTRADE_ACCOUNT_EQUITY placeholder Phase 3
introduced — a sizing inaccuracy in paper mode, never a financial risk. A
genuine (successfully read) $0 or negative deployable figure is NOT a
failure and is NOT substituted — it's clamped to 0.0 and returned as-is: if
this account has no dry powder right now, its day-trade budget is also zero.
"""
from __future__ import annotations

import logging

import config

logger = logging.getLogger("cfm.daytrade")


def daytrade_budget(account_id: str) -> dict:
    """``{"amount": float, "source": "dry_powder" | "fallback", "detail": str}``
    — ``detail`` names the account on success, or the failure reason when
    falling back."""
    try:
        import accounts
        import logging_handler as log
        import position_manager
        with accounts.use(account_id):
            state = log.load_state()
            summary = position_manager.capital_summary(state)
        deployable = summary.get("deployable")
        if deployable is None:
            raise ValueError("capital_summary() returned no 'deployable' figure "
                             "(operating cash is unknown for this book)")
        return {"amount": max(0.0, float(deployable)), "source": "dry_powder",
               "detail": f"{account_id} book's dry powder"}
    except Exception as e:  # noqa: BLE001 — a budget read must never block the signal engine
        logger.warning("daytrade budget: could not read %s's dry powder (%s); "
                       "falling back to DAYTRADE_ACCOUNT_EQUITY", account_id, e)
        return {"amount": config.DAYTRADE_ACCOUNT_EQUITY, "source": "fallback", "detail": str(e)}
