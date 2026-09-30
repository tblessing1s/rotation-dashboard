"""Day-trade period rollups + export (daytrade/report.py). Offline, tmp store."""
from __future__ import annotations

import pytest

from daytrade import report, store


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", str(tmp_path / "daytrade_log"))


def _t(tid, day, pnl, r, status="closed", sym="AAA"):
    return {"trade_id": tid, "symbol": sym, "direction": "long", "status": status,
            "entry": {"price": 50.0, "size": 10, "at": f"{day}T09:40:00-04:00"},
            "exits": [{"kind": "final_target", "price": 51, "size": 10,
                       "at": f"{day}T09:50:00-04:00", "r": r, "pnl": pnl}],
            "realized_pnl": pnl, "realized_r": r,
            "closed_at": f"{day}T09:50:00-04:00" if status == "closed" else None}


@pytest.fixture
def seeded(tmp_store):
    store.save_trades("2025-12-31", "primary", {"a": _t("a", "2025-12-31", 10.0, 1.0)})
    store.save_trades("2026-01-05", "primary", {"b": _t("b", "2026-01-05", -5.0, -1.0),
                                                "c": _t("c", "2026-01-05", 20.0, 2.0)})
    store.save_trades("2026-02-02", "primary", {"d": _t("d", "2026-02-02", 3.0, None, "open")})
    store.save_trades("2026-01-05", "other", {"x": _t("x", "2026-01-05", 99.0, 1.0)})


def test_rollups_by_period(seeded):
    rep = report.build_report("primary")
    assert rep["total"]["trades"] == 4 and rep["total"]["net_pnl"] == 28.0
    assert rep["total"]["open"] == 1 and rep["total"]["win_rate"] == 66.7
    assert [y["period"] for y in rep["year"]] == ["2025", "2026"]
    assert [m["period"] for m in rep["month"]] == ["2025-12", "2026-01", "2026-02"]
    # 2025-12-31 and 2026-01-05 are ISO weeks 1/2 of different ISO years
    assert [w["period"] for w in rep["week"]] == ["2026-W01", "2026-W02", "2026-W06"]
    jan = rep["month"][1]
    assert jan["net_pnl"] == 15.0 and jan["profit_factor"] == 4.0 and jan["avg_loss"] == -5.0


def test_account_isolation_and_bounds(seeded):
    assert report.build_report("other")["total"]["net_pnl"] == 99.0
    rep = report.build_report("primary", since="2026-01-01", until="2026-01-31")
    assert rep["total"]["trades"] == 2


def test_empty_account(tmp_store):
    rep = report.build_report("primary")
    assert rep["total"] is None and rep["trades"] == []
    assert "No trades yet" in report.to_markdown(rep)


def test_exports(seeded):
    rep = report.build_report("primary")
    assert report.to_csv(rep, "trades").splitlines()[0].startswith("date,symbol")
    assert len(report.to_csv(rep, "month").splitlines()) == 4
    md = report.to_markdown(rep)
    for h in ("## Total", "## Yearly", "## Monthly", "## Weekly", "## Daily", "## Trades"):
        assert h in md


def test_routes(seeded, monkeypatch):
    import app as app_mod
    client = app_mod.create_app().test_client()
    r = client.get("/api/daytrade/report")
    assert r.status_code == 200 and r.get_json()["total"]["trades"] == 4
    r = client.get("/api/daytrade/export?format=csv&view=week")
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    assert client.get("/api/daytrade/export?format=csv&view=bogus").status_code == 400
    assert client.get("/api/daytrade/report?since=nope").status_code == 400
