"""Live-fill verification tests — the harness that diffs committed executions
against Schwab's own order record. No real network: the Schwab client's
get_order and reconcile are stubbed. OCC symbols are built with the real
occ_option_symbol so they round-trip through reconcile.parse_option_symbol.
Run offline with: python -m pytest backend -q
"""
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-fv-test-"))

import pytest  # noqa: E402

import data_handler  # noqa: E402
import fill_verify  # noqa: E402
import logging_handler as log  # noqa: E402
import migrations  # noqa: E402
import reconcile  # noqa: E402
import schwab_api  # noqa: E402

EXP = "2026-07-10"


def _order(status, legs):
    """legs: list of (instruction, symbol, quantity, fill_price)."""
    return {
        "status": status,
        "orderLegCollection": [
            {"legId": i + 1, "instruction": instr, "quantity": qty,
             "instrument": {"symbol": sym}}
            for i, (instr, sym, qty, _price) in enumerate(legs)
        ],
        "orderActivityCollection": [
            {"executionLegs": [
                {"legId": i + 1, "price": price}
                for i, (_instr, _sym, _qty, price) in enumerate(legs)
            ]}
        ],
    }


class _FakeClient:
    def __init__(self, order):
        self._order = order

    def get_order(self, account_hash, order_id):
        return self._order


@pytest.fixture(autouse=True)
def _clean_reconcile(monkeypatch):
    monkeypatch.setattr(reconcile, "run_reconciliation",
                        lambda persist=True: {"status": "clean", "broker_ok": True, "diffs": []})


def _seed_close_short(recorded_price=0.50):
    execution = {"id": "exec_0001", "action": "close_short", "strike": 78.0,
                 "contracts": 5, "close_price_per_share": recorded_price,
                 "ticker": "NVDA", "live_transmitted": True,
                 "date": "2026-07-05T14:00:00Z"}
    state = {"schema_version": migrations.CURRENT_VERSION, "metadata": {},
             "positions": [], "executions": [execution],
             "order_receipts": [{"order_id": "111", "kind": "close_short",
                                 "ticker": "NVDA", "account_hash": "HASH",
                                 "broker_status": "FILLED",
                                 "execution_ids": ["exec_0001"],
                                 "captured_at": "2026-07-05T14:00:00Z"}],
             "alerts": migrations.default_alert_state()}
    log.save_state(state)


def _connect(monkeypatch, order):
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(data_handler, "client", lambda: _FakeClient(order))


def test_matching_fill_is_green(monkeypatch):
    _seed_close_short(0.50)
    sym = schwab_api.occ_option_symbol("NVDA", EXP, 78.0)
    _connect(monkeypatch, _order("FILLED", [("BUY_TO_CLOSE", sym, 5, 0.50)]))

    out = fill_verify.verify_live_fills()
    assert out["schwab_connected"] is True and out["checked"] == 1
    assert out["all_ok"] is True
    order = out["orders"][0]
    assert order["ok"] is True and order["issues"] == []
    assert order["legs"][0]["recorded_price"] == 0.50
    assert order["legs"][0]["broker_price"] == 0.50
    assert out["reconcile"]["status"] == "clean"


def test_price_drift_is_flagged(monkeypatch):
    _seed_close_short(0.50)                       # we logged 0.50…
    sym = schwab_api.occ_option_symbol("NVDA", EXP, 78.0)
    _connect(monkeypatch, _order("FILLED", [("BUY_TO_CLOSE", sym, 5, 0.65)]))  # …broker says 0.65

    out = fill_verify.verify_live_fills()
    assert out["all_ok"] is False
    order = out["orders"][0]
    assert order["ok"] is False
    assert order["legs"][0]["drift"] == pytest.approx(0.15, abs=1e-6)
    assert any("recorded 0.5 vs broker 0.65" in i for i in order["issues"])


def test_not_filled_is_flagged(monkeypatch):
    _seed_close_short(0.50)
    sym = schwab_api.occ_option_symbol("NVDA", EXP, 78.0)
    _connect(monkeypatch, _order("WORKING", [("BUY_TO_CLOSE", sym, 5, 0.50)]))

    out = fill_verify.verify_live_fills()
    order = out["orders"][0]
    assert order["ok"] is False
    assert any("not FILLED" in i for i in order["issues"])


def test_missing_broker_leg_is_flagged(monkeypatch):
    _seed_close_short(0.50)
    other = schwab_api.occ_option_symbol("NVDA", EXP, 80.0)  # wrong strike
    _connect(monkeypatch, _order("FILLED", [("BUY_TO_CLOSE", other, 5, 0.50)]))

    out = fill_verify.verify_live_fills()
    order = out["orders"][0]
    assert order["ok"] is False
    assert any("no matching broker leg" in i for i in order["issues"])


def test_disconnected_skips_broker_check(monkeypatch):
    _seed_close_short(0.50)
    monkeypatch.setattr(schwab_api, "configured", lambda: False)

    out = fill_verify.verify_live_fills()
    assert out["schwab_connected"] is False
    assert out["all_ok"] is None            # not a vacuous pass
    assert out["orders"][0]["ok"] is None
    assert any("not connected" in i for i in out["orders"][0]["issues"])
    assert out["reconcile"]["status"] == "clean"
