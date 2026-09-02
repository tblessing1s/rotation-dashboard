"""The chrome's per-position price strip (spot + distance to each short strike).
Offline: the quote path is stubbed. Run: python -m pytest backend/test_ticker_strip.py -q
"""
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-strip-test-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import pytest  # noqa: E402

import data_handler  # noqa: E402
import position_manager as pm  # noqa: E402


def _quotes(monkeypatch, prices):
    monkeypatch.setattr(data_handler, "latest_quote",
                        lambda t: ({"symbol": t, "price": prices[t], "source": "schwab"}
                                   if prices.get(t) is not None else None))


def test_strip_reports_spot_and_signed_distance_per_short_leg(monkeypatch):
    _quotes(monkeypatch, {"SPCX": 139.51, "ON": 47.0, "OLD": 10.0})
    state = {"positions": [
        {"ticker": "SPCX", "status": "active", "needs_review": True,
         "shares": {"count": 100},
         "short_calls": [{"strike": 133.0, "contracts": 1, "expiration": "2026-09-11"}]},
        {"ticker": "ON", "status": "active", "shares": {"count": 200},
         "short_calls": [{"strike": 50.0, "contracts": 2, "expiration": "2026-09-11"}],
         "short_puts": [{"strike": 45.0, "contracts": 1, "expiration": "2026-09-18"}]},
        {"ticker": "OLD", "status": "closed", "shares": {"count": 0}, "short_calls": []},
    ]}
    rows = pm.ticker_strip(state)
    assert [r["ticker"] for r in rows] == ["SPCX", "ON"]          # closed positions excluded

    spcx = rows[0]
    assert spcx["stock_price"] == 139.51 and spcx["shares"] == 100
    assert spcx["needs_review"] is True and spcx["price_source"] == "schwab"
    leg = spcx["legs"][0]
    assert leg["kind"] == "call" and leg["strike"] == 133.0 and leg["expiration"] == "2026-09-11"
    assert leg["distance"] == pytest.approx(6.51) and leg["itm"] is True and leg["moneyness"] == "ITM"
    assert leg["distance_pct"] == pytest.approx(6.51 / 139.51 * 100, abs=0.01)

    on = rows[1]
    call, put = on["legs"]
    assert call["kind"] == "call" and call["distance"] == pytest.approx(-3.0) and call["itm"] is False
    # Same signed distance convention for the put (spot − strike); only ITM inverts.
    assert put["kind"] == "put" and put["distance"] == pytest.approx(2.0) and put["itm"] is False


def test_strip_survives_a_missing_quote(monkeypatch):
    _quotes(monkeypatch, {"SPCX": None})
    state = {"positions": [{"ticker": "SPCX", "status": "active", "shares": {"count": 100},
                            "short_calls": [{"strike": 133.0, "contracts": 1}]}]}
    rows = pm.ticker_strip(state)
    assert rows[0]["stock_price"] is None and rows[0]["price_source"] is None
    assert rows[0]["legs"][0]["distance"] is None and rows[0]["legs"][0]["itm"] is None


def test_strip_isolates_a_quote_failure_to_that_name(monkeypatch):
    def q(t):
        if t == "BAD":
            raise RuntimeError("HTTP 429")
        return {"symbol": t, "price": 20.0, "source": "schwab"}
    monkeypatch.setattr(data_handler, "latest_quote", q)
    state = {"positions": [
        {"ticker": "BAD", "status": "active", "shares": {"count": 100}, "short_calls": []},
        {"ticker": "GOOD", "status": "active", "shares": {"count": 100}, "short_calls": []},
    ]}
    rows = pm.ticker_strip(state)
    assert rows[0]["stock_price"] is None and rows[1]["stock_price"] == 20.0
    assert rows[1]["legs"] == []


def test_route_serves_the_strip(monkeypatch):
    import app as flask_app
    import logging_handler as log
    _quotes(monkeypatch, {"SPCX": 139.51})
    monkeypatch.setattr(log, "load_state", lambda: {"positions": [
        {"ticker": "SPCX", "status": "active", "shares": {"count": 100},
         "short_calls": [{"strike": 133.0, "contracts": 1, "expiration": "2026-09-11"}]}]})
    monkeypatch.setattr(flask_app.auth, "gate", lambda: None)
    client = flask_app.app.test_client()
    res = client.get("/api/ticker-strip")
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["as_of"] and body["positions"][0]["legs"][0]["distance"] == pytest.approx(6.51)
