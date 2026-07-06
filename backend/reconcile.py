"""Position reconciliation — does state.json match what Schwab actually holds?

Every guardrail in this app (kill switch, coverage, payback, burn, alerts)
computes off state.json. Nothing else verifies that state.json matches the
brokerage account. This module is that verification.

Design constraint (confirmed by the operator): ALL trading on this account goes
through this app. Any divergence between Schwab and state is therefore an
anomaly — assignment, expiry, partial fill, corporate action, or a bug — never
a "legitimate external trade". There is NO adopt-external-trade flow. The
correct response to a diff is: freeze the position, alert, human resolves.

The core (`reconcile`) is a pure function over a parsed broker view + an
expected view built from state, mirroring the codebase's offline-testable
pattern. The thin fetch wrapper (`run_reconciliation`) pulls the live Schwab
positions, isolates fetch failures (a failed call must NOT masquerade as an
empty account), and hands the parsed view to the core.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import config

logger = logging.getLogger("cfm.alerts")

# ---- Classifications -------------------------------------------------------
MATCH = "MATCH"
MISSING_AT_BROKER = "MISSING_AT_BROKER"
UNEXPECTED_AT_BROKER = "UNEXPECTED_AT_BROKER"
QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
SHORT_STOCK_DETECTED = "SHORT_STOCK_DETECTED"          # highest severity
EXPIRED_WORTHLESS_PENDING = "EXPIRED_WORTHLESS_PENDING"  # benign carve-out

# Diffs of these classes are benign / non-freezing.
BENIGN = {MATCH, EXPIRED_WORTHLESS_PENDING}

# Instrument types.
EQUITY = "EQUITY"
OPTION = "OPTION"
CALL = "CALL"
PUT = "PUT"

# Report status.
CLEAN = "CLEAN"
DIRTY = "DIRTY"


class OptionSymbolParseError(ValueError):
    """A Schwab option symbol did not match the 21-char OCC layout. Raised (not
    silently skipped) so a broker position we can't understand surfaces loudly."""


# ---------------------------------------------------------------------------
# Schwab OCC option-symbol parser
# ---------------------------------------------------------------------------
def parse_option_symbol(symbol: str) -> dict:
    """Parse a 21-char OCC option symbol into its components.

    Layout (the inverse of schwab_api.occ_option_symbol): 6-char root
    (left-justified, space-padded) + YYMMDD + C/P + strike×1000 zero-padded to
    8 digits. e.g. ``"AAPL  260117C00150000"`` -> underlying AAPL, expiry
    2026-01-17, CALL, strike 150.0.

    Raises OptionSymbolParseError on anything that doesn't fit — a malformed
    symbol is a loud failure, never a silent skip.
    """
    if symbol is None:
        raise OptionSymbolParseError("option symbol is None")
    raw = str(symbol)
    # The root is space-padded to 6 chars, so the tail (YYMMDD + C/P + 8 digits
    # = 15 chars) is fixed-width. Total is 21; strip a trailing newline only.
    s = raw.rstrip("\n")
    if len(s) != 21:
        raise OptionSymbolParseError(
            f"option symbol {raw!r} is {len(s)} chars, expected 21 (OCC layout)")
    root = s[:6].strip()
    yy, mm, dd = s[6:8], s[8:10], s[10:12]
    cp = s[12]
    strike_milli = s[13:21]
    if not root:
        raise OptionSymbolParseError(f"option symbol {raw!r} has an empty root")
    if cp not in ("C", "P"):
        raise OptionSymbolParseError(
            f"option symbol {raw!r} has C/P flag {cp!r} (expected 'C' or 'P')")
    if not (yy.isdigit() and mm.isdigit() and dd.isdigit() and strike_milli.isdigit()):
        raise OptionSymbolParseError(
            f"option symbol {raw!r} has non-numeric date/strike fields")
    month, day = int(mm), int(dd)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise OptionSymbolParseError(
            f"option symbol {raw!r} has an out-of-range expiry ({mm}/{dd})")
    expiry = f"20{yy}-{mm}-{dd}"
    strike = int(strike_milli) / 1000.0
    return {
        "underlying": root.upper(),
        "expiry": expiry,
        "put_call": CALL if cp == "C" else PUT,
        "strike": strike,
    }


