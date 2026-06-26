"""
Admin CLI for the rotation dashboard.

  python cli.py ingest --now            force one ingestion cycle
  python cli.py ingest --symbols XLV,SPY  targeted run (bars + snapshots only)
  python cli.py status                  per-symbol / per-series freshness report
  python cli.py macro                   check macro series staleness (Alpha Vantage)
  python cli.py backtest-backfill --symbols AMD,HOOD --start 2026-05-15 --end 2026-06-14
                                        pull 5-minute bars from Schwab (Yahoo
                                        fallback) into the datastore
  python cli.py backtest-coverage --symbols AMD --start 2026-05-15 --end 2026-06-14
                                        report which intraday sessions are stored
  python cli.py schwab-auth             one-time OAuth dance to mint a Schwab
                                        refresh token (expires every 7 days)
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.parse


def cmd_ingest(args) -> int:
    import ingest

    symbols = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()] or None
    result = ingest.run(trigger="cli", symbols=symbols)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") in ("ok", "partial") else 1


def cmd_status(args) -> int:
    import status

    report = status.data_status()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(f"Last completed trading day: {report['lastCompletedTradingDay']}")
    run = report.get("lastRun") or {}
    print(f"Last ingest run: {run.get('started_at', '—')}  status={run.get('status', '—')}  trigger={run.get('trigger', '—')}")
    print(f"Open quarantine items: {report['quarantineOpen']}")
    print(f"Symbol freshness: {report['summary']}")
    print()
    print(f"{'SYMBOL':<8} {'LAST BAR':<12} {'CLOSE':>10} {'SOURCE':<8} {'STATE':<8} FETCHED AT")
    for sym, info in sorted(report["symbols"].items()):
        print(
            f"{sym:<8} {info.get('lastDate', '—'):<12} "
            f"{info.get('close', '—'):>10} {str(info.get('source', '—')):<8} "
            f"{info.get('staleness', '—'):<8} {info.get('fetchedAt', '—')}"
        )
    print()
    print(f"{'FRED':<10} {'LAST OBS':<12} {'VALUE':>10}  FETCHED AT")
    for sid, info in report["fredSeries"].items():
        print(
            f"{sid:<10} {info.get('lastDate', '—'):<12} "
            f"{info.get('value', '—'):>10}  {info.get('fetchedAt', '—')}"
        )
    return 0


def cmd_macro(args) -> int:
    from datetime import datetime, timezone

    def _ingest_staleness(fetched_at: str | None) -> str:
        if not fetched_at:
            return "unknown"
        try:
            fetched = datetime.strptime(fetched_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600
        except ValueError:
            return "unknown"
        if age_hours <= 36:
            return "fresh"
        if age_hours <= 96:
            return "yellow"
        return "red"

    import config as cfg
    import db
    import macro as macro_calc

    n = max(1, int(getattr(args, "observations", 6) or 6))

    # ---- 1) Snapshot freshness: what the regime gate actually reads ----------
    snap = db.latest_snapshot("macro", "macro") or {"values": {}, "fields": {}, "errors": {}}
    fields = dict(snap.get("fields") or {})
    errors = dict(snap.get("errors") or {})

    print("\nMacro Data Freshness Report")
    print("=" * 80)
    if fields:
        print()
        print(f"{'FIELD':<12} {'VALUE':>12} {'STATE':<8} {'FETCHED AT':<22} SOURCE")
        print("-" * 80)
        for key in sorted(fields.keys()):
            meta = fields[key]
            # vix/breadth are market inputs judged by observation date; the slow
            # series are judged by ingestion recency (matches /api/macro).
            staleness = meta.get("staleness") or _ingest_staleness(meta.get("fetchedAt"))
            print(
                f"{key:<12} {str(meta.get('value', '—')):>12} "
                f"{staleness:<8} {str(meta.get('fetchedAt') or '—'):<22} "
                f"{meta.get('source', '—')}"
            )
    if errors:
        print()
        print("Errors:")
        for key, err in errors.items():
            print(f"  {key}: {err}")
    print()
    print(f"Computed at: {snap.get('_computedAt', '—')}")

    # ---- 2) Raw stored observations + how each is interpreted ----------------
    # Proves the pull AND the math: the last few observations per series, the
    # active source/fetched_at, and the derived value the calculators produce.
    try:
        from providers import alphavantage
        econ_map = getattr(alphavantage, "_ECON_SERIES", {})
    except Exception:  # noqa: BLE001
        econ_map = {}

    print()
    print(f"Raw Series & Interpretation (last {n} stored observations)")
    print("=" * 80)
    series: dict = {}
    for sid in cfg.FRED_SERIES:
        s = db.get_macro_series(sid)
        series[sid] = s
        func = (econ_map.get(sid) or ("?", {}))[0]
        print()
        if s is None or s.empty:
            print(f"{sid}  (AV {func}):  MISSING — no stored observations")
            continue
        fa = s.attrs.get("fetched_at", "?")
        print(f"{sid}  (AV {func}) · source={s.attrs.get('source', '?')} · "
              f"{len(s)} obs · fetched_at={fa} [{_ingest_staleness(fa)}]")
        for idx, val in s.tail(n).items():
            print(f"    {str(idx.date())}:  {float(val):.4f}")
        try:
            if sid == "GDPC1":
                g = macro_calc.growth_from_gdp(s)
                print(f"    => growth: {g['value']}  qoqAnnualized={g['qoqAnnualized']}% "
                      f"(prev {g['previousQoqAnnualized']}%)  asOf {g['asOf']}")
            elif sid == "CPIAUCSL":
                inf = macro_calc.inflation_from_cpi(s)
                print(f"    => inflation YoY: {inf['value']}%  index={inf['index']}  asOf {inf['asOf']}")
            elif sid == "DFF":
                print(f"    => funds rate: {float(s.iloc[-1]):.2f}%  "
                      f"63-obs change={macro_calc._series_change(s, 63):+.2f}")
            elif sid == "UNRATE":
                print(f"    => unemployment: {float(s.iloc[-1]):.1f}%  "
                      f"3-obs change={macro_calc._series_change(s, 3):+.1f}")
        except Exception as e:  # noqa: BLE001
            print(f"    => interpretation failed: {e}")

    # Fed policy is a derived score across all four series — show the votes.
    needed = ["DFF", "CPIAUCSL", "GDPC1", "UNRATE"]
    if all(series.get(k) is not None and not series[k].empty for k in needed):
        try:
            fed = macro_calc.classify_fed_policy(
                series["DFF"], series["CPIAUCSL"], series["GDPC1"], series["UNRATE"]
            )
            print()
            print(f"Fed policy model:  {str(fed['value']).upper()}  (score {fed['score']})")
            print(f"    rate={fed['rate']}%  cpiYoY={fed['cpiYoY']}%  "
                  f"growth={fed['qoqAnnualizedGrowth']}%  unemp={fed['unemployment']}%  "
                  f"realRate={fed['realPolicyRate']}")
            if fed.get("hawkishConditions"):
                print(f"    hawkish ({len(fed['hawkishConditions'])}): {', '.join(fed['hawkishConditions'])}")
            if fed.get("dovishConditions"):
                print(f"    dovish  ({len(fed['dovishConditions'])}): {', '.join(fed['dovishConditions'])}")
        except Exception as e:  # noqa: BLE001
            print(f"\nFed policy model failed: {e}")
    print()
    return 0 if (fields or any(s is not None and not s.empty for s in series.values())) else 1


def _intraday_symbols(args) -> list[str]:
    return [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()]


def cmd_backtest_backfill(args) -> int:
    """Pull 5-minute bars for the given symbols/date range into the datastore.

    The quickest way to exercise the live Schwab intraday path in production:
    `fly ssh console` onto the machine (where the Schwab secrets live) and run
    this. Per-symbol errors (e.g. an expired refresh token) are printed, not
    swallowed."""
    import backtest_service

    symbols = _intraday_symbols(args)
    if not symbols:
        print("--symbols is required (comma-separated, e.g. AMD,HOOD)", file=sys.stderr)
        return 1
    result = backtest_service.backfill(symbols, args.start, args.end, int(args.interval))
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


def cmd_backtest_coverage(args) -> int:
    import backtest_service

    symbols = _intraday_symbols(args)
    if not symbols:
        print("--symbols is required (comma-separated, e.g. AMD,HOOD)", file=sys.stderr)
        return 1
    config = {
        "tickers": symbols,
        "date_range": {"start": args.start, "end": args.end},
        "interval_min": int(args.interval),
    }
    config = backtest_service._apply_default_sector_map(config)
    report = backtest_service.coverage_report(config)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("complete") else 1


def cmd_schwab_auth(args) -> int:
    import requests

    key = args.key or input("Schwab app key: ").strip()
    secret = args.secret or input("Schwab app secret: ").strip()
    redirect = args.redirect_uri
    auth_url = (
        "https://api.schwabapi.com/v1/oauth/authorize?"
        + urllib.parse.urlencode({"client_id": key, "redirect_uri": redirect})
    )
    print("\n1. Open this URL in a browser, log in, and approve access:\n")
    print(f"   {auth_url}\n")
    print(f"2. You will be redirected to {redirect}/?code=... (the page won't load — that's fine).")
    pasted = input("3. Paste the FULL redirected URL here: ").strip()
    code = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).get("code", [None])[0]
    if not code:
        print("No ?code= parameter found in that URL.", file=sys.stderr)
        return 1

    basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
    resp = requests.post(
        "https://api.schwabapi.com/v1/oauth/token",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect},
        timeout=20,
    )
    if resp.status_code != 200:
        print(f"Token exchange failed: HTTP {resp.status_code} {resp.text}", file=sys.stderr)
        return 1
    tokens = resp.json()
    print("\nSuccess. Set these as Fly secrets (refresh token expires in 7 days):\n")
    print(f"  fly secrets set SCHWAB_APP_KEY='{key}' \\")
    print(f"                  SCHWAB_APP_SECRET='{secret}' \\")
    print(f"                  SCHWAB_REFRESH_TOKEN='{tokens['refresh_token']}'")
    print(
        "\nTip: once deployed, renew weekly with one click at"
        " https://<your-app>.fly.dev/auth/schwab (register"
        " https://<your-app>.fly.dev/auth/schwab/callback as a Schwab callback URL)."
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cli.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="run one ingestion cycle")
    p_ingest.add_argument("--now", action="store_true", help="run immediately (default)")
    p_ingest.add_argument("--symbols", help="comma-separated symbols for a targeted run")
    p_ingest.set_defaults(fn=cmd_ingest)

    p_status = sub.add_parser("status", help="per-symbol freshness report")
    p_status.add_argument("--json", action="store_true", help="raw JSON output")
    p_status.set_defaults(fn=cmd_status)

    p_macro = sub.add_parser("macro", help="check macro series freshness, raw observations, and interpretation")
    p_macro.add_argument("-n", "--observations", type=int, default=6, help="raw observations to show per series (default 6)")
    p_macro.set_defaults(fn=cmd_macro)

    p_bf = sub.add_parser("backtest-backfill", help="pull 5-minute bars into the datastore")
    p_bf.add_argument("--symbols", required=True, help="comma-separated tickers (e.g. AMD,HOOD)")
    p_bf.add_argument("--start", required=True, help="start date YYYY-MM-DD")
    p_bf.add_argument("--end", required=True, help="end date YYYY-MM-DD (inclusive)")
    p_bf.add_argument("--interval", default=5, type=int, help="minutes per candle (default 5)")
    p_bf.set_defaults(fn=cmd_backtest_backfill)

    p_cov = sub.add_parser("backtest-coverage", help="report stored intraday coverage")
    p_cov.add_argument("--symbols", required=True, help="comma-separated tickers")
    p_cov.add_argument("--start", required=True, help="start date YYYY-MM-DD")
    p_cov.add_argument("--end", required=True, help="end date YYYY-MM-DD (inclusive)")
    p_cov.add_argument("--interval", default=5, type=int, help="minutes per candle (default 5)")
    p_cov.set_defaults(fn=cmd_backtest_coverage)

    p_auth = sub.add_parser("schwab-auth", help="mint a Schwab refresh token")
    p_auth.add_argument("--key", help="Schwab app key (prompted if omitted)")
    p_auth.add_argument("--secret", help="Schwab app secret (prompted if omitted)")
    p_auth.add_argument("--redirect-uri", default="https://127.0.0.1", help="must match the app's registered callback")
    p_auth.set_defaults(fn=cmd_schwab_auth)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
