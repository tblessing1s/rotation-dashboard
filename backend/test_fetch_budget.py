"""The interactive vs background fetch budget.

The bug these pin: a dead provider hung the dashboard. `schwab_api._request` gave
every caller the same retry budget on purpose ("so on-demand and background
fetches behave alike"), and that budget is 4 attempts x a 20s timeout plus 1+2+4s
of backoff = ~87 SECONDS for ONE symbol. The frontend aborts at 60s, so an
interactive request could not finish inside the window it was spending. Three
requests for the same ticker then queued on `data_handler`'s per-symbol lock
behind that one fetch and all three timed out together.

The invariants below are the fix, and each of the first four is a distinct way
the hang could come back.
"""
from __future__ import annotations

import time

import pytest

import alpha_vantage
import config
import data_handler
import fetch_budget
import schwab_api


# ---------------------------------------------------------------------------
# The default must stay PATIENT
# ---------------------------------------------------------------------------
def test_the_default_budget_is_the_old_behaviour_exactly():
    """Nothing changes for a caller that never opts in. The scheduler, the warm
    sweep and every CLI helper keep the knobs they had — this module is additive
    or it is a silent downgrade of every background path at once."""
    b = fetch_budget.current()
    assert b.name == fetch_budget.PATIENT
    assert b.attempts == config.SCHWAB_MAX_RETRIES
    assert b.base_seconds == config.SCHWAB_BACKOFF_BASE_SECONDS
    assert b.max_seconds == config.SCHWAB_BACKOFF_MAX_SECONDS
    assert b.deadline is None and not b.expired()
    # It must never narrow a call site's own timeout.
    assert b.cap_timeout(20) == 20
    assert b.sleep_for(16.0) == 16.0


def test_budget_reads_config_at_call_time_not_import_time(monkeypatch):
    """An operator's env override (or a test's monkeypatch) has to reach the very
    next fetch, not the next deploy."""
    monkeypatch.setattr(config, "SCHWAB_MAX_RETRIES", 9)
    assert fetch_budget.current().attempts == 9


# ---------------------------------------------------------------------------
# The interactive budget must fit inside the browser's patience
# ---------------------------------------------------------------------------
def test_interactive_worst_case_fits_under_the_frontend_timeout():
    """THE load-bearing arithmetic. frontend/src/api.js aborts at 60s. The whole
    point is that the SERVER decides the outcome, so the request deadline must
    leave room for the response itself."""
    with fetch_budget.interactive():
        b = fetch_budget.current()
        # Worst case for one symbol: every attempt burns its full timeout, with a
        # backoff between each.
        per_symbol = b.attempts * b.timeout + (b.attempts - 1) * b.max_seconds
        assert per_symbol < 30, per_symbol
        # And the request as a whole is bounded regardless of fan-out.
        assert b.deadline is not None
        assert b.remaining() < 60, "deadline must beat the frontend's 60s abort"


def test_interactive_is_strictly_tighter_than_patient():
    patient = fetch_budget.patient_budget()
    with fetch_budget.interactive():
        i = fetch_budget.current()
    assert i.attempts <= patient.attempts
    assert i.max_seconds <= patient.max_seconds
    assert i.timeout is not None and patient.timeout is None
    assert i.cap_timeout(20) < patient.cap_timeout(20)


def test_cap_timeout_only_ever_narrows():
    with fetch_budget.interactive():
        b = fetch_budget.current()
        assert b.cap_timeout(20) == b.timeout      # capped down
        assert b.cap_timeout(1) == 1               # a tighter caller wins
        assert b.cap_timeout(None) == b.timeout


def test_cap_timeout_never_overshoots_the_deadline():
    """The last attempt before the deadline must not be allowed to run past it."""
    with fetch_budget.interactive(deadline_seconds=2.0):
        assert fetch_budget.current().cap_timeout(20) <= 2.0


def test_sleep_for_never_sleeps_past_the_deadline():
    """Backoff must not spend the time it is backing off to preserve."""
    with fetch_budget.interactive(deadline_seconds=1.0):
        assert fetch_budget.current().sleep_for(16.0) <= 1.0


