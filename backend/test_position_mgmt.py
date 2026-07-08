"""Phase 2 tests — 75% buyback surfacing, defend roll-down math, atomic live
roll payload (asserted, never transmitted), the derived roll ledger, assignment
risk on the Positions view, and the accumulation-vs-kill-switch guard."""
import os
import tempfile
from datetime import date, timedelta

import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import config  # noqa: E402
import executor  # noqa: E402
import logging_handler as log  # noqa: E402
import position_manager as pm  # noqa: E402
import schwab_api  # noqa: E402


def _frame(values, vol=1e6):
    idx = pd.bdate_range("2024-01-01", periods=len(values))
    c = pd.Series(values, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": vol}, index=idx)


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


# ---- 75% buyback surfacing ---------------------------------------------------
def test_enrich_short_decay_and_roll_now():
    sc = {"strike": 132, "contracts": 5, "dte": 4, "current_bid": 0.25,
          "entry_premium_total": 600.0}  # sold 1.20/sh, now 0.25 -> 79.2% decayed
    out = pm.enrich_short(sc, stock_price=134.0, dividend=None)
    assert out["sold_per_share"] == 1.20
    assert out["decay_pct"] == pytest.approx(79.2, abs=0.1)
    assert out["roll_now"] is True
    assert out["below_strike"] is False
    # Inside expiry week (<=2 DTE) the 75% rule hands off to the normal roll.
    assert pm.enrich_short(dict(sc, dte=2), 134.0, None)["roll_now"] is False
    # Barely decayed -> no badge.
    assert pm.enrich_short(dict(sc, current_bid=0.90), 134.0, None)["roll_now"] is False


def test_deployed_capital_derives_from_open_positions():
    state = {"positions": [
        {"ticker": "A", "status": "active", "leap": {"cost_basis": 12000},
         "shares": {"count": 100, "cost_basis_per_share": 50.0}},   # 12000 + 5000
        {"ticker": "B", "status": "active", "leap": {"cost_basis": 8000},
         "shares": {"count": 0}},                                    # 8000
        {"ticker": "C", "status": "closed", "leap": {"cost_basis": 99999}},  # excluded
    ]}
    assert pm.position_capital(state["positions"][0]) == 17000.0
    assert pm.deployed_capital(state) == 25000.0


def test_enrich_short_extrinsic_capture():
    sc = {"strike": 132, "contracts": 5, "entry_premium_total": 600.0,
          "entry_extrinsic_per_share": 0.80, "current_bid": 1.20, "dte": 4}
    # Stock 134 -> 2.00 intrinsic. A live 2.30 mark is only 0.30 extrinsic, so
    # 0.50 of the 0.80 extrinsic sold has been captured (62.5%).
    out = pm.enrich_short(sc, stock_price=134.0, dividend=None, live_mark=2.30)
    assert out["current_bid"] == 2.30  # live mark overrides the stored entry mark
    assert out["entry_extrinsic_per_share"] == 0.80
    assert out["current_extrinsic_per_share"] == 0.30
    assert out["extrinsic_captured_per_share"] == 0.50
    assert out["extrinsic_captured_pct"] == 62.5
    assert out["entry_extrinsic_total"] == 400.0
    assert out["extrinsic_captured_total"] == 250.0
    assert out["extrinsic_remaining_total"] == 150.0

    # A mark at/under intrinsic -> extrinsic fully captured, clamped at 100%.
    out2 = pm.enrich_short(sc, stock_price=134.0, dividend=None, live_mark=1.90)
    assert out2["current_extrinsic_per_share"] == 0.0
    assert out2["extrinsic_captured_pct"] == 100.0

    # No entry extrinsic recorded -> capture fields stay None, target included.
    bare = {"strike": 132, "contracts": 5, "current_bid": 1.0}
    out3 = pm.enrich_short(bare, stock_price=134.0, dividend=None)
    assert out3["entry_extrinsic_per_share"] is None
    assert out3["extrinsic_captured_pct"] is None


def test_enrich_short_intrinsic_capture():
    # Sold an ITM call for 3.00 when the stock was ~135 (3.00 intrinsic at strike
    # 132) plus 0.80 extrinsic -> 3.80 total, so entry intrinsic = 3.80 - 0.80 = 3.00.
    sc = {"strike": 132, "contracts": 5, "entry_premium_total": 1900.0,
          "entry_extrinsic_per_share": 0.80, "dte": 4}
    # Stock fell to 133 -> current intrinsic 1.00, so 2.00/sh of the intrinsic sold
    # has melted back to us in cash (positive = kept).
    out = pm.enrich_short(sc, stock_price=133.0, dividend=None, live_mark=1.30)
    assert out["entry_intrinsic_per_share"] == 3.00
    assert out["current_intrinsic_per_share"] == 1.00
    assert out["intrinsic_captured_per_share"] == 2.00
    assert out["entry_intrinsic_total"] == 1500.0
    assert out["intrinsic_captured_total"] == 1000.0

    # Stock climbs to 138 -> current intrinsic 6.00 > 3.00 sold: 3.00/sh handed
    # back (negative), the loss the covering LEAP's intrinsic gain offsets.
    up = pm.enrich_short(sc, stock_price=138.0, dividend=None, live_mark=6.40)
    assert up["current_intrinsic_per_share"] == 6.00
    assert up["intrinsic_captured_per_share"] == -3.00
    assert up["intrinsic_captured_total"] == -1500.0

    # Stock under the strike -> intrinsic fully melted; full entry intrinsic kept.
    otm = pm.enrich_short(sc, stock_price=128.0, dividend=None, live_mark=0.15)
    assert otm["current_intrinsic_per_share"] == 0.0
    assert otm["intrinsic_captured_per_share"] == 3.00
    assert otm["intrinsic_captured_total"] == 1500.0

    # No entry extrinsic recorded -> entry intrinsic unknowable, fields stay None.
    bare = {"strike": 132, "contracts": 5, "current_bid": 1.0, "entry_premium_total": 500.0}
    out_bare = pm.enrich_short(bare, stock_price=134.0, dividend=None)
    assert out_bare["entry_intrinsic_per_share"] is None
    assert out_bare["intrinsic_captured_total"] is None


def test_enrich_short_assignment_risk_flag():
    today = date.today()
    sc = {"strike": 132, "contracts": 5, "dte": 4, "current_bid": 0.25,
          "entry_premium_total": 600.0}
    div = {"ex_date": (today + timedelta(days=2)).isoformat(), "amount": 0.55}
    out = pm.enrich_short(sc, stock_price=128.0, dividend=div)
    assert out["assignment_risk"]["dividend"] == 0.55
    assert "SHORT STOCK" in out["assignment_risk"]["note"]
    assert out["below_strike"] is True
    # Rich extrinsic -> no flag.
    assert pm.enrich_short(dict(sc, current_bid=1.50), 128.0, div)["assignment_risk"] is None


# ---- defend recommendation -----------------------------------------------------
def test_defend_recommendation_regime_atr_strike(isolated_state, monkeypatch):
    import data_handler
    import screening
    # Tiny close oscillation (ends exactly at 128) keeps realized vol nonzero
    # while the +/-1 High/Low range still pins ATR at 2.
    wiggle = ([128.1, 128.0, 127.9, 128.0] * 65)
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _frame(wiggle))
    state = log.load_state()
    state["positions"] = [{
        "ticker": "PG", "sector": "XLP", "status": "active",
        "leap": {"strike": 140, "contracts": 5},
        "short_calls": [{"strike": 132, "contracts": 5, "dte": 4, "current_bid": 0.25,
                         "entry_premium_total": 600.0}],
    }]
    log.save_state(state)

    # Default posture (no metadata.strike_posture set) is "conservative".
    monkeypatch.setattr(screening, "regime", lambda: {"status": "green"})
    rec = executor.defend_recommendation("PG")
    # flat frame: ATR 2. GREEN/conservative = 0.5 ATR / 1% ITM floor:
    # atr_strike=128-1=127, itm_strike=128*0.99=126.72 -> deeper (126.72) wins,
    # rounded to $0.50 -> 126.5.
    assert rec["breached"] is True and rec["recommended_strike"] == 126.5
    assert rec["atr_mult"] == 0.5 and rec["itm_pct"] == 0.01 and rec["posture"] == "conservative"
    assert rec["new_premium_per_share"] is not None
    assert rec["net_total"] is not None
    assert rec["cost_basis_effect"] == -rec["net_total"]

    monkeypatch.setattr(screening, "regime", lambda: {"status": "yellow"})
    rec = executor.defend_recommendation("PG")
    # YELLOW/conservative = 1.0 ATR / 3% ITM floor: atr_strike=128-2=126,
    # itm_strike=128*0.97=124.16 -> deeper (124.16) wins, rounded -> 124.0.
    assert rec["recommended_strike"] == 124.0 and rec["atr_mult"] == 1.0 and rec["itm_pct"] == 0.03

    # Stock above every short strike -> nothing to defend.
    state = log.load_state()
    state["positions"][0]["short_calls"][0]["strike"] = 120
    log.save_state(state)
    assert executor.defend_recommendation("PG")["breached"] is False