# ---------------------------------------------------------------------------
# Normalized instrument shape
# ---------------------------------------------------------------------------
def _instrument(symbol, underlying, itype, put_call, strike, expiry, quantity):
    return {
        "symbol": symbol,
        "underlying": (underlying or "").upper(),
        "instrument_type": itype,
        "put_call": put_call,
        "strike": strike,
        "expiry": expiry,
        "quantity": quantity,  # signed: negative = short
    }


def _key(inst: dict):
    """Identity of an instrument for matching broker vs expected. Options key on
    (underlying, C/P, strike, expiry); equity keys on the underlying alone."""
    if inst["instrument_type"] == OPTION:
        return (inst["underlying"], OPTION, inst["put_call"],
                round(float(inst["strike"]), 4) if inst["strike"] is not None else None,
                inst["expiry"])
    return (inst["underlying"], EQUITY)


# ---------------------------------------------------------------------------
# Broker view — parse the Schwab /accounts?fields=positions response
# ---------------------------------------------------------------------------
def normalize_broker_position(node: dict) -> dict | None:
    """One Schwab position node -> normalized instrument, or None to skip
    (unrecognized asset type / zero net quantity). Raises OptionSymbolParseError
    for an OPTION whose symbol can't be parsed — an unreadable broker holding
    must not be silently dropped."""
    inst = (node or {}).get("instrument") or {}
    asset = (inst.get("assetType") or "").upper()
    long_q = float(node.get("longQuantity") or 0)
    short_q = float(node.get("shortQuantity") or 0)
    qty = long_q - short_q  # signed
    if qty == 0:
        return None
    symbol = inst.get("symbol")
    if asset == "OPTION":
        parsed = parse_option_symbol(symbol)
        underlying = (inst.get("underlyingSymbol") or parsed["underlying"]).upper()
        return _instrument(symbol, underlying, OPTION, parsed["put_call"],
                           parsed["strike"], parsed["expiry"], qty)
    if asset in ("EQUITY", "COLLECTIVE_INVESTMENT", "ETF"):
        return _instrument(symbol, (symbol or "").upper(), EQUITY, None, None, None, qty)
    # Unknown/uninteresting asset type (e.g. MUTUAL_FUND, CASH_EQUIVALENT): skip.
    return None


def parse_broker_positions(accounts_response: list) -> list[dict]:
    """Flatten a Schwab /accounts?fields=positions response into normalized
    instruments. Reads the primary account (first node). Positions we can parse
    are returned; OptionSymbolParseError propagates so a malformed broker symbol
    is a loud failure at the fetch layer, not a silent gap."""
    accounts = accounts_response or []
    out: list[dict] = []
    for acct in accounts[:1]:  # primary account only (same one orders use)
        positions = ((acct or {}).get("securitiesAccount") or {}).get("positions") or []
        for node in positions:
            norm = normalize_broker_position(node)
            if norm is not None:
                out.append(norm)
    return out


# ---------------------------------------------------------------------------
# Expected view — build from state.json open positions (live-transmitted only)
# ---------------------------------------------------------------------------
def _ticker_liveness(state: dict, ticker: str) -> bool | None:
    """Was this ticker's position established by LIVE-transmitted orders?

    True  -> its most recent buy_leap was live_transmitted (reconcile it).
    False -> paper/logged (exclude from reconciliation; report-only).
    None  -> can't tell (pre-flag execution with unknown mode) -> exclude + log.
    """
    last = None
    for e in state.get("executions", []):
        if e.get("ticker", "").upper() == ticker.upper() and e.get("action") == "buy_leap":
            last = e
    if last is None:
        return None
    flag = last.get("live_transmitted")
    if flag is True:
        return True
    if flag is False:
        return False
    # Fall back to the raw mode when the explicit flag is absent.
    mode = last.get("mode")
    if mode == "live":
        return True
    if mode == "logged":
        return False
    return None


