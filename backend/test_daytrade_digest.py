"""Daily performance digest (daytrade/digest.py). Offline: notifier.CHANNELS
entries are stubbed, no real SMTP/ntfy/webpush call."""
from __future__ import annotations

import notifier
import pytest

from daytrade import digest, trial


class _StubChannel:
    def __init__(self, name, is_configured=True, raises=False, configured_raises=None):
        self.name = name
        self._configured = is_configured
        self._raises = raises
        self._configured_raises = configured_raises
        self.sent = None

    def configured(self):
        if self._configured_raises is not None:
            raise self._configured_raises
        return self._configured

    def send(self, subject, body, alerts):
        if self._raises:
            raise RuntimeError("channel down")
        self.sent = (subject, body, alerts)


class _NativePanic(BaseException):
    """Stands in for pyo3_runtime.PanicException — a real failure mode seen
    from webpush.configured() in an environment with a broken cryptography
    build, and notably NOT an Exception subclass."""


@pytest.fixture
def running_trial(monkeypatch):
    monkeypatch.setattr(trial, "trial_status", lambda: {
        "target_trades": 50, "completed_trades": 12, "status": "running",
        "verdict": None, "net_r": 3.25, "net_pnl": 187.5, "win_rate": 58.3,
    })


@pytest.fixture
def completed_trial(monkeypatch):
    monkeypatch.setattr(trial, "trial_status", lambda: {
        "target_trades": 50, "completed_trades": 50, "status": "complete",
        "verdict": "win", "net_r": 14.0, "net_pnl": 812.4, "win_rate": 61.0,
    })


def test_sends_to_every_configured_channel(monkeypatch, running_trial):
    a = _StubChannel("a")
    b = _StubChannel("b", is_configured=False)
    monkeypatch.setattr(notifier, "CHANNELS", [a, b])

    report = digest.send_daily_digest()

    assert report == [{"channel": "a", "ok": True}]
    assert a.sent is not None
    subject, body, alerts = a.sent
    assert "12/50" in subject
    assert "Net R: +3.25R" in body
    assert "Net P&L: $187.50" in body
    assert alerts == []


def test_completed_trial_reports_the_verdict(monkeypatch, completed_trial):
    a = _StubChannel("a")
    monkeypatch.setattr(notifier, "CHANNELS", [a])

    digest.send_daily_digest()

    subject, body, _ = a.sent
    assert "WIN" in subject
    assert "Trial complete — WIN" in body
    assert "No new paper entries" in body


def test_one_failing_channel_does_not_block_the_others(monkeypatch, running_trial):
    broken = _StubChannel("broken", raises=True)
    ok = _StubChannel("ok")
    monkeypatch.setattr(notifier, "CHANNELS", [broken, ok])

    report = digest.send_daily_digest()

    assert {"channel": "broken", "ok": False, "error": "channel down"} in report
    assert {"channel": "ok", "ok": True} in report
    assert ok.sent is not None


def test_no_configured_channel_does_not_raise(monkeypatch, running_trial):
    monkeypatch.setattr(notifier, "CHANNELS", [_StubChannel("x", is_configured=False)])

    report = digest.send_daily_digest()

    assert report == []


def test_a_configured_check_that_raises_a_non_exception_does_not_propagate(monkeypatch, running_trial):
    """Regression: webpush.configured() can raise pyo3_runtime.PanicException
    (a broken native cryptography build) — a BaseException, not an
    Exception — which an `except Exception` guard would NOT catch. Caught
    here means the scheduler's daemon thread survives it."""
    broken = _StubChannel("broken", configured_raises=_NativePanic("native lib broke"))
    ok = _StubChannel("ok")
    monkeypatch.setattr(notifier, "CHANNELS", [broken, ok])

    report = digest.send_daily_digest()  # must not raise

    assert {"channel": "broken", "ok": False, "error": "native lib broke"} in report
    assert {"channel": "ok", "ok": True} in report
    assert ok.sent is not None
