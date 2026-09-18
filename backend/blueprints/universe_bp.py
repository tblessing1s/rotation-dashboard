"""Universe (tradable-symbol list) maintenance (10 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from api_common import _err

import config
import logging_handler as log
import screening
import sector_data

universe_bp = Blueprint("universe", __name__)


@universe_bp.route("/api/universe", methods=["GET"])
def api_universe():
    """The ticker universe (editable JSON store on the volume): sectors with
    their constituents. Managed via /api/universe/add and /remove."""
    try:
        secs = sector_data.sectors()
        return jsonify({
            "sectors": [{"etf": s.etf, "name": s.name, "group": s.group,
                         "tickers": list(s.tickers), "count": len(s.tickers)}
                        for s in secs.values()],
            "total": sum(len(s.tickers) for s in secs.values()),
        })
    except Exception as e:  # noqa: BLE001
        return _err(e)


@universe_bp.route("/api/universe/add", methods=["POST"])
def api_universe_add():
    """Add a constituent to a sector: {ticker, sector}.

    The new name's daily bars and weeklies status are warmed in a detached thread
    so the next scan doesn't pay for them on the request path — a brand-new
    ticker is cold in both caches, and the weeklies probe is a live option-chain
    call. The response doesn't wait for it."""
    payload = request.get_json(silent=True) or {}
    try:
        out = sector_data.add_ticker(payload.get("ticker", ""), payload.get("sector", ""))
        out.update(screening.start_background_warm([out["added"]]))
        return jsonify(out)
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@universe_bp.route("/api/universe/remove", methods=["POST"])
def api_universe_remove():
    """Remove from the universe. {ticker} for one, or {tickers:[...]} to bulk
    remove (e.g. 'remove all dead' after a universe health check)."""
    payload = request.get_json(silent=True) or {}
    try:
        if isinstance(payload.get("tickers"), list):
            return jsonify(sector_data.remove_tickers(payload["tickers"]))
        return jsonify(sector_data.remove_ticker(payload.get("ticker", "")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@universe_bp.route("/api/universe/sync", methods=["POST"])
def api_universe_sync():
    """Additively pull any new names from the baked-in seed file into the store
    (e.g. after ETFs / S&P additions were added to the seed). Respects the
    operator's removals (tombstoned); never removes or moves anything."""
    try:
        out = sector_data.sync_from_seed()
        # Same reasoning as /add: names pulled in from the seed are cold in both
        # caches, so warm them off-request rather than on the next scan.
        out.update(screening.start_background_warm(out.get("added") or []))
        return jsonify(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@universe_bp.route("/api/universe/vet", methods=["POST"])
def api_universe_vet():
    """Vet candidate symbols against the CFM criteria (data + weeklies + Scorecard
    verdict): {symbols: [...] or "AAPL, MSFT"}. Returns which are add-ready."""
    payload = request.get_json(silent=True) or {}
    syms = payload.get("symbols")
    if isinstance(syms, str):
        import re
        syms = [s for s in re.split(r"[,\s]+", syms) if s]
    if not isinstance(syms, list):
        return jsonify({"error": "symbols must be a list or a comma/space-separated string"}), 400
    try:
        import universe_health
        return jsonify(universe_health.vet_candidates(syms))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@universe_bp.route("/api/maintenance/refresh", methods=["POST"])
def api_maintenance_refresh():
    """Force the nightly earnings/dividends refresh now (also runs on the
    scheduler's MAINTENANCE_ET slot)."""
    try:
        import maintenance
        return jsonify(maintenance.nightly_refresh())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@universe_bp.route("/api/admin/reset-book", methods=["POST"])
def api_admin_reset_book():
    """One-time hard reset of the book — start over with an empty state.

    Clears positions + the append-only execution log + every derived ledger,
    returning a fresh book at the current schema version. Deliberately hard to
    fire by accident: it needs (1) a valid login (the before_request auth gate),
    (2) config.RESET_BOOK_ENABLED — a server env flag OFF by default, and (3) a
    typed ``confirm: "RESET"`` in the body. Optional ``wipe_all`` also clears push
    subscriptions + account cash (kept by default). SAFE for the single-writer
    store: reset_book acquires the same in-process lock every save uses, so no
    scheduler tick can race it — no need to stop the app. Recoverable: a rotating
    backup is taken first (shipped off-machine when configured) and the prior file
    is written aside as state.json.pre-reset.<ts>."""
    if not config.RESET_BOOK_ENABLED:
        return jsonify({"error": "book reset is disabled; set RESET_BOOK_ENABLED=1 "
                        "to enable it (then unset it again afterwards)",
                        "reset_disabled": True}), 403
    body = request.get_json(silent=True) or {}
    if str(body.get("confirm") or "") != "RESET":
        return jsonify({"error": 'confirmation required: POST {"confirm": "RESET"}',
                        "confirm_required": True}), 400
    wipe_all = bool(body.get("wipe_all"))
    try:
        import backups
        target = config.active_state_path()
        # Recoverable backup FIRST — rotating copy in the backups dir, then a copy
        # shipped off-machine if configured. A failed off-machine copy is reported,
        # not fatal (the local backup + the pre-reset aside copy still recover it).
        backup = backups.make_nightly_backup(target)
        off = backups.send_offmachine_copy(backup)
        report = log.reset_book(build_fresh=lambda prior: log.book_fresh_state(prior, wipe_all))
        return jsonify({"ok": True, "cleared": report["cleared"],
                        "schema_version": report["schema_version"],
                        "wipe_all": wipe_all, "backup": backup,
                        "off_machine": off, "pre_reset": report["pre_reset"]})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@universe_bp.route("/api/refresh/hot", methods=["POST"])
def api_refresh_hot():
    """Force-refresh the hot set (open positions + live entry/earnings candidates)
    daily bars now, bypassing the freshness window. The scheduler does this
    automatically on the HOT_REFRESH_MINUTES cadence during market hours; this is
    the on-demand path for 'refresh these stocks now'."""
    try:
        import refresh_policy
        return jsonify(refresh_policy.maybe_refresh_hot(force=True))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@universe_bp.route("/api/refresh/ticker", methods=["POST"])
def api_refresh_ticker():
    """Force-refresh ONE ticker's daily bars now and return its fresh scorecard
    row — the on-demand 'this quote is stale, pull it live' path for a single
    name in the Scan. Names outside the hot set otherwise ride the daily cadence
    and read stale intraday; this pulls the current session's price on demand."""
    payload = request.get_json(silent=True) or {}
    ticker = (payload.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        import refresh_policy
        return jsonify(refresh_policy.refresh_tickers([ticker]))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@universe_bp.route("/api/refresh/sector", methods=["POST"])
def api_refresh_sector():
    """Force-refresh a whole sector — the ETF plus its constituents — now and
    return their fresh scorecard rows. 'Refresh this sector' from the Scan, for
    when you want the whole group live at once rather than name by name."""
    payload = request.get_json(silent=True) or {}
    sector = (payload.get("sector") or "").strip().upper()
    if not sector:
        return jsonify({"error": "sector is required"}), 400
    if sector not in sector_data.sector_etfs():
        return jsonify({"error": f"unknown sector '{sector}'"}), 400
    try:
        import refresh_policy
        names = [sector] + sector_data.constituents(sector)
        return jsonify(refresh_policy.refresh_tickers(names))
    except Exception as e:  # noqa: BLE001
        return _err(e)

