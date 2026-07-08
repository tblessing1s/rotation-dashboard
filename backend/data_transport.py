"""Transport / provider-routing layer for the tiered scheduler.

This is the ONLY place the scheduler touches a provider client — the tier/cadence
logic in ``market_scheduler`` stays provider-agnostic, so a provider can be swapped
per tier without touching any scheduling code. Responsibilities:

  * **Batched quotes** — all Tier 0 + Tier 1 quotes due in a cycle go out as ONE
    Schwab batch request (steady-state: one quote call per interval).
  * **Per-tier routing + failover** — Schwab primary, Alpha Vantage fallback,
    cached close as last resort. A Tier 0 name that can't get a primary-provider
    quote surfaces a degraded-data flag; it is NEVER silently continued.
  * **429 / Retry-After backoff** — the Schwab path had none; add bounded
    exponential backoff before falling through to the fallback provider.
  * **Budget accounting** — every provider call is logged to ``data_budget``.
  * **Staleness recording** — genuine live quotes are written to ``data_cache``
    with provider + tier; a cache-fallback is deliberately NOT marked fresh, so the
    staleness layer keeps reporting the gap.
  * **Defense-level derivation** — downside levels for a Tier 0 position, computed
    on demand from cached bars + persisted fields (no state.json schema change).

Provider access goes through the thin ``_schwab_batch`` / ``_av_quote`` /
``_cached_close`` wrappers so tests can inject fixtures without real HTTP.
"""
from __future__ import annotations

import logging
import time

import config
import data_budget
import data_cache
import market_scheduler as ms
from market_scheduler import QUOTE, Tier

logger = logging.getLogger("cfm.transport")


# ---- Provider wrappers (monkeypatched in tests) ----------------------------

def _schwab_batch(symbols: list[str]) -> dict:
    """ONE Schwab multi-symbol quote request. Returns {symbol: parsed-node|None}."""
    import data_handler
    return data_handler.client().get_quotes(symbols)


def _av_quote(symbol: str) -> dict:
    import alpha_vantage
    return alpha_vantage.global_quote(symbol)


def _cached_close(symbol: str) -> float | None:
    import data_handler
    df = data_handler.get_daily(symbol)
    if df is not None and not df.empty:
        return float(df["Close"].iloc[-1])
    return None


def _schwab_configured() -> bool:
    import schwab_api
    return schwab_api.configured()


def _av_configured() -> bool:
    import alpha_vantage
    return alpha_vantage.configured()


def _price_from_node(node) -> float | None:
    if not node:
        return None
    return (node.get("last") or node.get("mark") or node.get("close"))


def _is_rate_limited(exc: Exception) -> bool:
    """Detect a Schwab 429. The client folds the status into the error text (no
    structured code today), so we match on it; a future structured error can also
    carry ``.retry_after`` which the backoff loop honours."""
    return "429" in str(exc)


# ---- Batched quote fetch with backoff + failover ---------------------------

def _schwab_with_backoff(symbols: list[str], rep_tier: Tier, *, sleep, day=None) -> dict:
    """Issue the batched Schwab quote with 429/Retry-After exponential backoff.
    Each HTTP attempt is counted against the budget. Raises the last error if all
    attempts fail (caller then routes to the fallback provider)."""
    delay = config.SCHWAB_BACKOFF_BASE_SECONDS
    last_exc: Exception | None = None
    for attempt in range(config.SCHWAB_MAX_RETRIES):
        data_budget.record("schwab", rep_tier, QUOTE, day=day)
        try:
            return _schwab_batch(symbols)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if not _is_rate_limited(e):
                raise  # non-rate-limit failure: don't burn retries, fall to fallback
            if attempt < config.SCHWAB_MAX_RETRIES - 1:
                retry_after = getattr(e, "retry_after", None)
                wait = min(float(retry_after) if retry_after else delay,
                           config.SCHWAB_BACKOFF_MAX_SECONDS)
                logger.warning("schwab 429; backing off %.1fs (attempt %d/%d)",
                               wait, attempt + 1, config.SCHWAB_MAX_RETRIES)
                sleep(wait)
                delay = min(delay * 2, config.SCHWAB_BACKOFF_MAX_SECONDS)
    assert last_exc is not None
    raise last_exc


def _accept(symbol: str, price: float, source: str, tier: Tier,
            out: dict, fetched_at: float | None) -> None:
    out[symbol] = {"price": float(price), "source": source, "tier": int(tier)}
    # Only genuine live quotes update the staleness store. A cache fallback is
    # returned for display but NOT marked fresh — the staleness layer must keep
    # reporting that we couldn't get a live quote (unknown-fresh blocks action).
    if source in ("schwab", "alphavantage"):
        data_cache.put(symbol, QUOTE, price, source, tier, fetched_at=fetched_at)


