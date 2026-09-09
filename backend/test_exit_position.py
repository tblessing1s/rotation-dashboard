"""executor.exit_position() / _run_leg_to_fill() — the first full-exit
sequencing this app has ever had: close every open short, THEN sell every
share, never the other order. Offline throughout: paper mode (live_enabled
patched off), synthetic prices, a tmp_path store. No network, no live Schwab.
"""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-exit-test-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import config  # noqa: E402
import executor  # noqa: E402
import exit_reasons  # noqa: E402
import logging_handler as log  # noqa: E402
import position_manager  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: False)  # paper path throughout
    st = log.load_state()
    st["metadata"]["operating_cash"] = 100000
    log.save_state(st)
    return tmp_path


def _buy_shares(ticker, qty, price):
    return executor.execute({"action": "buy_shares", "ticker": ticker, "qty": qty,
                             "price_per_share": price, "stock_price": price})


def _sell_short(ticker, strike, contracts, prem, spot, exp="2026-09-11"):
    return executor.execute({"action": "sell_short", "ticker": ticker, "strike": strike,
                             "contracts": contracts, "premium_per_share": prem,
                             "stock_price": spot, "expiration": exp})


# ===========================================================================
# exit_position() — the orchestration.
# ===========================================================================
def test_exit_position_closes_short_then_sells_shares_in_order(store, monkeypatch):
    _buy_shares("KO", 100, 60.0)
    _sell_short("KO", 62.0, 1, 1.0, 60.0)
    monkeypatch.setattr(position_manager, "_stock_price", lambda t: 58.0)

    res = executor.exit_position("KO", exit_reason=exit_reasons.ExitReason.CB_DRAWDOWN_15)

    assert res["ok"] is True
    assert res["position_closed"] is True
    assert [s["leg"] for s in res["steps"]] == ["close_short", "sell_shares"]
    assert all(s["ok"] for s in res["steps"])

    st = log.load_state()
    actions = [e["action"] for e in st["executions"]
              if e["ticker"] == "KO" and e["action"] in ("close_short", "sell_shares")]
    # close_short must be recorded strictly before sell_shares.
    assert actions.index("close_short") < actions.index("sell_shares")
    p = log.find_position(st, "KO")
    assert p["shares"]["count"] == 0 and not p["short_calls"] and p["status"] == "closed"


def test_exit_position_closes_every_open_short_before_any_share_sells(store, monkeypatch):
    _buy_shares("KO", 200, 60.0)
    _sell_short("KO", 62.0, 1, 1.0, 60.0, exp="2026-09-11")
    _sell_short("KO", 63.0, 1, 0.8, 60.0, exp="2026-09-18")
    monkeypatch.setattr(position_manager, "_stock_price", lambda t: 58.0)

    res = executor.exit_position("KO", exit_reason=exit_reasons.ExitReason.CB_MANUAL_LINE)

    assert res["ok"] is True
    legs = [s["leg"] for s in res["steps"]]
    assert legs == ["close_short", "close_short", "sell_shares"]
    st = log.load_state()
    assert log.find_position(st, "KO")["status"] == "closed"


def test_exit_position_shares_only_skips_the_short_leg(store, monkeypatch):
    _buy_shares("KO", 100, 60.0)
    monkeypatch.setattr(position_manager, "_stock_price", lambda t: 58.0)

    res = executor.exit_position("KO", exit_reason=exit_reasons.ExitReason.CB_MA200_CLOSE)

    assert res["ok"] is True
    assert [s["leg"] for s in res["steps"]] == ["sell_shares"]
    assert log.find_position(log.load_state(), "KO")["status"] == "closed"


def test_exit_position_short_only_still_closes_cleanly(store):
    # An edge shape (shares trimmed out from under the call by hand, leaving a
    # dangling short) — closing the short alone must still work and finish
    # the close.
    _buy_shares("KO", 100, 60.0)
    _sell_short("KO", 62.0, 1, 1.0, 60.0)
    executor.execute({"action": "sell_shares", "ticker": "KO", "qty": 100,
                      "price_per_share": 61.0, "stock_price": 61.0})
    p = log.find_position(log.load_state(), "KO")
    assert p["shares"]["count"] == 0 and p["short_calls"] and p["status"] != "closed"

    res = executor.exit_position("KO", exit_reason=exit_reasons.ExitReason.CB_DRAWDOWN_15)
    assert res["ok"] is True
    assert [s["leg"] for s in res["steps"]] == ["close_short"]
    assert log.find_position(log.load_state(), "KO")["status"] == "closed"


def test_exit_position_stops_and_never_sells_when_short_cannot_be_priced(store, monkeypatch):
    _buy_shares("KO", 100, 60.0)
    _sell_short("KO", 62.0, 1, 1.0, 60.0)
    # Strip the only fallback price so neither a live mark nor a stored mark
    # is available.
    st = log.load_state()
    p = log.find_position(st, "KO")
    p["short_calls"][0].pop("current_bid", None)
    log.save_state(st)
    before_execs = len(log.load_state()["executions"])

    res = executor.exit_position("KO", exit_reason=exit_reasons.ExitReason.CB_DRAWDOWN_15)

    assert res["ok"] is False
    assert "shares NOT sold" in res["error"] or "priced" in res["error"]
    st = log.load_state()
    assert len(st["executions"]) == before_execs  # nothing was appended
    p = log.find_position(st, "KO")
    assert p["short_calls"] and p["shares"]["count"] == 100  # untouched