def test_defend_recommendation_clears_on_intraday_recovery(isolated_state, monkeypatch):
    """Closed below the short strike but recovered above it intraday -> the live
    price clears the breach, matching the alert engine's close+live gate."""
    import data_handler
    import screening
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _frame([130.0] * 60))
    monkeypatch.setattr(screening, "regime", lambda: {"status": "yellow"})
    state = log.load_state()
    state["positions"] = [{
        "ticker": "PG", "sector": "XLP", "status": "active",
        "leap": {"strike": 140, "contracts": 5},
        "short_calls": [{"strike": 132, "contracts": 5, "dte": 4, "current_bid": 0.25,
                         "entry_premium_total": 600.0}],
    }]
    log.save_state(state)

    # Live 133 > 132 strike -> not breached, even though the close (130) is below.
    monkeypatch.setattr(data_handler, "live_price", lambda s: 133.0)
    rec = executor.defend_recommendation("PG")
    assert rec["breached"] is False
    assert rec["stock_price"] == 133.0 and rec["last_close"] == 130.0

    # Live 131 < 132 strike (and close below) -> breached; the roll-down is sized
    # off the live price.
    monkeypatch.setattr(data_handler, "live_price", lambda s: 131.0)
    rec = executor.defend_recommendation("PG")
    assert rec["breached"] is True and rec["stock_price"] == 131.0 and rec["last_close"] == 130.0


