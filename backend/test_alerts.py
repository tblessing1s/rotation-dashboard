"""Alert engine tests — one test per condition with synthetic state, dedup /
resolve behavior, dry-run delivery, state migration, and the scheduler's pure
slot math. Run offline (no provider keys) with: python -m pytest backend -q
"""
import json
import os
import tempfile
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import alert_scheduler  # noqa: E402
import alerts  # noqa: E402
import config  # noqa: E402
import logging_handler as log  # noqa: E402
import migrations  # noqa: E402
import notifier  # noqa: E402


def _frame(values, vol=1e6):
    idx = pd.bdate_range("2024-01-01", periods=len(values))
    c = pd.Series(values, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": vol}, index=idx)


def _pos(ticker="PG", **over):
    p = {
        "ticker": ticker, "sector": "XLP", "status": "active",
        "leap": {"strike": 140, "contracts": 5, "dte": 150, "current_bid": 3000.0,
                 "cost_basis": 12250.0, "extrinsic_at_entry": 2250.0},
        "shares": {"count": 0, "cap": 500},
        "short_calls": [],
    }
    p.update(over)
    return p


def _state(*positions, **meta):
    return {"metadata": dict(meta), "positions": list(positions), "executions": [],
            "alerts": migrations.default_alert_state()}


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


# ---- individual conditions --------------------------------------------------
def test_kill_switch_alerts_sector_beats_spy(monkeypatch):
    import kill_switch
    evs = [
        {"ticker": "AAA", "rs3m_vs_spy": -3.0, "rs3m_vs_sector": -2.0, "status": "red"},
        {"ticker": "BBB", "rs3m_vs_spy": -1.0, "rs3m_vs_sector": 4.0, "status": "red"},
        {"ticker": "CCC", "rs3m_vs_spy": 6.0, "rs3m_vs_sector": 2.0, "status": "green"},
    ]
    monkeypatch.setattr(kill_switch, "evaluate_all", lambda state: evs)
    out = alerts.check_kill_switch(_state())
    assert [(a["type"], a["ticker"], a["severity"]) for a in out] == [
        ("KILL_SWITCH_SECTOR", "AAA", "CRITICAL"),  # sector rule wins when both trip
        ("KILL_SWITCH_SPY", "BBB", "CRITICAL"),
    ]
    assert "immediately" in out[0]["action"]


def test_delta_uncovered_floor_and_inversion(monkeypatch):
    import data_handler
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _frame([128.0] * 60))
    # LEAP 140 with stock at 128, marked 6.00/sh -> BS delta ~0.39 (< 0.50 floor);
    # the 1-DTE 126 short marked 2.30 is ITM with delta ~0.8 -> inversion too.
    p = _pos(short_calls=[{"strike": 126, "contracts": 5, "dte": 1, "current_bid": 2.30,
                           "entry_premium_total": 450.0}])
    out = alerts.check_delta_uncovered(_state(p))
    kinds = {a["fingerprint"].split("|")[-1].split(":")[0] for a in out}
    assert {a["type"] for a in out} == {"DELTA_UNCOVERED"}
    assert kinds == {"floor", "inverted"}
    floor = next(a for a in out if "floor" in a["fingerprint"])
    assert floor["data"]["leap_delta"] < config.LEAP_DELTA_FLOOR


def test_delta_covered_no_alert(monkeypatch):
    import data_handler
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _frame([128.0] * 60))
    # Deep-ITM 90 LEAP marked ~41.40/sh -> delta ~0.9, comfortably covered.
    p = _pos(leap={"strike": 90, "contracts": 5, "dte": 158, "current_bid": 20700.0},
             short_calls=[{"strike": 132, "contracts": 5, "dte": 5, "current_bid": 1.05,
                           "entry_premium_total": 525.0}])
    assert alerts.check_delta_uncovered(_state(p)) == []


def test_buyback_75_requires_decay_and_dte():
    sc = {"strike": 132, "contracts": 5, "dte": 4, "current_bid": 0.25,
          "entry_premium_total": 600.0, "open_date": "2026-06-25"}  # sold 1.20 -> 79% decayed
    out = alerts.check_buyback_75(_state(_pos(short_calls=[sc])))
    assert len(out) == 1 and out[0]["type"] == "BUYBACK_75"
    assert out[0]["data"]["decayed_pct"] == pytest.approx(79.2, abs=0.1)

    too_close = dict(sc, dte=2)  # inside expiry week -> normal roll, not the 75% rule
    assert alerts.check_buyback_75(_state(_pos(short_calls=[too_close]))) == []
    not_decayed = dict(sc, current_bid=0.40)  # 67% < 75%
    assert alerts.check_buyback_75(_state(_pos(short_calls=[not_decayed]))) == []


