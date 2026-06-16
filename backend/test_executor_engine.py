"""Executor engine tests — build step 1 (REPLAY validation + architecture).

The headline guarantee: REPLAY reproduces the backtester's trades EXACTLY on the
same data/config (same entry / stop / target / outcome / R). That is the spec's
acceptance criterion for going from backtest -> paper -> live with confidence.
The rest cover the MODE binding (one-line swap), the gap rule, the guarded LIVE
adapter, and the SimulatedExecutionAdapter's honest fill/exit math offline.
"""
import pandas as pd
import pytest

import backtest as engine
import executor_engine as ee
from test_backtest import daily_frame, intraday, make_loaders

DAY = "2026-06-15"
# Yesterday ranges 100–110 (daily Wilder ATR == 10); breakout day is the next
# session. atr_beyond_level x2 -> stop is 2*10=20 beyond the level.
DAILY = {"HOOD": daily_frame(end="2026-06-12", periods=20, high=110.0, low=100.0, close=105.0)}

# Shared rule knobs for both the backtester and the executor engine, so the only
# difference between them is the execution path — not the detection/sizing rules.
RULES = {
    "tickers": ["HOOD"],
    "setup_conditions": {"type": "support_resistance_break", "proximity_pct": 0.0},
    "entry_rules": {"volume_multiplier": 2.0, "vol_avg_length": 3, "entry_timing": "candle_close"},
    "stop_logic": "atr_beyond_level",
    "stop_params": {"atr_multiplier": 2.0, "atr_period": 14, "atr_timeframe": "daily"},
    "risk_reward": 2.0,
}

# Fields that must be identical between a backtest trade and a replay trade.
COMPARE = ["date", "ticker", "level_type", "direction", "entry_time", "entry_price",
           "stop_price", "target_price", "risk_amount", "reward_amount", "exit_price",
           "outcome", "r_result", "exit_time", "spy_direction", "sector_direction",
           "volume_ratio", "entry_volume", "avg_volume", "volume_spike"]


def engine_config(**over):
    cfg = {**RULES, "fixed_risk_per_trade": 100.0, **over}
    return cfg


def backtest_config(**over):
    cfg = {**RULES, "date_range": {"start": DAY, "end": DAY}, **over}
    c, errors = engine.validate_config(cfg)
    assert not errors, errors
    return c


def win_session():
    """Breakout closes 111 > Y-High 110, then prints the target (153) cleanly."""
    return {("HOOD", DAY): intraday(DAY, [
        ("09:30", 105, 106, 104, 105, 100),
        ("09:35", 105, 106, 104, 105, 100),
        ("09:40", 105, 106, 104, 105, 100),
        ("09:45", 109, 112, 109, 111, 1000),   # entry 111, stop 90, target 153
        ("09:50", 111, 153, 111, 152, 500),    # high 153 hits target, low 111 spares stop
    ])}


def loss_session():
    """Same breakout, but the next bar trades down to the stop (90) before target."""
    return {("HOOD", DAY): intraday(DAY, [
        ("09:30", 105, 106, 104, 105, 100),
        ("09:35", 105, 106, 104, 105, 100),
        ("09:40", 105, 106, 104, 105, 100),
        ("09:45", 109, 112, 109, 111, 1000),
        ("09:50", 111, 112, 90, 91, 500),      # low 90 hits stop, high 112 spares target
    ])}


def _replay(intraday_map, daily_map=DAILY, **over):
    loaders = make_loaders(intraday_map, daily_map)
    return ee.run_replay(engine_config(**over), date=DAY,
                         data_source=ee.ReplayDataSource(get_intraday_range=loaders[0],
                                                         get_daily=loaders[1]))


def _backtest(intraday_map, daily_map=DAILY, **over):
    loaders = make_loaders(intraday_map, daily_map)
    return engine.run_backtest(backtest_config(**over),
                               get_intraday_range=loaders[0], get_daily=loaders[1])


# ---------------------------------------------------------------------------
# REPLAY reproduces the backtester exactly
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("session_fn, expected", [
    (win_session, "Win"),
    (loss_session, "Loss"),
])
def test_replay_matches_backtest(session_fn, expected):
    session = session_fn()
    rep = _replay(session)
    bt = _backtest(session)

    assert rep["ok"] and rep["count"] == 1
    assert len(bt["trades"]) == 1
    r_trade, b_trade = rep["trades"][0], bt["trades"][0]

    assert r_trade["outcome"] == expected == b_trade["outcome"]
    for k in COMPARE:
        assert r_trade[k] == b_trade[k], f"{k}: replay {r_trade[k]!r} != backtest {b_trade[k]!r}"