def expected_view_from_state(state: dict, live_only: bool = True) -> tuple[list[dict], list[dict]]:
    """Normalized expected holdings from open positions.

    LEAPs are long calls (positive qty = contracts), short calls negative, held
    shares as EQUITY. When ``live_only`` (production default), only positions
    established by live-transmitted orders are included — paper positions won't
    exist at the broker, so reconciling them would mass-flag them. Returns
    (instruments, excluded) where ``excluded`` records report-only/unknown
    positions with a reason (logged by the caller).
    """
    out: list[dict] = []
    excluded: list[dict] = []
    for p in state.get("positions", []):
        if p.get("status") == "closed":
            continue
        ticker = (p.get("ticker") or "").upper()
        if not ticker:
            continue
        if live_only:
            live = _ticker_liveness(state, ticker)
            if live is False:
                excluded.append({"ticker": ticker, "reason": "paper"})
                continue
            if live is None:
                excluded.append({"ticker": ticker, "reason": "unknown_live_status"})
                continue

        leap = p.get("leap") or {}
        contracts = int(leap.get("contracts") or 0)
        if leap and contracts:
            out.append(_instrument(
                None, ticker, OPTION, CALL, leap.get("strike"),
                _norm_date(leap.get("expiration")), contracts))

        for sc in p.get("short_calls") or []:
            n = int(sc.get("contracts") or 0)
            if not n:
                continue
            out.append(_instrument(
                None, ticker, OPTION, CALL, sc.get("strike"),
                _norm_date(sc.get("expiration")), -n))

        shares = p.get("shares") or {}
        count = int(shares.get("count") or 0)
        if count:
            out.append(_instrument(None, ticker, EQUITY, None, None, None, count))
    return out, excluded


def _norm_date(v) -> str | None:
    if not v:
        return None
    return str(v)[:10]


# ---------------------------------------------------------------------------
# Expiry close lookup (cached OHLCV only — no new provider calls)
# ---------------------------------------------------------------------------
def cached_close_on(ticker: str, date_str: str) -> float | None:
    """Underlying's cached close on ``date_str`` (YYYY-MM-DD), or None if the
    cache has no bar for that day. Reads the parquet cache DIRECTLY (never
    fetches) so the expiry carve-out honours 'no new provider calls'."""
    import pandas as pd

    import data_handler
    df = data_handler._read_cache((ticker or "").upper())
    if df is None or getattr(df, "empty", True):
        return None
    try:
        ts = pd.Timestamp(str(date_str)[:10])
    except (ValueError, TypeError):
        return None
    try:
        idx = df.index.normalize()
        rows = df[idx == ts]
        if rows.empty:
            return None
        return float(rows["Close"].iloc[-1])
    except (KeyError, ValueError, TypeError):
        return None


def _in_past(date_str: str | None, today) -> bool:
    if not date_str:
        return False
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return d < today


# ---------------------------------------------------------------------------
# Core classification — pure over (broker_view, expected_view)
# ---------------------------------------------------------------------------
def _diff(idx, classification, inst, expected_qty, broker_qty, summary, **extra):
    d = {
        "id": f"diff_{idx:03d}",
        "classification": classification,
        "ticker": inst["underlying"],
        "instrument_type": inst["instrument_type"],
        "put_call": inst["put_call"],
        "strike": inst["strike"],
        "expiry": inst["expiry"],
        "symbol": inst.get("symbol"),
        "expected_qty": expected_qty,
        "broker_qty": broker_qty,
        "summary": summary,
    }
    d.update(extra)
    return d


def _fmt_inst(inst: dict) -> str:
    if inst["instrument_type"] == OPTION:
        cp = "call" if inst["put_call"] == CALL else "put"
        exp = inst["expiry"] or "?"
        return f"{inst['underlying']} {inst['strike']} {cp} exp {exp}"
    return f"{inst['underlying']} shares"