def test_defend_position_suggests_atr_roll_down(monkeypatch):
    import data_handler
    import screening
    import strike_policy
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _frame([128.0] * 60))
    monkeypatch.setattr(screening, "regime", lambda: {"status": "yellow"})
    monkeypatch.setattr(strike_policy, "get_posture", lambda state=None: "conservative")
    p = _pos(short_calls=[{"strike": 132, "contracts": 5, "dte": 4, "current_bid": 0.25,
                           "entry_premium_total": 600.0}])
    out = alerts.check_defend_position(_state(p))
    assert len(out) == 1 and out[0]["type"] == "DEFEND_POSITION"
    # flat frame with +/-1 range -> ATR 2. YELLOW/conservative (default posture)
    # = 1.0 ATR / 3% ITM floor: atr_strike=128-2=126, itm_strike=128*0.97=124.16
    # -> the deeper (lower) candidate wins, rounded to $0.50 -> 124.0.
    assert out[0]["data"]["suggested_strike"] == 124.0
    assert out[0]["data"]["posture"] == "conservative"
    # stock above the strike -> nothing to defend
    p2 = _pos(short_calls=[{"strike": 126, "contracts": 5, "dte": 4, "current_bid": 2.0,
                            "entry_premium_total": 450.0}])
    assert alerts.check_defend_position(_state(p2)) == []


def test_defend_position_requires_live_price_below_strike(monkeypatch):
    """Close-confirmed, but a stock that closed below the strike and has since
    recovered above it intraday isn't breached now — so no defend fires."""
    import data_handler
    import screening
    import strike_policy
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _frame([130.0] * 60))
    monkeypatch.setattr(screening, "regime", lambda: {"status": "yellow"})
    monkeypatch.setattr(strike_policy, "get_posture", lambda state=None: "conservative")
    sc = {"strike": 132, "contracts": 5, "dte": 4, "current_bid": 0.25,
          "entry_premium_total": 600.0}

    # Closed at 130 (below 132) but live at 133 (recovered above) -> suppressed.
    monkeypatch.setattr(data_handler, "live_price", lambda s: 133.0)
    assert alerts.check_defend_position(_state(_pos(short_calls=[sc]))) == []

    # Closed at 130 and still below intraday (131) -> fires; the live price is
    # surfaced in the message and the data payload.
    monkeypatch.setattr(data_handler, "live_price", lambda s: 131.0)
    out = alerts.check_defend_position(_state(_pos(short_calls=[sc])))
    assert len(out) == 1 and out[0]["type"] == "DEFEND_POSITION"
    assert out[0]["data"]["live_price"] == 131.0 and out[0]["data"]["last_close"] == 130.0
    assert "131.00" in out[0]["message"] and "last close 130.00" in out[0]["message"]


def _roll(ticker, days_ago, reason="defend", net=-50.0):
    d = (date.today() - timedelta(days=days_ago)).isoformat()
    return {"roll_id": f"{ticker}-{days_ago}", "ticker": ticker, "date": d,
            "reason": reason, "net": net}


def test_whipsaw_exit_trips_on_defensive_roll_count():
    entry = (date.today() - timedelta(days=90)).isoformat()
    rolls = [_roll("PG", 1), _roll("PG", 8), _roll("PG", 15)]  # 3 defends in 4wk
    state = _state(_pos(entry_date=entry))
    state["roll_ledger"] = {"rolls": rolls, "by_ticker": {}}
    out = alerts.check_whipsaw_exit(state)
    assert len(out) == 1 and out[0]["type"] == "WHIPSAW_EXIT" and out[0]["severity"] == "CRITICAL"
    assert out[0]["data"]["rolls_trip"] is True and out[0]["data"]["defensive_rolls"] == 3
    assert "EXIT" in out[0]["action"]


