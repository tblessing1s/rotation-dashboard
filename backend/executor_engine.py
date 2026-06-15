"""Executor engine — one shared core, swappable execution adapters.

This is the architectural spine of the paper/forward-testing executor. There is
exactly ONE place where setups are detected and orders are sized (``StrategyCore``);
everything that differs between replaying history, paper-trading live data, and
sending real orders is isolated behind an ``ExecutionAdapter`` and a
``DataSource``. A single ``MODE`` flag binds the pair, so going live is a
one-line binding change — never a rewrite.

    MODE      data source            execution adapter
    ──────    ───────────────────    ──────────────────────────
    REPLAY    ReplayDataSource       ReplayExecutionAdapter      (offline, full)
    PAPER     LiveDataSource         SimulatedExecutionAdapter   (real-time sim)
    LIVE      LiveDataSource         LiveExecutionAdapter         (guarded stub)

Detection + sizing reuse the backtest engine's *registered rules*
(``backtest.SETUP_TYPES`` / ``STOP_LOGIC``) and its exit simulator
(``backtest._simulate``). Because the rules and the simulator are shared, REPLAY
reproduces the backtester's trades exactly, and PAPER/LIVE inherit byte-identical
detection — the only thing that changes is how the exit is *observed* (historical
candles vs. a live tick/quote feed) and whether a real order is transmitted.

Build status: REPLAY is complete and validated against the backtester (see
``test_executor_engine.py``). The SimulatedExecutionAdapter's honest fill/exit
math is implemented and unit-tested offline; wiring it to a live Schwab quote
feed is step 2. LiveExecutionAdapter is a guarded scaffold — it builds the real
Schwab bracket but never transmits.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field

import pandas as pd

import backtest as engine
import intraday_executor as ix

# ---------------------------------------------------------------------------
# MODE
# ---------------------------------------------------------------------------
REPLAY = "REPLAY"
PAPER = "PAPER"
LIVE = "LIVE"
MODES = (REPLAY, PAPER, LIVE)


# ---------------------------------------------------------------------------
# Config (a superset of the monitor config + the adapter-only knobs)
# ---------------------------------------------------------------------------
DEFAULT_ENGINE_EXTRAS = {
    "mode": REPLAY,
    # Slippage models. type "cents" = a fixed per-share haircut; "spread" = a
    # fraction of the captured bid/ask spread. Applied in the *adverse* direction.
    "entry_slippage": {"type": "cents", "value": 0.02},
    "stop_slippage": {"type": "cents", "value": 0.02},
    # How the SimulatedExecutionAdapter resolves an exit in real time.
    "exit_resolution_granularity": "tick",   # tick | 1min
    # Gap rule: don't trade a day that opened beyond a level until price re-enters
    # yesterday's range. Matches the backtester when on.
    "gap_rule": True,
    # Replay playback speed for the (UI-facing) driver. Compute is unaffected.
    "replay_speed": "instant",               # instant | 10x | 1x
}

_SLIPPAGE_TYPES = {"cents", "spread"}
_GRANULARITIES = {"tick", "1min"}
_SPEEDS = {"instant", "10x", "1x"}


def validate_engine_config(raw: dict) -> tuple[dict, list[str]]:
    """Validate an engine config: the shared monitor config plus adapter knobs.

    The shared portion (tickers, setup, volume, stop, window, sizing) is
    validated by ``intraday_executor.validate_monitor_config`` so the engine and
    the backtester never diverge. Returns ``(config, errors)``.
    """
    raw = dict(raw or {})
    extras = {k: raw.pop(k, DEFAULT_ENGINE_EXTRAS[k]) for k in DEFAULT_ENGINE_EXTRAS}
    cfg, errors = ix.validate_monitor_config(raw)

    mode = str(extras["mode"] or REPLAY).upper()
    if mode not in MODES:
        errors.append(f"mode must be one of {MODES}.")
    cfg["mode"] = mode

    for key in ("entry_slippage", "stop_slippage"):
        model = extras[key] or {}
        if not isinstance(model, dict):
            errors.append(f"{key} must be an object {{type, value}}.")
            model = dict(DEFAULT_ENGINE_EXTRAS[key])
        stype = str(model.get("type", "cents")).lower()
        if stype not in _SLIPPAGE_TYPES:
            errors.append(f"{key}.type must be one of {sorted(_SLIPPAGE_TYPES)}.")
        try:
            value = float(model.get("value", 0) or 0)
            if value < 0:
                errors.append(f"{key}.value cannot be negative.")
        except (TypeError, ValueError):
            errors.append(f"{key}.value must be a number.")
            value = 0.0
        cfg[key] = {"type": stype, "value": value}

    gran = str(extras["exit_resolution_granularity"] or "tick").lower()
    if gran not in _GRANULARITIES:
        errors.append(f"exit_resolution_granularity must be one of {sorted(_GRANULARITIES)}.")
    cfg["exit_resolution_granularity"] = gran

    cfg["gap_rule"] = bool(extras["gap_rule"])

    speed = str(extras["replay_speed"] or "instant").lower()
    if speed not in _SPEEDS:
        errors.append(f"replay_speed must be one of {sorted(_SPEEDS)}.")
    cfg["replay_speed"] = speed

    return cfg, errors


# ---------------------------------------------------------------------------
# Data source abstraction
# ---------------------------------------------------------------------------
class DataSource(abc.ABC):
    """Where candles (and, for live modes, real-time quotes) come from.

    The two implementations sit behind one interface so StrategyCore and the
    adapters never know whether they are reading history or a live feed.
    """

    @abc.abstractmethod
    def intraday(self, symbol: str, start: str, end: str, interval_min: int) -> "pd.DataFrame | None":
        """Continuous intraday OHLCV for [start, end] (tz-naive ET index)."""

    @abc.abstractmethod
    def daily(self, symbol: str) -> "pd.DataFrame | None":
        """Daily OHLCV (date index) for yesterday's levels and ATR."""

    def quote(self, symbol: str) -> "dict | None":
        """Live last/bid/ask for the symbol, or None when unavailable.

        Only the live modes need this; replay never calls it.
        """
        return None


