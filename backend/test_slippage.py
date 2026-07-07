"""Paper-fill slippage: the mid-fill caveat until enough live fills exist, the
measured haircut past that bar (signed by side), and that a paper execution
stamps the mid-fill provenance the measurement reads."""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import config  # noqa: E402
import slippage  # noqa: E402


def _live(action, mid, fill, **extra):
    e = {"action": action, "live_transmitted": True, "quoted_mid_per_share": mid,
         "id": "x", "ticker": "ON"}
    if action == "buy_leap":
        e["execution_price"] = fill * 100
    elif action == "close_leap":
        e["close_price"] = fill * 100
    elif action == "sell_short":
        e["premium_per_share"] = fill
    elif action == "close_short":
        e["close_price_per_share"] = fill
    e.update(extra)
    return e


def test_report_assumed_until_enough_live_fills():
    r = slippage.report({"executions": []})
    assert r["live_fills"] == 0 and r["sufficient"] is False
    assert r["source"] == "assumed" and r["mid_fill_caveat"] is True
    assert r["effective_slippage_pct"] == round(config.ASSUMED_SLIPPAGE_PCT * 100, 3)


def test_report_measures_adverse_slippage_by_side():
    # Adverse = paying above mid on a buy, receiving below mid on a sell — each +2%.
    execs = [
        _live("buy_leap", 10.00, 10.20),    # +2%
        _live("close_short", 1.00, 1.02),   # +2%
        _live("sell_short", 2.00, 1.96),    # +2% (received below mid)
        _live("close_leap", 20.00, 19.60),  # +2%
        _live("buy_leap", 5.00, 5.10),      # +2%
    ]
    r = slippage.report({"executions": execs})
    assert r["live_fills"] == 5 and r["sufficient"] is True
    assert r["source"] == "measured" and r["mid_fill_caveat"] is False
    assert r["measured_slippage_pct"] == pytest.approx(2.0, abs=0.01)
    assert r["effective_slippage_pct"] == pytest.approx(2.0, abs=0.01)
    assert r["roundtrip_haircut_pct"] == pytest.approx(4.0, abs=0.02)
    assert r["by_action"]["buy_leap"]["n"] == 2


def test_report_excludes_paper_rolls_and_bad_mids():
    execs = [
        {"action": "sell_short", "live_transmitted": False,  # paper — booked at mid
         "quoted_mid_per_share": 2.0, "premium_per_share": 2.0},
        {"action": "roll_short", "live_transmitted": True,   # not a single buy/sell leg
         "quoted_mid_per_share": 2.0},
        _live("sell_short", 0.0, 2.0),                       # mid <= 0 -> excluded
        _live("sell_short", 2.0, 2.0, quoted_mid_per_share=None),  # no reference mid
    ]
    assert slippage.report({"executions": execs})["live_fills"] == 0


def test_paper_execution_stamps_mid_fill(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    import importlib

    import logging_handler
    importlib.reload(logging_handler)
    import executor
    importlib.reload(executor)

    executor.execute({"action": "buy_leap", "ticker": "ON", "strike": 130, "contracts": 5,
                      "execution_price": 3300, "stock_price": 145, "expiration": "2026-12-18",
                      "override_reason": "test fixture"})
    executor.execute({"action": "sell_short", "ticker": "ON", "strike": 140.5, "contracts": 5,
                      "premium_per_share": 6.0, "stock_price": 145})
    state = logging_handler.load_state()
    execs = state["executions"]
    assert all(e.get("fill_assumption") == "mid"
               for e in execs if e["action"] in ("buy_leap", "sell_short"))
    ss = next(e for e in execs if e["action"] == "sell_short")
    assert ss["quoted_mid_per_share"] == 6.0 and ss.get("live_transmitted") is False
    # Paper fills are excluded from realized slippage -> the caveat stands.
    r = slippage.report(state)
    assert r["live_fills"] == 0 and r["mid_fill_caveat"] is True
