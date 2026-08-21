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
# TWO rulesets live here, selected by ``config.GATE_RULESET`` (default "legacy").
# Both are computed on every evaluation so their divergence can be logged; only
# the configured one carries blocking authority. ``verdict`` below is the LEGACY
# rule and is deliberately left byte-identical — it is the regression anchor.
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


def verdict_proposed(greens: int, insufficient: bool, any_veto: bool,
                     core_green: bool) -> str:
    """PROPOSED mandatory-core N-of-4 rule (TRAVIS_EXTENSION, PROPOSED_DEFAULT).

        GREEN  = core light green AND greens >= config.SYM_MIN_GREEN_LIGHTS
        YELLOW = core light green AND greens == SYM_MIN_GREEN_LIGHTS - 1
        RED    = otherwise, OR core light not green, regardless of the others

    Vetoes and insufficient history still dominate, exactly as in the legacy rule.

    The point of the core: close > SMA50 is the light that maps to the
    Consider-Going-To-Cash exit level, so it stays mandatory — the entry veto set
    tracks the exit trigger set. Every OTHER light loses unilateral veto power,
    Parabolic SAR most importantly: it is binary, whippy, and its seed is
    path-dependent on the frame's first bar (indicators.parabolic_sar), so an
    entry must not hinge on it alone. SAR is still computed, still voted, still
    displayed — it simply cannot block by itself any more."""
    if any_veto or insufficient:
        return RED
    if not core_green:
        return RED
    if greens >= config.SYM_MIN_GREEN_LIGHTS:
        return GREEN
    if greens == config.SYM_MIN_GREEN_LIGHTS - 1:
        return YELLOW
    return RED


def verdict_for(greens: int, insufficient: bool, any_veto: bool, *,
                core_green: bool = False, ruleset: str | None = None) -> str:
    """The verdict under ``ruleset`` (default ``config.GATE_RULESET``). The single
    dispatch point — callers pick a ruleset, never a rule."""
    if (ruleset or config.GATE_RULESET) == config.RULESET_PROPOSED:
        return verdict_proposed(greens, insufficient, any_veto, core_green)
    return verdict(greens, insufficient, any_veto)


def verdicts_by_ruleset(greens: int, insufficient: bool, any_veto: bool,
                        core_green: bool) -> dict:
    """Every ruleset's verdict from one light vote — the shadow-compute input."""
    return {rs: verdict_for(greens, insufficient, any_veto,
                            core_green=core_green, ruleset=rs)
            for rs in config.GATE_RULESETS}


# ---------------------------------------------------------------------------
# Right-spot gate (SEPARATE, after the lights; blocking; stocks == ETFs)
# ---------------------------------------------------------------------------
def _spot_check(cid: str, value, ok) -> dict:
    return {"id": cid, "value": value, "pass": bool(ok)}


def atr_momentum_max(ruleset: str | None = None) -> float:
    """The Level-4 ATR/ATR_5EMA ceiling for ``ruleset``.

      legacy   — SPOT_ATR_MOMENTUM_MAX (1.0): ATR actively contracting or flat.
      proposed — L4_ATR_EXPANSION_MAX (1.05): ATR merely not EXPANDING.

    Deliberately NOT the shadow SCORE's ATR band (scan_score.ATR_CONTRACTING /
    ATR_EXPANDING), which still rewards contraction for RANKING. Only the veto
    relaxes; the rank is untouched, and the two stay independently tunable."""
    return (config.L4_ATR_EXPANSION_MAX
            if (ruleset or config.GATE_RULESET) == config.RULESET_PROPOSED
            else config.SPOT_ATR_MOMENTUM_MAX)


def _right_spot_from(atrp, momentum, extension, ruleset: str | None = None) -> dict:
    """The right-spot verdict over already-computed indicator values, so both
    rulesets can be evaluated from ONE pass over the bars."""
    checks = [
        _spot_check("atr_pct", atrp,
                    atrp is not None and atrp <= config.CONSOLIDATION_ATR_PCT_MAX),
        # ATR / ATR_5EMA <= the ruleset's ceiling (see atr_momentum_max).
        _spot_check("atr_5d_ema", momentum,
                    momentum is not None and momentum <= atr_momentum_max(ruleset)),
        _spot_check("extension", extension,
                    extension is not None and extension <= config.SPOT_ATR_EXTENSION_MAX),
    ]
    blocked_by = [f"spot:{c['id']}" for c in checks if not c["pass"]]
    return {"pass": not blocked_by, "checks": checks, "blocked_by": blocked_by}


def _spot_inputs(df) -> tuple:
    return (indicators.atr_pct(df) if df is not None else None,
            indicators.atr_momentum(df) if df is not None else None,
            indicators.atr_extension(df) if df is not None else None)


def right_spot(df, ruleset: str | None = None) -> dict:
    """The consolidation "right spot" gate — evaluated AFTER a GREEN light verdict,
    and blocking. NOT a light. Identical for stocks and ETFs. A check with no data
    (None) fails conservatively (you can't confirm a right spot you can't measure).
    ``blocked_by`` lists ``spot:<check>`` reasons; empty == in the right spot.

    ``ruleset`` selects the ATR ceiling only (see ``atr_momentum_max``); the
    ATR% and extension checks are identical under both."""
    return _right_spot_from(*_spot_inputs(df), ruleset)


def right_spots_by_ruleset(df) -> dict:
    """Every ruleset's right-spot verdict from ONE pass over the bars."""
    inputs = _spot_inputs(df)
    return {rs: _right_spot_from(*inputs, rs) for rs in config.GATE_RULESETS}


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

    # SHADOW-FIRST dual compute: every ruleset's verdict + right spot, from ONE
    # light vote and ONE indicator pass. Only `config.GATE_RULESET` is
    # authoritative below; the rest exist so the divergence can be recorded.
    verdicts = verdicts_by_ruleset(engine["greens"], engine["insufficient"],
                                   bool(trip), core)
    spots = right_spots_by_ruleset(df)
    by_ruleset = {
        rs: {"verdict": verdicts[rs], "right_spot": spots[rs],
             # YELLOW is never enterable, under either ruleset.
             "enterable": verdicts[rs] == GREEN and spots[rs]["pass"]}
        for rs in config.GATE_RULESETS
    }
    active = config.GATE_RULESET
    v, spot = verdicts[active], spots[active]
    return {
        "lights": engine["lights"],
        "greens": engine["greens"],
        "reds": engine["reds"],
        "insufficient": engine["insufficient"],
        "core_green": core,
        "verdict": v,
        "enterable": v == GREEN and spot["pass"],   # YELLOW is never enterable
        "vetoes": vetoes,
        "vetoed": bool(trip),
        "veto_reasons": [f"veto:{vid}" for vid in trip],
        "right_spot": spot,
        # The shadow record: which ruleset decided the fields above, and what
        # every ruleset would have said. Never read by the blocking path.
        "ruleset": active,
        "by_ruleset": by_ruleset,
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