def reconcile(broker_view: list[dict], expected_view: list[dict], as_of: str,
              close_on_expiry=None, today=None) -> dict:
    """Classify every instrument into exactly one diff class and build a report.

    ``broker_view`` / ``expected_view`` are normalized instrument lists.
    ``close_on_expiry(ticker, date_str) -> float | None`` supplies the cached
    close for the expiry carve-out (injected so the core stays offline). A pure
    function: no I/O, no state mutation.
    """
    if close_on_expiry is None:
        close_on_expiry = cached_close_on
    if today is None:
        today = datetime.now(timezone.utc).date()

    # Underlyings where state holds a LEAP (a long call) — the SHORT_STOCK_DETECTED
    # trigger set.
    leap_underlyings = {i["underlying"] for i in expected_view
                        if i["instrument_type"] == OPTION and i["put_call"] == CALL
                        and (i["quantity"] or 0) > 0}

    broker_by_key = {_key(i): i for i in broker_view}
    expected_by_key = {_key(i): i for i in expected_view}
    all_keys = list(dict.fromkeys(list(expected_by_key) + list(broker_by_key)))

    diffs: list[dict] = []
    idx = 1
    for k in all_keys:
        exp = expected_by_key.get(k)
        brk = broker_by_key.get(k)
        exp_qty = exp["quantity"] if exp else None
        brk_qty = brk["quantity"] if brk else None
        inst = exp or brk

        if exp and brk:
            if float(exp_qty) == float(brk_qty):
                continue  # MATCH — no diff recorded
            diffs.append(_diff(
                idx, QUANTITY_MISMATCH, inst, exp_qty, brk_qty,
                f"{_fmt_inst(inst)}: state expects {exp_qty}, broker holds {brk_qty}."))
            idx += 1
            continue

        if exp and not brk:
            diff = _classify_missing(idx, exp, close_on_expiry, today)
            diffs.append(diff)
            idx += 1
            continue

        # brk and not exp -> unexpected at broker.
        is_short_stock = (brk["instrument_type"] == EQUITY and float(brk_qty) < 0
                          and brk["underlying"] in leap_underlyings)
        if is_short_stock:
            diffs.append(_diff(
                idx, SHORT_STOCK_DETECTED, brk, None, brk_qty,
                (f"SHORT STOCK detected: broker holds {brk_qty} shares of "
                 f"{brk['underlying']} against an open LEAP — assignment likely occurred.")))
        else:
            diffs.append(_diff(
                idx, UNEXPECTED_AT_BROKER, brk, None, brk_qty,
                f"{_fmt_inst(brk)}: broker holds {brk_qty}, state does not expect it."))
        idx += 1

    suggested = _suggest_resolutions(diffs)
    non_benign = [d for d in diffs if d["classification"] not in BENIGN]
    report = {
        "as_of": as_of,
        "status": DIRTY if non_benign else CLEAN,
        "diffs": diffs,
        "suggested_resolutions": suggested,
        "counts": _counts(diffs),
        "broker_ok": True,
        "error": None,
    }
    return report


def _classify_missing(idx, exp, close_on_expiry, today) -> dict:
    """A state-expected instrument the broker doesn't hold. Short calls get the
    expiry carve-out; a long call (LEAP) or equity is always MISSING_AT_BROKER."""
    is_short_call = (exp["instrument_type"] == OPTION and exp["put_call"] == CALL
                     and float(exp["quantity"]) < 0)
    if is_short_call and _in_past(exp["expiry"], today):
        strike = exp["strike"]
        close = None
        try:
            close = close_on_expiry(exp["underlying"], exp["expiry"])
        except Exception:  # noqa: BLE001 — a lookup failure must never crash the run
            close = None
        if close is not None and strike is not None:
            if float(close) < float(strike):
                return _diff(
                    idx, EXPIRED_WORTHLESS_PENDING, exp, exp["quantity"], None,
                    (f"{_fmt_inst(exp)} expired worthless (close {close:.2f} < strike "
                     f"{strike}) — book it at $0."),
                    expiry_close=round(float(close), 2))
            # At/above strike on expiry -> assignment is the likely cause.
            return _diff(
                idx, MISSING_AT_BROKER, exp, exp["quantity"], None,
                (f"{_fmt_inst(exp)} gone at broker; close {close:.2f} ≥ strike {strike} on "
                 f"expiry — assignment suspected."),
                assignment_suspected=True, expiry_close=round(float(close), 2))
        # Past expiry but no cached close for that day -> never silently benign.
        return _diff(
            idx, MISSING_AT_BROKER, exp, exp["quantity"], None,
            (f"{_fmt_inst(exp)} gone at broker and past expiry, but no cached close for "
             f"{exp['expiry']} — cannot confirm worthless; treat as missing."),
            assignment_suspected=True, expiry_close=None)

    # Long call (LEAP), equity, or a not-yet-expired short: plain missing.
    extra = {}
    label = "MISSING at broker"
    if exp["instrument_type"] == OPTION and float(exp["quantity"]) > 0:
        label = "LEAP MISSING at broker"  # should never happen under the lifecycle policy
    return _diff(
        idx, MISSING_AT_BROKER, exp, exp["quantity"], None,
        f"{_fmt_inst(exp)}: state expects {exp['quantity']}, broker does not hold it ({label}).",
        **extra)


