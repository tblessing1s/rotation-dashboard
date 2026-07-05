"""Off-machine S3 backup path tests — that _s3_upload targets the right
bucket/key/endpoint and that send_offmachine_copy routes to S3 when enabled. No
real network/AWS: boto3 is replaced with a fake that records the upload. Run
offline with: python -m pytest backend -q
"""
import os
import sys
import tempfile
import types

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-s3-test-"))

import pytest  # noqa: E402

import backups  # noqa: E402
import config  # noqa: E402


class _FakeS3Client:
    def __init__(self):
        self.uploads = []

    def upload_file(self, path, bucket, key):
        self.uploads.append((path, bucket, key))


def _install_fake_boto3(monkeypatch):
    """Replace the boto3 module so _s3_upload's lazy import gets our fake."""
    client = _FakeS3Client()
    made = {}

    def make_client(service, endpoint_url=None):
        made["service"] = service
        made["endpoint_url"] = endpoint_url
        return client

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=make_client))
    return client, made


@pytest.fixture(autouse=True)
def _clean_env():
    for k in ("BACKUP_S3_BUCKET", "BACKUP_S3_ENDPOINT", "BACKUP_S3_KEY_PREFIX", "CFM_BACKUP_S3"):
        os.environ.pop(k, None)
    yield


def _backup_file(tmp_path):
    p = tmp_path / "state-20260704-000000.json"
    p.write_text("{}")
    return str(p)


def test_s3_upload_targets_endpoint_bucket_and_prefixed_key(monkeypatch, tmp_path):
    os.environ["BACKUP_S3_BUCKET"] = "cfm-bucket"
    os.environ["BACKUP_S3_ENDPOINT"] = "https://fly.storage.tigris.dev"
    os.environ["BACKUP_S3_KEY_PREFIX"] = "cfm-backups"
    client, made = _install_fake_boto3(monkeypatch)

    path = _backup_file(tmp_path)
    out = backups._s3_upload(path)

    assert made["service"] == "s3"
    assert made["endpoint_url"] == "https://fly.storage.tigris.dev"
    assert client.uploads == [(path, "cfm-bucket", "cfm-backups/state-20260704-000000.json")]
    assert out == {"method": "s3", "ok": True, "bucket": "cfm-bucket",
                   "key": "cfm-backups/state-20260704-000000.json",
                   "endpoint": "https://fly.storage.tigris.dev"}


def test_s3_upload_defaults_key_prefix(monkeypatch, tmp_path):
    os.environ["BACKUP_S3_BUCKET"] = "cfm-bucket"
    client, _ = _install_fake_boto3(monkeypatch)
    backups._s3_upload(_backup_file(tmp_path))
    assert client.uploads[0][2].startswith("cfm-backups/")  # default prefix


def test_s3_upload_requires_bucket(monkeypatch, tmp_path):
    _install_fake_boto3(monkeypatch)
    with pytest.raises(RuntimeError, match="BACKUP_S3_BUCKET"):
        backups._s3_upload(_backup_file(tmp_path))


def test_send_offmachine_routes_to_s3_when_enabled(monkeypatch, tmp_path):
    os.environ["BACKUP_S3_BUCKET"] = "cfm-bucket"
    _install_fake_boto3(monkeypatch)
    monkeypatch.setattr(config, "BACKUP_S3_ENABLED", True)
    out = backups.send_offmachine_copy(_backup_file(tmp_path))
    assert out["ok"] is True and out["method"] == "s3"


def test_send_offmachine_s3_failure_is_reported_not_raised(monkeypatch, tmp_path):
    # No bucket -> _s3_upload raises -> send_offmachine_copy catches and reports.
    _install_fake_boto3(monkeypatch)
    monkeypatch.setattr(config, "BACKUP_S3_ENABLED", True)
    out = backups.send_offmachine_copy(_backup_file(tmp_path))
    assert out["ok"] is False and out["method"] == "s3" and "error" in out
