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
def evaluate_vetoes(df, sector_df, ivr_percentile: float | None,
                    is_etf: bool, benchmark: str | None = None) -> list[dict]:
    """The three entry vetoes, each recorded as {id, value, applicable, tripped}
    whether or not it fires (so ``entry_context`` can freeze every evaluation).
    Any ``tripped`` -> the verdict is RED.

      1. rs3m_vs_sector < 0   (stocks only — an ETF has no growth-leader peer
         sector to beat; same waiver as the kill switch)
      2. atr_expanding AND ivr_percentile >= VETO_IVR_PERCENTILE_MIN  (a volatile
         name into rich IV — the wrong tape for a new CFM entry)
      3. close < ma200        (the trend-is-broken line)

    ``sector_df`` is the COMPARISON frame for veto 1, whatever the caller decided
    that should be — the name's sector ETF for a JUICE_ENGINE candidate, or the
    dividend-peer benchmark for a DIVIDEND_COMPOUNDER (schema v21). This function
    does not resolve it; ``benchmark`` is carried purely as PROVENANCE so a frozen
    entry snapshot records which benchmark actually gated the entry and a variant
    change can never be silent. The RULE is unchanged for both profiles — the
    comparison must still be beaten, only the peer group differs.
    """
    out: list[dict] = []

    # 1. rs3m_vs_sector < 0 (stocks only)
    rs_sec = None
    applicable = bool(not is_etf and df is not None and sector_df is not None)
    if applicable:
        rs_sec = indicators.rs3m(df, sector_df)
    out.append({
        "id": "rs3m_vs_sector",
        "value": rs_sec,
        "applicable": applicable,
        "tripped": bool(applicable and rs_sec is not None and rs_sec < 0),
        "benchmark": benchmark,
    })

    # 2. atr_expanding AND ivr_percentile >= threshold
    expanding = indicators.atr_expanding(df) if df is not None else None
    out.append({
        "id": "atr_expanding_high_ivr",
        "value": {"atr_expanding": expanding, "ivr_percentile": ivr_percentile,
                  "ivr_min": config.VETO_IVR_PERCENTILE_MIN},
        "applicable": True,
        "tripped": bool(expanding and ivr_percentile is not None
                        and ivr_percentile >= config.VETO_IVR_PERCENTILE_MIN),
    })

    # 3. close < ma200
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
# ---------------------------------------------------------------------------
def verdict(greens: int, insufficient: bool, any_veto: bool) -> str:
    """GREEN = 4/4 green AND no veto; YELLOW = exactly 3 green AND no veto (a
    watchlist state, never enterable); RED otherwise (<=2 green, any veto, or
    insufficient history — a name whose lights can't all be computed is never
    GREEN or YELLOW)."""
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


def right_spot(df) -> dict:
    """The consolidation "right spot" gate — evaluated AFTER a GREEN light verdict,
    and blocking. NOT a light. Identical for stocks and ETFs. A check with no data
    (None) fails conservatively (you can't confirm a right spot you can't measure).
    ``blocked_by`` lists ``spot:<check>`` reasons; empty == in the right spot."""
    atrp = indicators.atr_pct(df) if df is not None else None
    momentum = indicators.atr_momentum(df) if df is not None else None
    extension = indicators.atr_extension(df) if df is not None else None

    checks = [
        _spot_check("atr_pct", atrp,
                    atrp is not None and atrp <= config.CONSOLIDATION_ATR_PCT_MAX),
        # ATR / ATR_5EMA <= 1 (or the configured max) = contracting or flat.
        _spot_check("atr_5d_ema", momentum,
                    momentum is not None and momentum <= config.SPOT_ATR_MOMENTUM_MAX),
        _spot_check("extension", extension,
                    extension is not None and extension <= config.SPOT_ATR_EXTENSION_MAX),
    ]
    blocked_by = [f"spot:{c['id']}" for c in checks if not c["pass"]]
    return {"pass": not blocked_by, "checks": checks, "blocked_by": blocked_by}


