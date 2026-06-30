# Rotation Dashboard — Cash Flow Machine (CFM)

A focused trading dashboard for the **Cash Flow Machine** strategy:
**scan markets → filter stocks → execute trades → auto-log executions → track
positions.** Buy deep-ITM LEAP calls in strong, consolidating stocks, sell
weekly ITM short calls against them, and track extrinsic payback until each
position is "in profit mode."

```
  Schwab (primary) ─┐
                    ├─►  data_handler  ──►  parquet cache (DATA_DIR/cache)
  Alpha Vantage  ───┘         │
                              ▼
        indicators (RS3M · ATR · MA · RSI · breadth)
                              ▼
     screening (regime · sectors · stock filter · entry gate)
                              ▼
           Flask API  ◄──►  state.json (source of truth)
                              ▼
                     React + Tailwind UI
```

Data sources are **Schwab + Alpha Vantage only**. Every execution is captured
(stock price + premium + timestamp) and appended to `state.json`; the theta
ledger and extrinsic-payback meters are *derived* from those records, never
hand-maintained.

---

## The CFM system

**Entry gate (4 levels, stop on first fail):**

1. **Market regime green** — SPY breadth positive, VIX calm.
2. **Sector strong** — RS3M vs SPY > +10%, breadth > 60%, ATR expanding.
3. **Stock beats peers** — RS3M vs SPY > +5%, RS3M vs Sector > 0%.
4. **Consolidating, not breaking** — low ATR%, price near MA21.

**Weekly routine:** roll the short ITM call (strike = stock − 1.5×ATR), log the
extrinsic sold and paid back, check the kill switch. The **Positions** tab rolls
a short in place — pick the same or a different week, and the same or a different
strike (e.g. deep-ITM into earnings) — as a single `roll_short` action.

**Earnings:** each open position surfaces its next earnings date (Positions tab,
Kill Switch, and the daily checklist) and flags it inside `EARNINGS_WARN_DAYS` so
the short can be rolled deep-ITM for protection or the position exited entirely
before the report.

**Kill switch (binary):** RS3M vs Sector turns negative → exit immediately;
RS3M vs SPY turns negative (confirmed close) → exit within 1–2 days.

**Delta coverage (the diagonal guardrail):** the LEAP delta must hold the **0.50
floor** (below it the LEAP stops acting like a deep-ITM stock proxy — roll it
deeper ITM), and the long's total delta must stay **≥ the short's** (once the
short's delta climbs past the long's, an up-move loses faster on the short than
it gains on the long — i.e. uncovered, so roll the short up/out). The
**Positions** tab shows each leg's live delta and a covered/uncovered badge; this
is why the recomputed deltas (skew-aware + dividend-adjusted) need to be right.

**Size:** 5 deep-ITM LEAPs (~0.90 delta, ~180 DTE) per stock; accumulate shares
on pullbacks toward a 500-share cap; open a new stock only when the current one
maxes out.

---

## Architecture

### Backend (`backend/`, Python Flask)

| Module | Responsibility |
|---|---|
| `app.py` | Flask app + all CFM routes; serves the built frontend. |
| `config.py` | Thresholds, calibration, capital figures, DATA_DIR-aware paths. |
| `sector_data.py` | Parses the root-level `tickers_by_sector.txt` into the sector universe. |
| `indicators.py` | RS3M, ATR, MA, RSI, breadth, consolidation, strike spacing. |
| `data_handler.py` | Daily OHLCV (Schwab → Alpha Vantage) with a parquet cache. |
| `schwab_api.py` | **Kept** — market data, quotes, option chains, order execution. |
| `alpha_vantage.py` | **Kept** — daily OHLCV + quotes fallback. |
| `screening.py` | Regime, sector strength, stock filter, the 4-level entry gate, checklist. |
| `executor.py` | Execute buy_leap / sell_short / close_short; capture + auto-log. |
| `position_manager.py` | LEAP intrinsic/extrinsic, share-cap progress, capital + milestones. |
| `logging_handler.py` | `state.json` I/O; derives the theta ledger + payback meters. |
| `kill_switch.py` | Per-position RS3M monitoring and exit signals. |

### Frontend (`frontend/src/`, React + Tailwind)

`App.jsx` drives six tabs: **Scan** (`RegimeScanner` + `StockFilter`),
**Execute** (`ExecuteTab` — entry gate + execution), **Theta** (`ThetaLedger`),
**Kill Switch** (`KillSwitchMonitor`), **Positions** (`PositionTracker`), and
**Checklist** (`DailyChecklist`).

---

## API