# ---- roll ledger (derived) ------------------------------------------------------
def test_roll_writes_ledger_with_reason_and_net(isolated_state):
    executor.execute({"action": "sell_short", "ticker": "NVDA", "strike": 130,
                      "contracts": 5, "premium_per_share": 1.20, "stock_price": 128})
    res = executor.execute({
        "action": "roll_short", "ticker": "NVDA", "contracts": 5,
        "from_strike": 130, "close_price_per_share": 0.25,
        "to_strike": 125, "to_expiration": "2026-07-10", "to_dte": 7,
        "premium_per_share": 1.10, "stock_price": 128,
        "roll_reason": "defend",
    })
    assert res["net_credit"] == pytest.approx((1.10 - 0.25) * 5 * 100)

    state = log.load_state()
    ledger = state["roll_ledger"]
    assert len(ledger["rolls"]) == 1
    roll = ledger["rolls"][0]
    assert roll["reason"] == "defend"
    assert roll["from_strike"] == 130 and roll["to_strike"] == 125
    assert roll["buyback_cost"] == 125.0 and roll["new_premium"] == 550.0
    assert roll["net"] == 425.0
    agg = ledger["by_ticker"]["NVDA"]
    assert agg["count"] == 1 and agg["net_total"] == 425.0 and agg["drag_total"] == 0.0
    # Both execution legs carry the same roll_id + reason.
    legs = [e for e in state["executions"] if e.get("roll_id")]
    assert len(legs) == 2 and len({e["roll_id"] for e in legs}) == 1
    assert all(e["roll_reason"] == "defend" for e in legs)


def test_roll_debit_counts_as_drag(isolated_state):
    executor.execute({"action": "sell_short", "ticker": "NVDA", "strike": 130,
                      "contracts": 5, "premium_per_share": 0.50, "stock_price": 128})
    executor.execute({
        "action": "roll_short", "ticker": "NVDA", "contracts": 5,
        "from_strike": 130, "close_price_per_share": 2.00,  # buying back ITM, dear
        "to_strike": 124, "to_dte": 7, "premium_per_share": 1.50,
        "stock_price": 126, "roll_reason": "not-a-valid-reason",
    })
    state = log.load_state()
    roll = state["roll_ledger"]["rolls"][0]
    assert roll["reason"] == "scheduled"  # unknown reasons normalize
    assert roll["net"] == -250.0
    assert state["roll_ledger"]["by_ticker"]["NVDA"]["drag_total"] == -250.0


