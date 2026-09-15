"""Day-trade sleeve budget (daytrade/budget.py) — sizing off an account's
own real dry powder, with a safe fallback. Offline: accounts.use and the
capital-summary read are monkeypatched, no real state file or Schwab call."""
from __future__ import annotations

import contextlib

import accounts
import config
import logging_handler as log
import position_manager
import pytest

from daytrade import budget


@pytest.fixture(autouse=True)
def _isolated_accounts_use(monkeypatch):
    """budget.daytrade_budget() only needs accounts.use to bind a book for
    the duration of the read — these tests aren't exercising accounts.py's
    own registry validation, so a no-op context manager keeps them focused
    and independent of what accounts happen to be registered."""
    monkeypatch.setattr(accounts, "use", lambda account_id: contextlib.nullcontext())


def test_returns_the_primary_books_deployable_figure(monkeypatch):
    monkeypatch.setattr(log, "load_state", lambda: {"fake": "state"})
    monkeypatch.setattr(position_manager, "capital_summary", lambda state: {"deployable": 1234.56})

    result = budget.daytrade_budget("primary")

    assert result == {"amount": 1234.56, "source": "dry_powder",
                      "detail": "primary book's dry powder"}


def test_a_genuine_zero_deployable_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(log, "load_state", lambda: {})
    monkeypatch.setattr(position_manager, "capital_summary", lambda state: {"deployable": 0.0})

    result = budget.daytrade_budget("primary")

    assert result["amount"] == 0.0
    assert result["source"] == "dry_powder"  # NOT "fallback" — zero is a real answer


def test_a_negative_deployable_is_clamped_to_zero(monkeypatch):
    monkeypatch.setattr(log, "load_state", lambda: {})
    monkeypatch.setattr(position_manager, "capital_summary", lambda state: {"deployable": -50.0})

    result = budget.daytrade_budget("primary")

    assert result["amount"] == 0.0
    assert result["source"] == "dry_powder"


def test_an_unknown_deployable_falls_back(monkeypatch):
    monkeypatch.setattr(log, "load_state", lambda: {})
    monkeypatch.setattr(position_manager, "capital_summary", lambda state: {"deployable": None})

    result = budget.daytrade_budget("primary")

    assert result["amount"] == config.DAYTRADE_ACCOUNT_EQUITY
    assert result["source"] == "fallback"


def test_a_read_failure_falls_back(monkeypatch):
    def _boom():
        raise RuntimeError("no primary book registered")
    monkeypatch.setattr(log, "load_state", _boom)

    result = budget.daytrade_budget("primary")

    assert result["amount"] == config.DAYTRADE_ACCOUNT_EQUITY
    assert result["source"] == "fallback"
    assert "no primary book registered" in result["detail"]


def test_reads_the_requested_accounts_own_book_not_always_primary(monkeypatch):
    seen = {}

    def _use(account_id):
        seen["account_id"] = account_id
        return contextlib.nullcontext()
    monkeypatch.setattr(accounts, "use", _use)
    monkeypatch.setattr(log, "load_state", lambda: {})
    monkeypatch.setattr(position_manager, "capital_summary", lambda state: {"deployable": 42.0})

    result = budget.daytrade_budget("ira")

    assert seen["account_id"] == "ira"
    assert result["detail"] == "ira book's dry powder"