| Route | Purpose |
|---|---|
| `GET /api/regime` | Market regime: status (green/yellow/red), breadth, VIX, SPY trend. |
| `GET /api/sectors` | Per-sector RS3M, breadth, ATR-expanding, status. |
| `GET /api/stock-filter?sector=XLK` | Candidates with RS3M vs SPY/Sector, ATR%, consolidating, status. |
| `GET /api/entry-gate?ticker=ON` | The 4-level gate, pass/fail per level, verdict. |
| `GET /api/roll-suggestion?ticker=ON` | Suggested weekly short strike (stock − 1.5×ATR). |
| `GET /api/roll-options?ticker=ON` | Roll picker data: current short + live buyback, plus every expiration to ROLL_MAX_DTE with nearby strikes (choose week + strike). |
| `GET /api/earnings?ticker=ON` | Next earnings date (Alpha Vantage, day-cached; `&refresh=1` to force). Manual override via `metadata.earnings_overrides`. |
| `POST /api/execute` | Execute + auto-log a CFM action (`buy_leap`/`sell_short`/`close_short`/`close_leap`/`roll_short`); returns execution id + captured prices. |
| `GET /api/positions` | Positions (LEAP/share/cap), capital summary, milestones. |
| `GET /api/coverage?ticker=ON` | Delta-coverage guardrail: LEAP vs short deltas, the 0.50 LEAP floor, and whether the long still covers the short. |
| `GET /api/theta-ledger` | Net juice (week/month/YTD) + extrinsic payback per position. |
| `GET /api/kill-switch` | Per-position RS3M vs SPY/Sector + exit signals. |
| `GET /api/daily-checklist` | Today's routine: regime, reserve, expiring shorts, LEAP DTE. |
| `GET/POST /api/state` | Read the full state; POST updates metadata. |
| `GET /api/config` | Thresholds, sector universe, Schwab/AV status, live-trading flag. |

---

## Run it locally

Requirements: Python 3.10+, Node 18+.

```bash
./start.sh        # macOS / Linux  (start.bat on Windows)
```

Open **http://localhost:5179**. Without API keys the UI still renders; data
values read `—` until Schwab/Alpha Vantage are configured.

```bash
cd frontend && npm install && npm run build   # build the UI
cd backend && pip install -r requirements.txt && python app.py
```

---

## Data sources & credentials

| Source | Used for | Credentials |
|---|---|---|
| **Schwab Trader API** (primary) | daily OHLCV, quotes, option chains, order execution, dividend yield (fundamentals) | `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`, refresh token |
| **Alpha Vantage** (fallback) | daily OHLCV + quotes + next-earnings calendar + dividend yield (overview) | `ALPHAVANTAGE_API_KEY` |

**Dividend-adjusted greeks.** A call holder forgoes the underlying's dividends,
so a dividend yield `q` lowers the call's delta (`delta = e^(−qT)·N(d1)`). The
yield is fetched per ticker (Schwab fundamentals → Alpha Vantage overview),
day-cached, and overridable by hand via `metadata.dividend_overrides` (e.g.
`{"CSCO": 0.03}`; a value > 1 is read as a percent). The effect is negligible on
the weekly short but ~1–2% on the 171-DTE LEAP for a ~3% payer — enough to shift
a strike across the LEAP delta band. Non-payers (`q = 0`) are unaffected.

### Schwab setup

1. Create a Trader API app at https://developer.schwab.com. Register the
   callback `https://<your-app>.fly.dev/auth/schwab/callback`.
2. `fly secrets set SCHWAB_APP_KEY=… SCHWAB_APP_SECRET=…`
3. Visit `https://<your-app>.fly.dev/auth/schwab`, approve, and the refresh
   token is stored automatically (in `DATA_DIR/schwab_token.json`).

Schwab refresh tokens expire every 7 days and require a fresh browser login;
`/api/config` reports the token's status. For live order placement (off by
default), set `CFM_LIVE_TRADING=1` — otherwise executions are captured against
live prices and logged but no order is transmitted (the honest paper path).

---

## Deploy to Fly.io

The root `Dockerfile` builds the frontend and runs Gunicorn. A persistent
volume at `/data` (`DATA_DIR=/data`) holds `state.json`, the parquet cache, and
the Schwab token across deploys.

```bash
fly launch
fly volume create data --region iad --size 1
fly secrets set SCHWAB_APP_KEY=… SCHWAB_APP_SECRET=… ALPHAVANTAGE_API_KEY=…
fly deploy && fly scale count 1
```

**Run exactly one machine.** `state.json` is a single-writer store and a Fly
volume attaches to one machine — `fly scale count 1`. Pushes to `master` deploy
via `.github/workflows/fly.yml`.

---

## Tests

```bash
python -m pytest backend -q
```

Covers the indicator formulas, sector parsing, and the execute → theta-ledger →
extrinsic-payback flow end to end (offline, no provider keys needed).

---

This implements a mechanical framework. It is not financial advice; the
GO/WAIT verdicts are checklist outputs, not recommendations.
