"""Threshold calibration harness — upgrade PROPOSED_DEFAULTs from guess to
measured, using only data already on disk.

Replays the scorecard over the cached daily OHLCV history for the holdings
list: at each as-of date the metrics/verdict are computed exactly as the live
scorecard computes them (same functions), then paired with the FORWARD 4- and
8-week returns from that date. Buckets by verdict answer "did GO names
actually outperform CAUTION/AVOID over a CFM cycle?"; the sensitivity sweeps
re-bucket the SAME metric rows under alternative thresholds (ATR-extension
cutoff 2.0-4.0, MFI band variants) so each candidate threshold gets a measured
forward-return profile. Offline only — reads the parquet cache, never a
provider. CLI: scripts/calibrate.py
"""
from __future__ import annotations

import argparse
from datetime import datetime

import config
import data_handler
import sector_data
from metrics import scorecard as sc
from metrics import thresholds as T

# Forward horizons in trading days: a 4-week and an 8-week CFM cycle.
HORIZONS = {"fwd_4w": 20, "fwd_8w": 40}
MIN_HISTORY = 210          # bars needed before the first as-of (MA200 + slope)
ATR_EXTENSION_SWEEP = [2.0, 2.5, 3.0, 3.5, 4.0]
MFI_BAND_SWEEP = [(40.0, 60.0), (35.0, 65.0), (30.0, 70.0), (45.0, 55.0)]


def _verdict_with(metrics: dict, atr_max: float | None = None,
                  mfi_band: tuple[float, float] | None = None) -> str:
    """compute_verdict under temporarily-overridden thresholds (restored after)."""
    saved = (T.ATR_EXTENSION_MAX, T.MFI_MIN, T.MFI_MAX)
    try:
        if atr_max is not None:
            T.ATR_EXTENSION_MAX = atr_max
        if mfi_band is not None:
            T.MFI_MIN, T.MFI_MAX = mfi_band
        return sc.compute_verdict(metrics)["verdict"]
    finally:
        T.ATR_EXTENSION_MAX, T.MFI_MIN, T.MFI_MAX = saved


def collect_rows(tickers: list[str] | None = None, step: int = 5) -> list[dict]:
    """One row per (ticker, as-of date): the scorecard metrics + forward returns.

    `step` trading days between as-of dates keeps adjacent samples from being
    near-duplicates while still walking the whole cached history.
    """
    names = tickers or sector_data.all_tickers()
    spy = data_handler.get_daily(config.BENCHMARK)
    if spy is None:
        raise RuntimeError(f"no cached data for {config.BENCHMARK} — run the app once to warm the cache")
    max_h = max(HORIZONS.values())
    rows: list[dict] = []
    for t in names:
        df = data_handler.get_daily(t)
        if df is None or len(df) < MIN_HISTORY + max_h:
            continue
        etf = sector_data.sector_for(t) or ""
        sector_df = data_handler.get_daily(etf) if etf else None
        closes = df["Close"].astype(float)
        for i in range(MIN_HISTORY, len(df) - max_h, step):
            asof = df.index[i]
            sub = df.iloc[: i + 1]
            spy_sub = spy[spy.index <= asof]
            sec_sub = sector_df[sector_df.index <= asof] if sector_df is not None else None
            metrics = sc.metrics_for(sub, spy_sub, sec_sub)
            row = {"ticker": t, "asof": str(asof)[:10], "metrics": metrics,
                   "verdict": sc.compute_verdict(metrics)["verdict"]}
            base = float(closes.iloc[i])
            for name, h in HORIZONS.items():
                row[name] = round((float(closes.iloc[i + h]) / base - 1) * 100, 2)
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Closed-cycle loader — the real training data (entry context -> outcome).
# ---------------------------------------------------------------------------
def load_closed_cycles(state: dict) -> tuple[list[tuple], int]:
    """Yield ``(entry_context, exit_reason, cycle_outcome_metrics)`` for every
    closed cycle that carries a frozen entry_context, and a count of legacy
    cycles skipped.

    A cycle with ``entry_context is None`` was closed before the snapshot feature
    shipped (exit_reason ``LEGACY_UNRECORDED``); it can never be calibration-usable
    and is NOT fabricated — it's skipped and counted. This is the input shape the
    threshold calibration consumes: the entry-time feature values that produced
    the GO verdict, why the cycle ended, and how it turned out (including the
    exit-time counterpart metrics for entry->exit deltas)."""
    tuples: list[tuple] = []
    skipped = 0
    for c in state.get("cycles", []):
        ec = c.get("entry_context")
        if ec is None:
            skipped += 1
            continue
        outcome = {
            "ticker": c.get("ticker"),
            "entry_date": c.get("entry_date"),
            "exit_date": c.get("exit_date"),
            "days_held": c.get("days_held"),
            "net_result": c.get("net_result"),
            "net_return_pct": c.get("net_return_pct"),
            "gross_juice": c.get("gross_juice"),
            "leap_pnl": c.get("leap_pnl"),
            "roll_drag": c.get("roll_drag"),
            "target_met": c.get("target_met"),
            "exit_metrics": c.get("exit_metrics"),
        }
        tuples.append((ec, c.get("exit_reason"), outcome))
    return tuples, skipped