def test_expired_budget_reports_zero_remaining():
    with fetch_budget.interactive(deadline_seconds=-1):
        b = fetch_budget.current()
        assert b.expired()
        assert b.remaining() == 0.0
        assert b.sleep_for(5.0) == 0.0


# ---------------------------------------------------------------------------
# Scoping — the context must not leak across requests
# ---------------------------------------------------------------------------
def test_the_budget_does_not_leak_out_of_its_block():
    """gunicorn REUSES threads. A budget left installed would hand the next
    request this one's already-expired deadline, and every fetch would serve
    cache for the life of the worker — a far worse bug than the one being fixed."""
    with fetch_budget.interactive():
        assert fetch_budget.current().interactive
    assert fetch_budget.current().name == fetch_budget.PATIENT


def test_patient_nests_inside_interactive_and_restores():
    with fetch_budget.interactive():
        with fetch_budget.patient():
            assert fetch_budget.current().name == fetch_budget.PATIENT
            assert fetch_budget.current().deadline is None
        assert fetch_budget.current().interactive


def test_set_current_and_reset_round_trip():
    """The Flask before_request/teardown_request pair can't hold a `with` open."""
    token = fetch_budget.set_current(fetch_budget.interactive_budget())
    try:
        assert fetch_budget.current().interactive
    finally:
        fetch_budget.reset(token)
    assert fetch_budget.current().name == fetch_budget.PATIENT


def test_propagate_carries_the_budget_into_a_pool_thread():
    """A ThreadPoolExecutor worker starts with an EMPTY context. Without
    `propagate`, data_handler.get_many would silently revert to the patient
    budget and the hang would survive in the batch path only."""
    import concurrent.futures as cf
    seen = {}

    def _look():
        seen["name"] = fetch_budget.current().name

    with fetch_budget.interactive():
        with cf.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(fetch_budget.propagate(_look)).result()
    assert seen["name"] == fetch_budget.INTERACTIVE


def test_without_propagate_a_pool_thread_would_lose_it():
    """Pins WHY propagate exists — if this ever starts returning 'interactive',
    contextvars changed and propagate can be simplified."""
    import concurrent.futures as cf
    seen = {}

    with fetch_budget.interactive():
        with cf.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(lambda: seen.setdefault("name", fetch_budget.current().name)).result()
    assert seen["name"] == fetch_budget.PATIENT


# ---------------------------------------------------------------------------
# schwab_api._request honours the budget
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status=200):
        self.status_code = status
        self.headers = {}


