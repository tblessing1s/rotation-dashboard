"""Process-wide Schwab request pacing + the short quote cache.

Schwab allows ~120 requests a minute across the whole app. The suite runs
UNPACED (conftest sets the rate to 0); these tests turn pacing on with a fake
clock and an injected sleep, so nothing here waits for real.
Run: python -m pytest backend/test_schwab_rate_limit.py -q
"""
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-rl-test-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import pytest  # noqa: E402

import config  # noqa: E402
import data_handler  # noqa: E402
import fetch_budget  # noqa: E402
import schwab_api  # noqa: E402


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        return self.t


@pytest.fixture()
def paced(monkeypatch):
    """Pacing ON at 60/min (one token a second, a 60-token burst), fake clock."""
    monkeypatch.setattr(config, "SCHWAB_REQUESTS_PER_MINUTE", 60)
    clock = _Clock()
    schwab_api.reset_rate_limiter()
    monkeypatch.setattr(schwab_api._limiter, "_clock", clock)
    return clock


# ---- the bucket itself ------------------------------------------------------

def test_a_burst_within_the_minute_never_waits(paced):
    lim = schwab_api._limiter
    assert [lim.wait_seconds() for _ in range(60)] == [0.0] * 60


def test_past_the_burst_callers_queue_in_order(paced):
    lim = schwab_api._limiter
    for _ in range(60):
        lim.wait_seconds()
    # 61st, 62nd, 63rd: one token a second, booked in sequence.
    assert lim.wait_seconds() == pytest.approx(1.0)
    assert lim.wait_seconds() == pytest.approx(2.0)
    assert lim.wait_seconds() == pytest.approx(3.0)
    # Time passes: the bucket refills and the queue drains.
    paced.t += 3.5
    assert lim.wait_seconds() == pytest.approx(0.5)
    paced.t += 10
    assert lim.wait_seconds() == 0.0


def test_a_429_pauses_every_caller(paced):
    lim = schwab_api._limiter
    assert lim.note_rate_limited("3") == 3.0
    assert lim.wait_seconds() == pytest.approx(3.0)        # even with tokens to spare
    paced.t += 1
    assert lim.wait_seconds() == pytest.approx(2.0)
    paced.t += 2
    assert lim.wait_seconds() == 0.0


def test_a_429_without_retry_after_uses_the_configured_pause(paced, monkeypatch):
    monkeypatch.setattr(config, "SCHWAB_429_PAUSE_SECONDS", 2.5)
    assert schwab_api._limiter.note_rate_limited(None) == 2.5
    assert schwab_api._limiter.note_rate_limited("garbage") == 2.5
    assert schwab_api._limiter.wait_seconds() == pytest.approx(2.5)


def test_the_order_path_never_queues_but_sits_out_a_pause(paced):
    lim = schwab_api._limiter
    for _ in range(70):                       # bucket well past empty
        lim.wait_seconds()
    slept = []
    assert lim.before_order(sleep=slept.append) == 0.0 and slept == []
    lim.note_rate_limited("9")                # a pause is honoured, bounded
    assert lim.before_order(sleep=slept.append) == 5.0
    assert slept == [5.0]


def test_rate_zero_disables_pacing(monkeypatch):
    monkeypatch.setattr(config, "SCHWAB_REQUESTS_PER_MINUTE", 0)
    schwab_api.reset_rate_limiter()
    assert all(schwab_api._limiter.wait_seconds() == 0.0 for _ in range(500))


# ---- wired into the request path ---------------------------------------------

class _Resp:
    def __init__(self, status, headers=None):
        self.status_code, self.text, self.headers = status, "", headers or {}
    def json(self):
        return {}