# ---------------------------------------------------------------------------
# Genius regime calibration — recompute the raw-vote / published-regime series
# from cached SPY bars under ALTERNATIVE parameter sets, for offline comparison
# against realized cycle outcomes. Comparison-only: no auto-tuning, no
# persistence (unlike regime_history.backfill, which writes the live store under
# the config defaults). Offline — reads the parquet cache, never a provider.
# ---------------------------------------------------------------------------
def regime_series(params: dict | None = None, step: int = 1) -> list[dict]:
    """Daily {date, raw_condition, published_regime, green_count} replayed from
    cached SPY bars under the Genius parameter set ``params`` (defaults = config).
    The dwell is accumulated on EVERY trading day (path-dependent); ``step`` only
    thins the returned sample. The published regime is the four lights + dwell —
    breadth/VIX are secondary and don't affect it — so this needs only SPY bars.
    Recomputes the formula directly from cached bars rather than calling
    ``screening.regime()`` so it stays offline/parquet-only."""
    import regime_genius
    import regime_history
    spy = data_handler.get_daily(config.GENIUS_INDEX_SYMBOL)
    if spy is None or spy.empty:
        raise RuntimeError(f"no cached data for {config.GENIUS_INDEX_SYMBOL} — warm the cache first")
    slow = (params or {}).get("slow_ma", config.GENIUS_SLOW_MA)
    published: list[str] = []
    rows: list[dict] = []
    index = list(spy.index)
    last = len(index) - 1
    for i, ts in enumerate(index):
        if (i + 1) < slow:
            continue
        sub = spy.iloc[: i + 1]
        tr = regime_genius.compute_trace(sub, None, None, published, params)
        published.append(tr["published_regime"])
        if (i % step) == 0 or i == last:
            rows.append({"date": regime_history._fmt_day(ts),
                         "raw_condition": tr["raw_condition"],
                         "published_regime": tr["published_regime"],
                         "green_count": tr["vote"]["green_count"]})
    return rows


def _regime_daymap(params: dict | None) -> dict[str, str]:
    """date -> published_regime for every trading day (step=1), for as-of lookup."""
    return {r["date"]: r["published_regime"] for r in regime_series(params, step=1)}


def _transitions(rows: list[dict], field: str = "published_regime") -> int:
    """Number of day-over-day changes in ``field`` — a flap gauge (lower = steadier)."""
    n = 0
    prev = None
    for r in rows:
        cur = r[field]
        if prev is not None and cur != prev:
            n += 1
        prev = cur
    return n


def regime_param_compare(param_sets: dict[str, dict] | None = None) -> dict:
    """Per parameter set: day-count in each published regime plus the raw-vote and
    published transition counts. Comparison-only — shows how much steadier the
    dwell makes the published series vs the raw vote, and how alternative params
    shift the regime distribution. ``param_sets`` maps a label -> params override
    (``None`` label uses the config defaults)."""
    sets = param_sets or {"defaults": {}}
    out: dict[str, dict] = {}
    for label, params in sets.items():
        rows = regime_series(params, step=1)
        counts = {"green": 0, "yellow": 0, "red": 0}
        for r in rows:
            counts[r["published_regime"]] = counts.get(r["published_regime"], 0) + 1
        out[label] = {
            "days": len(rows),
            "published_days": counts,
            "raw_transitions": _transitions(rows, "raw_condition"),
            "published_transitions": _transitions(rows, "published_regime"),
        }
    return out


def regime_vs_cycles(state: dict, param_sets: dict[str, dict] | None = None) -> dict:
    """For each parameter set, bucket every closed cycle's realized outcome by the
    PUBLISHED regime as-of that cycle's entry date. Answers "did entries taken in a
    green tape actually outperform ones taken in yellow?" under alternative regime
    parameters. Comparison-only; never mutates state or the regime store."""
    cycles = [c for c in state.get("cycles", []) if c.get("entry_date")]
    sets = param_sets or {"defaults": {}}
    out: dict[str, dict] = {}
    for label, params in sets.items():
        daymap = _regime_daymap(params)
        buckets: dict[str, list[float]] = {"green": [], "yellow": [], "red": [], "unknown": []}
        for c in cycles:
            day = str(c.get("entry_date"))[:10]
            reg = daymap.get(day, "unknown")
            ret = c.get("net_return_pct")
            if ret is not None:
                buckets[reg].append(float(ret))
        out[label] = {
            reg: {"n": len(v),
                  "mean_net_return_pct": round(sum(v) / len(v), 2) if v else None}
            for reg, v in buckets.items()
        }
    return out


