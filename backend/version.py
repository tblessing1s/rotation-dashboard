"""Application version metadata — the single place the app reports "what am I?".

The human-facing version is the repo-root ``VERSION`` file (committed, edited by
hand on a release). The exact build is further pinned by the git commit and a
build timestamp, which are baked into the container at build time via the
``APP_GIT_SHA`` / ``APP_BUILD_TIME`` env vars (see the Dockerfile). In a local
checkout — where ``.git`` is present but those env vars are not — they fall back
to reading git directly, so `flask run` also shows a real commit.

Every signal is best-effort: a missing one degrades to ``None`` rather than
raising, so ``/api/version`` can never fail. Resolution is memoized — the version
of a running process doesn't change, so we resolve it once.
"""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache

import config

_FALLBACK_VERSION = "0.0.0-dev"


def _read_version_file() -> str:
    """The semantic version string from the VERSION file (repo root, or alongside
    the backend as a fallback). Returns a dev sentinel when the file is absent."""
    for path in (os.path.join(config.REPO_DIR, "VERSION"),
                 os.path.join(config.BACKEND_DIR, "VERSION")):
        try:
            with open(path, encoding="utf-8") as fh:
                value = fh.read().strip()
            if value:
                return value
        except OSError:
            continue
    return _FALLBACK_VERSION


def _git(*args: str) -> str | None:
    """Run a read-only git command in the repo, or None if git/.git is absent
    (e.g. the container runtime, which has no checkout) or the call fails."""
    try:
        out = subprocess.run(
            ["git", "-C", config.REPO_DIR, *args],
            capture_output=True, text=True, timeout=2,
        )
        value = out.stdout.strip()
        return value or None
    except Exception:  # noqa: BLE001 — git missing / not a repo / timeout
        return None


@lru_cache(maxsize=1)
def info() -> dict:
    """{"version", "commit", "built_at"} for the running build.

    ``commit`` is the short git SHA, ``built_at`` an ISO-8601 timestamp. Both come
    from the build-time env vars first (the deployed container), then fall back to
    live git (a local checkout), then None.
    """
    commit = (os.environ.get("APP_GIT_SHA") or "").strip() or _git("rev-parse", "--short", "HEAD")
    built_at = (os.environ.get("APP_BUILD_TIME") or "").strip() or _git("show", "-s", "--format=%cI", "HEAD")
    return {
        "version": _read_version_file(),
        "commit": commit,
        "built_at": built_at,
    }