def _suggest_resolutions(diffs: list[dict]) -> list[dict]:
    """One suggested resolution per non-benign diff, plus the one-click expiry
    path for the benign carve-out."""
    out = []
    for d in diffs:
        cls = d["classification"]
        if cls == EXPIRED_WORTHLESS_PENDING:
            out.append({
                "diff_id": d["id"], "kind": "resolve_expiry", "ticker": d["ticker"],
                "strike": d["strike"], "expiry": d["expiry"],
                "contracts": abs(int(d["expected_qty"] or 0)),
                "label": f"Book {d['ticker']} {d['strike']} short expiry at $0.00 (worthless)."})
        elif cls == SHORT_STOCK_DETECTED:
            out.append({
                "diff_id": d["id"], "kind": "resolve_with_adjustment", "ticker": d["ticker"],
                "instrument_type": EQUITY, "quantity_delta": d["broker_qty"],
                "label": ("Record the assignment: buy back the short stock or close the "
                          "position, then log a compensating adjustment. Do NOT exercise the LEAP.")})
        elif cls in (MISSING_AT_BROKER, UNEXPECTED_AT_BROKER, QUANTITY_MISMATCH):
            out.append({
                "diff_id": d["id"], "kind": "resolve_with_adjustment", "ticker": d["ticker"],
                "instrument_type": d["instrument_type"],
                "label": f"Resolve {d['ticker']} {cls} with a compensating adjustment or acknowledge."})
    return out


def _counts(diffs: list[dict]) -> dict:
    out: dict[str, int] = {}
    for d in diffs:
        out[d["classification"]] = out.get(d["classification"], 0) + 1
    return out


# ---------------------------------------------------------------------------
# Fetch wrapper + persistence
# ---------------------------------------------------------------------------
def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def failure_report(as_of: str, error: str) -> dict:
    """A run where the broker fetch FAILED. No diffs are generated — a failed
    call must never masquerade as an empty account (which would mass-classify
    everything as MISSING_AT_BROKER). Feeds the reconcile_stale clock via the
    absence of a successful run."""
    return {
        "as_of": as_of,
        "status": "FAILED",
        "diffs": [],
        "suggested_resolutions": [],
        "counts": {},
        "broker_ok": False,
        "error": error,
    }


def run_reconciliation(state: dict | None = None, persist: bool = True) -> dict:
    """Fetch live Schwab positions, reconcile against state, and (by default)
    persist the report + apply freezes. Returns the ReconcileReport.

    Distinguishes 'broker returned zero positions' (a valid all-cash response ->
    a normal CLEAN/DIRTY run) from 'the call failed' (a failure report that does
    NOT touch position freezes and leaves the stale clock running).
    """
    import logging_handler as log
    import schwab_api

    owns_state = state is None
    state = state if state is not None else log.load_state()
    as_of = _utcnow()

    # Demo mode: reconcile the whole demo book against a synthetic broker fixture
    # (report-only; no live Schwab call, no freezes on the demo store unless the
    # fixture is deliberately divergent). Paper positions are included here.
    demo = config.demo_enabled()

    # Fetch — a failure here yields a failure report, never an empty broker view.
    try:
        accounts = _demo_broker_accounts() if demo else data_handler_client_accounts()
    except Exception as e:  # noqa: BLE001 — isolate the fetch failure
        report = failure_report(as_of, str(e))
        if persist:
            _persist_report(state, report)
            if owns_state:
                log.save_state(state)
        logger.warning("reconciliation fetch failed: %s", e)
        return report

    try:
        broker_view = parse_broker_positions(accounts)
    except OptionSymbolParseError as e:
        report = failure_report(as_of, f"unparseable broker option symbol: {e}")
        if persist:
            _persist_report(state, report)
            if owns_state:
                log.save_state(state)
        logger.error("reconciliation parse failed: %s", e)
        return report

    expected_view, excluded = expected_view_from_state(state, live_only=not demo)
    for ex in excluded:
        if ex["reason"] == "unknown_live_status":
            logger.info("reconcile: excluding %s (unknown live-transmission status)", ex["ticker"])

    report = reconcile(broker_view, expected_view, as_of)
    report["excluded"] = excluded
    if persist:
        _persist_report(state, report)
        apply_report_to_state(state, report)
        if owns_state:
            log.save_state(state)
    return report


def data_handler_client_accounts() -> list:
    """Live Schwab positions call, isolated in one function so tests can monkeypatch
    it. Uses the shared client; a read-only account call (no CFM_LIVE_TRADING
    needed)."""
    import data_handler
    return data_handler.client().get_accounts(positions=True)