def _bucket(rows: list[dict], key) -> dict[str, dict]:
    """Aggregate forward returns per bucket label produced by key(row)."""
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(key(r), []).append(r)
    out = {}
    for label, rs in sorted(buckets.items()):
        stats = {"n": len(rs)}
        for h in HORIZONS:
            vals = sorted(r[h] for r in rs)
            n = len(vals)
            stats[h] = {
                "mean": round(sum(vals) / n, 2),
                "median": round(vals[n // 2], 2),
                "win_rate": round(sum(1 for v in vals if v > 0) / n * 100, 1),
            }
        out[label] = stats
    return out


def _stats_table(buckets: dict[str, dict], label_header: str) -> list[str]:
    lines = [f"| {label_header} | n | 4w mean | 4w median | 4w win% | 8w mean | 8w median | 8w win% |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for label, s in buckets.items():
        f4, f8 = s["fwd_4w"], s["fwd_8w"]
        lines.append(f"| {label} | {s['n']} | {f4['mean']} | {f4['median']} | {f4['win_rate']} "
                     f"| {f8['mean']} | {f8['median']} | {f8['win_rate']} |")
    return lines


def report(rows: list[dict]) -> str:
    lines = ["# CFM Scorecard Calibration Report", "",
             f"generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"samples: {len(rows)} (ticker x as-of date pairs), "
             f"horizons: 4w = {HORIZONS['fwd_4w']} and 8w = {HORIZONS['fwd_8w']} trading days", ""]
    if not rows:
        lines.append("No samples — cache too short or holdings unavailable.")
        return "\n".join(lines) + "\n"

    lines += ["## Forward returns by verdict (current thresholds)", ""]
    lines += _stats_table(_bucket(rows, lambda r: r["verdict"]), "verdict")

    lines += ["", "## Sensitivity: ATR-extension cutoff (GO rows only)", "",
              "Each cutoff re-buckets the same samples; rows shown are those the "
              "cutoff would let through as GO.", ""]
    atr_buckets = {}
    for cutoff in ATR_EXTENSION_SWEEP:
        go = [r for r in rows if _verdict_with(r["metrics"], atr_max=cutoff) == "GO"]
        if go:
            atr_buckets[f"ATR ext ≤ {cutoff:g}"] = _bucket(go, lambda r: "GO")["GO"]
    lines += _stats_table(atr_buckets, "cutoff")

    lines += ["", "## Sensitivity: MFI band (GO rows only)", ""]
    mfi_buckets = {}
    for lo, hi in MFI_BAND_SWEEP:
        go = [r for r in rows if _verdict_with(r["metrics"], mfi_band=(lo, hi)) == "GO"]
        if go:
            mfi_buckets[f"MFI {lo:g}–{hi:g}"] = _bucket(go, lambda r: "GO")["GO"]
    lines += _stats_table(mfi_buckets, "band")

    lines += ["", "## Reading the report", "",
              "- If GO does not beat CAUTION/AVOID on 4-8w forward returns, the "
              "verdict thresholds are not earning their keep.",
              "- Pick the ATR-extension cutoff whose GO bucket has the best "
              "risk-adjusted profile with a usable sample size (n).",
              "- The MFI sweep shows whether the 40-60 coil band (HARD rule) is "
              "binding or could be relaxed.",
              "- Mid-fill caveat: this sweep uses forward *price* returns, but the "
              "paper juice/payback figures the strategy 'proved' are booked at the "
              "quoted mid. Deep-ITM options rarely fill at mid, so realized income "
              "runs below the paper numbers — see `slippage.report` / GET "
              "/api/slippage for the measured (or assumed) haircut before trusting "
              "a threshold tuned against optimistic fills.", ""]
    return "\n".join(lines) + "\n"


def run(tickers: list[str] | None = None, step: int = 5,
        out_path: str | None = None) -> str:
    rows = collect_rows(tickers, step=step)
    text = report(rows)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return text


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay the CFM scorecard over cached history "
                                             "and measure forward returns per verdict/threshold.")
    ap.add_argument("--tickers", help="comma-separated subset (default: all holdings)")
    ap.add_argument("--step", type=int, default=5, help="trading days between as-of samples")
    ap.add_argument("--out", default="calibration_report.md", help="markdown output path")
    args = ap.parse_args()
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None
    run(tickers, step=args.step, out_path=args.out)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