def _count_calls(monkeypatch, responses):
    calls = {"n": 0, "timeouts": []}

    def _fake_get(url, **kwargs):
        calls["n"] += 1
        calls["timeouts"].append(kwargs.get("timeout"))
        r = responses[min(calls["n"] - 1, len(responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(schwab_api.requests, "get", _fake_get)
    return calls


def test_request_uses_fewer_attempts_when_interactive(monkeypatch):
    slept = []
    calls = _count_calls(monkeypatch, [_Resp(503)] * 10)
    with fetch_budget.interactive():
        schwab_api._request("get", "http://x", sleep=slept.append, timeout=20)
    assert calls["n"] == config.INTERACTIVE_MAX_RETRIES
    assert calls["n"] < config.SCHWAB_MAX_RETRIES, "must be tighter than background"


def test_request_caps_the_call_sites_timeout_when_interactive(monkeypatch):
    calls = _count_calls(monkeypatch, [_Resp(200)])
    with fetch_budget.interactive():
        schwab_api._request("get", "http://x", sleep=lambda _: None, timeout=20)
    assert calls["timeouts"] == [config.INTERACTIVE_TIMEOUT_SECONDS]


def test_request_leaves_the_timeout_alone_in_the_background(monkeypatch):
    calls = _count_calls(monkeypatch, [_Resp(200)])
    schwab_api._request("get", "http://x", sleep=lambda _: None, timeout=20)
    assert calls["timeouts"] == [20]


def test_request_stops_at_the_deadline_and_says_so(monkeypatch):
    """Past the deadline it must RAISE, not return None — every caller does
    `resp.status_code`, so a None would surface as an AttributeError instead of
    the thing that actually happened."""
    calls = _count_calls(monkeypatch, [_Resp(503)] * 10)
    with fetch_budget.interactive(deadline_seconds=-1):
        with pytest.raises(schwab_api.SchwabError, match="deadline"):
            schwab_api._request("get", "http://x", sleep=lambda _: None, timeout=20)
    assert calls["n"] == 1, "the first attempt still runs; only retries are cut"


def test_request_never_returns_none(monkeypatch):
    """A future edit that adds a `break` must fail loudly here rather than
    handing None to `.status_code`."""
    _count_calls(monkeypatch, [_Resp(200)])
    assert schwab_api._request("get", "http://x", sleep=lambda _: None) is not None


# ---------------------------------------------------------------------------
# Alpha Vantage — the fallback leg
# ---------------------------------------------------------------------------
def test_av_daily_cap_is_terminal_not_retryable(monkeypatch):
    """'Information' is the DAILY cap. It does not lift for the rest of the day,
    so sleeping 2s and asking again just spends the caller's deadline being told
    no twice more — that is what made an exhausted free-tier key look like a hung
    server. Only 'Note' (the per-minute throttle) is worth waiting out."""
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "k")
    calls = {"n": 0}
    slept = []

    class _Fake:
        def __enter__(self):
            calls["n"] += 1
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"Information": "daily rate limit reached"}'

    monkeypatch.setattr(alpha_vantage, "urlopen", lambda *a, **kw: _Fake())
    monkeypatch.setattr(alpha_vantage.time, "sleep", slept.append)
    with pytest.raises(alpha_vantage.AlphaVantageError, match="daily rate limit"):
        alpha_vantage._get({"function": "X"})
    assert calls["n"] == 1, "the daily cap must not be retried"
    assert slept == [], "and must not sleep"


def test_av_per_minute_note_is_still_retried(monkeypatch):
    """The distinction has to cut both ways, or this is just 'retry less'."""
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "k")
    calls = {"n": 0}

    class _Fake:
        def __enter__(self):
            calls["n"] += 1
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"Note": "call frequency"}'

    monkeypatch.setattr(alpha_vantage, "urlopen", lambda *a, **kw: _Fake())
    monkeypatch.setattr(alpha_vantage.time, "sleep", lambda _: None)
    with pytest.raises(alpha_vantage.AlphaVantageError):
        alpha_vantage._get({"function": "X"})
    assert calls["n"] > 1


# ---------------------------------------------------------------------------
# data_handler — the lock pile-up that was actually observed
# ---------------------------------------------------------------------------
def test_expired_budget_serves_cache_without_taking_the_symbol_lock(monkeypatch):
    """THE observed bug. Three requests for one ticker queued on `_symbol_lock`
    behind a single long fetch, so the 2nd and 3rd paid the 1st's full retry
    budget before starting their own. Past the deadline a reader must not touch
    the provider or the lock at all."""
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(data_handler, "_is_fresh", lambda *a, **kw: False)
    monkeypatch.setattr(data_handler, "_fallback", lambda s: "CACHED")

    def _boom(symbol):
        raise AssertionError("the provider must not be called past the deadline")

    monkeypatch.setattr(data_handler, "_fetch", _boom)

    lock = data_handler._symbol_lock("EOG")
    assert lock.acquire(blocking=False), "precondition: lock is free"
    try:
        with fetch_budget.interactive(deadline_seconds=-1):
            # Would deadlock (or block) if get_daily queued on the held lock.
            assert data_handler.get_daily("EOG") == "CACHED"
    finally:
        lock.release()


def test_the_degraded_read_records_a_reason(monkeypatch):
    """A degraded read must be visible, not silent — /api/data-health reads
    `last_error`, and STALE_BLOCKS_GO is what keeps a stale frame from voting."""
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(data_handler, "_is_fresh", lambda *a, **kw: False)
    monkeypatch.setattr(data_handler, "_fallback", lambda s: "CACHED")
    with fetch_budget.interactive(deadline_seconds=-1):
        data_handler.get_daily("ZZZ")
    assert "deadline" in (data_handler.last_error("ZZZ") or "")


def test_get_many_propagates_the_budget(monkeypatch):
    """The batch path is where a lost context would hide the longest."""
    monkeypatch.setattr(config, "_demo_mode", False)
    seen = []
    monkeypatch.setattr(data_handler, "get_daily",
                        lambda s, force=False: seen.append(fetch_budget.current().name))
    with fetch_budget.interactive():
        data_handler.get_many(["A", "B", "C"])
    assert seen and all(n == fetch_budget.INTERACTIVE for n in seen), seen


