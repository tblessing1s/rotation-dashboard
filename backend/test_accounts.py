"""Multi-account tests — registry, per-account stores, request scoping, the
brokerage-account binding, and the cross-account roll-up.

The invariant under test throughout: two books never touch each other. One
account's executions, positions, alerts and orders live in its own state file,
and an order placed from one book goes to that book's brokerage account.

Offline, no provider keys. Run with: python -m pytest backend -q
"""
import json
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-accounts-test-"))

import accounts            # noqa: E402
import config              # noqa: E402
import logging_handler as log  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """An isolated DATA_DIR in live mode with an empty account registry."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(accounts, "registry_path", lambda: str(tmp_path / "accounts.json"))
    return tmp_path


# ---------------------------------------------------------------------------
# 1. The registry
# ---------------------------------------------------------------------------
def test_no_registry_file_is_the_single_account_default(store):
    """An existing single-account deployment has no accounts.json — it must read
    as exactly one account on the un-suffixed store, with nothing to migrate."""
    assert not os.path.exists(accounts.registry_path())
    assert [a["id"] for a in accounts.list_accounts()] == [accounts.DEFAULT_ID]
    assert accounts.active_id() == accounts.DEFAULT_ID
    assert accounts.active_state_path() == config.STATE_PATH
    assert config.active_state_path() == config.STATE_PATH


def test_primary_keeps_the_unsuffixed_paths_in_both_modes(store, monkeypatch):
    accounts.create("IRA")
    assert accounts.state_path(accounts.DEFAULT_ID) == config.STATE_PATH
    assert accounts.state_path("ira") == str(store / "state.ira.json")
    monkeypatch.setattr(config, "_demo_mode", True)
    assert accounts.state_path(accounts.DEFAULT_ID) == config.DEMO_STATE_PATH
    assert accounts.state_path("ira") == str(store / "state.demo.ira.json")


def test_create_derives_a_slug_id_and_dedupes(store):
    first = accounts.create("Roth IRA")
    second = accounts.create("Roth IRA")
    assert first["id"] == "roth-ira"
    assert second["id"] == "roth-ira-2"
    assert [a["id"] for a in accounts.list_accounts()] == \
        [accounts.DEFAULT_ID, "roth-ira", "roth-ira-2"]


def test_unknown_account_is_refused_never_silently_primary(store):
    with pytest.raises(accounts.UnknownAccount):
        accounts.require("nope")
    with pytest.raises(accounts.UnknownAccount):
        accounts.set_override("nope")


def test_corrupt_registry_refuses_rather_than_guessing(store):
    with open(accounts.registry_path(), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    with pytest.raises(accounts.RegistryCorrupt):
        accounts.load_registry()


def test_primary_cannot_be_deleted_or_archived(store):
    with pytest.raises(ValueError):
        accounts.delete(accounts.DEFAULT_ID)
    with pytest.raises(ValueError):
        accounts.update(accounts.DEFAULT_ID, archived=True)


def test_archiving_hands_the_active_slot_back_to_primary(store):
    accounts.create("IRA")
    accounts.set_active("ira")
    assert accounts.active_id() == "ira"
    accounts.update("ira", archived=True)
    assert accounts.active_id() == accounts.DEFAULT_ID
    assert [a["id"] for a in accounts.list_accounts()] == [accounts.DEFAULT_ID]
    assert "ira" in [a["id"] for a in accounts.list_accounts(include_archived=True)]


def test_delete_refuses_a_book_with_executions_and_purge_sets_it_aside(store):
    accounts.create("IRA")
    with accounts.use("ira"):
        state = log.load_state()
        state["executions"].append({"id": "exec_0001", "action": "sell_call"})
        log.save_state(state)
    with pytest.raises(accounts.AccountInUse):
        accounts.delete("ira")
    report = accounts.delete("ira", purge=True)
    assert report["books_set_aside"], "the execution log must be kept, not unlinked"
    assert os.path.exists(report["books_set_aside"][0])
    assert not os.path.exists(store / "state.ira.json")
    assert not accounts.exists("ira")


# ---------------------------------------------------------------------------
# 2. Store isolation — the whole point
# ---------------------------------------------------------------------------
def test_each_account_writes_its_own_state_file(store):
    accounts.create("IRA")
    log.save_state({**log.load_state(), "positions": [{"ticker": "AAPL", "status": "open"}]})
    with accounts.use("ira"):
        log.save_state({**log.load_state(), "positions": [{"ticker": "MSFT", "status": "open"}]})

    assert [p["ticker"] for p in log.load_state()["positions"]] == ["AAPL"]
    with accounts.use("ira"):
        assert [p["ticker"] for p in log.load_state()["positions"]] == ["MSFT"]
    assert os.path.exists(store / "state.json")
    assert os.path.exists(store / "state.ira.json")


def test_a_new_account_starts_as_a_fresh_book_at_the_current_schema(store):
    import migrations
    accounts.create("IRA")
    with accounts.use("ira"):
        state = log.load_state()
    assert state["executions"] == [] and state["positions"] == []
    assert state["schema_version"] == migrations.CURRENT_VERSION


def test_appending_an_execution_touches_only_the_active_book(store):
    accounts.create("IRA")
    with accounts.use("ira"):
        log.append_execution({"action": "sell_call", "ticker": "MSFT", "contracts": 1})
        assert len(log.load_state()["executions"]) == 1
    assert log.load_state()["executions"] == []


def test_the_context_manager_restores_the_previous_binding(store):
    accounts.create("IRA")
    accounts.set_active(accounts.DEFAULT_ID)
    with accounts.use("ira"):
        assert accounts.active_id() == "ira"
    assert accounts.active_id() == accounts.DEFAULT_ID


def test_orphan_temp_sweep_covers_every_account_store(store):
    accounts.create("IRA")
    orphans = [str(store / "state.json.tmp.123"), str(store / "state.ira.json.tmp.456"),
               str(store / "state.demo.ira.json.tmp.789")]
    for path in orphans:
        open(path, "w").close()
    removed = log.cleanup_orphan_temp_files()
    assert sorted(removed) == sorted(orphans)


# ---------------------------------------------------------------------------
# 3. Backups stay per account
# ---------------------------------------------------------------------------
def test_backups_are_isolated_per_account(store):
    import backups
    accounts.create("IRA")
    log.save_state(log.load_state())
    with accounts.use("ira"):
        log.save_state(log.load_state())
        ira_backup = backups.make_nightly_backup()
    primary_backup = backups.make_nightly_backup()

    assert os.path.dirname(ira_backup) == str(store / "backups" / "ira")
    assert os.path.dirname(primary_backup) == str(store / "backups")
    # Rotation is scoped: emptying the primary's pool leaves the IRA's alone.
    with accounts.use("ira"):
        assert [b["path"] for b in backups.list_backups()] == [ira_backup]
    assert backups.rotate(keep=0) == 1
    assert os.path.exists(ira_backup)


def test_offmachine_name_keeps_two_books_apart(store):
    import backups
    accounts.create("IRA")
    with accounts.use("ira"):
        log.save_state(log.load_state())
        ira_backup = backups.make_nightly_backup()
    log.save_state(log.load_state())
    primary_backup = backups.make_nightly_backup()
    assert backups.offmachine_name(ira_backup).startswith("ira/")
    assert "/" not in backups.offmachine_name(primary_backup)


# ---------------------------------------------------------------------------
# 4. Brokerage binding — one Schwab login, several linked accounts
# ---------------------------------------------------------------------------
class _Client:
    """A Schwab client whose login reaches two linked accounts."""

    NUMBERS = [{"accountNumber": "11112222", "hashValue": "HASH_TAXABLE"},
               {"accountNumber": "33334444", "hashValue": "HASH_IRA"}]

    def account_numbers(self):
        return list(self.NUMBERS)

    def primary_account_hash(self):
        return self.NUMBERS[0]["hashValue"]


def test_unbound_account_keeps_the_first_linked_account(store):
    assert accounts.broker_hash(_Client()) == "HASH_TAXABLE"


def test_bound_account_routes_to_its_own_brokerage_account(store):
    accounts.create("IRA", broker_account_number="33334444")
    with accounts.use("ira"):
        assert accounts.broker_hash(_Client()) == "HASH_IRA"
    assert accounts.broker_hash(_Client()) == "HASH_TAXABLE"


def test_a_binding_that_is_not_linked_raises_instead_of_routing_elsewhere(store):
    accounts.create("Old", broker_account_number="99999999")
    with accounts.use("old"), pytest.raises(RuntimeError):
        accounts.broker_hash(_Client())


def test_executor_order_hash_follows_the_active_account(store):
    import executor
    accounts.create("IRA", broker_account_number="33334444")
    with accounts.use("ira"):
        assert executor._order_account_hash(_Client()) == "HASH_IRA"


def test_cash_balance_reads_the_bound_account_node(store):
    import schwab_api
    nodes = [
        {"securitiesAccount": {"accountNumber": "11112222",
                               "currentBalances": {"cashAvailableForTrading": 1000}}},
        {"securitiesAccount": {"accountNumber": "33334444",
                               "currentBalances": {"cashAvailableForTrading": 7500}}},
    ]
    assert schwab_api._account_cash(schwab_api.select_account_node(nodes, None)) == 1000
    assert schwab_api._account_cash(schwab_api.select_account_node(nodes, "33334444")) == 7500
    with pytest.raises(schwab_api.SchwabError):
        schwab_api.select_account_node(nodes, "55556666")


def test_reconcile_reads_only_the_bound_accounts_positions(store):
    """A login's /accounts response carries every linked account. Reconciling a
    book against the union would report the sibling account's shares as an
    unexpected broker holding."""
    import reconcile
    response = [
        {"securitiesAccount": {"accountNumber": "11112222", "positions": [
            {"longQuantity": 100, "instrument": {"assetType": "EQUITY", "symbol": "AAPL"}}]}},
        {"securitiesAccount": {"accountNumber": "33334444", "positions": [
            {"longQuantity": 100, "instrument": {"assetType": "EQUITY", "symbol": "MSFT"}}]}},
    ]
    assert [p["underlying"] for p in reconcile.parse_broker_positions(response)] == ["AAPL"]
    assert [p["underlying"] for p in
            reconcile.parse_broker_positions(response, "33334444")] == ["MSFT"]


# ---------------------------------------------------------------------------
# 5. The roll-up
# ---------------------------------------------------------------------------
def test_summary_rolls_every_book_up_without_provider_calls(store):
    accounts.create("IRA")
    state = log.load_state()
    state["positions"] = [{"ticker": "AAPL", "status": "open"}]
    state["metadata"]["capital_deployed"] = 20000
    state["theta_ledger"]["totals"] = {"this_week": 300, "this_month": 900, "ytd": 5000}
    log.save_state(state)
    with accounts.use("ira"):
        ira = log.load_state()
        ira["positions"] = [{"ticker": "MSFT", "status": "open"},
                            {"ticker": "NVDA", "status": "closed"}]
        ira["metadata"]["capital_deployed"] = 10000
        ira["theta_ledger"]["totals"] = {"this_week": 100, "this_month": 250, "ytd": 1200}
        ira["alerts"]["active"] = {"fp1": {"type": "ROLL_DUE"}}
        log.save_state(ira)

    summary = accounts.summary()
    rows = {r["id"]: r for r in summary["accounts"]}
    assert rows[accounts.DEFAULT_ID]["open_positions"] == 1
    assert rows["ira"]["open_positions"] == 1 and rows["ira"]["tickers"] == ["MSFT"]
    assert rows["ira"]["active_alerts"] == 1
    assert summary["totals"] == {
        "accounts": 2, "open_positions": 2, "capital_deployed": 30000.0,
        "operating_cash": 0.0, "this_week": 400.0, "this_month": 1150.0, "ytd": 6200.0,
        "active_alerts": 1, "pending_orders": 0, "open_proposals": 0,
    }


def test_summary_reports_a_book_that_has_never_been_opened(store):
    accounts.create("IRA")
    row = {r["id"]: r for r in accounts.summary()["accounts"]}["ira"]
    assert row["exists"] is False and row["open_positions"] == 0 and row["error"] is None


# ---------------------------------------------------------------------------
# 6. HTTP surface
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(store, monkeypatch):
    monkeypatch.setenv("CFM_SKIP_STARTUP_CHECK", "1")
    monkeypatch.setenv("CFM_ALERTS_SCHEDULER", "0")
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_api_lists_creates_switches_and_rolls_up(client, store):
    assert client.get("/api/accounts").get_json()["accounts"][0]["id"] == accounts.DEFAULT_ID

    created = client.post("/api/accounts", json={"label": "IRA",
                                                 "broker_account_number": "33334444"})
    assert created.status_code == 200 and created.get_json()["id"] == "ira"

    assert client.post("/api/accounts/active", json={"id": "ira"}).status_code == 200
    assert accounts.load_registry()["active"] == "ira"

    renamed = client.patch("/api/accounts/ira", json={"label": "Roth IRA"})
    assert renamed.get_json()["label"] == "Roth IRA"

    summary = client.get("/api/accounts/summary").get_json()
    assert {r["id"] for r in summary["accounts"]} == {accounts.DEFAULT_ID, "ira"}
    # The bound brokerage number is masked on the way out.
    ira_row = {r["id"]: r for r in summary["accounts"]}["ira"]
    assert ira_row["broker_account_number"] == "…4444" and ira_row["broker_bound"] is True


def test_request_header_scopes_one_request_without_switching_the_active_book(client, store):
    accounts.create("IRA")
    accounts.set_active(accounts.DEFAULT_ID)
    state = log.load_state()
    state["metadata"]["operating_cash"] = 111
    log.save_state(state)
    with accounts.use("ira"):
        ira = log.load_state()
        ira["metadata"]["operating_cash"] = 222
        log.save_state(ira)

    assert client.get("/api/state").get_json()["metadata"]["operating_cash"] == 111
    scoped = client.get("/api/state", headers={"X-CFM-Account": "ira"})
    assert scoped.get_json()["metadata"]["operating_cash"] == 222
    # The header is per request — the persisted choice is untouched.
    assert accounts.load_registry()["active"] == accounts.DEFAULT_ID
    assert client.get("/api/state").get_json()["metadata"]["operating_cash"] == 111


def test_unknown_account_header_is_refused_but_the_accounts_api_still_answers(client, store):
    refused = client.get("/api/state", headers={"X-CFM-Account": "ghost"})
    assert refused.status_code == 404 and refused.get_json()["unknown_account"] is True
    # …so a UI holding a stale id can still list accounts and recover.
    assert client.get("/api/accounts", headers={"X-CFM-Account": "ghost"}).status_code == 200


def test_broker_account_picker_reports_why_it_is_empty(client, store, monkeypatch):
    """An empty picker has several causes and the operator has to be able to tell
    them apart — so this endpoint answers 200 with the reason, never an opaque
    error the panel can only render as a blank dropdown."""
    import schwab_api
    monkeypatch.setattr(schwab_api, "configured", lambda: False)
    body = client.get("/api/accounts/broker-accounts").get_json()
    assert body["count"] == 0 and body["accounts"] == []
    assert "isn't connected" in body["error"]

    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    import data_handler

    class _NoAccounts:
        def account_numbers(self):
            return []

    monkeypatch.setattr(data_handler, "client", lambda: _NoAccounts())
    body = client.get("/api/accounts/broker-accounts").get_json()
    assert body["count"] == 0 and "consent screen" in body["error"]

    class _Broken:
        def account_numbers(self):
            raise RuntimeError("HTTP 401 token expired")

    monkeypatch.setattr(data_handler, "client", lambda: _Broken())
    body = client.get("/api/accounts/broker-accounts").get_json()
    assert body["error"] == "HTTP 401 token expired"


def test_broker_account_picker_lists_every_linked_account_and_its_binding(client, store, monkeypatch):
    import data_handler
    import schwab_api
    accounts.create("IRA", broker_account_number="33334444")
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(data_handler, "client", lambda: _Client())

    body = client.get("/api/accounts/broker-accounts").get_json()
    assert body["error"] is None and body["count"] == 2
    assert [r["masked"] for r in body["accounts"]] == ["…2222", "…4444"]
    assert [r["bound_to"] for r in body["accounts"]] == [None, "ira"]


def test_deleting_a_book_with_executions_is_refused_over_http(client, store):
    accounts.create("IRA")
    with accounts.use("ira"):
        log.append_execution({"action": "sell_call", "ticker": "MSFT", "contracts": 1})
    assert client.delete("/api/accounts/ira").status_code == 409
    assert client.delete("/api/accounts/ira?purge=1").status_code == 200


# ---------------------------------------------------------------------------
# 7. Scheduled work covers every book
# ---------------------------------------------------------------------------
def test_scheduler_fans_out_over_non_archived_accounts(store):
    import alert_scheduler
    accounts.create("IRA")
    accounts.create("Old")
    accounts.update("old", archived=True)

    seen = []
    alert_scheduler.for_each_account("test", lambda acct_id: seen.append(
        (acct_id, accounts.active_id(), config.active_state_path())))

    assert [s[0] for s in seen] == [accounts.DEFAULT_ID, "ira"]
    # Each callback ran BOUND to its account — the state path proves it.
    assert [s[2] for s in seen] == [str(store / "state.json"), str(store / "state.ira.json")]


def test_one_accounts_failure_does_not_skip_the_others(store):
    import alert_scheduler
    accounts.create("IRA")
    seen = []

    def boom(account_id):
        seen.append(account_id)
        if account_id == accounts.DEFAULT_ID:
            raise RuntimeError("simulated")

    alert_scheduler.for_each_account("test", boom)
    assert seen == [accounts.DEFAULT_ID, "ira"]


def test_expiry_check_registries_are_per_account(store):
    import alert_scheduler
    accounts.create("IRA")
    primary = alert_scheduler.mandatory_registry(accounts.DEFAULT_ID)
    assert primary is alert_scheduler._mandatory_run  # unchanged for a single install
    assert alert_scheduler.mandatory_registry("ira") is not primary


def test_alert_deep_links_name_the_account_once_there_is_more_than_one(store):
    import alerts
    assert "account=" not in (alerts._action_url("EXPIRY_FRIDAY", "AAPL") or "")
    accounts.create("IRA")
    with accounts.use("ira"):
        assert "account=ira" in alerts._action_url("EXPIRY_FRIDAY", "AAPL")
        assert "account=ira" in alerts._payout_action_url()


def test_alert_messages_name_the_account_once_there_is_more_than_one(store):
    import notifier
    batch = [{"severity": "HIGH", "type": "ROLL_DUE", "ticker": "AAPL", "message": "m"}]
    assert notifier.format_subject(batch) == "[CFM HIGH] 1 alert(s) — AAPL"
    accounts.create("IRA")
    with accounts.use("ira"):
        assert notifier.format_subject(batch) == "[CFM HIGH · IRA] 1 alert(s) — AAPL"
        assert "IRA account" in notifier.format_body(batch)


def test_push_devices_are_shared_across_books(store):
    """A phone is registered once, from whichever book was on screen — every
    account's alerts must still reach it."""
    import webpush
    accounts.create("IRA")
    sub = {"endpoint": "https://push.example/abc",
           "keys": {"p256dh": "key", "auth": "auth"}}
    webpush.add_subscription(sub)                      # registered on the primary book

    with accounts.use("ira"):
        assert webpush.list_subscriptions() == []      # the record lives in the primary book…
        assert webpush.subscription_count() == 1       # …but delivery sees the device
        assert [d["endpoint"] for d in webpush.all_subscriptions()] == [sub["endpoint"]]


def test_unsubscribing_a_device_reaches_every_book(store):
    import webpush
    accounts.create("IRA")
    sub = {"endpoint": "https://push.example/abc",
           "keys": {"p256dh": "key", "auth": "auth"}}
    webpush.add_subscription(sub)
    with accounts.use("ira"):
        webpush.add_subscription(sub)                  # same phone, registered twice

    assert webpush.remove_subscription(sub["endpoint"])["removed"] == 2
    assert webpush.all_subscriptions() == []


def test_registry_survives_a_json_round_trip(store):
    accounts.create("IRA", broker_account_number="33334444", note="spouse")
    with open(accounts.registry_path(), encoding="utf-8") as fh:
        raw = json.load(fh)
    assert raw["accounts"][1]["broker_account_number"] == "33334444"
    assert accounts.get("ira")["note"] == "spouse"