class ReplayDataSource(DataSource):
    """Historical candles from the datastore — the offline feed.

    Loaders are injectable so the engine is unit-testable with synthetic data
    (identical pattern to the backtest engine's loaders).
    """

    def __init__(self, *, get_intraday_range=None, get_daily=None):
        if get_intraday_range is None or get_daily is None:
            loaders = ix._loaders()
            get_intraday_range = get_intraday_range or loaders[0]
            get_daily = get_daily or loaders[1]
        self._get_intraday_range = get_intraday_range
        self._get_daily = get_daily

    def intraday(self, symbol, start, end, interval_min):
        return self._get_intraday_range(symbol, start, end, interval_min)

    def daily(self, symbol):
        return self._get_daily(symbol)


class LiveDataSource(ReplayDataSource):
    """Schwab real-time feed for PAPER/LIVE.

    Candles still come from the datastore (the existing polling backfill keeps it
    fresh), and ``quote`` reads the live last/bid/ask from the Schwab provider so
    the SimulatedExecutionAdapter can capture an entry at the true signal moment.

    NOTE: ``quote`` is scaffolded for build step 2 (PAPER). It reuses the existing
    Schwab connection — no new auth — but the real-time quote endpoint wiring is
    intentionally a TODO so REPLAY validation (step 1) stays self-contained.
    """

    def __init__(self, *, get_intraday_range=None, get_daily=None, quote_fn=None):
        super().__init__(get_intraday_range=get_intraday_range, get_daily=get_daily)
        self._quote_fn = quote_fn

    def quote(self, symbol):
        if self._quote_fn is not None:
            return self._quote_fn(symbol)
        # TODO (step 2 / PAPER): call the Schwab real-time quotes endpoint via the
        # existing SchwabProvider connection and return {last, bid, ask}.
        return None


