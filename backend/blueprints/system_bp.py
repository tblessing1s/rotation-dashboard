"""Dashboard/system: regime, overview, mode, config (14 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from api_common import _err

import config
import data_handler
import executor
import kill_switch
import logging_handler as log
import position_manager
import schwab_api
import screening
import sector_data
import strike_policy

system_bp = Blueprint("system", __name__)


@system_bp.route("/api/regime")
def api_regime():
    try:
        return jsonify(screening.regime())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@system_bp.route("/api/kill-switch")
def api_kill_switch():
    try:
        return jsonify({"positions": kill_switch.evaluate_all(log.load_state())})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@system_bp.route("/api/symbol-genius/flips")
def api_symbol_genius_flips():
    """Symbol Genius flip-frequency shadow-log — how often each tracked name's SYM
    color changed over the retained window. The measurement that must precede any
    decision to add a per-symbol yellow dwell (does SYM churn enough to warrant
    one?). Read-only telemetry; empty until the nightly sweep has logged a few days."""
    try:
        import symbol_genius_history
        return jsonify(symbol_genius_history.flip_stats())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@system_bp.route("/api/csp-dry-powder/summary")
def api_csp_dry_powder_summary():
    """Dry-powder cash-secured-put shadow sleeve (csp_dry_powder.py) — a second,
    distinct income sweep run nightly on idle cash, entirely separate from the
    CFM entry mechanism. SHADOW ONLY: it never places an order. Optional
    ?days=N bounds the rollup to the N most recent stored scan days (default:
    all retained, up to DRY_POWDER_LOG_RETENTION_DAYS). Read-only telemetry;
    empty until the nightly sweep has logged a few days."""
    try:
        import csp_dry_powder
        days = int(request.args.get("days") or 0) or None
        return jsonify(csp_dry_powder.summary(days=days))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@system_bp.route("/api/overview")
def api_overview():
    """One-call landing payload for the Overview tab: regime + positions/capital
    + theta totals/payback + kill-switch, pre-joined server-side so the landing
    screen renders from a single fetch instead of stitching four.

    Sections are best-effort independent — a data-provider hiccup in one (e.g.
    regime needs fresh SPY/VIX bars) must not blank the position-derived rest,
    so a failed section carries {"error": ...} instead of failing the request."""
    def section(fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    try:
        state = log.load_state()
    except Exception as e:  # noqa: BLE001
        return _err(e)
    ledger = state.get("theta_ledger", {})
    positions = section(lambda: position_manager.positions_view(state))
    return jsonify({
        "regime": section(screening.regime),
        "positions": positions,
        "capital": section(lambda: position_manager.capital_summary(state)),
        "theta": {
            "totals": ledger.get("totals", {}),
            "extrinsic_payback": state.get("extrinsic_payback", {}),
            # Forward NET juice/week rollup (juice - LEAP burn), the headline
            # income figure; extrinsic_payback stays as the capital-recovery view.
            "net_juice_rollup": (position_manager.net_juice_rollup(positions)
                                 if isinstance(positions, list) else {}),
            # The 1-2%/week-of-deployed target band (HARD_CFM_RULE), so the
            # Overview can show this week's juice against pace without a second
            # call — same formula the History weekly chart uses.
            "weekly_target": section(lambda: {
                "target_low": round(position_manager.deployed_capital(state)
                                    * config.WEEKLY_JUICE_TARGET_PCT_MIN / 100, 2),
                "target_high": round(position_manager.deployed_capital(state)
                                     * config.WEEKLY_JUICE_TARGET_PCT_MAX / 100, 2),
            }),
        },
        # Live BS-engine verification harness: realized-vs-projected burn drift.
        "burn_divergence": section(lambda: __import__("burn_marks").aggregate_divergence()),
        "kill_switch": section(lambda: kill_switch.evaluate_all(state)),
        # Monthly payout glance: this month's estimated payout + last month's, so
        # the landing shows "what the payout is going to be" without a second call.
        "payouts": section(lambda: _payouts_glance(state)),
    })


def _payouts_glance(state: dict) -> dict:
    """The compact current+previous payout figures for the Overview landing,
    pulled from the payouts view (full detail lives on the Payouts tab)."""
    import payouts
    v = payouts.view(state)
    keep = ("month", "label", "net_juice", "leap_burn", "burn_tracked",
            "intrinsic_lost", "intrinsic_repaid", "intrinsic_debt",
            "intrinsic_repayment_on", "net_payout", "payout_amount", "status",
            "finalizable", "finalized", "paid", "estimated")
    return {
        "current": {k: v["current"].get(k) for k in keep},
        "previous": {k: v["previous"].get(k) for k in keep},
    }


@system_bp.route("/api/state", methods=["GET", "POST"])
def api_state():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        try:
            state = log.load_state()
            # Only metadata + thesis-style fields are user-editable here.
            if "metadata" in payload:
                state.setdefault("metadata", {}).update(payload["metadata"])
            log.recompute_derived(state)
            return jsonify(log.save_state(state))
        except Exception as e:  # noqa: BLE001
            return _err(e)
    return jsonify(log.load_state())


@system_bp.route("/api/mode", methods=["GET", "POST"])
def api_mode():
    """Read or set the demo/live data switch. Setting it points the app at the
    separate demo store (seeding it on first use) or back at the live store, and
    clears the in-memory scan/data caches so the next reads reflect the switch."""
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        demo = bool(payload.get("demo"))
        seeded = False
        try:
            config.set_demo_enabled(demo)
            screening.clear_cache()
            data_handler.reset_caches()
            if demo:
                import seed_demo_data
                seeded = seed_demo_data.ensure_seeded()
            return jsonify({"demo": config.demo_enabled(), "seeded": seeded})
        except Exception as e:  # noqa: BLE001
            return _err(e)
    return jsonify({"demo": config.demo_enabled()})


def _live_trading_status() -> dict:
    """Current live-trading state for the UI switch. `enabled` is the toggle
    (env or persisted); `transmit` is the EFFECTIVE gate — orders only reach the
    broker when live is on AND not in demo. Preconditions are surfaced so the UI
    can explain why a switched-on session might still be paper."""
    return {
        "enabled": config.live_trading_enabled(),
        "env_locked": config.live_trading_env(),
        "transmit": executor.live_transmit(),
        "demo": config.demo_enabled(),
        "schwab_configured": schwab_api.configured(),
        "schwab": schwab_api.token_status(),
        # Actions that never reach the broker whatever the switches say, so the
        # confirmation dialog cannot promise a transmit the executor will not
        # perform. Served rather than duplicated in the frontend: one source of
        # truth, and it can never drift from the dispatch that enforces it.
        "non_transmitting_actions": sorted(executor.non_transmitting_actions()),
        # How long the UI should let a working order try to fill before cancelling
        # it. Served so the window is ONE number the operator can tune, not a
        # constant compiled into the bundle.
        "order_fill_wait_seconds": config.ORDER_FILL_WAIT_SECONDS,
        "equity_placement": config.EQUITY_ORDER_PLACEMENT_ENABLED,
    }


@system_bp.route("/api/live-trading", methods=["GET", "POST"])
def api_live_trading():
    """Read or set the live-trading toggle. Enabling it means executed orders are
    transmitted to the real Schwab account (unless in demo mode). Locked when
    CFM_LIVE_TRADING is set in the environment."""
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        try:
            config.set_live_trading_enabled(bool(payload.get("enabled")))
        except RuntimeError as e:
            return _err(e, 400)  # env-locked
        except Exception as e:  # noqa: BLE001
            return _err(e)
    return jsonify(_live_trading_status())


@system_bp.route("/api/config")
def api_config():
    return jsonify({
        "demo": config.demo_enabled(),
        "benchmark": config.BENCHMARK,
        "sectors": {etf: s.as_dict() for etf, s in sector_data.sectors().items()},
        "thresholds": {
            "regime_breadth_green": config.REGIME_BREADTH_GREEN,
            "vix_calm": config.VIX_CALM,
            "sector_rs3m_min": config.SECTOR_RS3M_MIN,
            "stock_rs_vs_spy_min": config.STOCK_RS_VS_SPY_MIN,
        },
        "cfm": {
            "leap_contracts": config.LEAP_CONTRACTS,
            "leap_target_delta": config.LEAP_TARGET_DELTA,
            "leap_target_dte": config.LEAP_TARGET_DTE,
            "short_atr_mult": config.SHORT_ATR_MULT,
            "share_cap": config.SHARE_CAP,
            "strike_table": config.STRIKE_TABLE,
            "strike_posture": strike_policy.get_posture(),
        },
        # Effective transmit capability, NOT the raw flag: in demo mode a trade
        # never reaches the broker (see executor.live_transmit), so the Paper/Live
        # badge must read paper even when CFM_LIVE_TRADING is on. live_trading_flag
        # exposes the raw env flag for diagnostics.
        "live_trading": executor.live_transmit(),
        "live_trading_flag": executor.live_enabled(),
        "demo": config.demo_enabled(),
        "schwab": schwab_api.token_status(),
        "alpha_vantage_configured": __import__("alpha_vantage").configured(),
    })


@system_bp.route("/api/version")
def api_version():
    """Build identity: {version, commit, built_at}. Open (no auth) so the login
    screen and external health checks can read it without a session."""
    import version
    return jsonify(version.info())


@system_bp.route("/api/portfolio-risk")
def api_portfolio_risk():
    """Aggregate book exposure: delta (raw + SPY-beta-adjusted), theta/day,
    vega, capital vs cap, reserve status, sector exposure breakdown."""
    try:
        import portfolio_risk
        return jsonify(portfolio_risk.portfolio_view(log.load_state()))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@system_bp.route("/api/account/status")
def api_account_status():
    return jsonify(schwab_api.token_status())