def test_exit_position_refuses_a_legacy_leap_position(store):
    executor.execute({"action": "buy_leap", "ticker": "MSFT", "strike": 300,
                      "contracts": 1, "execution_price": 5000, "stock_price": 350,
                      "override_reason": "test fixture — legacy LEAP"})
    before = log.load_state()

    res = executor.exit_position("MSFT", exit_reason=exit_reasons.ExitReason.CB_DRAWDOWN_15)

    assert res["ok"] is False
    assert "legacy LEAP" in res["error"]
    assert log.load_state() == before  # no mutation at all


def test_exit_position_no_open_position_reports_closed_true(store):
    res = executor.exit_position("ZZZZ", exit_reason=exit_reasons.ExitReason.CB_DRAWDOWN_15)
    assert res == {"ok": False, "ticker": "ZZZZ", "steps": [],
                   "position_closed": True, "error": "no open position"}


def test_exit_position_rejects_an_unrecognized_exit_reason(store):
    _buy_shares("KO", 100, 60.0)
    with pytest.raises(ValueError, match="exit_reason"):
        executor.exit_position("KO", exit_reason="NOT_A_REAL_CODE")
    assert log.find_position(log.load_state(), "KO")["shares"]["count"] == 100


def test_exit_position_operator_discretion_requires_a_note(store):
    _buy_shares("KO", 100, 60.0)
    with pytest.raises(ValueError, match="exit_note"):
        executor.exit_position("KO", exit_reason=exit_reasons.ExitReason.OPERATOR_DISCRETION)


def test_exit_position_stamps_source_rec_id_on_every_leg(store, monkeypatch):
    _buy_shares("KO", 100, 60.0)
    _sell_short("KO", 62.0, 1, 1.0, 60.0)
    monkeypatch.setattr(position_manager, "_stock_price", lambda t: 58.0)

    executor.exit_position("KO", exit_reason=exit_reasons.ExitReason.CB_DRAWDOWN_15,
                           source_rec_id="rec_test_123")

    st = log.load_state()
    legs = [e for e in st["executions"] if e["ticker"] == "KO"
           and e["action"] in ("close_short", "sell_shares")]
    assert len(legs) == 2
    assert all(e.get("source_rec_id") == "rec_test_123" for e in legs)
    sell = next(e for e in legs if e["action"] == "sell_shares")
    assert sell["exit_reason"] == exit_reasons.ExitReason.CB_DRAWDOWN_15


# ===========================================================================
# _run_leg_to_fill() — the fill-confirmation helper in isolation.
# ===========================================================================
def test_run_leg_to_fill_returns_immediately_on_a_paper_fill(monkeypatch):
    monkeypatch.setattr(executor, "execute", lambda payload, now=None: {"status": "filled", "x": 1})
    result, err = executor._run_leg_to_fill({"action": "sell_shares"})
    assert err is None and result == {"status": "filled", "x": 1}


def test_run_leg_to_fill_polls_a_working_order_to_a_fill(monkeypatch):
    monkeypatch.setattr(executor, "_EXIT_LEG_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(config, "ORDER_FILL_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(executor, "execute",
                        lambda payload, now=None: {"status": "working", "order_id": "o1"})
    calls = {"n": 0}
    def _poll(order_id):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"order_id": order_id, "status": "working"}
        return {"order_id": order_id, "status": "filled"}
    monkeypatch.setattr(executor, "order_status", _poll)
    result, err = executor._run_leg_to_fill({"action": "close_short"})
    assert err is None and result["status"] == "filled" and calls["n"] == 3


def test_run_leg_to_fill_stops_on_rejection(monkeypatch):
    monkeypatch.setattr(executor, "_EXIT_LEG_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(config, "ORDER_FILL_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(executor, "execute",
                        lambda payload, now=None: {"status": "working", "order_id": "o1"})
    monkeypatch.setattr(executor, "order_status",
                        lambda oid: {"order_id": oid, "status": "rejected"})
    result, err = executor._run_leg_to_fill({"action": "close_short"})
    assert result is None and "rejected" in err


def test_run_leg_to_fill_times_out_without_guessing_forward(monkeypatch):
    monkeypatch.setattr(executor, "_EXIT_LEG_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(config, "ORDER_FILL_WAIT_SECONDS", 0.03)
    monkeypatch.setattr(executor, "execute",
                        lambda payload, now=None: {"status": "working", "order_id": "o1"})
    monkeypatch.setattr(executor, "order_status",
                        lambda oid: {"order_id": oid, "status": "working"})
    result, err = executor._run_leg_to_fill({"action": "close_short"})
    assert result is None and "did not fill" in err


def test_run_leg_to_fill_reports_the_exception_rather_than_raising(monkeypatch):
    def _boom(payload, now=None):
        raise executor.PositionFrozenError("KO", {"note": "diverged"})
    monkeypatch.setattr(executor, "execute", _boom)
    result, err = executor._run_leg_to_fill({"action": "close_short"})
    assert result is None and "KO" in err