def test_request_paces_before_sending_and_pauses_after_a_429(paced, monkeypatch):
    calls = {"n": 0}
    responses = [_Resp(200)] * 60 + [_Resp(429, {"Retry-After": "4"}), _Resp(200)]

    def fake_get(url, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return responses[min(i, len(responses) - 1)]
    monkeypatch.setattr(schwab_api.requests, "get", fake_get)

    slept = []
    for _ in range(60):
        schwab_api._request("get", "http://x", sleep=slept.append)
    assert slept == []                                   # the burst goes straight out
    # 61st call: paced 1s (bucket empty), then Schwab answers 429 Retry-After 4 —
    # the retry backs off 4s AND every other caller is paused for 4s.
    resp = schwab_api._request("get", "http://x", sleep=slept.append)
    assert resp.status_code == 200
    assert slept[0] == pytest.approx(1.0)                # pacing before the send
    assert 4.0 in slept                                  # the retry honoured Retry-After
    assert schwab_api._limiter.wait_seconds() == pytest.approx(4.0)   # shared pause


def test_pacing_that_outlives_an_interactive_deadline_raises_not_hangs(paced, monkeypatch):
    monkeypatch.setattr(schwab_api.requests, "get", lambda url, **kw: _Resp(200))
    for _ in range(60):
        schwab_api._limiter.wait_seconds()
    schwab_api._limiter.note_rate_limited("30")
    with fetch_budget.interactive(deadline_seconds=5):
        with pytest.raises(schwab_api.SchwabError) as ei:
            schwab_api._request("get", "http://x", sleep=lambda s: None)
    assert "rate-limit pacing" in str(ei.value)


def test_status_reports_the_pacing_in_effect(paced):
    st = schwab_api.rate_limit_status()
    assert st["requests_per_minute"] == 60 and st["paused_for"] == 0.0
    schwab_api._limiter.note_rate_limited("2")
    assert schwab_api.rate_limit_status()["paused_for"] == 2.0


# ---- the quote cache ----------------------------------------------------------

class _QuoteClient:
    def __init__(self):
        self.single, self.batch = [], []
        self.price = 100.0
    def get_quote(self, symbol):
        self.single.append(symbol)
        return {"last": self.price}
    def get_quotes(self, symbols):
        self.batch.append(list(symbols))
        return {s: {"last": self.price + i} for i, s in enumerate(symbols)}


@pytest.fixture()
def quotes(monkeypatch):
    fake = _QuoteClient()
    clock = _Clock()
    monkeypatch.setattr(config, "QUOTE_CACHE_SECONDS", 5.0)
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(schwab_api, "market_configured", lambda: True)
    monkeypatch.setattr(data_handler, "client", lambda: fake)
    monkeypatch.setattr(data_handler, "_quote_clock", clock)
    data_handler.clear_quote_cache()
    return fake, clock


def test_display_readers_share_one_quote_within_the_ttl(quotes):
    fake, clock = quotes
    a = data_handler.latest_quote("SPCX")
    b = data_handler.latest_quote("SPCX")
    assert a["price"] == 100.0 and b["price"] == 100.0 and b.get("cached") is True
    assert fake.single == ["SPCX"]                       # one request served both
    clock.t += 6                                          # past the TTL
    fake.price = 101.0
    assert data_handler.latest_quote("SPCX")["price"] == 101.0
    assert fake.single == ["SPCX", "SPCX"]


def test_the_booking_path_never_sees_a_cached_quote(quotes):
    fake, _ = quotes
    data_handler.latest_quote("SPCX")
    fake.price = 102.0
    q = data_handler.fresh_quote("SPCX")
    assert q["price"] == 102.0 and not q.get("cached")
    assert fake.single == ["SPCX", "SPCX"]


def test_many_symbols_go_out_as_one_batch_and_fill_the_cache(quotes):
    fake, _ = quotes
    out = data_handler.latest_quotes(["SPCX", "ON", "XLK"])
    assert fake.batch == [["SPCX", "ON", "XLK"]] and fake.single == []
    assert out["ON"]["price"] == 101.0
    # Now cached: the position card's per-name reads cost nothing.
    assert data_handler.latest_quote("XLK")["cached"] is True
    assert fake.single == []
    # A second strip poll inside the TTL is served entirely from the cache.
    data_handler.latest_quotes(["SPCX", "ON", "XLK"])
    assert len(fake.batch) == 1


def test_a_single_missing_symbol_falls_back_per_name(quotes):
    fake, _ = quotes
    data_handler.latest_quotes(["SPCX"])
    assert fake.batch == [] and fake.single == ["SPCX"]


def test_capture_paths_use_the_fresh_quote(monkeypatch):
    import executor
    seen = []
    monkeypatch.setattr(data_handler, "fresh_quote",
                        lambda s: seen.append(s) or {"symbol": s, "price": 50.0, "source": "schwab"})
    monkeypatch.setattr(data_handler, "latest_quote",
                        lambda s: (_ for _ in ()).throw(AssertionError("cached path used")))
    assert executor._capture_price("SPCX", None, at_order_time=True) == (50.0, "schwab")
    rec = {"ticker": "SPCX", "stock_price": 49.0, "price_source": "schwab"}
    executor._stamp_fill_spot(rec, {})
    assert rec["stock_price"] == 50.0 and seen == ["SPCX", "SPCX"]