# ---- atomic live roll ------------------------------------------------------------
def test_live_roll_builds_single_two_leg_net_order(isolated_state, monkeypatch):
    placed = {}

    class _FakeClient:
        def primary_account_hash(self):
            return "HASH"

        def place_order(self, account_hash, order):
            placed["account_hash"] = account_hash
            placed["order"] = order
            return {"orderId": "9001"}

    import data_handler
    # Seed the open short on the paper path, then flip live for the roll.
    executor.execute({"action": "sell_short", "ticker": "NVDA", "strike": 130,
                      "contracts": 5, "premium_per_share": 1.20, "stock_price": 128,
                      "expiration": "2026-07-02"})
    monkeypatch.setenv("CFM_LIVE_TRADING", "1")
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(data_handler, "client", lambda: _FakeClient())
    res = executor.execute({
        "action": "roll_short", "ticker": "NVDA", "contracts": 5,
        "from_strike": 130, "from_expiration": "2026-07-02",
        "close_price_per_share": 0.25,
        "to_strike": 125, "to_expiration": "2026-07-10", "to_dte": 7,
        "premium_per_share": 1.10, "stock_price": 128, "roll_reason": "75%-rule",
    })
    assert res["status"] == "working" and res["order_id"] == "9001"

    order = placed["order"]
    assert order["orderType"] == "NET_CREDIT"  # 1.10 - 0.25 = +0.85 credit
    assert order["price"] == "0.85"
    assert order["orderStrategyType"] == "SINGLE"
    assert order["complexOrderStrategyType"] == "CUSTOM"
    legs = order["orderLegCollection"]
    assert [l["instruction"] for l in legs] == ["BUY_TO_CLOSE", "SELL_TO_OPEN"]
    assert legs[0]["instrument"]["symbol"] == schwab_api.occ_option_symbol("NVDA", "2026-07-02", 130)
    assert legs[1]["instrument"]["symbol"] == schwab_api.occ_option_symbol("NVDA", "2026-07-10", 125)
    assert all(l["quantity"] == 5 for l in legs)

    # Nothing committed yet — the roll is pending, the old short still open.
    state = log.load_state()
    assert "9001" in state["pending_orders"]
    assert state["pending_orders"]["9001"]["kind"] == "roll_short"
    assert not any(e.get("roll_id") for e in state["executions"])


def test_live_roll_fill_commits_both_legs_at_leg_prices(isolated_state, monkeypatch):
    close_sym = schwab_api.occ_option_symbol("NVDA", "2026-07-02", 130)
    open_sym = schwab_api.occ_option_symbol("NVDA", "2026-07-10", 125)

    class _FakeClient:
        def primary_account_hash(self):
            return "HASH"

        def place_order(self, account_hash, order):
            return {"orderId": "9001"}

        def get_order(self, account_hash, order_id):
            return {
                "status": "FILLED",
                "orderLegCollection": [
                    {"legId": 1, "instrument": {"symbol": close_sym}},
                    {"legId": 2, "instrument": {"symbol": open_sym}},
                ],
                "orderActivityCollection": [{
                    "executionLegs": [
                        {"legId": 1, "price": 0.30},   # actual buyback fill
                        {"legId": 2, "price": 1.15},   # actual new-premium fill
                    ],
                }],
            }

    import data_handler
    # Seed the open short on the paper path, then flip live for the roll.
    executor.execute({"action": "sell_short", "ticker": "NVDA", "strike": 130,
                      "contracts": 5, "premium_per_share": 1.20, "stock_price": 128,
                      "expiration": "2026-07-02"})
    monkeypatch.setenv("CFM_LIVE_TRADING", "1")
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(data_handler, "client", lambda: _FakeClient())
    executor.execute({
        "action": "roll_short", "ticker": "NVDA", "contracts": 5,
        "from_strike": 130, "from_expiration": "2026-07-02",
        "close_price_per_share": 0.25,
        "to_strike": 125, "to_expiration": "2026-07-10", "to_dte": 7,
        "premium_per_share": 1.10, "stock_price": 128, "roll_reason": "75%-rule",
    })
    out = executor.order_status("9001")
    assert out["status"] == "filled"

    state = log.load_state()
    assert state["pending_orders"] == {}
    roll = state["roll_ledger"]["rolls"][0]
    # Committed at the ACTUAL leg fills, not the staged estimates.
    assert roll["buyback_cost"] == pytest.approx(0.30 * 5 * 100)
    assert roll["new_premium"] == pytest.approx(1.15 * 5 * 100)
    assert roll["reason"] == "75%-rule"
    pos = log.find_position(state, "NVDA")
    assert [sc["strike"] for sc in pos["short_calls"]] == [125]


# ---- accumulation guard -------------------------------------------------------
def test_accumulation_blocked_on_kill_switch(isolated_state, monkeypatch):
    import kill_switch
    monkeypatch.setattr(kill_switch, "evaluate",
                        lambda t: {"ticker": t, "status": "yellow",
                                   "rs3m_vs_spy": 1.0, "rs3m_vs_sector": 0.5})
    state = log.load_state()
    # Flag off (default): the cap is the only limit.
    assert pm.can_add_shares(state, "NVDA") is True
    monkeypatch.setattr(config, "BLOCK_ACCUMULATION_ON_RS_DETERIORATION", True)
    assert pm.can_add_shares(state, "NVDA") is False
    monkeypatch.setattr(kill_switch, "evaluate",
                        lambda t: {"ticker": t, "status": "green",
                                   "rs3m_vs_spy": 8.0, "rs3m_vs_sector": 3.0})
    assert pm.can_add_shares(state, "NVDA") is True
