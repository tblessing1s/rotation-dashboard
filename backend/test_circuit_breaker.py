"""Circuit breaker tests — the multi-condition exit rule (whichever comes first:
15% drop from entry, 3 closes below the 50-day MA, a close below the 200-day MA,
or the operator's line-in-the-sand)."""
import os
import tempfile

import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import circuit_breaker  # noqa: E402
import config  # noqa: E402
import indicators  # noqa: E402


def _frame(values, vol=1e6):
    idx = pd.bdate_range("2020-01-01", periods=len(values))
    c = pd.Series(values, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": vol}, index=idx)


def _pos(**cb):
    return {"ticker": "PG", "status": "active",
            "circuit_breaker": dict(cb) if cb else None}


def _tripped(verdict):
    return set(verdict["tripped_conditions"])


# ---- condition 1: drawdown from entry ---------------------------------------
def test_drawdown_trips_at_or_below_15pct():
    # entry 100, a 16% drop to 84 — short frame so the MA legs stay inert.
    v = circuit_breaker.evaluate(_pos(entry_price=100.0), df=_frame([84.0] * 5))
    assert v["tripped"] and "drawdown" in _tripped(v)
    assert v["status"] == "red"


def test_drawdown_holds_above_the_line_but_warns_when_two_thirds_there():
    v = circuit_breaker.evaluate(_pos(entry_price=100.0), df=_frame([90.0] * 5))  # -10%
    assert not v["tripped"]
    assert v["status"] == "yellow" and "drawdown" in v["approaching"]


def test_drawdown_inert_without_entry_price():
    # No stored entry price (older position, not backfilled) -> leg can't fire.
    v = circuit_breaker.evaluate(_pos(price=None), df=_frame([50.0] * 5))
    assert "drawdown" not in _tripped(v)
    assert v["conditions"][0]["detail"]["entry_price"] is None


# ---- condition 2: consecutive closes below the fast (50-day) MA -------------
def test_fast_ma_trips_on_three_closes_below():
    closes = [100.0] * 100 + [90.0, 90.0, 90.0]  # last 3 dip below their 50-day MA
    v = circuit_breaker.evaluate(_pos(), df=_frame(closes))
    assert v["tripped"] and "ma_fast" in _tripped(v)


def test_fast_ma_warns_one_close_away():
    closes = [100.0] * 100 + [90.0, 90.0]  # only 2 below -> approaching, not tripped
    v = circuit_breaker.evaluate(_pos(), df=_frame(closes))
    assert not v["tripped"] and "ma_fast" in v["approaching"]


# ---- condition 3: close below the slow (200-day) MA -------------------------
def test_slow_ma_trips_on_close_below():
    closes = list(range(300, 40, -1))  # 260 strictly descending closes
    v = circuit_breaker.evaluate(_pos(), df=_frame([float(c) for c in closes]))
    assert v["tripped"] and "ma_slow" in _tripped(v)


def test_slow_ma_holds_above():
    closes = list(range(40, 300))  # 260 ascending -> last close above its 200-day MA
    v = circuit_breaker.evaluate(_pos(), df=_frame([float(c) for c in closes]))
    assert "ma_slow" not in _tripped(v)


# ---- condition 4: operator line-in-the-sand ---------------------------------
def test_manual_line_trips_at_or_below():
    v = circuit_breaker.evaluate(_pos(price=131.0), df=_frame([128.0] * 5))
    assert v["tripped"] and "manual_line" in _tripped(v)
    v2 = circuit_breaker.evaluate(_pos(price=120.0), df=_frame([128.0] * 5))
    assert "manual_line" not in _tripped(v2)


# ---- all clear + whichever-comes-first --------------------------------------
def test_all_clear_is_green():
    v = circuit_breaker.evaluate(_pos(entry_price=100.0), df=_frame([100.0] * 250))
    assert not v["tripped"] and v["status"] == "green"
    assert v["tripped_conditions"] == []


def test_whichever_comes_first_reports_every_breached_condition():
    # A collapsing series past a manual line: several conditions fire at once;
    # the verdict lists them all and is a single red exit.
    closes = [float(c) for c in range(300, 40, -1)]
    v = circuit_breaker.evaluate(_pos(price=100.0, entry_price=300.0), df=_frame(closes))
    assert v["tripped"] and v["status"] == "red"
    assert {"drawdown", "ma_fast", "ma_slow", "manual_line"} <= _tripped(v)


def test_evaluate_all_skips_closed_positions():
    state = {"positions": [{"ticker": "AAPL", "status": "closed",
                            "circuit_breaker": {"price": 10.0}}]}
    assert circuit_breaker.evaluate_all(state) == []


# ---- the indicator helper ---------------------------------------------------
def test_consecutive_closes_below_sma_counts_the_trailing_run():
    closes = [100.0] * 100 + [90.0, 90.0, 90.0]
    assert indicators.consecutive_closes_below_sma(_frame(closes), 50) == 3


def test_consecutive_closes_below_sma_zero_when_last_close_is_above():
    assert indicators.consecutive_closes_below_sma(_frame([100.0] * 60), 50) == 0


def test_consecutive_closes_below_sma_none_without_enough_history():
    assert indicators.consecutive_closes_below_sma(_frame([100.0] * 10), 50) is None
