"""Per-name Genius stock lights + right-spot gate + vetoes.

The SAME four Genius lights as the market regime (``genius_lights.compute``),
applied per name — one indicator system, fractal across market and stock. This
module owns only the STOCK-level layers on top of the shared engine:

  * VERDICT mapping (differs from the market vote):
        GREEN  = 4/4 lights green AND no veto
        YELLOW = exactly 3 green AND no veto  -> watchlist, never enterable
        RED    = <= 2 green, OR any veto, OR insufficient history
    No yellow dwell at stock level in v1 (PROPOSED future: a 2-day dwell to
    reduce churn; shadow-log flip frequency first). Evaluation is IDENTICAL for
    stocks and ETFs.

  * RIGHT-SPOT gate — a SEPARATE, blocking gate applied AFTER the lights (the
    consolidation "right spot" checks are NOT lights). Identical for stocks/ETFs.

  * VETOES — evaluated before the vote; any one forces RED.

Lights reuse the SAME params as the market (SMA50 / EMA21 / SAR / ROC10) — no new
per-stock indicator constants. SAR is canonical-start seeded, so a name needs
``config.STOCK_LIGHTS_WARMUP_BARS`` bars before its lights are trusted; inside the
warm-up the vote is insufficient and the verdict is RED (never GREEN), which keeps
fixtures/backfill reproducible.

Pure core: ``compute`` takes frames + scalars and does no I/O. ``evaluate`` gathers
those inputs from the caches (never a fresh provider call) and calls ``compute``.
"""
from __future__ import annotations

import config
import genius_lights
import indicators

GREEN = genius_lights.GREEN
YELLOW = genius_lights.YELLOW
RED = genius_lights.RED

MA200_WINDOW = 200


# ---------------------------------------------------------------------------
# Vetoes (any one -> RED, evaluated before the vote)
# ---------------------------------------------------------------------------
def evaluate_vetoes(df, ivr_percentile: float | None,
                    is_etf: bool) -> list[dict]:
    """The two entry vetoes, each recorded as {id, value, applicable, tripped}
    whether or not it fires (so ``entry_context`` can freeze every evaluation).
    Any ``tripped`` -> the verdict is RED.

      1. atr_expanding AND ivr_percentile >= VETO_IVR_PERCENTILE_MIN  (a volatile
         name into rich IV — the wrong tape for a new CFM entry)
      2. close < ma200        (the trend-is-broken line)

    REMOVED 2026-08-21 (TRAVIS_EXTENSION): the ``rs3m_vs_sector < 0`` veto that
    led this list, along with the ``sector_df`` comparison frame and the
    ``benchmark`` provenance argument that existed ONLY to serve it. A stock's
    strength against its cap-weighted sector ETF is not a peer comparison, so
    the signal was removed system-wide —
    docs/decision-2026-08-21-remove-sector-rs.md.

    Note this leaves NO relative-strength veto at entry: RS3M-vs-SPY was never
    one (the "beats SPY" gate leg had been removed earlier), so the sector veto
    was the only RS check the entry gate had. That is deliberate and recorded.
    """
    out: list[dict] = []

    # 1. atr_expanding AND ivr_percentile >= threshold
    expanding = indicators.atr_expanding(df) if df is not None else None
    out.append({
        "id": "atr_expanding_high_ivr",
        "value": {"atr_expanding": expanding, "ivr_percentile": ivr_percentile,
                  "ivr_min": config.VETO_IVR_PERCENTILE_MIN},
        "applicable": True,
        "tripped": bool(expanding and ivr_percentile is not None
                        and ivr_percentile >= config.VETO_IVR_PERCENTILE_MIN),
    })

    # 2. close < ma200
    close = indicators.last(df) if df is not None else None
    ma200 = indicators.sma(df, MA200_WINDOW) if df is not None else None
    out.append({
        "id": "close_below_ma200",
        "value": {"close": close, "ma200": ma200},
        "applicable": True,
        "tripped": bool(close is not None and ma200 is not None and close < ma200),
    })
    return out


def tripped_vetoes(vetoes: list[dict]) -> list[str]:
    """The ids of the vetoes that fired (empty == clear)."""
    return [v["id"] for v in vetoes if v.get("tripped")]


# ---------------------------------------------------------------------------
# Verdict (stock-level mapping — differs from the market vote + dwell)
#
# ONE rule. The dual-ruleset shadow compute (`config.GATE_RULESET`, legacy vs
# proposed) was deleted with the filter it existed to recalibrate: both rulesets
# only ever differed on the L3 light vote and the L4 right spot, and BOTH of those
# are now RANKING inputs that cannot block. A flag selecting between two ways of
# not-blocking decides nothing, and a scan path with a switchable rule regime is
# exactly the parallel implementation this redesign forbids.
#
# The verdict below is therefore display + RANK provenance only. `stock_greens`
# is what the ranker consumes (`scan_score._lights_sub`); this mapping survives
# because the drawer still shows a colour and the regression fixtures pin it.
# ---------------------------------------------------------------------------
CORE_LIGHT = "close_vs_ma"   # the MANDATORY core light under the proposed ruleset


def core_is_green(lights: dict | None) -> bool:
    """Whether the mandatory core light (close > SMA50) is green. A missing or
    uncomputable core light is NOT green — an unmeasurable core never clears."""
    return ((lights or {}).get(CORE_LIGHT) or {}).get("signal") == GREEN


def verdict(greens: int, insufficient: bool, any_veto: bool) -> str:
    """LEGACY vote rule. GREEN = 4/4 green AND no veto; YELLOW = exactly 3 green
    AND no veto (a watchlist state, never enterable); RED otherwise (<=2 green,
    any veto, or insufficient history — a name whose lights can't all be computed
    is never GREEN or YELLOW).

    UNCHANGED — every existing caller and fixture pins this exact mapping."""
    if any_veto or insufficient:
        return RED
    if greens >= 4:
        return GREEN
    if greens == 3:
        return YELLOW
    return RED


