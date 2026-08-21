"""Income-profile discriminator (schema v21) — the dividend sleeve's boundary.

PROVENANCE — ``TRAVIS_EXTENSION``, not a CFM rule.
The CFM source methodology (Mark Yegge) explicitly PREFERS volatile stocks
because they carry more juice, and warns against "safe" low-volatility names.
The dividend sleeve is Travis's extension, made economically viable only by the
shares-primary model: with real shares as the base leg the dividends are actually
collected, which was never true under a LEAP. Nothing in this module may weaken
or blend into the CFM juice engine.

Two profiles:

- ``JUICE_ENGINE`` — the CFM default. Every pre-existing position is backfilled
  to it, and every code path it touches must behave EXACTLY as it did before this
  module existed (regression-locked by test_dividend_profile.py).
- ``DIVIDEND_COMPOUNDER`` — a lower-volatility dividend payer held for the
  combination of weekly juice AND the dividend, compounding into further lots.

What the profile changes (and ONLY this):

  1. the RS3M comparison BENCHMARK for the sector leg — a dividend-peer ETF
     instead of the growth-tilted sector ETF (``benchmark_for``);
  2. which SHADOW floor a candidate is measured against (``shadow_floor``);
  3. a display badge.

What the profile NEVER changes [HARD_CFM_RULE]:

  * trend quality — the Genius four-light vote (per-stock AND market regime),
    consolidation-near-MA21, the ATR posture, the RSI band and the earnings-window
    exclusion apply IDENTICALLY to both profiles;
  * the YELLOW verdict lockout (watchlist only, no entry, no override);
  * the RS3M-vs-SPY kill-switch leg, which retains full exit authority untouched.

Resolution order for a candidate (``profile_for``): an explicit operator
assignment in state metadata always wins; otherwise a trailing dividend yield at
or above ``config.DIVIDEND_PROFILE_MIN_YIELD_PCT`` classifies the name as a
compounder; otherwise ``JUICE_ENGINE``. A POSITION never re-derives — it carries
the discriminator stamped at entry (``of``), so a yield print can never silently
re-profile an open position.

PURE: no I/O, no clock. Callers pass in state / yields.
"""
from __future__ import annotations

import config

JUICE_ENGINE = "JUICE_ENGINE"
DIVIDEND_COMPOUNDER = "DIVIDEND_COMPOUNDER"

ALL = frozenset({JUICE_ENGINE, DIVIDEND_COMPOUNDER})

# Display badge per profile (scan table + position cards).
BADGE = {JUICE_ENGINE: "JUICE", DIVIDEND_COMPOUNDER: "DIV"}


def normalize(value) -> str:
    """Coerce any stored/supplied value to a valid profile. Anything that is not
    an explicit ``DIVIDEND_COMPOUNDER`` resolves to ``JUICE_ENGINE`` — the CFM
    default is opt-OUT-proof: the dividend sleeve is only ever entered by an
    explicit tag, never by omission, a typo, or a None."""
    return DIVIDEND_COMPOUNDER if value == DIVIDEND_COMPOUNDER else JUICE_ENGINE


def of(obj) -> str:
    """The canonical income_profile for a position (or scan-candidate) dict."""
    if not isinstance(obj, dict):
        return JUICE_ENGINE
    return normalize(obj.get("income_profile"))


def badge(profile: str | None) -> str:
    return BADGE.get(normalize(profile), BADGE[JUICE_ENGINE])


# ---------------------------------------------------------------------------
# Candidate classification
# ---------------------------------------------------------------------------
def assignments(state: dict | None) -> dict:
    """The operator's explicit ticker -> profile map from state metadata
    (``income_profile_overrides``). Always wins over the yield heuristic."""
    if not isinstance(state, dict):
        return {}
    return (state.get("metadata") or {}).get("income_profile_overrides") or {}