def test_replay_win_has_expected_numbers():
    """Pin the actual values so a silent rule change is caught, not just parity."""
    t = _replay(win_session())["trades"][0]
    assert t["entry_price"] == 111.0 and t["stop_price"] == 90.0 and t["target_price"] == 153.0
    assert t["outcome"] == "Win" and t["exit_price"] == 153.0 and t["r_result"] == 2.0
    assert t["position_size"] == 4              # floor(100 / 21) — sizing in StrategyCore
    assert t["mode"] == ee.REPLAY and t["account_type"] == "REPLAY"


def test_replay_summary_aggregates():
    out = _replay(loss_session())
    assert out["summary"]["losses"] == 1 and out["summary"]["wins"] == 0


# ---------------------------------------------------------------------------
# MODE binding — going live is a one-line binding change, no logic edits
# ---------------------------------------------------------------------------
def test_mode_binding_selects_data_source_and_adapter():
    expected = {
        ee.REPLAY: (ee.ReplayDataSource, ee.ReplayExecutionAdapter),
        ee.PAPER: (ee.LiveDataSource, ee.SimulatedExecutionAdapter),
        ee.LIVE: (ee.LiveDataSource, ee.LiveExecutionAdapter),
    }
    for mode, (ds_cls, ad_cls) in expected.items():
        cfg, errors = ee.validate_engine_config(engine_config(mode=mode))
        assert not errors
        eng = ee.build_engine(cfg)
        assert eng.mode == mode
        assert isinstance(eng.data, ds_cls)
        assert isinstance(eng.adapter, ad_cls)
        # The shared core is the same class regardless of mode.
        assert isinstance(eng.core, ee.StrategyCore)


def test_switching_mode_changes_only_binding_not_detection():
    """The detected setup must be identical across modes (detection is shared)."""
    loaders = make_loaders(win_session(), DAILY)
    data = ee.ReplayDataSource(get_intraday_range=loaders[0], get_daily=loaders[1])

    setups = {}
    for mode in (ee.REPLAY, ee.PAPER, ee.LIVE):
        cfg, _ = ee.validate_engine_config(engine_config(mode=mode))
        core = ee.StrategyCore(cfg)
        ctx = core.context("HOOD", DAY, data)
        setups[mode] = core.detect("HOOD", DAY, ctx, data)

    base = setups[ee.REPLAY]
    for mode in (ee.PAPER, ee.LIVE):
        assert setups[mode].signal() == base.signal()


# ---------------------------------------------------------------------------
# Gap rule (StrategyCore) — matches the backtester
# ---------------------------------------------------------------------------
def gapped_open_session():
    """Opens at 120, above Y-High 110, and never returns into the range — the gap
    rule forbids the trade even though later candles 'close beyond' the level."""
    return {("HOOD", DAY): intraday(DAY, [
        ("09:30", 120, 122, 119, 121, 100),
        ("09:35", 121, 123, 120, 122, 100),
        ("09:40", 121, 123, 120, 122, 100),
        ("09:45", 121, 124, 121, 123, 1000),   # volume spike, close > Y-High, but gapped
    ])}


def test_gap_rule_blocks_and_matches_backtest():
    session = gapped_open_session()
    assert _replay(session)["count"] == 0
    assert len(_backtest(session)["trades"]) == 0


def test_gap_rule_off_allows_trade():
    """With the gap rule disabled the same gapped day produces a setup."""
    assert _replay(gapped_open_session(), gap_rule=False)["count"] == 1


# ---------------------------------------------------------------------------
# LIVE adapter is a guarded stub — MODE=LIVE cannot fire a real order
# ---------------------------------------------------------------------------
def test_live_adapter_is_guarded():
    adapter = ee.LiveExecutionAdapter()
    setup = _build_one_setup(win_session())
    with pytest.raises(RuntimeError, match="guarded"):
        adapter.execute(setup, core=None, data=None)


def test_live_adapter_builds_real_bracket_but_never_transmits():
    adapter = ee.LiveExecutionAdapter(armed=True)
    setup = _build_one_setup(win_session())
    order = adapter.build_order(setup)
    assert order["orderStrategyType"] == "TRIGGER"          # entry + attached OCO
    assert order["orderLegCollection"][0]["instruction"] == "BUY"
    with pytest.raises(NotImplementedError):
        adapter.transmit(order)


