"""Durability backups for state.json.

Three call sites feed through the one helper here so the naming, locking and
on-volume location are identical everywhere:

  * nightly rotating backups (maintenance.nightly_refresh)   -> state-<ts>.json
  * pre-migration snapshots   (migrations.migrate)            -> pre-migration-*
  * the restore CLI           (scripts/restore_state.py)      -> reads these back

Backups land under ``DATA_DIR/backups`` which, on Fly, is the persistent volume
mount (/data) — NOT the ephemeral rootfs. The nightly job also ships one copy
OFF the machine (email attachment or an optional S3-compatible upload) because
the single volume is itself a single point of failure.

Copies are taken while holding the store's write lock — with atomic writes a
mid-write file can't exist, but the lock makes the backup point-in-time
consistent for free, so we take it.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import shutil
from datetime import datetime
from email.message import EmailMessage

import config

logger = logging.getLogger("cfm.alerts")

BACKUP_PREFIX = "state-"                 # rotating nightly copies
PREMIGRATION_PREFIX = "pre-migration-"   # kept forever, exempt from rotation


def backups_dir() -> str:
    """DATA_DIR/backups — the Fly persistent volume (/data) in production."""
    return os.path.join(config.DATA_DIR, "backups")


def _ensure_dir() -> str:
    d = backups_dir()
    os.makedirs(d, exist_ok=True)
    return d


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _copy_locked(src: str, dst: str) -> None:
    """Copy src->dst under the store write lock (point-in-time consistent)."""
    import logging_handler as log  # lazy: avoid import cycle (log -> migrations -> backups)
    with log._lock:
        if not os.path.exists(src):
            raise FileNotFoundError(f"no state file to back up at {src}")
        shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# Listing / metadata
# ---------------------------------------------------------------------------
def _schema_version(path: str) -> int | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return int(json.load(fh).get("schema_version") or 1)
    except (OSError, ValueError, TypeError):
        return None


def list_backups(include_premigration: bool = True) -> list[dict]:
    """Newest-first list of backups with timestamp + schema version."""
    d = backups_dir()
    if not os.path.isdir(d):
        return []
    out: list[dict] = []
    for path in glob.glob(os.path.join(d, "*.json")):
        name = os.path.basename(path)
        is_pre = name.startswith(PREMIGRATION_PREFIX)
        if is_pre and not include_premigration:
            continue
        out.append({
            "path": path,
            "name": name,
            "pre_migration": is_pre,
            "mtime": os.path.getmtime(path),
            "schema_version": _schema_version(path),
        })
    out.sort(key=lambda b: b["mtime"], reverse=True)
    return out


def latest_backup() -> str | None:
    """Path of the most recent backup of any kind, or None."""
    backups = list_backups()
    return backups[0]["path"] if backups else None


# ---------------------------------------------------------------------------
# Nightly rotating backups
# ---------------------------------------------------------------------------
def make_nightly_backup(state_path: str | None = None) -> str:
    src = state_path or config.active_state_path()
    dst = os.path.join(_ensure_dir(), f"{BACKUP_PREFIX}{_timestamp()}.json")
    _copy_locked(src, dst)
    return dst


def rotate(keep: int | None = None) -> int:
    """Keep the newest ``keep`` rotating backups (config.BACKUP_RETENTION by
    default); delete older ones. Pre-migration snapshots are EXEMPT — they're
    rare, small, and each pins a distinct migration rollback point. Returns the
    number of files deleted."""
    keep = config.BACKUP_RETENTION if keep is None else keep
    d = backups_dir()
    if not os.path.isdir(d):
        return 0
    rotating = sorted(glob.glob(os.path.join(d, f"{BACKUP_PREFIX}*.json")),
                      key=os.path.getmtime, reverse=True)
    deleted = 0
    for old in rotating[keep:]:
        try:
            os.remove(old)
            deleted += 1
        except OSError as e:  # noqa: BLE001 — a stuck delete must not sink the sweep
            logger.error("backup rotation could not delete %s: %s", old, e)
    return deleted


# ---------------------------------------------------------------------------
# Pre-migration snapshots (Task 3)
# ---------------------------------------------------------------------------
def snapshot_before_migration(state_path: str, from_v: int, to_v: int,
                              state: dict | None = None) -> str:
    """Snapshot the pre-migration state to backups/ and return the path. Copies
    the on-disk file (the exact bytes about to be migrated); falls back to
    serializing ``state`` if the file isn't on disk. Raises on failure so the
    caller can ABORT the migration — a migration without a rollback point on
    live data is not acceptable."""
    dst = os.path.join(_ensure_dir(),
                       f"{PREMIGRATION_PREFIX}v{from_v}-to-v{to_v}-{_timestamp()}.json")
    if os.path.exists(state_path):
        _copy_locked(state_path, dst)
    elif state is not None:
        import logging_handler as log  # lazy: import cycle
        log._atomic_write(dst, json.dumps(state, indent=2))
    else:
        raise FileNotFoundError(
            f"cannot snapshot before migration: {state_path} missing and no state given")
    return dst


# ---------------------------------------------------------------------------
# Off-machine copy (email attachment or optional S3)
# ---------------------------------------------------------------------------
def _email_backup(backup_path: str) -> dict | None:
    """Attach the state file to a nightly 'CFM backup' email. If the file
    exceeds config.BACKUP_EMAIL_MAX_BYTES, email a warning INSTEAD of the
    attachment. Returns a report dict, or None when email isn't configured."""
    import smtplib
    if not (os.environ.get("SMTP_HOST") and os.environ.get("ALERT_EMAIL_TO")):
        return None
    size = os.path.getsize(backup_path)
    name = os.path.basename(backup_path)
    msg = EmailMessage()
    msg["From"] = os.environ.get("ALERT_EMAIL_FROM") or os.environ.get("SMTP_USER", "")
    msg["To"] = os.environ["ALERT_EMAIL_TO"]
    over_cap = size > config.BACKUP_EMAIL_MAX_BYTES
    if over_cap:
        msg["Subject"] = f"[CFM backup] WARNING — state too large to attach ({size} bytes)"
        msg.set_content(
            f"Nightly CFM backup {name} is {size} bytes, over the "
            f"{config.BACKUP_EMAIL_MAX_BYTES}-byte email cap. It was saved to the "
            f"volume ({backup_path}) but NOT attached. Configure the S3 off-machine "
            f"upload (CFM_BACKUP_S3) for large states.")
    else:
        msg["Subject"] = f"[CFM backup] {name}"
        msg.set_content(f"Nightly CFM state backup attached: {name} ({size} bytes).")
        with open(backup_path, "rb") as fh:
            msg.add_attachment(fh.read(), maintype="application", subtype="json",
                               filename=name)
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        if user and password:
            smtp.login(user, password)
        smtp.send_message(msg)
    return {"method": "email", "ok": True, "attached": not over_cap, "bytes": size}


