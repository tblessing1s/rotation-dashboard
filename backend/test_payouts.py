"""Monthly payout tracker tests — per-month net-juice derivation, the
mark-paid/unmark bookkeeping (with amount snapshotting), the PAYOUT_READY alert
that fires once a finalized month has unpaid income, and the migration seed."""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import alerts  # noqa: E402
import config  # noqa: E402
import logging_handler as log  # noqa: E402
import migrations  # noqa: E402
import payouts  # noqa: E402


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def _close(ticker, date, net):
    return {"action": "close_short", "ticker": ticker, "date": date,
            "net_juice_total": net}


def _seed(monkeypatch, execs, cur_month="2026-07"):
    """Write a state file with the given executions and pin 'now' to cur_month."""
    monkeypatch.setattr(payouts, "_cur_month", lambda: cur_month)
    state = log.load_state()
    state["executions"] = execs
    log.save_state(state)
    return state


# --- derivation ------------------------------------------------------------
def test_monthly_net_juice_buckets_close_shorts(isolated_state, monkeypatch):
    state = _seed(monkeypatch, [
        _close("ON", "2026-05-08T15:00:00Z", 420.0),
        _close("ON", "2026-05-15T15:00:00Z", 380.5),
        _close("AA", "2026-06-05T15:00:00Z", 510.0),
        _close("AA", "2026-07-02T15:00:00Z", 110.0),
        {"action": "buy_leap", "ticker": "ON", "date": "2026-05-01T15:00:00Z"},
    ])
    by_month = payouts.monthly_net_juice(state)
    assert by_month["2026-05"] == {"net_juice": 800.5, "closes": 2}
    assert by_month["2026-06"] == {"net_juice": 510.0, "closes": 1}
    assert by_month["2026-07"] == {"net_juice": 110.0, "closes": 1}
    # buy_leap doesn't count as income
    assert set(by_month) == {"2026-05", "2026-06", "2026-07"}


def test_view_current_is_estimate_previous_is_final(isolated_state, monkeypatch):
    state = _seed(monkeypatch, [
        _close("AA", "2026-06-05T15:00:00Z", 510.0),
        _close("AA", "2026-07-02T15:00:00Z", 110.0),
    ])
    v = payouts.view(state)
    assert v["current"]["month"] == "2026-07"
    assert v["current"]["estimated"] is True
    assert v["current"]["status"] == "in_progress"
    assert v["previous"]["month"] == "2026-06"
    assert v["previous"]["estimated"] is False
    assert v["previous"]["status"] == "unpaid"
    assert v["previous"]["net_juice"] == 510.0
    assert v["totals"]["ytd"] == 620.0
    assert v["totals"]["unpaid"] == 510.0  # July is current -> excluded


# --- mark paid -------------------------------------------------------------
def test_mark_paid_snapshots_amount_and_resolves(isolated_state, monkeypatch):
    _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)])
    v = payouts.mark_paid("2026-06")
    prev = v["previous"]
    assert prev["paid"] is True
    assert prev["paid_amount"] == 510.0
    assert prev["status"] == "paid"
    assert prev["paid_at"]
    assert v["totals"]["paid_out"] == 510.0
    assert v["totals"]["unpaid"] == 0


def test_mark_paid_amount_override(isolated_state, monkeypatch):
    _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)])
    v = payouts.mark_paid("2026-06", amount=500, note="rounded down")
    rec = (log.load_state().get("payouts") or {}).get("records", {})["2026-06"]
    assert rec["paid_amount"] == 500.0
    assert rec["net_juice_at_finalize"] == 510.0
    assert rec["note"] == "rounded down"
    assert v["previous"]["paid_amount"] == 500.0


def test_mark_paid_amount_frozen_against_later_recompute(isolated_state, monkeypatch):
    """The snapshot is what was withdrawn — a later execution correction to the
    same month must not silently change the recorded payout."""
    _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)])
    payouts.mark_paid("2026-06")
    # A correction lands after the payout was booked.
    state = log.load_state()
    state["executions"].append(_close("AA", "2026-06-20T15:00:00Z", 90.0))
    log.save_state(state)
    v = payouts.view()
    assert v["previous"]["paid_amount"] == 510.0        # frozen
    assert v["previous"]["net_juice"] == 600.0          # live figure moved


def test_cannot_mark_current_month(isolated_state, monkeypatch):
    _seed(monkeypatch, [_close("AA", "2026-07-02T15:00:00Z", 110.0)])
    with pytest.raises(ValueError):
        payouts.mark_paid("2026-07")


def test_unmark_paid_reverts(isolated_state, monkeypatch):
    _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)])
    payouts.mark_paid("2026-06")
    v = payouts.unmark_paid("2026-06")
    assert v["previous"]["paid"] is False
    assert v["previous"]["paid_amount"] is None
    assert v["totals"]["paid_out"] == 0


# --- pending / alert -------------------------------------------------------
def test_pending_payout_only_previous_month(isolated_state, monkeypatch):
    state = _seed(monkeypatch, [
        _close("ON", "2026-05-08T15:00:00Z", 800.0),  # older unpaid month
        _close("AA", "2026-06-05T15:00:00Z", 510.0),  # the previous month
    ])
    pending = payouts.pending_payout(state)
    assert pending["month"] == "2026-06"
    assert pending["net_juice"] == 510.0


def test_pending_none_when_paid_or_no_income(isolated_state, monkeypatch):
    _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)])
    payouts.mark_paid("2026-06")
    assert payouts.pending_payout(log.load_state()) is None
    # negative month (buybacks > premium) is not a payout
    state2 = _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", -50.0)])
    assert payouts.pending_payout(state2) is None


def test_payout_ready_alert_fires_and_resolves(isolated_state, monkeypatch):
    state = _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)])
    fired = alerts.check_payout_ready(state)
    assert len(fired) == 1
    a = fired[0]
    assert a["type"] == "PAYOUT_READY"
    assert a["fingerprint"] == "PAYOUT_READY|2026-06"
    assert a["action_url"] == "/?tab=Payouts"
    assert "June 2026" in a["message"]
    # once paid, the condition clears
    payouts.mark_paid("2026-06")
    assert alerts.check_payout_ready(log.load_state()) == []


# --- migration -------------------------------------------------------------
def test_migration_seeds_payouts_key():
    old = {"schema_version": 14, "executions": [], "positions": []}
    migrated, changed = migrations.migrate(old)
    assert changed is True
    assert migrated["schema_version"] == migrations.CURRENT_VERSION
    assert migrated["payouts"] == {"records": {}}
