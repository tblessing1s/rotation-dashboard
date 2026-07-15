#!/bin/bash
# SessionStart hook — installs backend + frontend dependencies so tests and the
# app are ready to run the moment a Claude Code on the web session starts.
# Idempotent and non-interactive; safe to re-run.
set -euo pipefail

# Only run in Claude Code on the web (remote) sessions; a no-op locally.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$ROOT"

echo "[session-start] Installing backend Python dependencies…"
# Best-effort: some base images ship a distro-managed pip that can't self-upgrade.
python -m pip install --quiet --upgrade pip || true

# pytest isn't in requirements.txt but the suite needs it. pywebpush pulls in
# http-ece, which needs a C build toolchain and is used only by the optional
# web-push feature (lazily imported, not exercised by the tests). Install the
# full set, but fall back to everything-except-pywebpush so a build failure on
# that one optional package never blocks session startup.
if ! python -m pip install --quiet -r backend/requirements.txt pytest; then
  echo "[session-start] Full install failed (likely pywebpush/http-ece build); installing the test-critical subset."
  grep -viE '^[[:space:]]*(#|pywebpush)' backend/requirements.txt \
    | python -m pip install --quiet -r /dev/stdin pytest
fi

# Frontend deps (enables `npm run build` and UI work). Non-fatal if it hiccups.
if [ -d frontend ] && command -v npm >/dev/null 2>&1; then
  echo "[session-start] Installing frontend dependencies…"
  (cd frontend && npm install --no-audit --no-fund --loglevel=error) \
    || echo "[session-start] npm install failed (non-fatal); frontend build may be unavailable."
fi

# The backend is a flat module layout (tests do `import logging_handler`), so the
# suite runs from inside backend/. Put it on PYTHONPATH so imports resolve from
# any cwd. CLAUDE_ENV_FILE persists this for the whole session.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PYTHONPATH=\"$ROOT/backend:\${PYTHONPATH:-}\"" >> "$CLAUDE_ENV_FILE"
fi

echo "[session-start] Done. Run tests with: python -m pytest backend -q"