# ---------------------------------------------------------------------------
# Setup — the output of detection, the input to an adapter
# ---------------------------------------------------------------------------
@dataclass
class Setup:
    """A detected setup with its computed order (entry/stop/target/size).

    This is what StrategyCore produces and what an ExecutionAdapter consumes.
    Raw (unrounded) ``entry``/``stop``/``target`` feed exit simulation; the
    rounded values live in ``signal()`` for the DB / order builder. ``kind`` is
    "trade" for a tradeable setup or "skip" for a setup blocked by a skip rule.
    """
    ticker: str
    date: str
    ts: pd.Timestamp
    entry_time: str
    direction: str
    level: float
    level_type: str
    entry: float
    stop: float
    target: float
    risk: float
    reward: float
    position_size: int
    entry_volume: float
    avg_volume: float
    volume_ratio: "float | None"
    volume_spike: bool
    atr: "float | None"
    spy_direction: str
    sector_direction: str
    interval_min: int
    kind: str = "trade"
    skip_note: str = ""
    # The day's full session candles, so the replay adapter can walk forward
    # without re-loading. Excluded from equality / repr.
    session: "pd.DataFrame | None" = field(default=None, repr=False, compare=False)

    def signal(self) -> dict:
        """The rounded signal payload (DB log + Schwab order builder shape)."""
        return {
            "date": self.date,
            "ticker": self.ticker,
            "candle_time": self.entry_time,
            "direction": self.direction,
            "level_type": self.level_type,
            "level": round(self.level, 2),
            "entry_price": round(self.entry, 2),
            "stop_price": round(self.stop, 2),
            "target_price": round(self.target, 2),
            "risk": round(self.risk, 2),
            "reward": round(self.reward, 2),
            "risk_reward_ratio": round(self.reward / self.risk, 2) if self.risk else None,
            "position_size": self.position_size,
            "entry_volume": int(round(self.entry_volume)),
            "avg_volume": int(round(self.avg_volume)),
            "volume_ratio": self.volume_ratio,
            "atr": round(float(self.atr), 4) if self.atr else None,
            "spy_direction": self.spy_direction,
            "sector_direction": self.sector_direction,
        }

    def base_trade(self) -> dict:
        """Trade fields shared by every adapter (mirrors the backtester schema so
        backtest / replay / paper / live analytics line up)."""
        return {
            "date": self.date,
            "ticker": self.ticker,
            "level_type": self.level_type,
            "volume_spike": self.volume_spike,
            "entry_volume": int(round(self.entry_volume)),
            "avg_volume": int(round(self.avg_volume)),
            "volume_ratio": self.volume_ratio,
            "direction": self.direction,
            "entry_time": self.entry_time,
            "spy_direction": self.spy_direction,
            "sector_direction": self.sector_direction,
            "position_size": self.position_size,
        }