# ---------------------------------------------------------------------------
# Right-spot gate (SEPARATE, after the lights; blocking; stocks == ETFs)
# ---------------------------------------------------------------------------
def _spot_check(cid: str, value, ok) -> dict:
    return {"id": cid, "value": value, "pass": bool(ok)}


def _right_spot_from(atrp, momentum, extension) -> dict:
    """The right-spot read over already-computed indicator values.

    NO LONGER A GATE. These three checks were the Level-4 veto; they are now
    RANKING inputs (`scan_score._extension_sub` / `_atr_sub`) and a failing check
    costs a name rank, never eligibility. The structure is kept — ids, values,
    pass flags — because the drawer renders it and the telemetry records it."""
    checks = [
        _spot_check("atr_pct", atrp,
                    atrp is not None and atrp <= config.CONSOLIDATION_ATR_PCT_MAX),
        # ATR / ATR_5EMA <= the contraction ceiling.
        _spot_check("atr_5d_ema", momentum,
                    momentum is not None and momentum <= config.SPOT_ATR_MOMENTUM_MAX),
        _spot_check("extension", extension,
                    extension is not None and extension <= config.SPOT_ATR_EXTENSION_MAX),
    ]
    blocked_by = [f"spot:{c['id']}" for c in checks if not c["pass"]]
    return {"pass": not blocked_by, "checks": checks, "blocked_by": blocked_by}


def _spot_inputs(df) -> tuple:
    return (indicators.atr_pct(df) if df is not None else None,
            indicators.atr_momentum(df) if df is not None else None,
            indicators.atr_extension(df) if df is not None else None)


def right_spot(df) -> dict:
    """The consolidation "right spot" gate — evaluated AFTER a GREEN light verdict,
    and blocking. NOT a light. Identical for stocks and ETFs. A check with no data
    (None) fails conservatively (you can't confirm a right spot you can't measure).
    ``blocked_by`` lists ``spot:<check>`` reasons; empty == in the right spot.

    """
    return _right_spot_from(*_spot_inputs(df))


# ---------------------------------------------------------------------------
# Pure core — lights + verdict + vetoes + right-spot over frames/scalars
# ---------------------------------------------------------------------------
def compute(df, ivr_percentile: float | None = None,
            is_etf: bool = False, params: dict | None = None) -> dict:
    """The full per-name evaluation over the already-fetched frame + IVR scalar.
    PURE (no I/O). Returns the four lights, the green count, the stock verdict, the
    veto evaluations, and the right-spot gate — everything a caller needs to gate
    and everything ``entry_context`` freezes.

    The four lights, the verdict mapping, the YELLOW lockout and the right-spot
    gate are IDENTICAL for every profile — trend quality is never waived or
    relaxed by the dividend sleeve [HARD_CFM_RULE].

    ``sector_df`` / ``benchmark`` were removed 2026-08-21 with the vs-sector veto
    they existed to serve (docs/decision-2026-08-21-remove-sector-rs.md). Nothing
    in this function reads a peer frame any more.
    """
    engine = genius_lights.compute(df, params=params)
    vetoes = evaluate_vetoes(df, ivr_percentile, is_etf)
    trip = tripped_vetoes(vetoes)
    core = core_is_green(engine["lights"])

    v = verdict(engine["greens"], engine["insufficient"], bool(trip))
    spot = right_spot(df)
    return {
        "lights": engine["lights"],
        "greens": engine["greens"],
        "reds": engine["reds"],
        "insufficient": engine["insufficient"],
        "core_green": core,
        "verdict": v,
        # `enterable` is a DISPLAY read now, not a gate: the light vote and the
        # right spot are ranking inputs and neither can block an entry.
        "enterable": v == GREEN and spot["pass"],
        "vetoes": vetoes,
        "vetoed": bool(trip),
        "veto_reasons": [f"veto:{vid}" for vid in trip],
        "right_spot": spot,
        "is_etf": bool(is_etf),
    }


# ---------------------------------------------------------------------------
# Input-gathering wrapper — reads caches only, never a fresh provider call
# ---------------------------------------------------------------------------
def evaluate(ticker: str, *, df=None, spy_df=None,
             profile: str | None = None) -> dict:
    """Gather this name's cached inputs and run ``compute``. Frames may be passed
    in (the scan already has them warm) or are read from ``data_handler``'s cache.
    IVR percentile comes from the local IV history file (never a provider call).

    ``profile`` (schema v21) no longer selects anything here — it used to pick the
    vs-peer benchmark for the rs3m veto, which was removed 2026-08-21
    (docs/decision-2026-08-21-remove-sector-rs.md). It is still resolved for the
    ``income_profile`` / ``is_sector_etf`` provenance on the returned dict."""
    import data_handler
    import income_profile
    import iv_history
    import sector_data

    ticker = (ticker or "").upper()
    if df is None:
        df = data_handler.get_daily(ticker)

    sector_etf = sector_data.sector_for(ticker)
    is_etf = sector_data.is_etf(ticker)
    peer = income_profile.resolve(ticker, profile, sector_etf)

    try:
        ivr_percentile = (iv_history.iv_rank(ticker) or {}).get("iv_percentile")
    except Exception:  # noqa: BLE001 — IVR is advisory; a missing file just skips the veto
        ivr_percentile = None

    out = compute(df, ivr_percentile=ivr_percentile, is_etf=is_etf)
    out["ticker"] = ticker
    out["sector"] = sector_etf
    out["is_sector_etf"] = peer["is_sector_etf"]
    out["income_profile"] = peer["profile"]
    return out
