"""Shared test scaffolding — a scriptable mock Schwab client.

Purely additive: this file defines a reusable ``MockSchwabClient`` and an opt-in
``mock_schwab`` fixture. New tests (transaction ingestion, and any future
consolidation of the inline fakes) can build on this one replayable surface,
which — unlike the older inline mocks — can script the ``get_transactions`` feed
the §4 ingestion path consumes.

One autouse fixture (``_legacy_leap_open_in_tests``) relaxes the shares-primary
read-only guard for the suite: many tests predate the migration and build LEGACY
LEAP positions as *history fixtures* via ``execute(buy_leap / open_position_atomic
/ roll_leap)`` to exercise the still-supported read-only machinery (payback meters,
history, rolls of the short leg, reconciliation, exits). In production the guard is
ON (``config.LEGACY_LEAP_READONLY`` defaults True) — ``test_shares_migration`` flips
it back on to assert the block directly.

No live Schwab call is ever made; every method returns scripted data.
"""
from __future__ import annotations


class MockSchwabClient:
    """A Schwab client whose every endpoint replays scripted data.

    Construct with the responses a test needs; unset endpoints return empty
    defaults. Call records are captured on ``.calls`` for assertions (e.g. "the
    submit endpoint was hit exactly once").
    """

    def __init__(self, *, account_hash: str = "ACCT_HASH",
                 transactions: list | None = None,
                 accounts: list | None = None,
                 quotes: dict | None = None,
                 orders: dict | None = None,
                 recent_orders: list | None = None,
                 submit_result=None,
                 place_result=None):
        self._account_hash = account_hash
        self._transactions = transactions or []
        self._accounts = accounts or []
        self._quotes = quotes or {}
        # order_id -> a single snapshot, or a list of snapshots popped in sequence
        # (to replay WORKING -> terminal across successive polls).
        self._orders = orders or {}
        self._recent_orders = recent_orders or []
        self._submit_result = submit_result
        self._place_result = place_result
        self.calls: list[tuple] = []

    # -- account / market data ------------------------------------------------
    def primary_account_hash(self) -> str:
        self.calls.append(("primary_account_hash",))
        return self._account_hash

    def get_accounts(self, positions: bool = True) -> list:
        self.calls.append(("get_accounts", positions))
        return self._accounts

    def get_quotes(self, symbols) -> dict:
        self.calls.append(("get_quotes", tuple(symbols) if not isinstance(symbols, str) else symbols))
        return self._quotes

    # -- transactions (execution ingestion) -----------------------------------
    def get_transactions(self, account_hash: str, start_date=None, end_date=None,
                         types: str = "TRADE") -> list:
        self.calls.append(("get_transactions", account_hash, start_date, end_date, types))
        return list(self._transactions)

    # -- orders ---------------------------------------------------------------
    def submit_order(self, account_hash: str, order: dict) -> dict:
        self.calls.append(("submit_order", account_hash, order))
        res = self._submit_result
        if callable(res):
            res = res(len([c for c in self.calls if c[0] == "submit_order"]))
        return res if res is not None else {"outcome": "accepted", "order_id": "OID1", "location": "/OID1"}

    def place_order(self, account_hash: str, order: dict) -> dict:
        self.calls.append(("place_order", account_hash, order))
        res = self._place_result
        if callable(res):
            res = res(len([c for c in self.calls if c[0] == "place_order"]))
        return res if res is not None else {"orderId": "OID1", "location": "/OID1"}

    def get_order(self, account_hash: str, order_id: str) -> dict:
        self.calls.append(("get_order", account_hash, order_id))
        snap = self._orders.get(str(order_id))
        if isinstance(snap, list):
            return snap.pop(0) if snap else {}
        return snap or {}

    def list_orders(self, account_hash: str, from_entered_time=None,
                    to_entered_time=None, max_results: int = 50) -> list:
        self.calls.append(("list_orders", account_hash))
        return list(self._recent_orders)

    def cancel_order(self, account_hash: str, order_id: str) -> dict:
        self.calls.append(("cancel_order", account_hash, order_id))
        return {"orderId": order_id, "canceled": True}


def _pytest_fixtures():
    import pytest

    @pytest.fixture
    def mock_schwab():
        """Return the MockSchwabClient class (tests instantiate with their script)."""
        return MockSchwabClient

    @pytest.fixture(autouse=True)
    def _legacy_leap_open_in_tests():
        """Let the suite build LEGACY LEAP positions as history fixtures. The
        shares-primary read-only guard (config.LEGACY_LEAP_READONLY) is ON in
        production; here it is relaxed so pre-migration tests can seed legacy
        state via execute(buy_leap/...). Tests that assert the guard set it back
        on explicitly (see test_shares_migration)."""
        import config
        prev = config.LEGACY_LEAP_READONLY
        config.LEGACY_LEAP_READONLY = False
        try:
            yield
        finally:
            config.LEGACY_LEAP_READONLY = prev

    @pytest.fixture(autouse=True)
    def _isolated_account_registry(tmp_path_factory, monkeypatch):
        """Every test runs against its own EMPTY account registry.

        The registry decides which state file a book resolves to, so a stray
        DATA_DIR/accounts.json (a developer's real one, or another test's) could
        silently point a test at a different store. An empty registry is the
        single-account default: the primary account on config.STATE_PATH, exactly
        what the suite assumed before accounts existed. Tests that exercise the
        registry itself repoint this at their own tmp path."""
        import accounts
        directory = tmp_path_factory.mktemp("accounts-registry")
        monkeypatch.setattr(accounts, "registry_path",
                            lambda: str(directory / "accounts.json"))

    @pytest.fixture(autouse=True)
    def _unpaced_schwab_and_fresh_quotes(monkeypatch):
        """The process-wide Schwab pacing is OFF in the suite (the fakes answer
        instantly and a test module can easily make more mocked calls than a
        minute's budget), and the short quote cache starts empty so a price from
        one test never leaks into the next. The limiter's own tests turn it on."""
        import config as _config
        import data_handler as _dh
        import schwab_api as _sa
        monkeypatch.setattr(_config, "SCHWAB_REQUESTS_PER_MINUTE", 0)
        _sa.reset_rate_limiter()
        _dh.clear_quote_cache()
        try:
            yield
        finally:
            _sa.reset_rate_limiter()
            _dh.clear_quote_cache()

    return (mock_schwab, _legacy_leap_open_in_tests, _isolated_account_registry,
            _unpaced_schwab_and_fresh_quotes)


(mock_schwab, _legacy_leap_open_in_tests, _isolated_account_registry,
 _unpaced_schwab_and_fresh_quotes) = _pytest_fixtures()