def fetch_quotes_batched(symbols_by_tier: dict[str, Tier], *, fetched_at: float | None = None,
                         sleep=time.sleep, day=None) -> dict:
    """Fetch quotes for the Tier 0/1 symbols due this cycle in ONE batched Schwab
    call, falling back per-symbol to Alpha Vantage then the cached close.

    ``symbols_by_tier`` is the already-cadence-filtered due set (fetch_due). Returns
    ``{"quotes": {sym: {price, source, tier}}, "degraded": [...],
       "tier0_degraded": [...], "requested": n, "resolved": n}``. Degradation
    (anything not served live by Schwab) is always surfaced; a degraded Tier 0
    name is additionally logged — Tier 0 freshness is never silently dropped.
    """
    syms = list(dict.fromkeys(s.upper() for s in symbols_by_tier))
    tiers = {s.upper(): Tier(t) for s, t in symbols_by_tier.items()}
    out: dict[str, dict] = {}
    if not syms:
        return {"quotes": out, "degraded": [], "tier0_degraded": [],
                "requested": 0, "resolved": 0}

    remaining = set(syms)
    rep_tier = min(tiers[s] for s in syms)  # the batch exists for the top tier present

    if _schwab_configured():
        try:
            nodes = _schwab_with_backoff(syms, rep_tier, sleep=sleep, day=day)
            for s in syms:
                price = _price_from_node(nodes.get(s))
                if price is not None:
                    _accept(s, price, "schwab", tiers[s], out, fetched_at)
                    remaining.discard(s)
        except Exception as e:  # noqa: BLE001 — degrade to fallbacks, never raise
            logger.warning("schwab batch quote failed after backoff: %s", e)

    # Alpha Vantage fallback, per remaining symbol.
    if remaining and _av_configured():
        for s in list(remaining):
            data_budget.record("alphavantage", tiers[s], QUOTE, day=day)
            try:
                q = _av_quote(s)
                if q and q.get("last"):
                    _accept(s, float(q["last"]), "alphavantage", tiers[s], out, fetched_at)
                    remaining.discard(s)
            except Exception as e:  # noqa: BLE001
                logger.warning("AV quote %s failed: %s", s, e)

    # Cached daily close — last resort, visibly degraded (not marked fresh).
    for s in list(remaining):
        px = _cached_close(s)
        if px is not None:
            _accept(s, px, "cache", tiers[s], out, fetched_at)
            remaining.discard(s)

    degraded, tier0_degraded = [], []
    for s in syms:
        src = out.get(s, {}).get("source")
        if src != "schwab":
            info = {"symbol": s, "tier": int(tiers[s]), "source": src or "unresolved"}
            degraded.append(info)
            if tiers[s] == Tier.T0:
                tier0_degraded.append(info)
                logger.warning("Tier 0 data degraded: %s served from %s (not Schwab) — surfacing",
                               s, src or "NONE")
    return {"quotes": out, "degraded": degraded, "tier0_degraded": tier0_degraded,
            "requested": len(syms), "resolved": len(out)}


# ---- Defense-level derivation (from cached bars + persisted fields) --------

def _atr_mult_for(symbol: str) -> float:
    """Trailing-stop ATR multiplier for a symbol. Defaults to the CFM 1.5x
    (SHORT_ATR_MULT); per-symbol overrides come from an optional config dict so a
    name like APP can run a tighter 1.0x without hardcoding a ticker here."""
    overrides = getattr(config, "DEFENSE_ATR_MULT_OVERRIDES", {}) or {}
    return float(overrides.get(symbol.upper(), config.SHORT_ATR_MULT))


def defense_levels(position: dict, bars_df, atr_mult: float | None = None) -> dict:
    """Downside defense levels for a Tier-0 position — the inputs to a defense
    escalation. Derived on demand (no persisted trailing-stop/consolidation-low
    fields, per the AUDIT decision):

      * ``short_strike``      — the highest open short-call strike (nearest above);
      * ``trailing_stop``     — last close − atr_mult × ATR (from cached bars);
      * ``consolidation_low`` — the recent swing low (min low over MA_WINDOW bars);
      * ``circuit_breaker``   — the persisted line-in-the-sand price.

    Any level that can't be derived is ``None`` and is skipped by the escalation
    check (``breached_defense_levels`` ignores None). Pure given its inputs.
    """
    import indicators
    sym = (position.get("ticker") or "").upper()
    levels: dict[str, float | None] = {
        "short_strike": None, "trailing_stop": None,
        "consolidation_low": None, "circuit_breaker": None,
    }

    strikes = [sc.get("strike") for sc in position.get("short_calls", [])
               if sc.get("strike") is not None]
    if strikes:
        levels["short_strike"] = float(max(strikes))

    cb = (position.get("circuit_breaker") or {}).get("price")
    if cb is not None:
        levels["circuit_breaker"] = float(cb)

    if bars_df is not None and not bars_df.empty:
        last_close = float(bars_df["Close"].iloc[-1])
        a = indicators.atr(bars_df)
        if a is not None:
            mult = atr_mult if atr_mult is not None else _atr_mult_for(sym)
            levels["trailing_stop"] = round(last_close - mult * a, 2)
        window = min(len(bars_df), config.MA_WINDOW)
        if window > 0:
            levels["consolidation_low"] = round(float(bars_df["Low"].iloc[-window:].min()), 2)

    return levels


def intraday_move_pct(cur_price: float | None, bars_df) -> float:
    """Intraday % move of a symbol vs its prior daily close (the last cached bar),
    for the market-escalation check. 0.0 when either input is missing."""
    if cur_price is None or bars_df is None or bars_df.empty:
        return 0.0
    prev_close = float(bars_df["Close"].iloc[-1])
    return ms.index_move_pct(prev_close, cur_price)
