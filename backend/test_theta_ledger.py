"""Theta ledger mark-to-market: re-quoting open options stores a dated mark and
the read-only enrich computes time-value decay (bled to date + day-over-day).
The Schwab provider is fully mocked — no live quote is ever pulled."""
import db
import theta_ledger


def _seed_fill(**over):
    """Insert one open call fill: 150 strike, $8 premium, intrinsic 6 / extrinsic 2."""
    fill = {
        "order_id": over.get("order_id", "ORD1"),
        "underlying": "AAPL",
        "osi_symbol": "AAPL  260619C00150000",
        "option_type": "call",
        "strike": 150.0,
        "expiry": "2099-06-19",   # far future so it counts as open
        "side": "buy",
        "quantity": 2,
        "premium": 8.0,
        "stock_price": 156.0,
        "intrinsic": 6.0,
        "extrinsic": 2.0,
    }
    fill.update(over)
    return db.record_option_fill(fill)


# ---------------------------------------------------------------------------
# refresh — one batched quote, stored mark, enriched ledger
# ---------------------------------------------------------------------------
def test_refresh_stores_mark_and_computes_decay(fresh_db, monkeypatch):
    row = _seed_fill()
    monkeypatch.setattr(theta_ledger.SchwabProvider, "configured", staticmethod(lambda: True))

    captured = {}

    def fake_quotes(self, symbols):
        captured["symbols"] = list(symbols)
        # Option quote with underlyingPrice baked in: stock 157, option mark 7.5
        # -> intrinsic 7, extrinsic 0.5 (entry extrinsic was 2.0 -> 1.5 bled).
        return {
            "AAPL  260619C00150000": {
                "symbol": "AAPL  260619C00150000", "mark": 7.5,
                "underlyingPrice": 157.0, "theta": -0.05, "last": 7.5,
            },
            "AAPL": {"symbol": "AAPL", "last": 157.0},
        }

    monkeypatch.setattr(theta_ledger.SchwabProvider, "get_quotes", fake_quotes)

    out = theta_ledger.refresh(as_of="2026-06-25")
    assert out["ok"] is True
    assert out["refreshed"] == 1
    assert out["missing"] == []
    # The batch asked for both the option and its underlying.
    assert "AAPL  260619C00150000" in captured["symbols"]
    assert "AAPL" in captured["symbols"]

    item = out["ledger"][0]
    assert item["mark"]["mark"] == 7.5
    assert item["mark"]["extrinsic"] == 0.5
    pnl = item["thetaPnl"]
    assert pnl["multiplier"] == 200                  # 2 contracts * 100
    assert pnl["extrinsicNow"] == 0.5
    assert pnl["bledPerShare"] == 1.5                # 2.0 entry - 0.5 now
    assert pnl["bledDollars"] == 300.0              # 1.5 * 200
    # Option P&L: (mark 7.5 - premium 8.0) * 200 = -100 for a long.
    assert pnl["optionPnlDollars"] == -100.0


def test_refresh_falls_back_to_underlying_quote(fresh_db, monkeypatch):
    _seed_fill()
    monkeypatch.setattr(theta_ledger.SchwabProvider, "configured", staticmethod(lambda: True))
    # Option node lacks underlyingPrice; the stock price must come from the
    # underlying quote in the same batch.
    monkeypatch.setattr(theta_ledger.SchwabProvider, "get_quotes", lambda self, syms: {
        "AAPL  260619C00150000": {"mark": 9.0, "underlyingPrice": None, "last": 9.0},
        "AAPL": {"last": 158.0},
    })
    out = theta_ledger.refresh(as_of="2026-06-25")
    item = out["ledger"][0]
    # stock 158 -> intrinsic 8, extrinsic 1.0
    assert item["mark"]["stock_price"] == 158.0
    assert item["mark"]["extrinsic"] == 1.0


def test_day_over_day_uses_prior_stored_mark(fresh_db, monkeypatch):
    _seed_fill()
    monkeypatch.setattr(theta_ledger.SchwabProvider, "configured", staticmethod(lambda: True))

    def quotes_for(extrinsic_mark):
        # Build a quote whose extrinsic resolves to a target by setting mark.
        # stock fixed at 156 -> intrinsic 6, so mark = 6 + extrinsic.
        return lambda self, syms: {
            "AAPL  260619C00150000": {"mark": 6.0 + extrinsic_mark, "underlyingPrice": 156.0},
            "AAPL": {"last": 156.0},
        }

    monkeypatch.setattr(theta_ledger.SchwabProvider, "get_quotes", quotes_for(2.0))
    theta_ledger.refresh(as_of="2026-06-24")       # day 1: extrinsic 2.0
    monkeypatch.setattr(theta_ledger.SchwabProvider, "get_quotes", quotes_for(1.4))
    out = theta_ledger.refresh(as_of="2026-06-25")  # day 2: extrinsic 1.4

    pnl = out["ledger"][0]["thetaPnl"]
    assert pnl["extrinsicNow"] == 1.4
    assert pnl["dayPerShare"] == 0.6                # 2.0 (prior) - 1.4 (now)
    assert pnl["dayDollars"] == 120.0              # 0.6 * 200


def test_expired_fill_is_not_refreshed(fresh_db, monkeypatch):
    _seed_fill(order_id="OLD", expiry="2020-01-01")
    monkeypatch.setattr(theta_ledger.SchwabProvider, "configured", staticmethod(lambda: True))
    monkeypatch.setattr(
        theta_ledger.SchwabProvider, "get_quotes",
        lambda self, syms: (_ for _ in ()).throw(AssertionError("no quote for expired option")),
    )
    out = theta_ledger.refresh(as_of="2026-06-25")
    assert out["ok"] is True
    assert out["refreshed"] == 0
    assert out["ledger"][0]["mark"] is None


def test_enrich_without_marks_is_none(fresh_db):
    _seed_fill()
    enriched = theta_ledger.enrich(db.list_option_fills())
    assert enriched[0]["mark"] is None
    assert enriched[0]["thetaPnl"] is None


def test_short_option_decay_is_a_gain(fresh_db, monkeypatch):
    # Sold-to-open: extrinsic decay is income, so optionPnl is positive as the
    # mark falls below the premium received.
    _seed_fill(side="sell", premium=2.0, intrinsic=0.0, extrinsic=2.0,
               strike=150.0, stock_price=148.0, osi_symbol="AAPL  260619P00150000",
               option_type="put")
    monkeypatch.setattr(theta_ledger.SchwabProvider, "configured", staticmethod(lambda: True))
    monkeypatch.setattr(theta_ledger.SchwabProvider, "get_quotes", lambda self, syms: {
        "AAPL  260619P00150000": {"mark": 1.2, "underlyingPrice": 149.0},
        "AAPL": {"last": 149.0},
    })
    out = theta_ledger.refresh(as_of="2026-06-25")
    pnl = out["ledger"][0]["thetaPnl"]
    # Short put: (mark 1.2 - premium 2.0) * 200 * (-1 side) = +160.
    assert pnl["optionPnlDollars"] == 160.0
