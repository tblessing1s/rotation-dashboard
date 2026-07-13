"""Tests for the PENDING_SETTLE recommendation lifecycle (offline, injected clock)."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import config
import execution_gate as eg
import migrations
import recommendation_runner as runner
import recommendation_settle as settle
import session
from rec_types import ActionType

ET = ZoneInfo("America/New_York")


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rec(rec_id="rec_00001", action=ActionType.DEFEND, ticker="XLK",
         emitted=None, valid_hours=48):
    emitted = emitted or datetime(2026, 7, 13, 9, 32, tzinfo=ET)
    return {"rec_id": rec_id, "ticker": ticker, "position_id": ticker,
            "action_type": action, "trigger_rule": "DEFEND_BELOW_STRIKE",
            "proposed_ticket": {"action": "roll_short", "roll_reason": "defend"},
            "emitted_at": _iso(emitted),
            "valid_until": _iso(emitted + timedelta(hours=valid_hours))}


# ---- schema migration v18 ----------------------------------------------------

def test_migration_v17_to_v18_seeds_spread_baselines():
    state = {"schema_version": 17, "positions": [], "recommendations": []}
    out = migrations._v17_to_v18(state)
    assert out["spread_baselines"] == {}
    assert out["metadata"]["market_settle_gate_since"]


def test_current_version_is_18():
    # v19 adds the transaction-ingestion stores (ingested_transactions / ingestion).
    assert migrations.CURRENT_VERSION == 19
    assert migrations.MIGRATIONS[17] is migrations._v17_to_v18
    assert migrations.MIGRATIONS[18] is migrations._v18_to_v19


# ---- staging -----------------------------------------------------------------

def _blocked_verdict(action=eg.GateAction.DEFENSE, h=9, mi=40):
    s = session.session_state(datetime(2026, 7, 13, h, mi, tzinfo=ET))
    return eg.execution_window(action, s.now_et, s, None)


def test_stage_attaches_pending_block_when_blocked():
    rec = _rec()
    now = datetime(2026, 7, 13, 9, 40, tzinfo=ET)
    staged = settle.stage(rec, _blocked_verdict(), now)
    assert staged is True
    sb = rec["settle"]
    assert sb["status"] == settle.SettleStatus.PENDING
    assert sb["gate_action"] == eg.GateAction.DEFENSE
    assert sb["pre_approved"] is False
    assert settle.parse_ts(sb["executable_at"]) == datetime(2026, 7, 13, 10, 0, tzinfo=ET)
    assert sb["events"] and sb["events"][0]["status"] == settle.SettleStatus.PENDING


def test_stage_noop_when_allowed():
    rec = _rec()
    s = session.session_state(datetime(2026, 7, 13, 12, 0, tzinfo=ET))
    allowed = eg.execution_window(eg.GateAction.DEFENSE, s.now_et, s, None)
    assert settle.stage(rec, allowed, s.now_et) is False
    assert "settle" not in rec


def test_stage_idempotent():
    rec = _rec()
    now = datetime(2026, 7, 13, 9, 40, tzinfo=ET)
    assert settle.stage(rec, _blocked_verdict(), now) is True
    assert settle.stage(rec, _blocked_verdict(), now) is False  # already staged


# ---- pending / due / expiry / pre-approval -----------------------------------

def _state_with_pending(executable_at, valid_hours=48, pre_approved=False, rec_id="rec_00001"):
    rec = _rec(rec_id=rec_id, valid_hours=valid_hours)
    rec["settle"] = {"status": settle.SettleStatus.PENDING,
                     "executable_at": _iso(executable_at), "reason": "SETTLE_WINDOW",
                     "gate_action": eg.GateAction.DEFENSE, "pre_approved": pre_approved,
                     "events": []}
    return {"recommendations": [rec]}


def test_pending_and_due():
    ea = datetime(2026, 7, 13, 10, 0, tzinfo=ET)
    state = _state_with_pending(ea)
    assert len(settle.pending(state)) == 1
    assert settle.due(state, ea - timedelta(minutes=1)) == []        # not yet
    assert len(settle.due(state, ea)) == 1                            # exactly due
    assert len(settle.due(state, ea + timedelta(hours=1))) == 1


def test_is_expired():
    rec = _rec(emitted=datetime(2026, 7, 13, 9, 32, tzinfo=ET), valid_hours=2)
    assert not settle.is_expired(rec, datetime(2026, 7, 13, 10, 0, tzinfo=ET))
    assert settle.is_expired(rec, datetime(2026, 7, 13, 12, 0, tzinfo=ET))


def test_set_pre_approved_only_on_pending():
    state = _state_with_pending(datetime(2026, 7, 13, 10, 0, tzinfo=ET))
    now = datetime(2026, 7, 13, 9, 45, tzinfo=ET)
    rec = settle.set_pre_approved(state, "rec_00001", True, now)
    assert rec["settle"]["pre_approved"] is True
    # A non-existent id -> None; a non-pending rec -> None.
    assert settle.set_pre_approved(state, "nope", True, now) is None
    rec["settle"]["status"] = settle.SettleStatus.RELEASED
    assert settle.set_pre_approved(state, "rec_00001", True, now) is None


# ---- release re-validation ---------------------------------------------------

@pytest.fixture()
def paper_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "active_state_path", lambda: str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    # Release re-validation re-runs the engine; isolate it with a stub snapshot.
    monkeypatch.setattr(runner, "build_market_snapshot", lambda *a, **k: {"tickers": {}})
    return monkeypatch


def _persist(state):
    import logging_handler as log
    log.save_state(state)


def test_release_holds_marks_released(paper_state, monkeypatch):
    ea = datetime(2026, 7, 13, 10, 0, tzinfo=ET)
    _persist(_state_with_pending(ea))
    # Trigger still fires -> engine re-emits the same DEFEND.
    monkeypatch.setattr(runner.engine, "evaluate",
                        lambda *a, **k: [{"action_type": ActionType.DEFEND, "position_id": "XLK", "ticker": "XLK"}])
    summary = runner.release_pending(now=ea, notify=False)
    assert summary["released"] == 1 and summary["self_canceled"] == 0
    import logging_handler as log
    rec = settle.find(log.load_state(), "rec_00001")
    assert rec["settle"]["status"] == settle.SettleStatus.RELEASED


def test_release_self_cancels_when_trigger_cleared(paper_state, monkeypatch):
    ea = datetime(2026, 7, 13, 10, 0, tzinfo=ET)
    _persist(_state_with_pending(ea))
    # The gap filled -> engine no longer emits a DEFEND (only an ALL_CLEAR).
    monkeypatch.setattr(runner.engine, "evaluate",
                        lambda *a, **k: [{"action_type": ActionType.NO_ACTION, "position_id": "XLK", "ticker": "XLK"}])
    summary = runner.release_pending(now=ea, notify=False)
    assert summary["self_canceled"] == 1 and summary["released"] == 0
    import logging_handler as log
    rec = settle.find(log.load_state(), "rec_00001")
    assert rec["settle"]["status"] == settle.SettleStatus.SELF_CANCELED


def test_release_expires_stale_pending(paper_state, monkeypatch):
    # executable_at arrives but the rec's validity already elapsed.
    ea = datetime(2026, 7, 13, 10, 0, tzinfo=ET)
    state = _state_with_pending(ea, valid_hours=0)  # valid_until == emitted (in the past)
    _persist(state)
    monkeypatch.setattr(runner.engine, "evaluate", lambda *a, **k: [])
    summary = runner.release_pending(now=ea, notify=False)
    assert summary["expired"] == 1
    import logging_handler as log
    assert settle.find(log.load_state(), "rec_00001")["settle"]["status"] == settle.SettleStatus.EXPIRED


def test_release_pre_approved_autosubmits(paper_state, monkeypatch):
    ea = datetime(2026, 7, 13, 10, 0, tzinfo=ET)
    _persist(_state_with_pending(ea, pre_approved=True))
    monkeypatch.setattr(runner.engine, "evaluate",
                        lambda *a, **k: [{"action_type": ActionType.DEFEND, "position_id": "XLK", "ticker": "XLK"}])
    calls = []
    summary = runner.release_pending(now=ea, notify=False,
                                     submit_fn=lambda rec, now: calls.append(rec["rec_id"]))
    assert summary["executed"] == 1 and calls == ["rec_00001"]
    import logging_handler as log
    assert settle.find(log.load_state(), "rec_00001")["settle"]["status"] == settle.SettleStatus.EXECUTED


def test_release_pre_approved_submit_failure_leaves_released(paper_state, monkeypatch):
    ea = datetime(2026, 7, 13, 10, 0, tzinfo=ET)
    _persist(_state_with_pending(ea, pre_approved=True))
    monkeypatch.setattr(runner.engine, "evaluate",
                        lambda *a, **k: [{"action_type": ActionType.DEFEND, "position_id": "XLK", "ticker": "XLK"}])

    def _boom(rec, now):
        raise RuntimeError("broker down")
    summary = runner.release_pending(now=ea, notify=False, submit_fn=_boom)
    assert summary["released"] == 1 and summary["executed"] == 0
    import logging_handler as log
    rec = settle.find(log.load_state(), "rec_00001")
    assert rec["settle"]["status"] == settle.SettleStatus.RELEASED
    assert "auto-submit failed" in rec["settle"]["events"][-1]["note"]


def test_no_due_recs_is_noop(paper_state):
    ea = datetime(2026, 7, 13, 10, 0, tzinfo=ET)
    _persist(_state_with_pending(ea))
    summary = runner.release_pending(now=ea - timedelta(minutes=5), notify=False)
    assert summary == {"released": 0, "self_canceled": 0, "expired": 0, "executed": 0}


# ---- dual-timezone formatting (Design §7) ------------------------------------

def test_fmt_dual_tz_shows_et_and_operator_local():
    dt = datetime(2026, 7, 13, 10, 0, tzinfo=ET)
    s = runner._fmt_dual_tz(dt)
    assert "10:00 ET" in s and "9:00 CDT" in s   # default operator TZ is Central