def _s3_upload(backup_path: str) -> dict:
    """Optional S3-compatible upload (Tigris/S3/B2). boto3 is imported LAZILY so
    it's never a hard dependency; if the flag is on but boto3 is missing we fail
    with a clear message. Endpoint/bucket/prefix + creds come from env."""
    try:
        import boto3  # noqa: F401 — lazy optional dependency
    except ImportError as e:
        raise RuntimeError(
            "CFM_BACKUP_S3 is on but boto3 is not installed. "
            "`pip install boto3` or turn the flag off.") from e
    import boto3
    # BUCKET_NAME is what `fly storage create` (Tigris) sets automatically;
    # BACKUP_S3_BUCKET wins so a non-Tigris target can still be pointed at.
    bucket = os.environ.get("BACKUP_S3_BUCKET") or os.environ.get("BUCKET_NAME")
    if not bucket:
        raise RuntimeError(
            "CFM_BACKUP_S3 is on but no bucket is set (BACKUP_S3_BUCKET or BUCKET_NAME).")
    endpoint = os.environ.get("BACKUP_S3_ENDPOINT")  # e.g. https://fly.storage.tigris.dev
    prefix = os.environ.get("BACKUP_S3_KEY_PREFIX", "cfm-backups")
    key = f"{prefix.rstrip('/')}/{os.path.basename(backup_path)}"
    client = boto3.client("s3", endpoint_url=endpoint or None)
    client.upload_file(backup_path, bucket, key)
    return {"method": "s3", "ok": True, "bucket": bucket, "key": key,
            "endpoint": endpoint}


def send_offmachine_copy(backup_path: str) -> dict:
    """Ship one copy off the machine. S3 wins if enabled, else email, else none.
    Never raises — returns a report; the caller alerts on ``ok == False``."""
    if config.BACKUP_S3_ENABLED:
        try:
            return _s3_upload(backup_path)
        except Exception as e:  # noqa: BLE001 — reported, not raised
            logger.error("off-machine S3 upload failed: %s", e)
            return {"method": "s3", "ok": False, "error": str(e)}
    try:
        emailed = _email_backup(backup_path)
    except Exception as e:  # noqa: BLE001
        logger.error("off-machine email backup failed: %s", e)
        return {"method": "email", "ok": False, "error": str(e)}
    if emailed is not None:
        return emailed
    return {"method": "none", "ok": False,
            "detail": "no off-machine method configured (set SMTP_* or CFM_BACKUP_S3)"}


def _notify_failure(message: str) -> None:
    """Fire a backup failure as an ops alert through the existing Notifier
    interface (same severity class as data-staleness)."""
    try:
        import notifier
        notifier.dispatch([{
            "severity": "HIGH",
            "type": "BACKUP_FAILURE",
            "ticker": None,
            "message": message,
            "action": "Check the volume and off-machine backup config; see docs/recovery.md.",
        }])
    except Exception as e:  # noqa: BLE001 — alerting must never sink the sweep
        logger.error("could not dispatch backup-failure alert: %s", e)


def nightly_backup() -> dict:
    """Local rotating backup + off-machine copy. Returns a report; on a hard
    failure (local backup or off-machine) also fires an ops alert. Never raises."""
    report: dict = {"local": None, "rotated": 0, "offmachine": None, "errors": []}
    try:
        report["local"] = make_nightly_backup()
        logger.info("nightly backup written: %s", report["local"])
    except Exception as e:  # noqa: BLE001
        report["errors"].append(f"local backup: {e}")
        logger.error("nightly local backup FAILED: %s", e)
        _notify_failure(f"Nightly local state backup failed: {e}")
        return report  # no local copy -> nothing to ship off-machine
    try:
        report["rotated"] = rotate()
    except Exception as e:  # noqa: BLE001
        report["errors"].append(f"rotation: {e}")
        logger.error("backup rotation failed: %s", e)
    off = send_offmachine_copy(report["local"])
    report["offmachine"] = off
    if off.get("ok"):
        logger.info("off-machine backup via %s: ok", off.get("method"))
    else:
        detail = off.get("error") or off.get("detail") or "unknown"
        report["errors"].append(f"off-machine ({off.get('method')}): {detail}")
        logger.error("off-machine backup did not succeed: %s", detail)
        _notify_failure(f"Nightly off-machine backup did not succeed: {detail}")
    return report
