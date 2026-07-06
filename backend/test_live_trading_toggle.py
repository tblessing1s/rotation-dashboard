"""Live-trading toggle: a persisted UI switch (or CFM_LIVE_TRADING env override)
controls whether executed orders may transmit to the broker — while the demo
gate (executor.live_transmit) still wins regardless.
"""
import pytest

import config
import executor


@pytest.fixture()
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "LIVE_TRADING_PATH", str(tmp_path / "live_trading.json"))
    monkeypatch.setattr(config, "_live_trading", None)
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.delenv("CFM_LIVE_TRADING", raising=False)
    return tmp_path


def test_default_is_paper(clean):
    assert config.live_trading_enabled() is False
    assert executor.live_enabled() is False
    assert executor.live_transmit() is False


def test_persisted_toggle_on_off(clean):
    config.set_live_trading_enabled(True)
    assert config.live_trading_enabled() is True
    assert executor.live_enabled() is True
    assert executor.live_transmit() is True  # live on and not demo
    config.set_live_trading_enabled(False)
    assert config.live_trading_enabled() is False
    assert executor.live_transmit() is False


def test_toggle_survives_process_restart(clean, monkeypatch):
    config.set_live_trading_enabled(True)
    monkeypatch.setattr(config, "_live_trading", None)  # simulate a fresh process
    assert config.live_trading_enabled() is True         # read back from the volume file


def test_env_override_forces_on_and_locks_the_ui(clean, monkeypatch):
    monkeypatch.setenv("CFM_LIVE_TRADING", "1")
    assert config.live_trading_env() is True
    assert config.live_trading_enabled() is True
    with pytest.raises(RuntimeError, match="locked on"):
        config.set_live_trading_enabled(False)


def test_demo_keeps_transmit_off_even_when_enabled(clean, monkeypatch):
    config.set_live_trading_enabled(True)
    monkeypatch.setattr(config, "_demo_mode", True)
    assert config.live_trading_enabled() is True
    assert executor.live_transmit() is False  # demo gate still wins


def test_api_live_trading_get_and_post(clean, monkeypatch):
    import app as app_module
    client = app_module.app.test_client()

    assert client.get("/api/live-trading").get_json()["enabled"] is False

    posted = client.post("/api/live-trading", json={"enabled": True}).get_json()
    assert posted["enabled"] is True and posted["transmit"] is True

    # Env lock: the POST is rejected (400) with an explanatory error.
    monkeypatch.setenv("CFM_LIVE_TRADING", "1")
    resp = client.post("/api/live-trading", json={"enabled": False})
    assert resp.status_code == 400
    assert "locked on" in resp.get_json()["error"]
    # GET still reports it enabled + env_locked while the override is set.
    got = client.get("/api/live-trading").get_json()
    assert got["enabled"] is True and got["env_locked"] is True