def test_whipsaw_exit_trips_on_cumulative_drag():
    entry = (date.today() - timedelta(days=90)).isoformat()
    # Only 2 defends (below the count bar) but heavy drag: 800 / 12250 capital ~6.5% > 5%.
    rolls = [_roll("PG", 1, net=-400.0), _roll("PG", 8, net=-400.0)]
    state = _state(_pos(entry_date=entry))
    state["roll_ledger"] = {"rolls": rolls, "by_ticker": {}}
    out = alerts.check_whipsaw_exit(state)
    assert len(out) == 1 and out[0]["data"]["drag_trip"] is True
    assert out[0]["data"]["rolls_trip"] is False


def test_whipsaw_exit_quiet_when_under_thresholds_and_out_of_window():
    entry = (date.today() - timedelta(days=180)).isoformat()
    # 1 recent defend + 2 OLD ones outside the 4-week window; small drag -> no trip.
    rolls = [_roll("PG", 2), _roll("PG", 40), _roll("PG", 50)]
    state = _state(_pos(entry_date=entry))
    state["roll_ledger"] = {"rolls": rolls, "by_ticker": {}}
    out = alerts.check_whipsaw_exit(state)
    assert out == []
    # Credit rolls (net >= 0) are not drag and non-defend reasons don't count.
    scheduled = [_roll("PG", 1, reason="scheduled", net=200.0),
                 _roll("PG", 8, reason="75%-rule", net=150.0)]
    state["roll_ledger"] = {"rolls": scheduled, "by_ticker": {}}
    assert alerts.check_whipsaw_exit(state) == []


def test_circuit_breaker_trips_at_or_below_line(monkeypatch):
    monkeypatch.setattr(alerts, "_last_close", lambda t: 128.0)
    p = _pos(circuit_breaker={"price": 131.0})
    out = alerts.check_circuit_breaker(_state(p))
    assert len(out) == 1 and out[0]["severity"] == "CRITICAL"
    p2 = _pos(circuit_breaker={"price": 120.0})
    assert alerts.check_circuit_breaker(_state(p2)) == []
    assert alerts.check_circuit_breaker(_state(_pos())) == []  # no line stored


def test_earnings_window(monkeypatch):
    import earnings
    monkeypatch.setattr(earnings, "next_earnings",
                        lambda t, refresh=False: {"ticker": t, "date": "2026-07-05",
                                                  "days_until": 3, "warning": True})
    out = alerts.check_earnings_window(_state(_pos()))
    assert len(out) == 1 and out[0]["type"] == "EARNINGS_WINDOW"
    monkeypatch.setattr(earnings, "next_earnings",
                        lambda t, refresh=False: {"warning": False})
    assert alerts.check_earnings_window(_state(_pos())) == []


def test_assignment_risk_extrinsic_vs_dividend(monkeypatch):
    monkeypatch.setattr(alerts, "_last_close", lambda t: 128.0)
    today = date.today()
    div = {"ex_date": (today + timedelta(days=2)).isoformat(), "amount": 0.55}
    sc = {"strike": 132, "contracts": 5, "dte": 4, "current_bid": 0.25,
          "entry_premium_total": 600.0}  # OTM short: extrinsic 0.25 < 0.55 dividend
    out = alerts.check_assignment_risk(_state(_pos(dividend=div, short_calls=[sc])))
    assert len(out) == 1 and out[0]["type"] == "ASSIGNMENT_RISK"
    assert "short stock" in out[0]["action"].lower()
    # Plenty of extrinsic -> no risk.
    rich = dict(sc, current_bid=1.50)
    assert alerts.check_assignment_risk(_state(_pos(dividend=div, short_calls=[rich]))) == []
    # Ex-div after the short's expiry -> the short doesn't span the dividend.
    late = {"ex_date": (today + timedelta(days=10)).isoformat(), "amount": 0.55}
    assert alerts.check_assignment_risk(_state(_pos(dividend=late, short_calls=[sc]))) == []