# ---------------------------------------------------------------------------
# Pure core — lights + verdict + vetoes + right-spot over frames/scalars
# ---------------------------------------------------------------------------
def compute(df, sector_df=None, ivr_percentile: float | None = None,
            is_etf: bool = False, params: dict | None = None,
            benchmark: str | None = None) -> dict:
    """The full per-name evaluation over already-fetched frames + IVR scalar.
    PURE (no I/O). Returns the four lights, the green count, the stock verdict, the
    veto evaluations, and the right-spot gate — everything a caller needs to gate
    and everything ``entry_context`` freezes.

    ``benchmark`` is the symbol whose frame the caller passed as ``sector_df``,
    recorded as provenance only (schema v21). The four lights, the verdict mapping,
    the YELLOW lockout and the right-spot gate are IDENTICAL for every profile —
    trend quality is never waived or relaxed by the dividend sleeve [HARD_CFM_RULE].
    """
    engine = genius_lights.compute(df, params=params)
    vetoes = evaluate_vetoes(df, sector_df, ivr_percentile, is_etf, benchmark=benchmark)
    trip = tripped_vetoes(vetoes)
    v = verdict(engine["greens"], engine["insufficient"], bool(trip))
    spot = right_spot(df)
    return {
        "lights": engine["lights"],
        "greens": engine["greens"],
        "reds": engine["reds"],
        "insufficient": engine["insufficient"],
        "verdict": v,
        "enterable": v == GREEN and spot["pass"],   # YELLOW is never enterable
        "vetoes": vetoes,
        "vetoed": bool(trip),
        "veto_reasons": [f"veto:{vid}" for vid in trip],
        "right_spot": spot,
        "is_etf": bool(is_etf),
        "benchmark": benchmark,
    }


# ---------------------------------------------------------------------------
# Input-gathering wrapper — reads caches only, never a fresh provider call
# ---------------------------------------------------------------------------
def evaluate(ticker: str, *, df=None, spy_df=None, sector_df=None,
             profile: str | None = None) -> dict:
    """Gather this name's cached inputs and run ``compute``. Frames may be passed
    in (the scan already has them warm) or are read from ``data_handler``'s cache.
    IVR percentile comes from the local IV history file (never a provider call).

    ``profile`` (schema v21) selects the vs-peer BENCHMARK for the rs3m veto: the
    name's sector ETF for JUICE_ENGINE, the dividend-peer benchmark for a
    DIVIDEND_COMPOUNDER. Nothing else about the evaluation changes."""
    import data_handler
    import income_profile
    import iv_history
    import sector_data

    ticker = (ticker or "").upper()
    if df is None:
        df = data_handler.get_daily(ticker)

    sector_etf = sector_data.sector_for(ticker)
    is_etf = sector_data.is_etf(ticker)
    # The comparison benchmark under this profile. For JUICE_ENGINE this IS the
    # sector ETF, so the pre-existing behavior is reproduced exactly.
    benchmark = income_profile.benchmark_for(profile, sector_etf)
    # A name that IS its own benchmark has no distinct peer to beat — the
    # comparison computes to a tautological 0.0, which reads as a real number and
    # silently breaks the veto. Asked against the ACTIVE benchmark, so a compounder
    # whose ticker is the dividend benchmark is caught too, not just a sector ETF.
    is_sector_etf = bool(sector_etf) and ticker == (sector_etf or "").upper()
    is_own_benchmark = income_profile.is_own_benchmark(ticker, profile, sector_etf)
    if sector_df is None and benchmark and not is_own_benchmark:
        sector_df = data_handler.get_daily(benchmark)
    # A sector ETF (or any ETF's) vs-sector veto is waived, so its sector frame is
    # irrelevant to the veto; leave it None for those.
    if is_etf or is_own_benchmark:
        sector_df = None

    try:
        ivr_percentile = (iv_history.iv_rank(ticker) or {}).get("iv_percentile")
    except Exception:  # noqa: BLE001 — IVR is advisory; a missing file just skips the veto
        ivr_percentile = None

    out = compute(df, sector_df=sector_df, ivr_percentile=ivr_percentile, is_etf=is_etf,
                  benchmark=None if sector_df is None else benchmark)
    out["ticker"] = ticker
    out["sector"] = sector_etf
    out["is_sector_etf"] = is_sector_etf
    out["income_profile"] = income_profile.normalize(profile)
    return out
