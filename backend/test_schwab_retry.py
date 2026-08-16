"""Transient-failure resilience for the on-demand Schwab read path.

`schwab_api._request` wraps read/idempotent calls with bounded exponential
backoff on connection resets / read timeouts (which `requests` raises) and the
retryable HTTP statuses (429 + 5xx), reusing the scheduler's knobs
(`config.SCHWAB_MAX_RETRIES` / `SCHWAB_BACKOFF_BASE_SECONDS` /
`SCHWAB_BACKOFF_MAX_SECONDS`). Order *submission* must stay single-attempt — a
retried POST could double-submit a live order.
"""
import pytest
import requests as _requests

import config
import schwab_api


class _Resp:
    def __init__(self, status, text="", payload=None, headers=None):
        self.status_code = status
        self.text = text
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


def _seq_get(monkeypatch, results):
    """Fake `requests.get` yielding results[i] on successive calls (a `_Resp` is
    returned, an `Exception` is raised); the last entry repeats. Records count."""
    calls = {"n": 0}

    def fake_get(url, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        item = results[min(i, len(results) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(schwab_api.requests, "get", fake_get)
    return calls


# ---- core retry matrix (exercises _request directly) -----------------------

def test_retries_on_timeout_then_succeeds(monkeypatch):
    calls = _seq_get(monkeypatch, [
        _requests.exceptions.ConnectTimeout("boom"),
        _requests.exceptions.ReadTimeout("boom"),
        _Resp(200, payload={"ok": True}),
    ])
    slept = []
    resp = schwab_api._request("get", "http://x", sleep=slept.append)
    assert resp.status_code == 200
    assert calls["n"] == 3
    assert slept == [config.SCHWAB_BACKOFF_BASE_SECONDS,
                     config.SCHWAB_BACKOFF_BASE_SECONDS * 2]   # base, then doubled


def test_retries_on_5xx_then_succeeds(monkeypatch):
    calls = _seq_get(monkeypatch, [_Resp(503), _Resp(200, payload={})])
    slept = []
    resp = schwab_api._request("get", "http://x", sleep=slept.append)
    assert resp.status_code == 200
    assert calls["n"] == 2
    assert slept == [config.SCHWAB_BACKOFF_BASE_SECONDS]


def test_honors_retry_after_header(monkeypatch):
    _seq_get(monkeypatch, [_Resp(429, headers={"Retry-After": "5"}), _Resp(200)])
    slept = []
    schwab_api._request("get", "http://x", sleep=slept.append)
    assert slept == [5.0]   # server-specified wait wins over the computed backoff


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_does_not_retry_client_errors(monkeypatch, code):
    calls = _seq_get(monkeypatch, [_Resp(code), _Resp(200)])
    slept = []
    resp = schwab_api._request("get", "http://x", sleep=slept.append)
    assert resp.status_code == code   # returned as-is for the caller to raise on
    assert calls["n"] == 1
    assert slept == []


def test_exhausts_retries_and_raises_last_network_error(monkeypatch):
    err = _requests.exceptions.ConnectionError("down")
    calls = _seq_get(monkeypatch, [err] * (config.SCHWAB_MAX_RETRIES + 2))
    slept = []
    with pytest.raises(_requests.exceptions.ConnectionError):
        schwab_api._request("get", "http://x", sleep=slept.append)
    assert calls["n"] == config.SCHWAB_MAX_RETRIES              # one call per attempt
    assert len(slept) == config.SCHWAB_MAX_RETRIES - 1         # no backoff after the last


def test_returns_last_response_when_status_stays_retryable(monkeypatch):
    # Never loops past the attempt budget even if every attempt is a 5xx.
    calls = _seq_get(monkeypatch, [_Resp(503)] * (config.SCHWAB_MAX_RETRIES + 5))
    slept = []
    resp = schwab_api._request("get", "http://x", sleep=slept.append)
    assert resp.status_code == 503
    assert calls["n"] == config.SCHWAB_MAX_RETRIES
    assert len(slept) == config.SCHWAB_MAX_RETRIES - 1


def test_backoff_is_capped(monkeypatch):
    # A long run of retries must never sleep beyond the configured ceiling.
    err = _requests.exceptions.ReadTimeout("slow")
    _seq_get(monkeypatch, [err] * (config.SCHWAB_MAX_RETRIES + 2))
    slept = []
    with pytest.raises(_requests.exceptions.ReadTimeout):
        schwab_api._request("get", "http://x", sleep=slept.append)
    assert all(w <= config.SCHWAB_BACKOFF_MAX_SECONDS for w in slept)


# ---- wiring: read methods retry, the order path does NOT --------------------

def test_get_quotes_retries_transient(monkeypatch):
    monkeypatch.setattr(schwab_api.time, "sleep", lambda s: None)
    calls = _seq_get(monkeypatch, [
        _Resp(500),
        _Resp(200, payload={"AAPL": {"quote": {"lastPrice": 100.0}}}),
    ])
    client = schwab_api.SchwabClient()
    monkeypatch.setattr(client, "_token", lambda: "tok")
    out = client.get_quotes(["AAPL"])
    assert calls["n"] == 2          # one failed attempt, one success — the read retried
    assert isinstance(out, dict)


def test_place_order_is_not_retried(monkeypatch):
    monkeypatch.setattr(schwab_api.time, "sleep", lambda s: None)
    posts = {"n": 0}

    def fake_post(url, **kwargs):
        posts["n"] += 1
        return _Resp(500, text="server error")

    monkeypatch.setattr(schwab_api.requests, "post", fake_post)
    client = schwab_api.SchwabClient()
    monkeypatch.setattr(client, "_token", lambda: "tok")
    with pytest.raises(schwab_api.SchwabError):
        client.place_order("acct-hash", {"orderType": "LIMIT"})
    assert posts["n"] == 1          # single attempt — a live order is never re-transmitted