# ---------------------------------------------------------------------------
# SimulatedExecutionAdapter — honest fill + exit math (offline)
# ---------------------------------------------------------------------------
def _build_one_setup(intraday_map, **over):
    loaders = make_loaders(intraday_map, DAILY)
    data = ee.ReplayDataSource(get_intraday_range=loaders[0], get_daily=loaders[1])
    cfg, _ = ee.validate_engine_config(engine_config(**over))
    core = ee.StrategyCore(cfg)
    ctx = core.context("HOOD", DAY, data)
    return core.detect("HOOD", DAY, ctx, data)


def test_sim_entry_applies_adverse_slippage_and_captures_spread():
    setup = _build_one_setup(win_session())   # Long, signal price 111
    adapter = ee.SimulatedExecutionAdapter()
    cfg, _ = ee.validate_engine_config(engine_config())  # entry slippage 0.02 cents
    fill = adapter.open_position(setup, live_price=111.0, bid=110.98, ask=111.02, config=cfg)
    assert fill["entry_fill"] == 111.02        # long pays up by the slippage
    assert fill["entry_spread"] == 0.04
    assert fill["entry_slippage"] == 0.02


def test_sim_resolves_exit_by_true_sequence_target_first():
    setup = _build_one_setup(win_session())    # stop 90, target 153
    adapter = ee.SimulatedExecutionAdapter()
    cfg, _ = ee.validate_engine_config(engine_config())
    fill = adapter.open_position(setup, live_price=111.0, bid=110.98, ask=111.02, config=cfg)
    # Sequence: a tick at 153 prints before anything near the stop.
    events = [{"price": 140, "time": "09:48"}, {"price": 153, "time": "09:49"}]
    out = adapter.resolve_exit(setup, fill, events, config=cfg)
    assert out["outcome"] == "Win" and out["exit_price"] == 153.0
    assert out["account_type"] == "PAPER"


def test_sim_resolves_stop_first_with_pessimistic_slippage():
    setup = _build_one_setup(win_session())    # stop 90
    adapter = ee.SimulatedExecutionAdapter()
    cfg, _ = ee.validate_engine_config(engine_config())
    fill = adapter.open_position(setup, live_price=111.0, bid=110.98, ask=111.02, config=cfg)
    events = [{"price": 90, "time": "09:47"}]      # stop touched first
    out = adapter.resolve_exit(setup, fill, events, config=cfg)
    assert out["outcome"] == "Loss"
    assert out["exit_price"] == pytest.approx(90 - 0.02)   # filled slightly worse than stop


def test_sim_window_end_close_when_neither_level_hit():
    setup = _build_one_setup(win_session())
    adapter = ee.SimulatedExecutionAdapter()
    cfg, _ = ee.validate_engine_config(engine_config())
    fill = adapter.open_position(setup, live_price=111.0, bid=110.98, ask=111.02, config=cfg)
    out = adapter.resolve_exit(setup, fill, [{"price": 120, "time": "09:55"}],
                               config=cfg, window_end_price=120.0)
    assert out["notes"] == "closed at window end"
    assert out["exit_price"] == 120.0 and out["outcome"] == "Win"   # 120 > entry


def test_sim_execute_refuses_without_live_stream():
    adapter = ee.SimulatedExecutionAdapter()
    setup = _build_one_setup(win_session())
    with pytest.raises(NotImplementedError):
        adapter.execute(setup, core=None, data=None)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------
def test_validate_engine_config_defaults():
    cfg, errors = ee.validate_engine_config({})
    assert not errors
    assert cfg["mode"] == ee.REPLAY
    assert cfg["gap_rule"] is True
    assert cfg["exit_resolution_granularity"] == "tick"
    assert cfg["entry_slippage"] == {"type": "cents", "value": 0.02}


# ---------------------------------------------------------------------------
# PAPER session runner — real-time virtual execution (offline, injected feed)
# ---------------------------------------------------------------------------
def _paper_source(intraday_map, quotes, fine_map=None):
    """A PAPER (live) data source with synthetic 5m/1m bars + a quote table.

    ``quotes`` maps ticker -> {last, bid, ask}, standing in for the Schwab feed.
    """
    loaders = make_loaders(intraday_map, DAILY, fine_map=fine_map)
    return ee.LiveDataSource(get_intraday_range=loaders[0], get_daily=loaders[1],
                             quote_fn=lambda sym: quotes.get(sym))


