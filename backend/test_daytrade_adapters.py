"""Execution adapters — Phase 3 (TRAVIS_EXTENSION). Offline: tmp_path store,
no network, no real Schwab connection of any kind (paper only)."""
from __future__ import annotations

import pytest

import config
from daytrade import adapters, store

ACCOUNT = "primary"


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", str(tmp_path / "daytrade_log"))
    return tmp_path


# ===========================================================================
# get_adapter — mode selection
# ===========================================================================
def test_get_adapter_defaults_to_paper(tmp_store, monkeypatch):
    monkeypatch.delenv("DAYTRADE_MODE", raising=False)
    a = adapters.get_adapter("2026-09-14", ACCOUNT)
    assert isinstance(a, adapters.PaperAdapter)


def test_get_adapter_raises_for_unimplemented_live_mode(tmp_store, monkeypatch):
    monkeypatch.setattr(config, "daytrade_mode", lambda: "live")
    with pytest.raises(NotImplementedError):
        adapters.get_adapter("2026-09-14", ACCOUNT)


# ===========================================================================
# PaperAdapter — fills and the trade log
# ===========================================================================
def test_enter_creates_an_open_trade_row(tmp_store):
    a = adapters.PaperAdapter("2026-09-14", ACCOUNT)
    fill = a.enter(symbol="ABC", trade_id="t1", direction="long", price=101.5,
                   size=50, at="2026-09-14T09:40:00-04:00")

    assert fill.price == 101.5 and fill.size == 50
    trade = a.trades["t1"]
    assert trade["status"] == "open"
    assert trade["entry"] == {"price": 101.5, "size": 50, "at": "2026-09-14T09:40:00-04:00"}
    assert trade["exits"] == []
    assert trade["realized_r"] is None


def test_enter_is_idempotent_and_never_overwrites_an_existing_row(tmp_store):
    a = adapters.PaperAdapter("2026-09-14", ACCOUNT)
    a.enter(symbol="ABC", trade_id="t1", direction="long", price=101.5, size=50, at="t0")
    a.exit(symbol="ABC", trade_id="t1", kind="half_target", price=102.5, size=25, at="t1", r=1.0)

    # A later replay re-requests the SAME entry — must not wipe the exit
    # already recorded, and must return the original fill, not a fresh one.
    fill = a.enter(symbol="ABC", trade_id="t1", direction="long", price=999, size=999, at="wrong")

    assert fill.price == 101.5 and fill.size == 50
    assert len(a.trades["t1"]["exits"]) == 1


def test_exit_computes_long_pnl(tmp_store):
    a = adapters.PaperAdapter("2026-09-14", ACCOUNT)
    a.enter(symbol="ABC", trade_id="t1", direction="long", price=100.0, size=50, at="t0")
    a.exit(symbol="ABC", trade_id="t1", kind="final_target", price=102.0, size=50, at="t1", r=2.0)

    trade = a.trades["t1"]
    assert trade["realized_pnl"] == pytest.approx(100.0)   # 50 * (102-100)
    assert trade["status"] == "closed"
    assert trade["realized_r"] == 2.0
    assert trade["closed_at"] == "t1"


def test_exit_computes_short_pnl(tmp_store):
    a = adapters.PaperAdapter("2026-09-14", ACCOUNT)
    a.enter(symbol="ABC", trade_id="t1", direction="short", price=100.0, size=50, at="t0")
    a.exit(symbol="ABC", trade_id="t1", kind="final_target", price=98.0, size=50, at="t1", r=2.0)

    assert a.trades["t1"]["realized_pnl"] == pytest.approx(100.0)  # short profits on a drop


def test_half_target_exit_keeps_the_trade_open(tmp_store):
    a = adapters.PaperAdapter("2026-09-14", ACCOUNT)
    a.enter(symbol="ABC", trade_id="t1", direction="long", price=100.0, size=50, at="t0")
    a.exit(symbol="ABC", trade_id="t1", kind="half_target", price=101.0, size=25, at="t1", r=1.0)

    trade = a.trades["t1"]
    assert trade["status"] == "open"
    assert trade["realized_r"] is None
    assert trade["realized_pnl"] == pytest.approx(25.0)  # 25 * (101-100)


def test_exit_is_idempotent_for_the_same_kind_and_bar(tmp_store):
    a = adapters.PaperAdapter("2026-09-14", ACCOUNT)
    a.enter(symbol="ABC", trade_id="t1", direction="long", price=100.0, size=50, at="t0")
    a.exit(symbol="ABC", trade_id="t1", kind="stop_out", price=99.0, size=50, at="t1", r=-1.0)
    a.exit(symbol="ABC", trade_id="t1", kind="stop_out", price=99.0, size=50, at="t1", r=-1.0)

    trade = a.trades["t1"]
    assert len(trade["exits"]) == 1
    assert trade["realized_pnl"] == pytest.approx(-50.0)  # not doubled


def test_flush_persists_and_load_trades_reads_it_back(tmp_store):
    a = adapters.PaperAdapter("2026-09-14", ACCOUNT)
    a.enter(symbol="ABC", trade_id="t1", direction="long", price=100.0, size=50, at="t0")
    a.flush()

    loaded = store.load_trades("2026-09-14", ACCOUNT)
    assert loaded["t1"]["entry"]["price"] == 100.0

    # A second adapter constructed for the same day/account picks up where this one left off.
    b = adapters.PaperAdapter("2026-09-14", ACCOUNT)
    assert "t1" in b.trades


def test_trades_are_isolated_per_account(tmp_store):
    a = adapters.PaperAdapter("2026-09-14", "primary")
    a.enter(symbol="ABC", trade_id="t1", direction="long", price=100.0, size=50, at="t0")
    a.flush()

    b = adapters.PaperAdapter("2026-09-14", "ira")
    assert b.trades == {}