def profile_for(ticker: str, state: dict | None = None,
                annual_dividend_yield_pct: float | None = None,
                overrides: dict | None = None) -> str:
    """Resolve a SCAN CANDIDATE's profile: explicit assignment -> yield heuristic
    -> JUICE_ENGINE.

    ``annual_dividend_yield_pct`` is the trailing annual yield in PERCENT (3.1 for
    a 3.1% payer). Passing None (unknown yield) yields JUICE_ENGINE — an unknown is
    never auto-enrolled into the dividend sleeve, so a fundamentals outage can only
    ever fall back to the CFM default, never into the extension.

    The assignment map may be given directly as ``overrides`` or extracted from
    ``state``. Taking the map is the primary form — a caller that has already
    pulled it (a bulk sweep) should not have to fabricate a state wrapper to hand
    it back.
    """
    t = (ticker or "").strip().upper()
    explicit = (overrides if overrides is not None else assignments(state)).get(t)
    if explicit is not None:
        return normalize(explicit)
    if (annual_dividend_yield_pct is not None
            and annual_dividend_yield_pct >= config.DIVIDEND_PROFILE_MIN_YIELD_PCT):
        return DIVIDEND_COMPOUNDER
    return JUICE_ENGINE


# ---------------------------------------------------------------------------
# Benchmark substitution (the Level-3 sector leg + the kill switch's sector leg)
# ---------------------------------------------------------------------------
def benchmark_for(profile: str | None, sector_etf: str | None) -> str | None:
    """The RS3M comparison benchmark for the SECTOR leg under a given profile.

    ``JUICE_ENGINE`` -> the name's own sector ETF, exactly as before.
    ``DIVIDEND_COMPOUNDER`` -> ``config.DIVIDEND_PEER_BENCHMARK`` (PROPOSED_DEFAULT
    SCHD; VYM / NOBL are the configured alternatives).

    Rationale: a dividend payer measured against a growth-tilted sector ETF is
    structurally rejected as a laggard — not because it is weak within its own
    peer group, but because the comparison is wrong. Substituting the peer
    benchmark keeps the filter doing its real job (rejecting relative laggards —
    the AAPL lesson) without rejecting the entire dividend universe.

    The RS3M-vs-SPY leg is NOT affected by this and never should be.
    """
    if normalize(profile) == DIVIDEND_COMPOUNDER:
        return config.DIVIDEND_PEER_BENCHMARK
    return sector_etf


def resolve(ticker: str, profile: str | None, sector_etf: str | None) -> dict:
    """The sleeve/benchmark resolution for one name, as data.

    Several call sites (the scan row, the scorecard row, the stock-lights
    wrapper) need the same answers — which sleeve, which benchmark, is this name
    its own benchmark. Deriving them independently at each site is a chance to
    drift, so they are derived once here. The kill switch no longer calls this
    at all: its peer leg was removed 2026-08-21.

    Returns:
      ``profile``          — normalized
      ``benchmark``        — the peer symbol this sleeve is judged against. It no
                             longer selects an RS comparison: the vs-peer RS legs
                             were removed 2026-08-21
                             (docs/decision-2026-08-21-remove-sector-rs.md). It
                             survives as the sleeve's identity for the shadow
                             income floors and as snapshot provenance.
      ``is_sector_etf``    — the name IS its own sector ETF (display/back-compat)
      ``is_own_benchmark`` — the name IS its active benchmark
    ``use_sector_df`` was dropped with the peer-frame fetch it existed to
    optimize — no caller loads a peer frame any more.
    PURE.
    """
    t = (ticker or "").strip().upper()
    prof = normalize(profile)
    bench = benchmark_for(prof, sector_etf)
    sector = (sector_etf or "").strip().upper()
    bench_u = (bench or "").strip().upper()
    return {
        "profile": prof,
        "benchmark": bench,
        "is_sector_etf": bool(sector) and t == sector,
        "is_own_benchmark": bool(t) and bool(bench_u) and t == bench_u,
    }


def is_own_benchmark(ticker: str, profile: str | None, sector_etf: str | None) -> bool:
    """True when a name IS its own comparison benchmark, so the sector leg is not
    applicable.

    A self-comparison computes to exactly 0.0, which reads as a real number
    rather than "N/A". The vs-peer RS legs this guard protected (the entry veto
    and the kill switch's sector leg) were removed 2026-08-21
    (docs/decision-2026-08-21-remove-sector-rs.md), so it no longer guards a
    live comparison — it remains as the profile-resolution predicate for
    "this name IS its own benchmark", which the sleeve still needs."""
    t = (ticker or "").strip().upper()
    bench = benchmark_for(profile, sector_etf)
    return bool(t) and bool(bench) and t == str(bench).strip().upper()