def _one_min(date, rows):
    """1-minute bars (HH:MM, open, high, low, close, volume) for exit resolution."""
    return {("HOOD", date): intraday(date, rows)}


def test_paper_opens_at_live_price_with_slippage(fresh_db):
    # Breakout at 09:45 closes 111; the live quote at signal time is 111.20.
    quotes = {"HOOD": {"last": 111.20, "bid": 111.18, "ask": 111.22}}
    data = _paper_source(win_session(), quotes)
    out = ee.run_paper(engine_config(), on_date=DAY, as_of="2026-06-15 09:51",
                       data_source=data)
    assert out["ok"] and len(out["opened"]) == 1
    t = out["opened"][0]
    assert t["account_type"] == "PAPER" and t["outcome"] == "OPEN"
    # Entry captured at the LIVE price (111.20) + adverse slippage (0.02), not the
    # candle close (111.00).
    assert t["entry_price"] == 111.22
    assert t["entry_spread"] == 0.04 and t["slippage"] == 0.02
    assert t["mode"] == "PAPER"


def test_paper_resolves_target_from_one_minute_feed(fresh_db):
    quotes = {"HOOD": {"last": 111.20, "bid": 111.18, "ask": 111.22}}
    # 1-minute bars after entry: price climbs and prints the target (153) cleanly.
    fine = _one_min(DAY, [
        ("09:50", 111, 140, 111, 139, 50),
        ("09:51", 139, 153, 139, 152, 50),   # high 153 -> target hit
    ])
    data = _paper_source(win_session(), quotes, fine_map=fine)
    out = ee.run_paper(engine_config(), on_date=DAY, as_of="2026-06-15 09:52",
                       data_source=data)
    assert len(out["resolved"]) == 1
    t = out["resolved"][0]
    assert t["outcome"] == "WIN" and t["exit_price"] == 153.0
    # Persisted: a follow-up poll sees it already resolved (no duplicate open).
    again = ee.run_paper(engine_config(), on_date=DAY, as_of="2026-06-15 09:53",
                         data_source=data)
    assert again["opened"] == [] and again["resolved"] == []


def test_paper_stop_fills_pessimistically(fresh_db):
    quotes = {"HOOD": {"last": 111.0, "bid": 110.98, "ask": 111.02}}
    fine = _one_min(DAY, [("09:50", 111, 112, 90, 91, 50)])   # low 90 -> stop hit
    data = _paper_source(win_session(), quotes, fine_map=fine)
    out = ee.run_paper(engine_config(), on_date=DAY, as_of="2026-06-15 09:51",
                       data_source=data)
    assert len(out["resolved"]) == 1
    t = out["resolved"][0]
    assert t["outcome"] == "LOSS"
    assert t["exit_price"] == pytest.approx(90 - 0.02)   # stop slippage, pessimistic


def test_paper_stays_open_until_a_level_is_touched(fresh_db):
    quotes = {"HOOD": {"last": 120.0, "bid": 119.98, "ask": 120.02}}
    fine = _one_min(DAY, [("09:50", 111, 119, 111, 118, 50)])  # neither stop nor target
    data = _paper_source(win_session(), quotes, fine_map=fine)
    out = ee.run_paper(engine_config(), on_date=DAY, as_of="2026-06-15 09:51",
                       data_source=data)
    assert len(out["opened"]) == 1 and out["resolved"] == []
    assert len(out["open"]) == 1 and out["open"][0]["outcome"] == "OPEN"


def test_paper_falls_back_to_candle_close_without_a_quote(fresh_db):
    data = _paper_source(win_session(), quotes={})   # quote feed returns nothing
    out = ee.run_paper(engine_config(), on_date=DAY, as_of="2026-06-15 09:51",
                       data_source=data)
    assert len(out["opened"]) == 1
    # No live quote -> entry falls back to the signal candle close (+slippage).
    assert out["opened"][0]["entry_price"] == 111.02


def test_validate_engine_config_rejects_bad_values():
    _, errors = ee.validate_engine_config({
        "mode": "BOGUS", "exit_resolution_granularity": "hourly",
        "entry_slippage": {"type": "nope", "value": -1}, "replay_speed": "warp",
    })
    joined = " ".join(errors)
    assert "mode" in joined and "exit_resolution_granularity" in joined
    assert "entry_slippage" in joined and "replay_speed" in joined
