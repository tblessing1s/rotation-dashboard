"""Weekly (Saturday) and monthly (the 1st) operator progress digests.

Same shape as check_daily_outlook (see test_daily_outlook.py): an
informational alert riding the existing dedup rather than a second delivery
path. The extra wrinkle here is the SCHEDULE — both fall on days
alert_scheduler's Mon-Fri due_slots never fires alerts.run() on its own
(due_slots excludes weekends outright; the 1st can itself land on a weekend),
so each evaluator self-gates on the calendar AND alert_scheduler carries its
own independent trigger (weekly_summary_due / monthly_summary_due).
"""
from __future__ import annotations

from datetime import date, datetime

import alert_scheduler
import alerts

ET = alert_scheduler.ET


def _freeze(monkeypatch, dt: datetime):
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, tzinfo=tz)
    monkeypatch.setattr(alerts, "datetime", _Frozen)


def _ledger(*rows, this_week=0.0):
    return {"theta_ledger": {"weeks": list(rows), "totals": {"this_week": this_week}}}


# ---------------------------------------------------------------------------
# check_weekly_summary
# ---------------------------------------------------------------------------
def test_weekly_summary_only_fires_on_saturday(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 1, 9, 0, tzinfo=ET))  # a Wednesday
    assert alerts.check_weekly_summary(_ledger()) == []
    _freeze(monkeypatch, datetime(2026, 7, 4, 9, 0, tzinfo=ET))  # the Saturday
    assert len(alerts.check_weekly_summary(_ledger())) == 1


def test_weekly_summary_fingerprint_is_keyed_by_iso_week(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 4, 9, 0, tzinfo=ET))
    a = alerts.check_weekly_summary(_ledger())[0]
    assert a["fingerprint"] == "WEEKLY_SUMMARY|2026-W27"


def test_weekly_summary_reports_this_weeks_total_and_by_ticker(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 4, 9, 0, tzinfo=ET))
    rows = [
        {"week": "2026-W27", "ticker": "AAA", "net_juice": 150.0},
        {"week": "2026-W27", "ticker": "BBB", "net_juice": 40.0},
        {"week": "2026-W26", "ticker": "CCC", "net_juice": 999.0},  # a prior week — must not leak in
    ]
    msg = alerts.check_weekly_summary(_ledger(*rows, this_week=190.0))[0]["message"]
    assert "$190.00 net juice captured" in msg
    assert "AAA $150.00" in msg and "BBB $40.00" in msg and "CCC" not in msg


def test_weekly_summary_a_quiet_week_still_reports(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 4, 9, 0, tzinfo=ET))
    msg = alerts.check_weekly_summary(_ledger())[0]["message"]
    assert "no closes this week" in msg


def test_weekly_summary_is_informational_and_registered(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 4, 9, 0, tzinfo=ET))
    a = alerts.check_weekly_summary(_ledger())[0]
    assert a["severity"] == "LOW" and a["ticker"] is None
    assert "not a trigger" in a["action"]
    assert alerts.check_weekly_summary in alerts.EVALUATORS
    assert "WEEKLY_SUMMARY" in alerts.ALERT_TYPES


# ---------------------------------------------------------------------------
# check_monthly_summary
# ---------------------------------------------------------------------------
def _state_with_closed_month(monkeypatch, net_juice=500.0, burn=0.0):
    """A minimal state whose PREVIOUS month (June 2026, given a July 'now')
    has closed income — enough for payouts.view()'s previous entry to carry a
    payout figure. Mirrors test_payouts.py's _seed(): pins _cur_month and
    stubs the burn-marks read rather than deriving through recompute_derived,
    which this evaluator (unlike check_weekly_summary) never needs.

    Defaults to burn=0 — the shares-primary strategy holds no LEAP, so a
    current book's realized burn is zero. A nonzero burn is exercised
    separately, for the legacy diagonal still winding down on an old book."""
    import payouts
    monkeypatch.setattr(payouts, "_cur_month", lambda: "2026-07")
    monkeypatch.setattr(payouts, "monthly_leap_burn", lambda: {"2026-06": burn})
    return {
        "positions": [],
        "executions": [
            {"action": "close_short", "ticker": "AAA", "date": "2026-06-15",
             "net_juice_total": net_juice},
        ],
    }


