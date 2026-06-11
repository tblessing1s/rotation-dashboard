import threading

import pytest

import db


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Point the datastore at a throwaway SQLite file for this test."""
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "test.db"))
    db._local = threading.local()
    yield
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
    db._local = threading.local()
