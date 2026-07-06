# Recovery runbook — `state.json`

`state.json` is the single source of truth for the CFM dashboard (positions,
the append-only execution log, and every derived ledger). It lives on the Fly
persistent volume at `/data/state.json`. This page is what to do when it breaks.

## Durability guarantees (how we avoid getting here)

- **Atomic writes.** Every save serializes to a string first, writes a temp file
  in the same directory, `fsync`s it, `os.replace()`s it over the target, then
  `fsync`s the directory. A crash, OOM kill, or deploy restart mid-write can
  never leave a half-written `state.json` — you either have the old file or the
  new one, never a truncated one. (`backend/logging_handler.py::_atomic_write`.)
- **Refuse-to-reinitialize.** On startup the app eagerly loads the store. If the
  file exists but won't parse, the app **refuses to start** rather than silently
  creating empty state over a live record (`StateCorruptError`). It logs CRITICAL
  and names the most recent backup.
- **Nightly rotating backups.** The nightly maintenance job copies `state.json`
  to `/data/backups/state-YYYYMMDD-HHMMSS.json` (30 kept), ships one copy off the
  machine (email attachment or optional S3), and alerts through the Notifier if a
  backup fails.
- **Pre-migration snapshots.** Before any schema migration runs on a loaded file,
  a snapshot is written to `/data/backups/pre-migration-v<from>-to-v<to>-<ts>.json`.
  If that snapshot can't be written, the migration **aborts** and the file is left
  at its old version. These snapshots are kept forever (exempt from rotation).

## "state.json is corrupt — what do I do"

Symptom: the app won't start / crashes on boot with `StateCorruptError`, or the
Fly logs show `state file ... is corrupt/unreadable ... refusing to start`.

1. **Stop the app** so nothing is writing while you work:

   ```
   fly scale count 0 -a rotation-dashboard
   ```

   (state.json is a single-writer store; restoring under a live writer can let
   the app clobber your restore a second later.)

2. **List the backups** and pick one. Newest first, with schema version:

   ```
   fly ssh console -a rotation-dashboard
   cd /app && python scripts/restore_state.py --list
   ```

   Prefer the most recent **nightly** backup. If the corruption was caused by a
   bad migration, restore the matching **pre-migration** snapshot instead (it's
   the exact bytes from before the migration ran).

3. **Restore it.** The restore goes through the atomic save path (never a raw
   copy) and writes the current — possibly corrupt — file aside as
   `state.json.pre-restore.<timestamp>` first, so this step is itself reversible:

   ```
   python scripts/restore_state.py --latest --yes
   # or a specific one:
   python scripts/restore_state.py --restore state-20260102-173000.json --yes
   ```

   `--yes` is required — without it the script refuses and reminds you to stop
   the app. There is no cross-process lock, so **you** are responsible for having
   stopped the app in step 1.

4. **Restart** and confirm:

   ```
   fly scale count 1 -a rotation-dashboard
   fly logs -a rotation-dashboard         # should boot clean, no StateCorruptError
   ```

## Undoing a mistaken restore

Every restore leaves the previous file aside as
`state.json.pre-restore.<timestamp>` next to `state.json`. To undo, restore that
file the same way:

```
python scripts/restore_state.py --restore /data/state.json.pre-restore.<timestamp> --yes
```

## Orphaned temp files

A write that crashed between temp-file creation and rename leaves a
`state.json.tmp.*` file. These are harmless and are cleaned up automatically at
the next startup (logged as a warning). No action needed.

## Off-machine copies

The Fly volume is itself a single point of failure. The nightly job ships one
copy off the machine:

- **Email** (default): if `SMTP_HOST` + `ALERT_EMAIL_TO` are set, the state file
  is attached to a nightly "CFM backup" email. Files over
  `BACKUP_EMAIL_MAX_BYTES` (5 MB) send a warning instead of the attachment.
- **S3-compatible** (optional): set `CFM_BACKUP_S3=1` plus `BACKUP_S3_BUCKET`
  (and optionally `BACKUP_S3_ENDPOINT` for Tigris/B2, `BACKUP_S3_KEY_PREFIX`, and
  `AWS_*` credentials). `boto3` is imported lazily — install it only if you turn
  this on. When on, S3 takes precedence over email.

If neither is configured the nightly job logs that no off-machine method
succeeded. Restoring one of these off-machine copies is a manual step: download
it into `/data/backups/` (or anywhere) and point `restore_state.py --restore` at
it.

## Related runbooks

- [`emergency-exit.md`](emergency-exit.md) — "Schwab is down / the token lapsed
  and the kill switch just fired": exit at the broker directly and reconcile the
  trade back in afterward.
- [`reconciliation.md`](reconciliation.md) — the state-vs-broker check and the
  compensating-adjustment mechanics.
