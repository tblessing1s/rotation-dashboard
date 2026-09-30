"""Exports of the Transactions table and the raw execution log."""
import csv
import io
import json

import record_export as rx


def _state():
    return {"executions": [
        {"id": "e1", "date": "2026-07-06T14:00:00Z", "ticker": "ON", "action": "sell_short",
         "strike": 139.5, "contracts": 5, "expiration": "2026-07-10",
         "premium_per_share": 6.0, "stock_price": 142.0, "entry_extrinsic_per_share": 3.5,
         "nested": {"a": 1}},
        {"id": "e2", "date": "2026-07-09", "ticker": "ON", "action": "close_short",
         "strike": 139.5, "contracts": 5, "expiration": "2026-07-10",
         "close_price_per_share": 1.0, "extrinsic_sold": 0.0},
        {"id": "e3", "date": "2026-07-01", "ticker": "ON", "action": "buy_shares",
         "qty": 500, "price_per_share": 140.0},
        {"id": "e4", "date": "2026-07-02", "ticker": "ON", "action": "sell_short",
         "strike": 1, "contracts": 1, "premium_per_share": 1.0, "reversed_by": "e5"},
        {"id": "e6", "date": "2026-07-03", "ticker": "=cmd", "action": "note"},
    ]}


def _rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_transactions_are_fills_only_oldest_first_and_skip_voided():
    rows = _rows(rx.transactions_csv(_state()))
    assert [r["id"] for r in rows] == ["e3", "e1", "e2"]      # e4 reversed, e6 not a fill
    sell = rows[1]
    assert sell["price"] == "6.0" and sell["extrinsic_or_basis"] == "3.5"
    assert sell["qty"] == "5" and sell["strike"] == "139.5"
    shares = rows[0]
    assert shares["qty"] == "500" and shares["strike"] == "" and shares["price"] == "140.0"
    assert shares["entry_stock_price"] == "140.0"


def test_raw_log_is_complete_and_unfiltered():
    st = _state()
    assert [e["id"] for e in json.loads(rx.executions_json(st))] == ["e1", "e2", "e3", "e4", "e6"]
    rows = _rows(rx.executions_csv(st))
    assert len(rows) == 5 and list(rows[0])[:4] == ["id", "date", "ticker", "action"]
    assert json.loads(rows[0]["nested"]) == {"a": 1}          # nested flattened to JSON text
    assert rows[3]["reversed_by"] == "e5"                      # reversed fills are kept


def test_formula_like_text_is_defused():
    rows = _rows(rx.executions_csv(_state()))
    assert rows[4]["ticker"] == "'=cmd"


def test_routes_serve_attachments(tmp_path, monkeypatch):
    import app as app_module
    import config
    import logging_handler as log
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(log, "load_state", lambda: _state())
    client = app_module.create_app().test_client()
    for path, name in (("/api/export/transactions", "transactions.csv"),
                       ("/api/export/executions?format=csv", "executions.csv"),
                       ("/api/export/executions", "executions.json")):
        r = client.get(path)
        assert r.status_code == 200, (path, r.data[:200])
        assert name in r.headers["Content-Disposition"]
