"""Per-account day-trading enable/disable toggle (daytrade/settings.py).
Offline: a tmp_path settings file, accounts.scheduled_ids() stubbed."""
from __future__ import annotations

import accounts
import pytest

from daytrade import settings


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PATH", str(tmp_path / "daytrade_log" / "enabled.json"))
    return tmp_path


def test_primary_defaults_to_enabled(tmp_settings):
    assert settings.is_enabled(accounts.DEFAULT_ID) is True


def test_a_non_primary_account_defaults_to_disabled(tmp_settings):
    assert settings.is_enabled("ira") is False


def test_set_enabled_persists_and_overrides_the_default(tmp_settings):
    settings.set_enabled("ira", True)
    assert settings.is_enabled("ira") is True

    settings.set_enabled(accounts.DEFAULT_ID, False)
    assert settings.is_enabled(accounts.DEFAULT_ID) is False


def test_set_enabled_survives_a_fresh_read(tmp_settings):
    settings.set_enabled("ira", True)

    # A second, independent read (e.g. a later request) must see the same
    # persisted value, not an in-memory cache that could go stale.
    assert settings.is_enabled("ira") is True


def test_enabled_account_ids_filters_scheduled_accounts_by_the_toggle(tmp_settings, monkeypatch):
    monkeypatch.setattr(accounts, "scheduled_ids", lambda: ["primary", "ira", "roth"])
    settings.set_enabled("ira", True)
    # "roth" left at its default (disabled, not primary) and "primary" left
    # at its default (enabled).

    assert settings.enabled_account_ids() == ["primary", "ira"]


def test_enabled_account_ids_respects_an_explicit_primary_opt_out(tmp_settings, monkeypatch):
    monkeypatch.setattr(accounts, "scheduled_ids", lambda: ["primary", "ira"])
    settings.set_enabled("primary", False)

    assert settings.enabled_account_ids() == []


def test_a_missing_settings_file_does_not_raise(tmp_settings):
    # No set_enabled call has ever run — the file doesn't exist yet.
    assert settings.is_enabled("primary") is True
    assert settings.is_enabled("ira") is False