def test_monthly_summary_only_fires_on_the_1st(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 2, 9, 0, tzinfo=ET))
    assert alerts.check_monthly_summary(_state_with_closed_month(monkeypatch)) == []
    _freeze(monkeypatch, datetime(2026, 7, 1, 9, 0, tzinfo=ET))
    assert len(alerts.check_monthly_summary(_state_with_closed_month(monkeypatch))) == 1


def test_monthly_summary_fingerprint_is_keyed_by_month(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 1, 9, 0, tzinfo=ET))
    a = alerts.check_monthly_summary(_state_with_closed_month(monkeypatch))[0]
    assert a["fingerprint"] == "MONTHLY_SUMMARY|2026-06"


def test_monthly_summary_reports_the_month_that_just_closed_not_the_new_one(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 1, 9, 0, tzinfo=ET))
    a = alerts.check_monthly_summary(_state_with_closed_month(monkeypatch))[0]
    assert a["data"]["month"] == "2026-06"
    assert "payout" in a["message"] and "YTD" in a["message"]


def test_monthly_summary_omits_leap_burn_when_zero(monkeypatch):
    """The shares-primary strategy holds no LEAP, so a current book's realized
    burn is zero — the message should read the juice/payout straight, not
    parenthesize a $0.00 LEAP burn nobody needs to see."""
    _freeze(monkeypatch, datetime(2026, 7, 1, 9, 0, tzinfo=ET))
    a = alerts.check_monthly_summary(_state_with_closed_month(monkeypatch, burn=0.0))[0]
    assert "LEAP burn" not in a["message"]
    assert "$500.00 payout" in a["message"]


def test_monthly_summary_shows_leap_burn_when_a_legacy_book_still_carries_it(monkeypatch):
    """A book still winding down a legacy LEAP diagonal (position_types.
    LEAP_PMCC_LEGACY) can still realize burn — the breakdown must surface it
    exactly as check_payout_ready already does."""
    _freeze(monkeypatch, datetime(2026, 7, 1, 9, 0, tzinfo=ET))
    a = alerts.check_monthly_summary(_state_with_closed_month(monkeypatch, burn=100.0))[0]
    assert "$100.00 LEAP burn" in a["message"]


def test_monthly_summary_deep_links_to_payouts(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 1, 9, 0, tzinfo=ET))
    a = alerts.check_monthly_summary(_state_with_closed_month(monkeypatch))[0]
    assert a["action_url"] == "/?tab=Payouts"


def test_monthly_summary_is_informational_and_registered(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 1, 9, 0, tzinfo=ET))
    a = alerts.check_monthly_summary(_state_with_closed_month(monkeypatch))[0]
    assert a["severity"] == "LOW" and a["ticker"] is None
    assert "not a trigger" in a["action"]
    assert alerts.check_monthly_summary in alerts.EVALUATORS
    assert "MONTHLY_SUMMARY" in alerts.ALERT_TYPES


# ---------------------------------------------------------------------------
# alert_scheduler gating — both digests fall outside due_slots' Mon-Fri
# schedule, so they need their own trigger.
# ---------------------------------------------------------------------------
def test_weekly_summary_due_only_saturday_morning_once():
    saturday = datetime(2026, 7, 4, 9, 0, tzinfo=ET)
    assert alert_scheduler.weekly_summary_due(saturday, None) is True
    assert alert_scheduler.weekly_summary_due(saturday, date(2026, 7, 4)) is False
    early = datetime(2026, 7, 4, 6, 0, tzinfo=ET)
    assert alert_scheduler.weekly_summary_due(early, None) is False
    weekday = datetime(2026, 7, 1, 9, 0, tzinfo=ET)
    assert alert_scheduler.weekly_summary_due(weekday, None) is False


def test_monthly_summary_due_only_the_1st_once():
    first = datetime(2026, 7, 1, 9, 0, tzinfo=ET)
    assert alert_scheduler.monthly_summary_due(first, None) is True
    assert alert_scheduler.monthly_summary_due(first, date(2026, 7, 1)) is False
    early = datetime(2026, 7, 1, 6, 0, tzinfo=ET)
    assert alert_scheduler.monthly_summary_due(early, None) is False
    second = datetime(2026, 7, 2, 9, 0, tzinfo=ET)
    assert alert_scheduler.monthly_summary_due(second, None) is False
    # Lands on a Saturday this run — must still fire (unlike the weekly digest,
    # the monthly one is NOT restricted to a particular weekday).
    saturday_first = datetime(2026, 8, 1, 9, 0, tzinfo=ET)
    assert alert_scheduler.monthly_summary_due(saturday_first, None) is True