def test_assignment_risk_collapsed_extrinsic_no_dividend(monkeypatch):
    """Base trigger: a deep-ITM short whose extrinsic has collapsed to a few
    cents is assignable any time, no dividend required."""
    monkeypatch.setattr(alerts, "_last_close", lambda t: 128.0)
    # ITM 118 short marked 10.03 -> intrinsic 10.00, extrinsic 0.03 < 0.10 floor.
    sc = {"strike": 118, "contracts": 5, "dte": 3, "current_bid": 10.03,
          "entry_premium_total": 5100.0}
    out = alerts.check_assignment_risk(_state(_pos(short_calls=[sc])))
    assert len(out) == 1 and out[0]["type"] == "ASSIGNMENT_RISK"
    assert out[0]["data"]["trigger"] == "extrinsic"
    assert "no ex-div required" in out[0]["message"]
    # Still meaningful time value -> no risk.
    rich = dict(sc, current_bid=10.60)  # extrinsic 0.60
    assert alerts.check_assignment_risk(_state(_pos(short_calls=[rich]))) == []
    # OTM short with thin extrinsic is never early-assigned absent a dividend.
    otm = {"strike": 132, "contracts": 5, "dte": 3, "current_bid": 0.04,
           "entry_premium_total": 600.0}
    assert alerts.check_assignment_risk(_state(_pos(short_calls=[otm]))) == []
    # A dividend the extrinsic no longer covers escalates (preferred over base).
    div = {"ex_date": (date.today() + timedelta(days=1)).isoformat(), "amount": 0.55}
    out2 = alerts.check_assignment_risk(_state(_pos(dividend=div, short_calls=[sc])))
    assert len(out2) == 1 and out2[0]["data"]["trigger"] == "dividend"


def test_expiry_friday():
    sc = {"strike": 126, "contracts": 5, "dte": 1, "current_bid": 2.30,
          "entry_premium_total": 450.0}
    out = alerts.check_expiry_friday(_state(_pos(short_calls=[sc])))
    assert len(out) == 1 and out[0]["type"] == "EXPIRY_FRIDAY"
    assert alerts.check_expiry_friday(_state(_pos(short_calls=[dict(sc, dte=3)]))) == []


def test_token_expiry(monkeypatch):
    import schwab_api
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(schwab_api, "token_status",
                        lambda: {"present": True, "daysLeft": 1.5, "mintedAt": "2026-06-26T00:00:00Z"})
    out = alerts.check_token_expiry(_state())
    assert len(out) == 1 and out[0]["type"] == "TOKEN_EXPIRY" and out[0]["ticker"] is None
    monkeypatch.setattr(schwab_api, "token_status",
                        lambda: {"present": True, "daysLeft": 5.0, "mintedAt": "x"})
    assert alerts.check_token_expiry(_state()) == []
    monkeypatch.setattr(schwab_api, "token_status", lambda: {"present": False})
    assert alerts.check_token_expiry(_state()) == []


def test_data_stale_market_day_only(monkeypatch):
    import data_handler

    class _WednesdayNoon(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 1, 12, 0, tzinfo=tz)  # a Wednesday

    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(alerts, "datetime", _WednesdayNoon)
    monkeypatch.setattr(data_handler, "cache_age_hours", lambda s: 40.0)
    out = alerts.check_data_stale(_state())
    assert len(out) == 1 and out[0]["type"] == "DATA_STALE"
    monkeypatch.setattr(data_handler, "cache_age_hours", lambda s: 3.0)
    assert alerts.check_data_stale(_state()) == []
    monkeypatch.setattr(data_handler, "cache_age_hours", lambda s: None)  # fresh install
    assert alerts.check_data_stale(_state()) == []

    class _Sunday(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 5, 12, 0, tzinfo=tz)

    monkeypatch.setattr(alerts, "datetime", _Sunday)
    monkeypatch.setattr(data_handler, "cache_age_hours", lambda s: 60.0)
    assert alerts.check_data_stale(_state()) == []


