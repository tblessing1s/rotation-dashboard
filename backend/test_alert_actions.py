"""Alert deep-link tests — the action_url that turns a push/alert into a
prefilled ticket, and that web push targets it only for a single actionable
alert. Pure functions; no state/network. Run: python -m pytest backend -q
"""
import json
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-act-test-"))

import alerts  # noqa: E402
import webpush  # noqa: E402


def test_roll_alerts_deep_link_to_the_roll_ticket():
    url = alerts._action_url("BUYBACK_75", "NVDA")
    assert url == "/?action=roll&ticker=NVDA&reason=75%25-rule"
    assert alerts._action_url("EXPIRY_FRIDAY", "ON") == "/?action=roll&ticker=ON&reason=scheduled"


def test_exit_alerts_focus_the_position():
    assert alerts._action_url("KILL_SWITCH_SPY", "NVDA") == "/?action=focus&ticker=NVDA"
    assert alerts._action_url("CIRCUIT_BREAKER", "AMD") == "/?action=focus&ticker=AMD"


def test_portfolio_alerts_have_no_deep_link():
    assert alerts._action_url("TOKEN_EXPIRY", None) is None
    assert alerts._action_url("DATA_STALE", None) is None


def test_alert_record_carries_action_url():
    a = alerts._alert("BUYBACK_75", "NVDA", "msg", "act", {"strike": 78})
    assert a["action_url"] == "/?action=roll&ticker=NVDA&reason=75%25-rule"


def test_push_deep_links_only_for_a_single_actionable_alert():
    one = [{"severity": "MEDIUM", "ticker": "NVDA",
            "action_url": "/?action=roll&ticker=NVDA&reason=scheduled"}]
    assert json.loads(webpush._payload("s", "b", one))["url"] == one[0]["action_url"]

    two = one + [{"severity": "HIGH", "ticker": "AMD", "action_url": "/?action=focus&ticker=AMD"}]
    assert json.loads(webpush._payload("s", "b", two))["url"] == "/"  # mixed batch -> dashboard
