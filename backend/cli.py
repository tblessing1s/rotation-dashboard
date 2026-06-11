"""
Admin CLI for the rotation dashboard.

  python cli.py ingest --now            force one ingestion cycle
  python cli.py ingest --symbols XLV,SPY  targeted run (bars + snapshots only)
  python cli.py status                  per-symbol / per-series freshness report
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

    p_auth = sub.add_parser("schwab-auth", help="mint a Schwab refresh token")
    p_auth.add_argument("--key", help="Schwab app key (prompted if omitted)")
    p_auth.add_argument("--secret", help="Schwab app secret (prompted if omitted)")
    p_auth.add_argument("--redirect-uri", default="https://127.0.0.1", help="must match the app's registered callback")
    p_auth.set_defaults(fn=cmd_schwab_auth)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
