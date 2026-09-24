"""Day-trade sleeve (17 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from api_common import _err

import accounts
import config
from daytrade import scheduler as daytrade_scheduler
from daytrade import store as daytrade_store

daytrade_bp = Blueprint("daytrade", __name__)


@daytrade_bp.route("/api/daytrade/universe")
def api_daytrade_universe():
    """Phase 1 read-only view of the day-trade sleeve's nightly screener
    output (defaults to today, ET). Read-only: the sleeve's scheduler is what
    populates this file — see daytrade/scheduler.py."""
    try:
        from datetime import datetime
        day = request.args.get("date") or datetime.now(daytrade_scheduler.ET).strftime("%Y-%m-%d")
        result = daytrade_store.load_screen(day)
        if result is None:
            return jsonify({"date": day, "picks": [], "screened": [], "ran": False})
        return jsonify({**result, "ran": True})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@daytrade_bp.route("/api/daytrade/universe/rescan", methods=["POST"])
def api_daytrade_universe_rescan():
    """Force the day-trade screener to run again right now, against the
    current day-trade universe (daytrade/tickers.py) — bypasses the nightly
    scheduler's own "already ran today" / "no account enabled" / "every
    trial complete" guards, which gate the SCHEDULED run, not an explicit
    manual one. Runs in a detached thread (daytrade/universe.py's
    start_background_screen) so this returns immediately; poll
    /api/daytrade/universe/rescan/status."""
    try:
        from daytrade import universe as daytrade_universe
        return jsonify(daytrade_universe.start_background_screen())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@daytrade_bp.route("/api/daytrade/universe/rescan/status")
def api_daytrade_universe_rescan_status():
    """Poll the on-demand rescan kicked off above: running / done / error."""
    try:
        from daytrade import universe as daytrade_universe
        return jsonify(daytrade_universe.screen_status())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@daytrade_bp.route("/api/daytrade/screen-health")
def api_daytrade_screen_health():
    """Last successful screener run, by trigger ("scheduled" — the once-per-
    trading-day pre-market job, i.e. this app's answer to "did this
    morning's job actually run" — and "manual" — the last "Rescan now"
    click). Empty for a trigger that has never succeeded. SHARED, like
    /universe and /prices."""
    try:
        return jsonify(daytrade_store.load_screen_health())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@daytrade_bp.route("/api/daytrade/config")
def api_daytrade_config():
    """The handful of DAYTRADE_* tunables the UI displays as text (rather
    than reading via a dedicated field) — a single source so a config
    change (e.g. raising the trades/day cap) can't leave a hardcoded
    display string quietly lying about the actual rule. SHARED (not
    account-scoped): these are process-wide constants, not per-account
    state."""
    return jsonify({
        "risk_pct": config.DAYTRADE_RISK_PCT,
        "max_trades_per_day": config.DAYTRADE_MAX_TRADES_PER_DAY,
        "max_losses_per_day": config.DAYTRADE_MAX_LOSSES_PER_DAY,
        "daily_stop_r": config.DAYTRADE_DAILY_STOP_R,
        "max_position_pct": config.DAYTRADE_MAX_POSITION_PCT,
    })


@daytrade_bp.route("/api/daytrade/bars/<symbol>")
def api_daytrade_bars(symbol: str):
    """Phase 1 read-only view of the day-trade sleeve's ingested 5-min bars
    for one symbol (defaults to today, ET)."""
    try:
        from datetime import datetime
        day = request.args.get("date") or datetime.now(daytrade_scheduler.ET).strftime("%Y-%m-%d")
        return jsonify({"date": day, "symbol": symbol.upper(),
                         "bars": daytrade_store.load_bars(day, symbol.upper())})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@daytrade_bp.route("/api/daytrade/prices")
def api_daytrade_prices():
    """The most recently ingested 5-min bar per symbol for the day (defaults
    to today, ET) — the day-trade sleeve's closest thing to a live price,
    since the screener's own `price` field is a static prior-day close
    stamped once at screen time. SHARED (bars aren't account-scoped), same
    as /api/daytrade/universe and /bars."""
    try:
        from datetime import datetime
        day = request.args.get("date") or datetime.now(daytrade_scheduler.ET).strftime("%Y-%m-%d")
        return jsonify({"date": day, "prices": daytrade_store.latest_bars(day)})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@daytrade_bp.route("/api/daytrade/quotes")
def api_daytrade_quotes():
    """TRUE live quotes for the day's screener picks, via
    data_handler.latest_quotes — the SAME centralized quote path
    /api/ticker-strip and every other live-price display in the app reads
    from. Deliberately separate from /prices (the last ingested 5-min bar):
    the signal engine's own breakout/stop rules need discrete OHLC candles,
    not a scalar quote, and that log freezes outside the 9:30-11:00 ET
    ingestion window — fine for the strategy, misleading as "the current
    price" to an operator watching the screen. This is what should be shown
    as that. SHARED (not account-scoped), same as /universe and /prices."""
    try:
        from datetime import datetime
        import data_handler
        import logging_handler as log
        day = request.args.get("date") or datetime.now(daytrade_scheduler.ET).strftime("%Y-%m-%d")
        screened = daytrade_store.load_screen(day)
        symbols = [p["symbol"] for p in (screened or {}).get("picks", [])]
        quotes = data_handler.latest_quotes(symbols) if symbols else {}
        return jsonify({"date": day, "as_of": log.utcnow(), "quotes": quotes})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@daytrade_bp.route("/api/daytrade/signals")
def api_daytrade_signals():
    """Read-only view of the ACTIVE ACCOUNT's signal-engine journal — every
    setup, entry, and exit for the day, taken or not (rule 8). Defaults to
    today, ET. Per account (daytrade/settings.py): the account is whichever
    book this request is bound to (_bind_account, X-CFM-Account header),
    same as every other account-scoped route. Read-only: the scheduler runs
    the engine — see daytrade/scheduler.py's _run_signals."""
    try:
        from datetime import datetime
        day = request.args.get("date") or datetime.now(daytrade_scheduler.ET).strftime("%Y-%m-%d")
        symbol = request.args.get("symbol")
        events = daytrade_store.load_signals(day, accounts.active_id(),
                                             symbol.upper() if symbol else None)
        return jsonify({"date": day, "account_id": accounts.active_id(), "events": events})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@daytrade_bp.route("/api/daytrade/live-status")
def api_daytrade_live_status():
    """The active account's symbols currently ``armed`` or ``in_trade`` right
    now (daytrade/signals.py's current_status — re-derived from the
    persisted signals log, display-only, never authoritative), each with the
    fields a fill/close-proximity meter needs plus the latest ingested price.
    Defaults to today, ET; per account like /signals."""
    try:
        from datetime import datetime
        from daytrade import signals as daytrade_signals
        day = request.args.get("date") or datetime.now(daytrade_scheduler.ET).strftime("%Y-%m-%d")
        return jsonify({"date": day, "account_id": accounts.active_id(),
                         "rows": daytrade_signals.current_status(day, accounts.active_id())})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@daytrade_bp.route("/api/daytrade/trades")
def api_daytrade_trades():
    """Read-only view of the active account's trade log for the day — one
    row per trade_id, with fills and $ P&L (daytrade/adapters.py's
    PaperAdapter). Defaults to today, ET."""
    try:
        from datetime import datetime
        day = request.args.get("date") or datetime.now(daytrade_scheduler.ET).strftime("%Y-%m-%d")
        return jsonify({"date": day, "account_id": accounts.active_id(),
                         "trades": daytrade_store.load_trades(day, accounts.active_id())})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@daytrade_bp.route("/api/daytrade/budget")
def api_daytrade_budget():
    """Live read of the active account's current sizing budget — its own
    dry powder, or the static fallback if that read fails right now (see
    daytrade/budget.py). Not cached: reflects CFM's capital as of THIS
    request, which is also what the next scheduler tick will size off."""
    try:
        from daytrade import budget
        return jsonify(budget.daytrade_budget(accounts.active_id()))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@daytrade_bp.route("/api/daytrade/trial")
def api_daytrade_trial():
    """Live read of the active account's paper-trading trial progress
    across every day (daytrade/trial.py) — completed/target trades, net R,
    net P&L, and the WIN/LOSS/FLAT verdict once complete. Not date-scoped,
    unlike the other daytrade routes: the trial spans days by design."""
    try:
        from daytrade import trial
        return jsonify(trial.trial_status(accounts.active_id()))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@daytrade_bp.route("/api/daytrade/enabled", methods=["GET", "POST"])
def api_daytrade_enabled():
    """Get or set whether the day-trade sleeve is turned on for the active
    account (daytrade/settings.py). Per-account so one book can run the
    trial while another sits out entirely — see settings.py's docstring for
    the default (on for the primary book, off for every other)."""
    try:
        from daytrade import settings
        account_id = accounts.active_id()
        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
            settings.set_enabled(account_id, bool(payload.get("enabled")))
        return jsonify({"account_id": account_id, "enabled": settings.is_enabled(account_id)})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@daytrade_bp.route("/api/daytrade/tickers", methods=["GET"])
def api_daytrade_tickers():
    """The day-trade sleeve's OWN ticker roster (daytrade/tickers.py) — a
    separate, editable list from CFM's universe (/api/universe), seeded once
    from it but independent from then on. Managed via /add and /remove
    below; the nightly screener (daytrade/universe.py) reads this list."""
    try:
        from daytrade import tickers as daytrade_tickers
        names = daytrade_tickers.all_tickers()
        return jsonify({"tickers": names, "total": len(names)})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@daytrade_bp.route("/api/daytrade/tickers/add", methods=["POST"])
def api_daytrade_tickers_add():
    """Add to the day-trade sleeve's roster. {ticker} for one (raises on a
    duplicate/blank), or {tickers:[...]} to bulk-import (e.g. a pasted CSV
    watchlist) — duplicates/blanks are silently skipped rather than raising,
    since a large import shouldn't abort on the first repeat."""
    payload = request.get_json(silent=True) or {}
    try:
        from daytrade import tickers as daytrade_tickers
        if isinstance(payload.get("tickers"), list):
            return jsonify(daytrade_tickers.add_tickers(payload["tickers"]))
        return jsonify(daytrade_tickers.add_ticker(payload.get("ticker", "")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@daytrade_bp.route("/api/daytrade/tickers/remove", methods=["POST"])
def api_daytrade_tickers_remove():
    """Remove from the day-trade sleeve's roster. {ticker} for one, or
    {tickers:[...]} to bulk remove."""
    payload = request.get_json(silent=True) or {}
    try:
        from daytrade import tickers as daytrade_tickers
        if isinstance(payload.get("tickers"), list):
            return jsonify(daytrade_tickers.remove_tickers(payload["tickers"]))
        return jsonify(daytrade_tickers.remove_ticker(payload.get("ticker", "")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)

