"""Data health / provider budget / diagnostics (5 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from api_common import _err

import config
import data_handler
import earnings
import logging_handler as log
import schwab_api

health_bp = Blueprint("health", __name__)


def _tier_poll_status():
    """Tier-poll status for the health panel; degrades to a disabled marker if the
    runtime isn't importable (e.g. scheduler off)."""
    try:
        import tier_poll
        return {**tier_poll.status(), "recent_alerts": tier_poll.recent_alerts()}
    except Exception:  # noqa: BLE001
        return {"available": False}


@health_bp.route("/api/data-health")
def api_data_health():
    """Last-successful-fetch per source + cache staleness, so silent data
    failures are visible instead of quietly serving stale frames."""
    try:
        import dividends
        import refresh_policy
        state = log.load_state()
        # Report cache age for the hot set (positions + live candidates) — those
        # are the names whose staleness actually matters intraday.
        hot = refresh_policy.hot_tickers(state)
        key_syms = [config.BENCHMARK, config.VIX_SYMBOL] + hot
        import data_budget
        import data_cache
        return jsonify({
            "providers": data_handler.health(),
            "ohlcv_cache_age_hours": {s: data_handler.cache_age_hours(s)
                                      for s in dict.fromkeys(s for s in key_syms if s)},
            "hot_refresh": refresh_policy.status(),
            "earnings_cache": earnings.cache_health(),
            "dividends_cache": dividends.cache_health(),
            "schwab_token": schwab_api.token_status(),
            "data_budget": data_budget.snapshot(),
            "staleness": data_cache.summary(),
            "tier_poll": _tier_poll_status(),
            "demo": config.demo_enabled(),
        })
    except Exception as e:  # noqa: BLE001
        return _err(e)


@health_bp.route("/api/data-budget")
def api_data_budget():
    """Today's provider-call budget per tier, per-provider usage vs configured
    daily limits, and the current shed level (Tier 3 → Tier 2 → Tier 1-cadence,
    never Tier 0). Telemetry only — persisted outside state.json."""
    try:
        import data_budget
        return jsonify(data_budget.snapshot())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@health_bp.route("/api/universe-health")
def api_universe_health():
    """Sweep the whole ticker universe and report dead names (no provider data —
    renamed/delisted/typo'd) and, with ?weeklies=1, names that lack weekly
    options (can't run CFM). On-demand only — fetches OHLCV for every ticker."""
    try:
        import universe_health
        weeklies = request.args.get("weeklies", "").strip() in ("1", "true", "yes")
        return jsonify(universe_health.check(check_weeklies=weeklies))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@health_bp.route("/api/diagnostics/vix")
def api_diag_vix():
    """Live, cache-bypassing probe of the VIX so a missing value can be
    diagnosed: token health, the raw Schwab quote, and the daily-bars result."""
    out = {"symbol": config.VIX_SYMBOL, "token": schwab_api.token_status(),
           "schwab_configured": schwab_api.configured()}
    try:
        out["quote"] = data_handler.client().get_quote(config.VIX_SYMBOL)
    except Exception as e:  # noqa: BLE001
        out["quote_error"] = str(e)
    try:
        df = data_handler.get_daily(config.VIX_SYMBOL, force=True)
        out["daily_rows"] = 0 if df is None else len(df)
    except Exception as e:  # noqa: BLE001
        out["daily_error"] = str(e)
    out["last_error"] = data_handler.last_error(config.VIX_SYMBOL)
    return jsonify(out)