# ---- engineered full scenario (demo-mode acceptance shape) -------------------
def test_engineered_state_trips_every_position_condition(isolated_state, monkeypatch):
    """The alert-demo scenario: one broken position, one evaluator pass, exactly
    the expected alert set (mirrors what seed_demo_data.ALERT_DEMO seeds)."""
    import data_handler
    frames = {
        "SPY": _frame([500.0] * 260),
        "XLP": _frame([80.0] * 260),
        "PG": _frame(list(np.linspace(180, 128, 260))),  # collapsing -> RS3M red
    }
    monkeypatch.setattr(data_handler, "get_daily",
                        lambda s, force=False: frames.get(s.upper()))
    today = date.today()
    p = _pos(
        short_calls=[
            {"strike": 132, "contracts": 5, "dte": 4, "current_bid": 0.25,
             "entry_premium_total": 600.0, "open_date": "2026-06-25"},
            {"strike": 126, "contracts": 5, "dte": 1, "current_bid": 2.30,
             "entry_premium_total": 450.0, "open_date": "2026-06-25"},
        ],
        circuit_breaker={"price": 131.0},
        dividend={"ex_date": (today + timedelta(days=2)).isoformat(), "amount": 0.55},
    )
    state = _state(p, earnings_overrides={"PG": (today + timedelta(days=3)).isoformat()})
    log.save_state(state)

    fired = sorted((a["type"], a["ticker"]) for a in alerts.evaluate(log.load_state()))
    assert fired == sorted([
        ("KILL_SWITCH_SECTOR", "PG"),
        ("CIRCUIT_BREAKER", "PG"),
        ("DELTA_UNCOVERED", "PG"),   # below the 0.50 floor
        ("DELTA_UNCOVERED", "PG"),   # long delta < 1-DTE ITM short's delta
        ("DEFEND_POSITION", "PG"),
        ("BUYBACK_75", "PG"),
        ("ASSIGNMENT_RISK", "PG"),
        ("EARNINGS_WINDOW", "PG"),
        ("EXPIRY_FRIDAY", "PG"),
    ])


# ---- dedup / resolve / ack / settings ----------------------------------------
def test_run_dedups_then_resolves(isolated_state, monkeypatch):
    firing = {"on": True}

    def fake_evaluator(state):
        if firing["on"]:
            return [alerts._alert("EXPIRY_FRIDAY", "NVDA", "expiring", "roll", {}, key="132")]
        return []

    monkeypatch.setattr(alerts, "EVALUATORS", [fake_evaluator])
    sent = []
    monkeypatch.setattr(alerts.notifier, "dispatch",
                        lambda batch, settings, dry_run=False: (sent.append(list(batch)) or []))

    r1 = alerts.run()
    assert len(r1["fired"]) == 1 and r1["active_count"] == 1
    r2 = alerts.run()  # same condition still true -> dedup, no re-fire
    assert len(r2["fired"]) == 0 and r2["active_count"] == 1
    assert len(sent[0]) == 1 and len(sent[1]) == 0

    firing["on"] = False
    r3 = alerts.run()  # condition cleared -> auto-resolve
    assert len(r3["resolved"]) == 1 and r3["active_count"] == 0
    view = alerts.view()
    assert view["active"] == []
    assert view["log"][0]["status"] == "resolved"

    firing["on"] = True
    r4 = alerts.run()  # re-trips after resolution -> fires anew
    assert len(r4["fired"]) == 1


def test_acknowledge_and_settings(isolated_state, monkeypatch):
    monkeypatch.setattr(alerts, "EVALUATORS",
                        [lambda s: [alerts._alert("BUYBACK_75", "AMD", "m", "a", {}, key="k")]])
    monkeypatch.setattr(alerts.notifier, "dispatch", lambda *a, **k: [])
    r = alerts.run()
    alert_id = r["fired"][0]["id"]
    acked = alerts.acknowledge(alert_id)
    assert acked["acknowledged"] is True
    assert alerts.view()["active"][0]["acknowledged"] is True
    with pytest.raises(ValueError):
        alerts.acknowledge("alert_9999")

    # Disabling a type filters it out of evaluation entirely.
    s = alerts.update_settings({"enabled": {"BUYBACK_75": False}, "dry_run": True})
    assert s["enabled"]["BUYBACK_75"] is False and s["dry_run"] is True
    assert alerts.evaluate(log.load_state()) == []


def test_dispatch_dry_run_and_fallback(monkeypatch):
    calls = []

    class _Fake(notifier.Notifier):
        name = "fake"

        def configured(self):
            return True

        def send(self, subject, body, batch):
            calls.append((subject, len(batch)))

    batch = [{"type": "CIRCUIT_BREAKER", "severity": "CRITICAL", "ticker": "PG",
              "message": "m", "action": "a"}]
    monkeypatch.setattr(notifier, "CHANNELS", [_Fake()])
    # Dry run: nothing hits a channel, the report says so.
    report = notifier.dispatch(batch, {}, dry_run=True)
    assert report == [{"channel": "log", "ok": True, "dry_run": True}] and calls == []
    # Real run: the configured channel gets one send with the whole batch.
    report = notifier.dispatch(batch, {}, dry_run=False)
    assert report == [{"channel": "fake", "ok": True}] and calls == [(notifier.format_subject(batch), 1)]
    # Channel disabled in settings -> falls back to the log channel.
    report = notifier.dispatch(batch, {"channels": {"fake": False}}, dry_run=False)
    assert report == [{"channel": "log", "ok": True}]
    # A channel that raises is reported, never raises out.
    class _Broken(_Fake):
        name = "broken"

        def send(self, subject, body, batch):
            raise RuntimeError("smtp down")

    monkeypatch.setattr(notifier, "CHANNELS", [_Broken()])
    report = notifier.dispatch(batch, {}, dry_run=False)
    assert report[0]["channel"] == "broken" and report[0]["ok"] is False


