"""Durability tests — atomic writes, corrupt-file refusal, rotating backups,
pre-migration snapshots, the restore path, and demo/live save parity.

Offline, no provider keys. Run with: python -m pytest backend -q
"""
import glob
import json
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import backups            # noqa: E402
import config             # noqa: E402
import logging_handler as log  # noqa: E402
import migrations         # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Point the whole store (state file, demo file, DATA_DIR/backups) at an
    isolated tmp dir in live mode."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def _tmp_files(directory) -> list[str]:
    return glob.glob(os.path.join(str(directory), "*.tmp.*"))


# ---------------------------------------------------------------------------
# 1. Atomic write crash simulation
# ---------------------------------------------------------------------------
def test_atomic_write_crash_leaves_original_intact(store, monkeypatch):
    state = log.load_state()          # creates a valid file
    state["metadata"]["capital_deployed"] = 111
    log.save_state(state)
    before = open(config.STATE_PATH, encoding="utf-8").read()

    # Crash mid-save: os.replace blows up after the temp file is written.
    def boom(src, dst):
        raise OSError("simulated crash during rename")
    monkeypatch.setattr(log.os, "replace", boom)

    state["metadata"]["capital_deployed"] = 999
    with pytest.raises(OSError):
        log.save_state(state)

    # Original file untouched and still parseable...
    after = open(config.STATE_PATH, encoding="utf-8").read()
    assert after == before
    assert json.loads(after)["metadata"]["capital_deployed"] == 111
    # ...and the temp file was cleaned up on the exception.
    assert _tmp_files(store) == []


def test_orphan_temp_files_cleaned_at_startup(store):
    log.load_state()
    orphan = os.path.join(str(store), "state.json.tmp.deadbeef")
    open(orphan, "w").write("{}")
    removed = log.cleanup_orphan_temp_files()
    assert orphan in removed
    assert not os.path.exists(orphan)


# ---------------------------------------------------------------------------
# 2. Serialization-failure safety
# ---------------------------------------------------------------------------
def test_serialization_failure_leaves_file_untouched(store):
    state = log.load_state()
    log.save_state(state)
    before = open(config.STATE_PATH, encoding="utf-8").read()

    state["positions"].append({"unserializable": {1, 2, 3}})  # a set -> TypeError
    with pytest.raises(TypeError):
        log.save_state(state)

    after = open(config.STATE_PATH, encoding="utf-8").read()
    assert after == before          # real file never touched
    assert _tmp_files(store) == []  # no temp file created (serialize happens first)


# ---------------------------------------------------------------------------
# 3. Corrupt-file startup refusal
# ---------------------------------------------------------------------------
def test_corrupt_state_refuses_to_reinitialize(store):
    open(config.STATE_PATH, "w").write('{"positions": [{"ticker": "NV')  # truncated
    with pytest.raises(log.StateCorruptError) as ei:
        log.load_state()
    assert "backup" in str(ei.value).lower()   # error references backups


def test_corrupt_state_error_names_latest_backup(store):
    # Seed a backup so the error can point the operator at it.
    log.load_state()
    b = backups.make_nightly_backup()
    open(config.STATE_PATH, "w").write("not json at all")
    with pytest.raises(log.StateCorruptError) as ei:
        log.load_state()
    assert os.path.basename(b) in str(ei.value)


# ---------------------------------------------------------------------------
# 4. Backup rotation
# ---------------------------------------------------------------------------
def test_rotation_keeps_newest_30_and_spares_snapshots(store):
    d = backups.backups_dir()
    os.makedirs(d, exist_ok=True)
    # 35 rotating backups with strictly increasing mtimes.
    made = []
    for i in range(35):
        p = os.path.join(d, f"state-201701{i:02d}-000000.json")
        open(p, "w").write("{}")
        os.utime(p, (1_000_000 + i, 1_000_000 + i))
        made.append(p)
    # 3 pre-migration snapshots — must be exempt from rotation.
    snaps = []
    for i in range(3):
        p = os.path.join(d, f"pre-migration-v{i+1}-to-v5-201701{i:02d}-000000.json")
        open(p, "w").write("{}")
        snaps.append(p)

    deleted = backups.rotate(keep=30)
    assert deleted == 5

    remaining = sorted(glob.glob(os.path.join(d, "state-*.json")))
    assert len(remaining) == 30
    # The 5 OLDEST rotating backups are gone, the 30 newest remain.
    assert made[0] not in remaining and made[4] not in remaining
    assert made[5] in remaining and made[34] in remaining
    # Snapshots untouched.
    for s in snaps:
        assert os.path.exists(s)