# ---------------------------------------------------------------------------
# The order path keeps its patience
# ---------------------------------------------------------------------------
def test_execute_opts_back_into_the_patient_budget(monkeypatch):
    """Money moving is the one place correctness outranks latency. A quote
    quietly served from this morning's cache is worse than an operator waiting."""
    import executor
    seen = {}

    def _spy(payload, now=None):
        seen["name"] = fetch_budget.current().name
        seen["deadline"] = fetch_budget.current().deadline
        return {"ok": True}

    monkeypatch.setattr(executor, "_execute", _spy)
    with fetch_budget.interactive():
        executor.execute({"action": "sell_short", "ticker": "AAA"})
    assert seen["name"] == fetch_budget.PATIENT
    assert seen["deadline"] is None


def test_execute_restores_the_caller_budget_afterwards(monkeypatch):
    import executor
    monkeypatch.setattr(executor, "_execute", lambda p, now=None: {})
    with fetch_budget.interactive():
        executor.execute({"action": "sell_short", "ticker": "AAA"})
        assert fetch_budget.current().interactive


def test_execute_restores_the_budget_even_when_it_raises(monkeypatch):
    import executor

    def _boom(payload, now=None):
        raise ValueError("nope")

    monkeypatch.setattr(executor, "_execute", _boom)
    with fetch_budget.interactive():
        with pytest.raises(ValueError):
            executor.execute({"action": "sell_short", "ticker": "AAA"})
        assert fetch_budget.current().interactive


# ---------------------------------------------------------------------------
# The Flask wiring
# ---------------------------------------------------------------------------
def test_every_request_runs_under_the_interactive_budget():
    """Drives the real before_request/teardown_request pair rather than adding a
    probe route — Flask refuses new routes once the app has served a request, and
    in a full-suite run it always has by the time this file is reached."""
    import app as app_module

    with app_module.app.test_request_context("/api/auth/status"):
        app_module.app.preprocess_request()
        try:
            assert fetch_budget.current().interactive
            assert fetch_budget.current().deadline is not None
        finally:
            app_module.app.do_teardown_request()
    assert fetch_budget.current().name == fetch_budget.PATIENT


def test_the_budget_is_released_after_the_request():
    """teardown_request must reset it, or a reused gunicorn thread inherits an
    expired deadline and serves cache forever."""
    import app as app_module

    client = app_module.app.test_client()
    client.get("/api/auth/status")
    assert fetch_budget.current().name == fetch_budget.PATIENT


def test_av_retryability_is_decided_by_key_not_message_text(monkeypatch):
    """Regression on a PRE-EXISTING bug this change surfaced. The old check was
    `"Error Message" in str(e)`, but the exception message carries the key's
    VALUE, not the key — so a hard error only short-circuited if Alpha Vantage's
    prose happened to contain the string "Error Message". It almost never does,
    so hard errors were retried exactly like throttles."""
    hard = alpha_vantage.AlphaVantageError("Alpha Vantage: Invalid API call",
                                           key="Error Message")
    assert hard.terminal
    assert "Error Message" not in str(hard), "the bug's precondition"

    cap = alpha_vantage.AlphaVantageError("Alpha Vantage: 25 requests/day",
                                          key="Information")
    assert cap.terminal

    throttle = alpha_vantage.AlphaVantageError("Alpha Vantage: call frequency",
                                               key="Note")
    assert not throttle.terminal

    # A transport failure carries no key and really is transient.
    assert not alpha_vantage.AlphaVantageError("connection reset").terminal


def test_av_hard_error_is_not_retried(monkeypatch):
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "k")
    calls = {"n": 0}

    class _Fake:
        def __enter__(self):
            calls["n"] += 1
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"Error Message": "Invalid API call"}'

    monkeypatch.setattr(alpha_vantage, "urlopen", lambda *a, **kw: _Fake())
    monkeypatch.setattr(alpha_vantage.time, "sleep", lambda _: None)
    with pytest.raises(alpha_vantage.AlphaVantageError):
        alpha_vantage._get({"function": "X"})
    assert calls["n"] == 1