# ---- migration ----------------------------------------------------------------
def test_v1_state_file_migrates_on_load(isolated_state):
    v1 = {
        "metadata": {"last_updated": "2026-06-01T00:00:00Z", "capital_deployed": 100},
        "positions": [{"ticker": "NVDA", "status": "active"}],
        "executions": [{"id": "exec_001", "action": "buy_leap", "ticker": "NVDA"}],
        "theta_ledger": {"weeks": [], "totals": {}},
        "extrinsic_payback": {},
        "pending_orders": {},
    }
    with open(config.STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(v1, fh)
    state = log.load_state()
    assert state["schema_version"] == migrations.CURRENT_VERSION
    assert state["alerts"]["active"] == {} and state["alerts"]["log"] == []
    assert state["executions"][0]["id"] == "exec_001"  # immutable records untouched
    with open(config.STATE_PATH, encoding="utf-8") as fh:  # persisted, runs once
        on_disk = json.load(fh)
    assert on_disk["schema_version"] == migrations.CURRENT_VERSION


# ---- scheduler slot math -------------------------------------------------------
def test_due_slots_market_days_and_once_per_day():
    et = alert_scheduler.ET
    wed_1003 = datetime(2026, 7, 1, 10, 3, tzinfo=et)
    # Fixed anchors + the post-open gap-cadence slots (09:40, 09:50) all past by 10:03.
    assert alert_scheduler.due_slots(wed_1003, {}) == ["08:30", "09:40", "09:50", "10:00"]
    already = {s: date(2026, 7, 1) for s in ("08:30", "09:40", "09:50", "10:00")}
    assert alert_scheduler.due_slots(wed_1003, already) == []
    yesterday = {"08:30": date(2026, 6, 30)}
    assert alert_scheduler.due_slots(wed_1003, yesterday) == ["08:30", "09:40", "09:50", "10:00"]
    sat = datetime(2026, 7, 4, 12, 0, tzinfo=et)
    assert alert_scheduler.due_slots(sat, {}) == []
    early = datetime(2026, 7, 1, 7, 0, tzinfo=et)
    assert alert_scheduler.due_slots(early, {}) == []


def test_open_gap_cadence_slots_close_the_blind_window():
    """The post-open window (09:30→10:00) is covered by tighter gap slots so a
    9:31 gap through a circuit breaker is seen well before the 10:00 anchor."""
    et = alert_scheduler.ET
    # Just after 09:40: the first gap slot is due, the pre-market anchor too.
    at_0941 = datetime(2026, 7, 1, 9, 41, tzinfo=et)
    assert alert_scheduler.due_slots(at_0941, {"08:30": date(2026, 7, 1)}) == ["09:40"]
    # 09:35 (inside the window, before the first gap slot) — nothing new yet.
    at_0935 = datetime(2026, 7, 1, 9, 35, tzinfo=et)
    assert alert_scheduler.due_slots(at_0935, {"08:30": date(2026, 7, 1)}) == []
    assert config._open_gap_slots() == ["09:40", "09:50", "10:00"]


def test_post_close_slot_fires_the_evening():
    """A post-close slot (16:15) is on the schedule so confirmed-close signals
    and end-of-day breaches page the same evening, not next morning."""
    assert config.POST_CLOSE_SLOT_ET in config.ALERT_SCHEDULE_ET
    et = alert_scheduler.ET
    ran = {s: date(2026, 7, 1) for s in config.ALERT_SCHEDULE_ET if s < "16:15"}
    at_1620 = datetime(2026, 7, 1, 16, 20, tzinfo=et)
    assert alert_scheduler.due_slots(at_1620, ran) == ["16:15"]
