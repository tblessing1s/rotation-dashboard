"""CFM dashboard Flask backend.

Serves the built React frontend and the CFM API: scan (regime/sectors/stock
filter) -> entry gate -> execute (Schwab + auto-log) -> track (positions/theta
ledger/kill switch/checklist). state.json is the source of truth; the only route
that contacts a provider live is the Schwab account/quote path used at execution.

Routes live in ``blueprints/`` (one module per URL-prefix group); this module is
just the app factory: request-scoped hooks (auth gate, fetch budget, account
binding), the SPA catch-all, and startup wiring, all of which need the concrete
``Flask`` instance rather than a blueprint.
"""
from __future__ import annotations

import os

from flask import Flask, g, jsonify, request, send_from_directory
from flask_cors import CORS

import accounts
import alert_scheduler
import auth
import config
import executor
import fetch_budget
import logging_handler as log
from daytrade import scheduler as daytrade_scheduler

from blueprints.accounts_bp import accounts_bp
from blueprints.alerts_bp import alerts_bp
from blueprints.auth_bp import auth_bp
from blueprints.daytrade_bp import daytrade_bp
from blueprints.execute_bp import execute_bp
from blueprints.executions_bp import executions_bp
from blueprints.gate_bp import gate_bp
from blueprints.health_bp import health_bp
from blueprints.ingestion_bp import ingestion_bp
from blueprints.ledger_bp import ledger_bp
from blueprints.positions_bp import positions_bp
from blueprints.push_bp import push_bp
from blueprints.reconcile_bp import reconcile_bp
from blueprints.recommendations_bp import recommendations_bp
from blueprints.scan_bp import scan_bp
from blueprints.schwab_auth_bp import schwab_auth_bp
from blueprints.system_bp import system_bp
from blueprints.universe_bp import universe_bp

DIST_DIR = os.path.join(config.REPO_DIR, "frontend", "dist")

ACCOUNT_HEADER = "X-CFM-Account"

BLUEPRINTS = (
    auth_bp, daytrade_bp, gate_bp, scan_bp, execute_bp, positions_bp,
    ledger_bp, executions_bp, system_bp, alerts_bp, recommendations_bp,
    push_bp, reconcile_bp, ingestion_bp, accounts_bp, health_bp,
    universe_bp, schwab_auth_bp,
)


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    CORS(app)
    auth.init_app(app)

    for bp in BLUEPRINTS:
        app.register_blueprint(bp)

    @app.before_request
    def _auth_gate():
        return auth.gate()

    @app.before_request
    def _bound_fetch_budget():
        """Every HTTP request has a human waiting, so bound what its provider fetches
        may spend. See fetch_budget.py — the background budget is ~87s for a single
        symbol, which cannot fit inside the 60s the frontend waits before aborting.

        Set here and reset in teardown because gunicorn REUSES threads: a context
        variable left set would hand the next request this one's already-expired
        deadline, and every fetch would short-circuit to cache for the life of the
        worker. `executor.execute` opts back into the patient budget for order flow.
        """
        g._fetch_budget_token = fetch_budget.set_current(fetch_budget.interactive_budget())

    @app.before_request
    def _bind_account():
        """Bind this request to ONE book.

        The dashboard can hold several accounts (accounts.py). A request names the one
        it means with the ``X-CFM-Account`` header (or ``?account=``) — so two browser
        tabs can watch two accounts at once — and anything that doesn't name one gets
        the persisted active account, which is also what the background scheduler and
        CLI tools read. The binding is a contextvar reset in teardown: gunicorn reuses
        threads, and a leaked selection would hand the next request another book.

        An unknown id is refused rather than silently served from the primary book —
        except on the /api/accounts endpoints themselves, which are how a UI holding a
        stale id recovers.
        """
        requested = request.headers.get(ACCOUNT_HEADER) or request.args.get("account")
        if not requested:
            return None
        try:
            g._account_token = accounts.set_override(requested)
        except accounts.UnknownAccount:
            if request.path.startswith("/api/accounts"):
                return None
            return jsonify({"error": f"unknown account '{requested}'",
                            "unknown_account": True}), 404
        except accounts.RegistryCorrupt as e:
            return jsonify({"error": str(e)}), 500
        return None

    @app.teardown_request
    def _release_account(exc=None):
        token = g.pop("_account_token", None)
        if token is not None:
            accounts.reset_override(token)

    @app.teardown_request
    def _release_fetch_budget(exc=None):
        token = g.pop("_fetch_budget_token", None)
        if token is not None:
            fetch_budget.reset(token)

    # -------------------------------------------------------------------
    # Static frontend
    # -------------------------------------------------------------------
    @app.route("/")
    @app.route("/<path:path>")
    def serve_frontend(path: str = ""):
        if path and os.path.exists(os.path.join(DIST_DIR, path)):
            return send_from_directory(DIST_DIR, path)
        index = os.path.join(DIST_DIR, "index.html")
        if os.path.exists(index):
            return send_from_directory(DIST_DIR, "index.html")
        return jsonify({"error": "frontend not built — run `npm run build` in frontend/"}), 404

    # Durability startup check: clear orphaned write-temp files and eagerly load the
    # active store so a corrupt state.json fails fast HERE (refuse to serve) instead
    # of silently re-initializing empty state over the live trading record. Skipped
    # only if explicitly disabled (some one-off scripts import app without a store).
    if os.environ.get("CFM_SKIP_STARTUP_CHECK", "").strip() not in ("1", "true", "yes"):
        log.startup_check()
        # Order-lifecycle startup reconciliation: any locally non-terminal order is
        # re-polled against the broker before new order activity is allowed for its
        # position (a crash mid-cancel must not orphan a working broker order). No-op
        # when no live broker is configured (paper/tests); never blocks serving.
        # Every account: a working order left behind by a crash is just as dangerous
        # in the second book as in the first, and each book's pending orders live in
        # its own store.
        for _account_id in accounts.scheduled_ids():
            try:
                with accounts.use(_account_id):
                    executor.reconcile_pending_orders_on_startup()
            except Exception as e:  # noqa: BLE001 — reconciliation must never block startup
                log.logger.error("startup order reconciliation failed for account %s: %s",
                                 _account_id, e)

    # Start the in-process alert scheduler (gunicorn imports this module; the CLI
    # path below reaches it too). start_once() is idempotent and a no-op when
    # CFM_ALERTS_SCHEDULER=0 (tests / one-off scripts).
    alert_scheduler.start_once()

    # Same for the day-trade sleeve's own scheduler (nightly screener + 5-min bar
    # ingestion) — a separate daemon thread so a bug in one sleeve's tick can
    # never stall the other's. CFM_DAYTRADE_SCHEDULER=0 disables it.
    daytrade_scheduler.start_once()

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5179)), debug=True)
