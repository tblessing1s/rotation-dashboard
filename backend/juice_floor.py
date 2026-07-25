"""Shares-base juice adequacy floor — SHADOW-by-default calibration evaluator (§4.6).

Under a LEAP base the adequacy floor (``config.JUICE_FLOOR_WK``, 1.5%/wk) was
measured against LEAP capital and enforced as a hard SAFETY block. Under a SHARES
base the denominator becomes full share notional — roughly 4-5x larger — so the
same 1.5% number would demand ~73-110% IV to clear at CFM strike depth, colliding
with the golden rule against chasing high juice. The number must be re-set from
real data, not guessed during a refactor.

So this module does NOT retune the floor. It:

  * moves the achieved figure onto the SHARE-NOTIONAL denominator
    (``extrinsic / spot`` — a delta-1.0 base controls one share of notional per
    share owned), and
  * stages the *adequacy* tier in ``SHADOW`` mode: it is evaluated and LOGGED as a
    calibration datapoint but never blocks entry and never affects SCORE.
    ``ENFORCE`` restores the safety-block behaviour.

The HARD floor (``net juice <= 0``) is a separate HARD_CFM_RULE and blocks in BOTH
modes — burn/slippage exceeding income is not a trade at any conviction.

Pure evaluation (``evaluate`` / ``shares_weekly_juice_pct``) + an append-only
calibration log under ``DATA_DIR`` (``record`` / ``series``), modelled on
``scan_rejection_log``. Recording changes NO behaviour and never raises.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

import config

SHADOW = "SHADOW"
ENFORCE = "ENFORCE"

LOG_PATH = os.path.join(config.DATA_DIR, "juice_floor_log.json")
_lock = threading.RLock()


def _mode(mode: str | None) -> str:
    m = (mode or getattr(config, "JUICE_FLOOR_MODE", SHADOW) or SHADOW).upper()
    return m if m in (SHADOW, ENFORCE) else SHADOW


def shares_weekly_juice_pct(weekly_extrinsic_per_share: float | None,
                            spot: float | None) -> float | None:
    """Weekly short-call extrinsic as a percent of SHARE NOTIONAL. A shares base is
    delta 1.0, so notional per share owned is exactly ``spot`` — the achieved figure
    is ``extrinsic / spot``. ``None`` (unpriceable) propagates, never 0."""
    if weekly_extrinsic_per_share is None or not spot:
        return None
    return round(weekly_extrinsic_per_share / spot * 100.0, 4)


def evaluate(symbol: str, *, weekly_extrinsic_per_share: float | None,
             spot: float | None, net_juice_weekly_pct: float | None = None,
             iv: float | None = None, ivr: float | None = None,
             hvr: float | None = None, atr_depth_mult: float | None = None,
             floor_pct: float | None = None, mode: str | None = None) -> dict:
    """Evaluate the shares juice floor for one candidate. Pure — no I/O, no clock.

    Returns the full calibration record (also what ``record`` persists):
      * ``achieved_pct`` — weekly extrinsic / share notional,
      * ``floor_pct`` — the adequacy bar (``config.SHARES_JUICE_FLOOR_PCT``),
      * ``hard_block`` — net juice <= 0 (HARD_CFM_RULE, both modes),
      * ``adequacy_fail`` — achieved < floor,
      * ``blocks`` — whether ENTRY is blocked: ``hard_block`` always, plus
        ``adequacy_fail`` only in ENFORCE,
      * ``tier`` — "hard" | "adequacy" | None (worst binding tier).
    """
    m = _mode(mode)
    floor = config.SHARES_JUICE_FLOOR_PCT if floor_pct is None else floor_pct
    achieved = shares_weekly_juice_pct(weekly_extrinsic_per_share, spot)
    hard_block = net_juice_weekly_pct is not None and net_juice_weekly_pct <= 0
    adequacy_fail = achieved is not None and achieved < floor
    blocks = bool(hard_block or (adequacy_fail and m == ENFORCE))
    tier = "hard" if hard_block else ("adequacy" if adequacy_fail else None)
    return {
        "symbol": (symbol or "").upper(),
        "mode": m,
        "floor_pct": floor,
        "achieved_pct": achieved,
        "net_juice_weekly_pct": net_juice_weekly_pct,
        "iv": iv,
        "ivr": ivr,
        "hvr": hvr,
        "atr_depth_mult": atr_depth_mult,
        "hard_block": bool(hard_block),
        "adequacy_fail": bool(adequacy_fail),
        "adequacy_pass": achieved is None or achieved >= floor,
        "blocks": blocks,
        "tier": tier,
    }


# ---------------------------------------------------------------------------
# Append-only calibration log (DERIVED telemetry — NOT in state.json).
# ---------------------------------------------------------------------------
def _load() -> dict:
    try:
        with open(LOG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("symbols"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"symbols": {}}


def _save(data: dict) -> None:
    tmp = f"{LOG_PATH}.tmp.{os.getpid()}"
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, LOG_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def record(evaluation: dict | None) -> dict:
    """Append one evaluation to the calibration log. Best-effort — a telemetry
    append must never sink the entry evaluation that called it. Idempotency is not
    needed here (each entry evaluation is a distinct datapoint); the per-symbol
    list is capped at ``config.JUICE_FLOOR_LOG_MAX`` newest records."""
    if not isinstance(evaluation, dict) or not evaluation.get("symbol"):
        return {"ok": False, "error": "no symbol"}
    cap = int(getattr(config, "JUICE_FLOOR_LOG_MAX", 500))
    point = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             **evaluation}
    try:
        with _lock:
            data = _load()
            recs = data["symbols"].setdefault(evaluation["symbol"], [])
            recs.append(point)
            del recs[:-cap]
            _save(data)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001 — telemetry must never sink its caller
        return {"ok": False, "error": str(e)}


def series(symbol: str) -> list[dict]:
    """All stored evaluations for one symbol, chronological (oldest first)."""
    return list(_load()["symbols"].get((symbol or "").upper(), []))


def summary() -> dict:
    """Calibration rollup: how often the adequacy floor would have bound, and the
    achieved-pct distribution head — the empirical read that sets the real number."""
    data = _load()["symbols"]
    total = 0
    adequacy_fails = 0
    achieved = []
    for recs in data.values():
        for r in recs:
            total += 1
            if r.get("adequacy_fail"):
                adequacy_fails += 1
            if r.get("achieved_pct") is not None:
                achieved.append(r["achieved_pct"])
    achieved.sort()
    median = achieved[len(achieved) // 2] if achieved else None
    return {
        "records": total,
        "symbols": len(data),
        "adequacy_fail_rate": round(adequacy_fails / total * 100, 1) if total else None,
        "achieved_pct_median": median,
        "floor_pct": config.SHARES_JUICE_FLOOR_PCT,
    }