def _demo_broker_accounts() -> list:
    """Synthetic Schwab /accounts?fields=positions payload for demo mode, written
    into the demo cache by seed_demo_data. Shaped exactly like the real response
    so it exercises the same parser + classifier path."""
    import json
    import os

    path = os.path.join(config.active_cache_dir(), "broker_positions.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        # No fixture seeded -> an all-cash broker (valid zero-positions response),
        # NOT a failure. The demo book will then read as MISSING_AT_BROKER, which
        # is itself a useful demonstration.
        return [{"securitiesAccount": {"positions": []}}]


def _persist_report(state: dict, report: dict) -> None:
    """Store the report as the last reconciliation + append to the capped history."""
    recon = state.setdefault("reconciliation", {"last": None, "history": []})
    recon["last"] = report
    if report.get("broker_ok"):
        recon["last_success"] = report["as_of"]
    hist = recon.setdefault("history", [])
    hist.append({k: report[k] for k in ("as_of", "status", "counts", "broker_ok", "error")})
    del hist[:-config.RECONCILE_HISTORY_MAX]


def apply_report_to_state(state: dict, report: dict) -> None:
    """Freeze positions touched by an OPEN non-benign diff (needs_review=true) and
    clear the flag on positions whose diffs have all resolved. Benign diffs
    (EXPIRED_WORTHLESS_PENDING) do NOT freeze — they surface as a one-click
    suggestion. A failed run touches nothing (the freezes from the prior good run
    stand)."""
    if not report.get("broker_ok"):
        return
    reevaluate_freezes(state)


# ---------------------------------------------------------------------------
# Resolution + freeze bookkeeping (called from the executor's resolve paths)
# ---------------------------------------------------------------------------
def _diff_open(d: dict) -> bool:
    """A diff still holds a freeze iff it is non-benign and unresolved/unacked."""
    if d.get("classification") in BENIGN:
        return False
    return not (d.get("resolution") or {}).get("status")


def reevaluate_freezes(state: dict) -> None:
    """Recompute needs_review on every open position from the latest report's
    still-open diffs. Freezes a position with any open non-benign diff; lifts the
    freeze once its diffs are all resolved/acknowledged."""
    report = (state.get("reconciliation") or {}).get("last") or {}
    open_by_ticker: dict[str, list[dict]] = {}
    for d in report.get("diffs", []):
        if _diff_open(d):
            open_by_ticker.setdefault(d["ticker"], []).append(d)
    for p in state.get("positions", []):
        # A closed position can never be under review — clear any stale freeze
        # left over from when it was open. Without this, resolving a MISSING diff
        # by adjusting the position closed leaves needs_review stuck True forever
        # (the diff is gone, but the flag that blocks new entries never lifts).
        if p.get("status") == "closed":
            if p.get("needs_review"):
                p["needs_review"] = False
                p["review"] = None
            continue
        ticker = (p.get("ticker") or "").upper()
        remaining = open_by_ticker.get(ticker)
        if remaining:
            p["needs_review"] = True
            p["review"] = {
                "since": report.get("as_of"),
                "diff_ids": [d["id"] for d in remaining],
                "summary": "; ".join(d["summary"] for d in remaining),
                "classifications": sorted({d["classification"] for d in remaining}),
            }
        elif p.get("needs_review"):
            p["needs_review"] = False
            p["review"] = None


def _find_diff(state: dict, diff_id: str):
    report = (state.get("reconciliation") or {}).get("last") or {}
    for d in report.get("diffs", []):
        if d.get("id") == diff_id:
            return report, d
    return None, None


def mark_diff_resolved(state: dict, diff_id: str, how: str, detail: dict | None = None) -> dict:
    """Mark a diff resolved by a compensating execution (expiry booking /
    adjustment) and re-evaluate freezes. Raises if the diff isn't in the latest
    report."""
    _report, d = _find_diff(state, diff_id)
    if d is None:
        raise ValueError(f"unknown diff id {diff_id!r} in the latest reconciliation report")
    d["resolution"] = {"status": "resolved", "how": how, "at": _utcnow(), "detail": detail}
    reevaluate_freezes(state)
    return d


def ack_diff(state: dict, diff_id: str, ack_reason: str) -> dict:
    """Acknowledge a diff the operator deems a non-issue. Requires a typed
    ack_reason, logged onto the reconciliation record; re-evaluates freezes."""
    reason = (ack_reason or "").strip()
    if not reason:
        raise ValueError("acknowledging a diff requires a typed ack_reason")
    _report, d = _find_diff(state, diff_id)
    if d is None:
        raise ValueError(f"unknown diff id {diff_id!r} in the latest reconciliation report")
    d["resolution"] = {"status": "acknowledged", "ack_reason": reason, "at": _utcnow()}
    reevaluate_freezes(state)
    return d
