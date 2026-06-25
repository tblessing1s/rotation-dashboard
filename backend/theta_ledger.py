"""Mark-to-market the theta ledger: track how each option's extrinsic decays.

The entry split (intrinsic/extrinsic at fill) is captured by option_trades. This
module is the *ongoing* half — it re-quotes open positions and records a dated
mark, so the ledger can show real time-value decay (theta bled to date and
day-over-day) instead of just the intrinsic shift the underlying has produced.

Two halves, matching the dashboard's split between fetch and read:

  * ``refresh`` contacts Schwab — ONE batched quotes call for every open option
    (and its underlying) — and stores today's mark. This is the only provider
    hit, and it is user-triggered (a refresh button), never on the request path.
  * ``enrich`` is pure/read-only: it reads the stored marks and computes the P&L
    fields, so GET /fills never touches a provider.
"""
from __future__ import annotations

from datetime import date

import db
from options_math import decompose
from providers.base import ProviderError
from providers.schwab import SchwabProvider

OPTION_MULTIPLIER = 100  # one US equity-option contract controls 100 shares.


def available() -> bool:
    return SchwabProvider.configured()


def _is_open(fill: dict, today: str) -> bool:
    """A fill is still trackable while its contract is unexpired."""
    expiry = str(fill.get("expiry") or "")[:10]
    return bool(expiry) and expiry >= today


def _pnl(fill: dict, latest: dict, prior: dict | None) -> dict:
    """Per-position theta metrics from the entry split + the latest/prior marks.

    `bled` is extrinsic lost since entry (a cost for a long option, income for a
    short — the UI signs it via side). `day` is the bleed vs. the prior stored
    day. `optionPnl` is the whole position's mark-to-market in dollars.
    """
    qty = int(fill.get("quantity") or 0)
    mult = qty * OPTION_MULTIPLIER
    side_sign = -1 if str(fill.get("side")).lower() == "sell" else 1

    ext_entry = fill.get("extrinsic")
    ext_now = latest.get("extrinsic")
    mark_now = latest.get("mark")
    premium = fill.get("premium")

    def _r(v, n=2):
        return round(v, n) if isinstance(v, (int, float)) else None

    bled_ps = (ext_entry - ext_now) if (ext_entry is not None and ext_now is not None) else None
    day_ps = (
        prior.get("extrinsic") - ext_now
        if (prior and prior.get("extrinsic") is not None and ext_now is not None)
        else None
    )
    opt_pnl = ((mark_now - premium) * mult * side_sign) if (mark_now is not None and premium is not None) else None

    return {
        "asOf": latest.get("as_of_date"),
        "multiplier": mult,
        "markNow": _r(mark_now),
        "extrinsicEntry": _r(ext_entry),
        "extrinsicNow": _r(ext_now),
        "bledPerShare": _r(bled_ps),
        "bledDollars": _r(bled_ps * mult) if bled_ps is not None else None,
        "dayPerShare": _r(day_ps),
        "dayDollars": _r(day_ps * mult) if day_ps is not None else None,
        "optionPnlDollars": _r(opt_pnl),
        "thetaGreek": _r(latest.get("theta"), 4),
        "sideSign": side_sign,
    }


def enrich(fills: list[dict]) -> list[dict]:
    """Attach the latest stored mark + theta P&L to each fill. Read-only."""
    out = []
    for f in fills:
        item = dict(f)
        latest = db.latest_option_mark(f["id"])
        if latest:
            prior = db.prior_option_mark(f["id"], latest["as_of_date"])
            item["mark"] = latest
            item["thetaPnl"] = _pnl(f, latest, prior)
        else:
            item["mark"] = None
            item["thetaPnl"] = None
        out.append(item)
    return out


def refresh(*, as_of: str | None = None, underlying: str | None = None) -> dict:
    """Re-quote every open option, store today's mark, return the enriched ledger.

    One batched Schwab quotes call covers all open OSI symbols and their
    underlyings. The option node usually carries ``underlyingPrice`` directly;
    the underlying quote is the fallback when it doesn't. Returns
    ``{ok, refreshed, missing, asOf, ledger}``.
    """
    if not SchwabProvider.configured():
        return {"ok": False, "error": "Schwab credentials are not set (SCHWAB_APP_KEY / SECRET / REFRESH_TOKEN)."}

    today = as_of or date.today().isoformat()
    all_fills = db.list_option_fills(underlying=underlying, limit=500)
    open_fills = [f for f in all_fills if _is_open(f, today)]
    if not open_fills:
        return {"ok": True, "refreshed": 0, "missing": [], "asOf": today, "ledger": enrich(all_fills)}

    # One batch: every open option symbol plus every distinct underlying.
    symbols = list({f["osi_symbol"] for f in open_fills} | {f["underlying"] for f in open_fills})
    provider = SchwabProvider()
    try:
        quotes = provider.get_quotes(symbols)
    except ProviderError as e:
        return {"ok": False, "error": str(e)}

    refreshed, missing = 0, []
    for f in open_fills:
        oq = quotes.get(f["osi_symbol"])
        if not oq:
            missing.append(f["osi_symbol"])
            continue
        mark = oq.get("mark") if oq.get("mark") is not None else oq.get("last")
        if mark is None:
            missing.append(f["osi_symbol"])
            continue
        # Prefer the underlyingPrice baked into the option quote; fall back to
        # the underlying's own quote from the same batch.
        stock = oq.get("underlyingPrice")
        if stock is None:
            uq = quotes.get(f["underlying"]) or {}
            stock = uq.get("last") if uq.get("last") is not None else uq.get("mark")

        intrinsic = extrinsic = None
        if stock is not None:
            split = decompose(f["option_type"], f["strike"], stock, mark)
            intrinsic, extrinsic = split["intrinsic"], split["extrinsic"]

        db.record_option_mark({
            "fill_id": f["id"], "as_of_date": today, "mark": mark,
            "stock_price": stock, "intrinsic": intrinsic, "extrinsic": extrinsic,
            "theta": oq.get("theta"), "source": "schwab",
        })
        refreshed += 1

    return {"ok": True, "refreshed": refreshed, "missing": missing, "asOf": today,
            "ledger": enrich(all_fills)}