# ---------------------------------------------------------------------------
# StrategyCore — the ONE place detection + order math lives
# ---------------------------------------------------------------------------
class StrategyCore:
    """Detect setups and size orders — identical across REPLAY / PAPER / LIVE.

    Reuses the backtest engine's registered detectors and stop-placement rules,
    plus its volume MA / ATR context assembly (``intraday_executor._ticker_context``),
    so the rules never drift from the backtester. The one-trade-per-ticker-per-day
    selection and the gap rule live here (the engine's ``_scan_day`` applies the
    same logic for offline backtests).
    """

    def __init__(self, config: dict):
        self.config = config
        self.interval = int(config.get("interval_min", 5))
        self.rr = float(config["risk_reward"])
        self.risk_dollars = float(config.get("fixed_risk_per_trade", 0) or 0)
        self._detector = engine.SETUP_TYPES[config["setup_conditions"]["type"]]
        self.place_stop = engine.STOP_LOGIC[config["stop_logic"]]
        self.is_break = config["setup_conditions"]["type"] == "support_resistance_break"
        self.entry_timing = config["entry_rules"].get("entry_timing", "candle_close")
        self.gap_rule = bool(config.get("gap_rule", True))
        self.skip = config.get("skip_conditions", {}) or {}

    def context(self, ticker: str, on_date: str, data: DataSource) -> "dict | None":
        """Per-ticker assembly: window candles, yesterday's levels, volume MA, ATR.

        Returns None when there is no session, or a dict carrying ``missing`` when
        yesterday's levels are unavailable (same skip rules as the backtester)."""
        return ix._ticker_context(
            ticker, on_date, self.config,
            get_intraday_range=data.intraday, get_daily=data.daily,
        )

    def detect(self, ticker: str, on_date: str, ctx: dict, data: DataSource,
               *, as_of=None) -> "Setup | None":
        """First qualifying setup of the day for ``ticker`` (one trade per day).

        ``as_of`` (live modes) restricts consideration to candles that have
        *closed* by that instant; omit it for replay to walk the whole day.
        """
        if not ctx or "window" not in ctx:
            return None
        window, session, levels = ctx["window"], ctx["session"], ctx["levels"]
        if window is None or window.empty:
            return None
        vol_avg = ctx["vol_avg"]
        resolve_atr = ctx["resolve_atr"]
        cfg = self.config
        as_of_ts = ix._to_et_naive(as_of) if as_of is not None else None

        # Gap filter (breakout only, when enabled): a day that opened *outside*
        # yesterday's range is a no-trade until price returns into [Y-Low, Y-High].
        gap_eligible = True
        if self.is_break and self.gap_rule and len(session):
            day_open = float(session.iloc[0]["Open"])
            gap_eligible = levels["y_low"] <= day_open <= levels["y_high"]

        day_session = self._day_session_fn(data)

        for ts, candle in window.iterrows():
            if as_of_ts is not None and ts + pd.Timedelta(minutes=self.interval) > as_of_ts:
                break  # candle has not closed yet — nothing after it has either
            avg_volume = float(vol_avg.get(ts, float("nan")))
            if avg_volume != avg_volume:  # NaN — MA window not full at this bar
                continue

            if self.is_break and self.gap_rule and not gap_eligible:
                if engine._in_yesterday_range(candle, levels):
                    gap_eligible = True
                continue  # the re-entry bar itself isn't a breakout entry

            setup = self._detector(candle, levels=levels, avg_volume=avg_volume, cfg=cfg)
            if not setup:
                continue
            return self._build_setup(ticker, on_date, ts, candle, setup, avg_volume,
                                     resolve_atr(ts), session, day_session, data)
        return None

    def _day_session_fn(self, data: DataSource):
        """A ``day_session(symbol, day)`` closure for market-context direction."""
        cache: dict[tuple, "pd.DataFrame | None"] = {}

        def day_session(sym, day):
            key = (sym, day)
            if key not in cache:
                s = data.intraday(sym, day, day, self.interval)
                if s is None or s.empty:
                    cache[key] = None
                else:
                    s = s.sort_index()
                    d = s[s.index.normalize() == pd.Timestamp(day).normalize()]
                    cache[key] = d if not d.empty else None
            return cache[key]
        return day_session

    def _build_setup(self, ticker, on_date, ts, candle, setup, avg_volume, atr,
                     session, day_session, data) -> "Setup | None":
        """Compute entry/stop/target/size for a detected setup (identical math to
        ``backtest._scan_day`` + the executor's position sizing)."""
        cfg = self.config
        direction = setup["direction"]
        level = float(setup["level"])
        entry = level if self.entry_timing == "immediate_touch" else float(candle["Close"])

        stop = self.place_stop(direction, level=level, entry=entry, atr=atr, cfg=cfg)
        if stop is None:
            return None  # ATR unavailable for stop placement — skip (matches engine)
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        reward = risk * self.rr
        target = entry + reward if direction == "Long" else entry - reward
        position_size = int(self.risk_dollars // risk) if self.risk_dollars > 0 else 0

        entry_volume = ix._finite(candle["Volume"], 0.0)
        volume_ratio = round(entry_volume / avg_volume, 2) if avg_volume > 0 else None

        spy_dir = engine._context_direction("SPY", on_date, ts,
                                             day_session=day_session, get_daily=data.daily)
        proxy = (cfg.get("sector_map") or {}).get(ticker.upper())
        sector_dir = engine._context_direction(proxy, on_date, ts,
                                                day_session=day_session, get_daily=data.daily)

        s = Setup(
            ticker=ticker, date=on_date, ts=ts, entry_time=engine._central_time_label(ts),
            direction=direction, level=level, level_type=setup["level_type"],
            entry=entry, stop=stop, target=target, risk=risk, reward=reward,
            position_size=position_size, entry_volume=entry_volume, avg_volume=avg_volume,
            volume_ratio=volume_ratio, volume_spike=bool(setup.get("volume_spike")),
            atr=atr, spy_direction=spy_dir, sector_direction=sector_dir,
            interval_min=self.interval, session=session,
        )

        # Skip conditions: a real setup blocked by market context is a logged skip
        # (same semantics as the backtester).
        if self.skip.get("skip_if_spy_down") and spy_dir == "Down":
            s.kind, s.skip_note = "skip", "skipped: SPY direction down"
        elif self.skip.get("skip_if_sector_down") and sector_dir == "Down":
            s.kind, s.skip_note = "skip", "skipped: sector direction down"
        return s


# ---------------------------------------------------------------------------
# Execution adapters
# ---------------------------------------------------------------------------
class ExecutionAdapter(abc.ABC):
    """Turns a detected Setup into a completed trade record.

    Every mode shares the same Setup; only *how the exit is observed* (and whether
    a real order is sent) differs. The returned dict mirrors the backtester's
    trade schema, with executor extras (``position_size``, ``mode``,
    ``account_type``).
    """

    mode: str = ""

    @abc.abstractmethod
    def execute(self, setup: Setup, *, core: StrategyCore, data: DataSource) -> dict:
        ...

    @staticmethod
    def _skip_trade(setup: Setup, *, mode: str, account_type: str) -> dict:
        return {
            **setup.base_trade(),
            "entry_price": round(setup.entry, 2), "stop_price": round(setup.stop, 2),
            "target_price": round(setup.target, 2), "risk_amount": round(setup.risk, 2),
            "reward_amount": round(setup.reward, 2), "exit_price": None,
            "outcome": "Skip", "r_result": 0.0, "exit_time": None,
            "notes": setup.skip_note, "mode": mode, "account_type": account_type,
        }


class ReplayExecutionAdapter(ExecutionAdapter):
    """Offline playback: resolve the exit over historical candles.

    Walks the day's candles forward from the entry with the backtester's own
    ``_simulate`` (including 1-minute refinement of ambiguous bars), so the
    outcome/exit/R reproduce the backtester exactly. No slippage is modeled —
    replay's job is to validate detection + sizing against known backtest results.
    """

    mode = REPLAY

    def execute(self, setup: Setup, *, core: StrategyCore, data: DataSource) -> dict:
        if setup.kind == "skip":
            return self._skip_trade(setup, mode=self.mode, account_type="REPLAY")

        cfg = core.config
        interval = setup.interval_min
        rr = core.rr
        session = setup.session
        forward = session[session.index > setup.ts] if session is not None else session

        refine = self._refiner(setup.ticker, setup.date, cfg, data, interval)
        outcome, exit_price, exit_ts, note = engine._simulate(
            setup.direction, setup.entry, setup.stop, setup.target, forward,
            refine=refine, interval_min=interval, diag={},
        )

        if outcome == "Unresolved" or exit_price is None:
            return {
                **setup.base_trade(),
                "entry_price": round(setup.entry, 2), "stop_price": round(setup.stop, 2),
                "target_price": round(setup.target, 2), "risk_amount": round(setup.risk, 2),
                "reward_amount": round(setup.reward, 2), "exit_price": None,
                "outcome": "Unresolved", "r_result": None, "exit_time": None,
                "notes": note, "mode": self.mode, "account_type": "REPLAY",
            }

        return {
            **setup.base_trade(),
            "entry_price": round(setup.entry, 2), "stop_price": round(setup.stop, 2),
            "target_price": round(setup.target, 2), "risk_amount": round(setup.risk, 2),
            "reward_amount": round(setup.reward, 2), "exit_price": round(exit_price, 2),
            "outcome": outcome, "r_result": engine._binary_r_result(outcome, rr),
            "exit_time": engine._central_time_label(exit_ts), "notes": note,
            "mode": self.mode, "account_type": "REPLAY",
        }

    @staticmethod
    def _refiner(ticker, day, cfg, data: DataSource, interval):
        """A finer-interval (1-minute) refiner to settle ambiguous bars, mirroring
        ``backtest.make_refiner``. Absent fine data just disables refinement."""
        refine_interval = int(cfg.get("refine_interval_min", 1) or 0)
        if not refine_interval or refine_interval >= interval:
            return None
        fine = data.intraday(ticker, day, day, refine_interval)
        if fine is None or fine.empty:
            return None
        fine = fine.sort_index()
        return lambda t0, t1: fine[(fine.index >= t0) & (fine.index < t1)]


class SimulatedExecutionAdapter(ExecutionAdapter):
    """PAPER: open a virtual position at the live price and resolve it honestly.

    This holds the *honest* fill math the spec demands; the live-feed driver that
    pumps real-time quotes through ``resolve_exit`` is build step 2. The pure
    methods below are deterministic and unit-tested offline:

      * ``open_position`` — fill at the live price ± entry slippage (adverse),
        capturing the bid/ask spread for later calibration.
      * ``resolve_exit``  — replay the *real* sequence of price events (ticks or
        1-minute bars) and close at whichever of stop/target is touched FIRST.
        Never assumes "target first" on an ambiguous event. Stop fills are modeled
        at the stop or slightly worse (configurable, pessimistic).

    ``execute`` is intentionally not wired to a live loop yet; calling it without a
    pre-collected event stream raises so PAPER can't silently no-op.
    """

    mode = PAPER

    def open_position(self, setup: Setup, *, live_price: float, bid=None, ask=None,
                      config: dict) -> dict:
        """Virtual entry fill at ``live_price`` plus an adverse slippage haircut."""
        slip = self._slippage(config.get("entry_slippage"), bid, ask)
        # Adverse: a long pays up, a short sells down.
        fill = live_price + slip if setup.direction == "Long" else live_price - slip
        spread = round(float(ask) - float(bid), 4) if bid is not None and ask is not None else None
        return {
            "entry_fill": round(fill, 4),
            "live_price": round(float(live_price), 4),
            "entry_spread": spread,
            "entry_slippage": round(slip, 4),
        }

    def resolve_exit(self, setup: Setup, fill: dict, events, *, config: dict,
                     window_end_price=None) -> dict:
        """Walk ``events`` (ticks or 1-min bars) in real order; close on first hit.

        ``events`` is an iterable of dicts with ``high``/``low`` (1-min bars) or a
        ``price`` (ticks). Returns the completed-trade fields. If neither level is
        hit, closes at ``window_end_price`` (the 10:00 CT market price) — labeled.
        """
        direction = setup.direction
        stop, target = setup.stop, setup.target
        stop_slip = self._slippage(config.get("stop_slippage"),
                                   fill.get("entry_spread"), None, spread_value=True)
        for ev in events or []:
            hi = float(ev.get("high", ev.get("price")))
            lo = float(ev.get("low", ev.get("price")))
            if direction == "Long":
                hit_stop, hit_target = lo <= stop, hi >= target
            else:
                hit_stop, hit_target = hi >= stop, lo <= target
            if hit_stop:
                # Pessimistic: stop fills at the level or slightly worse.
                px = stop - stop_slip if direction == "Long" else stop + stop_slip
                return self._exit(setup, fill, "Loss", px, ev.get("time"),
                                  "stop hit (sim)", config)
            if hit_target:
                return self._exit(setup, fill, "Win", target, ev.get("time"),
                                  "target hit (sim)", config)
        if window_end_price is not None:
            return self._exit(setup, fill, None, float(window_end_price), None,
                              "closed at window end", config)
        return self._exit(setup, fill, "Unresolved", None, None,
                          "window end reached, no closing price", config)

    def _exit(self, setup, fill, outcome, exit_price, exit_time, note, config) -> dict:
        entry = float(fill["entry_fill"])
        per_share_risk = abs(entry - setup.stop)
        if outcome is None and exit_price is not None:  # window-end close: R from price
            r = ((exit_price - entry) if setup.direction == "Long" else (entry - exit_price))
            r = round(r / per_share_risk, 3) if per_share_risk else 0.0
            outcome = "Win" if r > 0 else "Loss"
            r_result = r
        elif outcome in ("Win", "Loss"):
            r_result = engine._binary_r_result(outcome, setup.reward / setup.risk if setup.risk else 0)
        else:
            r_result = None
        return {
            **setup.base_trade(),
            "entry_price": round(entry, 4), "stop_price": round(setup.stop, 2),
            "target_price": round(setup.target, 2), "risk_amount": round(setup.risk, 2),
            "reward_amount": round(setup.reward, 2),
            "exit_price": round(exit_price, 4) if exit_price is not None else None,
            "outcome": outcome, "r_result": r_result, "exit_time": exit_time,
            "entry_spread": fill.get("entry_spread"), "entry_slippage": fill.get("entry_slippage"),
            "exit_resolution_granularity": config.get("exit_resolution_granularity"),
            "notes": note, "mode": self.mode, "account_type": "PAPER",
        }

    @staticmethod
    def _slippage(model, bid, ask, *, spread_value=False) -> float:
        """Resolve a slippage model to a per-share dollar haircut."""
        model = model or {}
        value = float(model.get("value", 0) or 0)
        if str(model.get("type", "cents")).lower() == "spread":
            if spread_value and bid is not None:
                return value * float(bid)        # bid here carries the spread width
            if bid is not None and ask is not None:
                return value * (float(ask) - float(bid))
            return 0.0
        return value

    def execute(self, setup: Setup, *, core: StrategyCore, data: DataSource) -> dict:
        # The real-time driver (collect a live event stream, then resolve) is
        # build step 2. Fail loudly rather than fabricate a fill.
        raise NotImplementedError(
            "SimulatedExecutionAdapter.execute needs a live event stream (build "
            "step 2). Use open_position()/resolve_exit() directly for offline tests."
        )


class LiveExecutionAdapter(ExecutionAdapter):
    """LIVE: transmit a real Schwab bracket order. GUARDED SCAFFOLD — not enabled.

    Builds the exact bracket (entry + attached stop/target) the live account would
    receive via the existing ``schwab_orders`` plumbing, but never POSTs it. The
    ``transmit`` method is a clearly-marked stub for a later phase; ``execute``
    refuses unless explicitly armed, so MODE=LIVE cannot fire a real order today.
    """

    mode = LIVE

    def __init__(self, *, armed: bool = False):
        self.armed = armed

    def build_order(self, setup: Setup) -> dict:
        import schwab_orders
        return schwab_orders.build_bracket_order(setup.signal())

    def transmit(self, order: dict) -> dict:
        # TODO (later phase): POST to the existing Schwab orders endpoint to place
        # the bracket. Deliberately unimplemented — live trading is not enabled.
        raise NotImplementedError(
            "LiveExecutionAdapter.transmit is a guarded stub — real order "
            "placement is intentionally not implemented yet."
        )

    def execute(self, setup: Setup, *, core: StrategyCore, data: DataSource) -> dict:
        if not self.armed:
            raise RuntimeError(
                "LIVE mode is guarded: refusing to transmit a real order. "
                "LiveExecutionAdapter must be explicitly armed (future phase)."
            )
        order = self.build_order(setup)
        self.transmit(order)  # raises — placement not implemented
        return {**setup.base_trade(), "mode": self.mode, "account_type": "LIVE"}


# ---------------------------------------------------------------------------
# MODE binding — the one place data source + adapter are chosen
# ---------------------------------------------------------------------------
DATA_SOURCE_FOR = {REPLAY: ReplayDataSource, PAPER: LiveDataSource, LIVE: LiveDataSource}
ADAPTER_FOR = {REPLAY: ReplayExecutionAdapter, PAPER: SimulatedExecutionAdapter,
               LIVE: LiveExecutionAdapter}


@dataclass
class ExecutorEngine:
    """Binds a StrategyCore to a DataSource + ExecutionAdapter for one MODE."""
    config: dict
    mode: str
    core: StrategyCore
    data: DataSource
    adapter: ExecutionAdapter


def build_engine(config: dict, *, data_source: "DataSource | None" = None,
                 adapter: "ExecutionAdapter | None" = None) -> ExecutorEngine:
    """Construct the engine for ``config['mode']``.

    Going live is exactly this one binding: swap the MODE and the adapter/data
    source are selected from the tables above — StrategyCore is untouched.
    Overrides (``data_source``/``adapter``) exist for tests and dependency
    injection.
    """
    mode = config.get("mode", REPLAY)
    data = data_source or DATA_SOURCE_FOR[mode]()
    adapter = adapter or ADAPTER_FOR[mode]()
    return ExecutorEngine(config=config, mode=mode, core=StrategyCore(config),
                          data=data, adapter=adapter)


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------
def run_replay(raw_config: dict, *, date=None, date_range=None,
               data_source: "DataSource | None" = None) -> dict:
    """Replay one date (or range) through the full engine and return completed
    trades with outcomes — the offline validation path for build step 1.

    Forces REPLAY mode regardless of the config's ``mode`` so it is always safe to
    call offline.
    """
    config, errors = validate_engine_config({**(raw_config or {}), "mode": REPLAY})
    if errors:
        return {"ok": False, "errors": errors}

    if date_range:
        start, end = date_range.get("start"), date_range.get("end")
    else:
        start = end = date
    if not start or not end:
        return {"ok": False, "errors": ["Provide date or date_range {start, end}."]}

    eng = build_engine(config, data_source=data_source)
    trades: list[dict] = []
    for day in engine._session_dates(start, end):
        for ticker in config["tickers"]:
            ctx = eng.core.context(ticker, day, eng.data)
            if not ctx or "window" not in ctx:
                continue
            setup = eng.core.detect(ticker, day, ctx, eng.data)
            if setup is None:
                continue
            trades.append(eng.adapter.execute(setup, core=eng.core, data=eng.data))

    trades.sort(key=lambda t: (t["date"], t.get("entry_time") or "", t["ticker"]))
    return {
        "ok": True, "mode": REPLAY, "date_range": {"start": start, "end": end},
        "trades": trades, "count": len(trades), "summary": engine.summarize(trades),
    }