# ---------------------------------------------------------------------------
# 5. Pre-migration snapshot + abort-on-failure
# ---------------------------------------------------------------------------
def _v4_state() -> dict:
    return {
        "schema_version": 4,
        "metadata": {"last_updated": "2024-01-01T00:00:00Z", "capital_deployed": 0},
        "positions": [{"ticker": "NVDA", "status": "open"}],
        "executions": [],
        "theta_ledger": {"weeks": [], "totals": {}},
        "extrinsic_payback": {},
        "roll_ledger": {"rolls": [], "by_ticker": {}},
        "pending_orders": {},
    }


def test_pre_migration_snapshot_written_before_migrated_save(store):
    with open(config.STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(_v4_state(), fh, indent=2)

    state = log.load_state()          # triggers v4 -> v5 migration
    assert state["schema_version"] == migrations.CURRENT_VERSION

    snaps = glob.glob(os.path.join(backups.backups_dir(),
                                   f"pre-migration-v4-to-v{migrations.CURRENT_VERSION}-*.json"))
    assert len(snaps) == 1
    # The snapshot captured the PRE-migration bytes (v4), proving it was taken
    # before the migrated state was written back.
    assert json.load(open(snaps[0], encoding="utf-8"))["schema_version"] == 4
    # And the live file is now migrated.
    assert json.load(open(config.STATE_PATH, encoding="utf-8"))["schema_version"] == migrations.CURRENT_VERSION


def test_migration_aborts_when_snapshot_fails(store, monkeypatch):
    with open(config.STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(_v4_state(), fh, indent=2)

    def boom(*a, **k):
        raise OSError("backups volume unwritable")
    monkeypatch.setattr(backups, "snapshot_before_migration", boom)

    with pytest.raises(migrations.MigrationAbortedError):
        log.load_state()

    # File left untouched at v4 — no partial migration on disk.
    assert json.load(open(config.STATE_PATH, encoding="utf-8"))["schema_version"] == 4


# ---------------------------------------------------------------------------
# 6. Restore round-trip
# ---------------------------------------------------------------------------
def test_restore_round_trip_and_pre_restore_copy(store):
    state = log.load_state()
    state["metadata"]["capital_deployed"] = 42
    log.save_state(state)
    backup = backups.make_nightly_backup()
    backup_bytes = open(backup, encoding="utf-8").read()

    # Mutate the live file, then restore the backup over it.
    state["metadata"]["capital_deployed"] = 99999
    log.save_state(state)

    report = log.restore_from_backup(backup)

    # Restored content is byte-identical to the backup...
    assert open(config.STATE_PATH, encoding="utf-8").read() == backup_bytes
    assert json.load(open(config.STATE_PATH, encoding="utf-8"))["metadata"]["capital_deployed"] == 42
    # ...and the pre-restore safety copy exists with the clobbered content.
    assert report["pre_restore"] and os.path.exists(report["pre_restore"])
    assert json.load(open(report["pre_restore"], encoding="utf-8"))["metadata"]["capital_deployed"] == 99999


# ---------------------------------------------------------------------------
# 7. Demo/live path parity
# ---------------------------------------------------------------------------
def test_demo_and_live_share_the_atomic_save_path(store, monkeypatch):
    seen: list[str] = []
    real = log._atomic_write

    def spy(path, payload):
        seen.append(path)
        return real(path, payload)
    monkeypatch.setattr(log, "_atomic_write", spy)

    # Live save.
    monkeypatch.setattr(config, "_demo_mode", False)
    log.save_state(log.load_state())
    # Demo save — same function, different path.
    monkeypatch.setattr(config, "_demo_mode", True)
    log.save_state(log.load_state())

    assert config.STATE_PATH in seen
    assert config.DEMO_STATE_PATH in seen
