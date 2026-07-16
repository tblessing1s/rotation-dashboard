"""Weekly candidate-universe store — the screened intake list + its append-only
change log + a sector-diversity report.

``universe_screen`` produces the momentum/quality-filtered candidate set from
cached bars; this store persists that set weekly, logs every add/drop (with the
criterion that changed) append-only, and folds the survivors by sector — the
empirical check that one-position-per-sector stops being theoretical.

The current sector universe (``sector_data``) stays the OPERATIVE scan universe;
this candidate list is a SHADOW artifact the operator can review, and the promotion
path (``active_universe``) is gated behind ``config.UNIVERSE_SCREEN_ENABLED``
(default off) so the screen changes nothing until deliberately switched on — with
the current universe as the fallback whenever the candidate list is empty/stale.

DERIVED telemetry under ``DATA_DIR`` — NOT in state.json, single weekly writer,
change log append-only. Best-effort; never raises into the nightly sweep.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

import config

STORE_PATH = os.path.join(config.DATA_DIR, "candidate_universe.json")
_lock = threading.RLock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict:
    try:
        with open(STORE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("candidates"), list):
            data.setdefault("changelog", [])
            data.setdefault("diversity", {})
            return data
    except (OSError, ValueError):
        pass
    return {"date": None, "candidates": [], "changelog": [], "diversity": {}}


def _save(data: dict) -> None:
    tmp = f"{STORE_PATH}.tmp.{os.getpid()}"
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, STORE_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def weekly_due(day: str | None = None) -> bool:
    """True at most once per ISO week (Friday onward), unless a screen has already
    been recorded this ISO week — the same end-of-week cadence gate burn_marks uses,
    so a missed Friday nightly (holiday/restart) still gets caught over the weekend
    without a double run."""
    day = day or _today()
    try:
        d = datetime.strptime(day[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    if d.weekday() < 4:            # Mon–Thu: wait for the end of the week
        return False
    last = _load().get("date")
    if not last:
        return True
    try:
        ld = datetime.strptime(str(last)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return True
    return ld.isocalendar()[:2] != d.isocalendar()[:2]


def current() -> list[str]:
    """The last screened candidate list (empty before the first weekly run)."""
    return list(_load().get("candidates") or [])


def report() -> dict:
    """The current candidate set, its sector-diversity fold, and the recent change
    log (newest first) — the intake dashboard / retrospective read."""
    data = _load()
    return {"date": data.get("date"), "candidates": data.get("candidates") or [],
            "diversity": data.get("diversity") or {},
            "changelog": list(reversed(data.get("changelog") or []))[:200]}


def diversity(candidates: list[str], sector_of) -> dict:
    """Candidates per sector — the empirical one-position-per-sector check.
    ``sector_of`` maps a ticker to its sector ETF (e.g. sector_data.sector_for)."""
    out: dict[str, int] = {}
    for t in candidates:
        sec = sector_of(t) or "—"
        out[sec] = out.get(sec, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: kv[1], reverse=True))


def record(screen_result: dict, sector_of, day: str | None = None,
           max_changelog: int | None = None) -> dict:
    """Persist a weekly screen: replace the candidate list + diversity report, and
    append the add/drop transitions (drops carry the failing criteria) to the
    append-only change log. Best-effort; never raises. Returns {ok, added, dropped}."""
    day = day or _today()
    max_changelog = max_changelog or config.CANDIDATE_UNIVERSE_CHANGELOG_MAX
    try:
        passed = list(screen_result.get("passed") or [])
        results = screen_result.get("results") or {}
        with _lock:
            data = _load()
            prev = set(data.get("candidates") or [])
            now = set(passed)
            added = sorted(now - prev)
            dropped = sorted(prev - now)
            ts = _now_iso()
            import universe_screen
            for t in added:
                data["changelog"].append({"date": day, "ts": ts, "ticker": t,
                                          "action": "added", "criterion": "passed_screen"})
            for t in dropped:
                fails = universe_screen.failing_criteria(results.get(t, {}))
                data["changelog"].append({"date": day, "ts": ts, "ticker": t,
                                          "action": "dropped",
                                          "criterion": ", ".join(fails) or "not_screened"})
            del data["changelog"][:-max_changelog]
            data["date"] = day
            data["candidates"] = sorted(passed)
            data["diversity"] = diversity(passed, sector_of)
            _save(data)
        return {"ok": True, "added": len(added), "dropped": len(dropped),
                "candidates": len(passed)}
    except Exception as e:  # noqa: BLE001 — telemetry must never sink its caller
        return {"ok": False, "error": str(e)}


def active_universe(fallback: list[str]) -> list[str]:
    """The universe the scan should consume: the screened candidates when the
    screen is enabled AND non-empty, else the ``fallback`` (the current
    sector_data universe). Default OFF — the screen changes nothing until
    ``config.UNIVERSE_SCREEN_ENABLED`` is deliberately set. The promotion path."""
    if not config.UNIVERSE_SCREEN_ENABLED:
        return fallback
    cand = current()
    return cand or fallback
