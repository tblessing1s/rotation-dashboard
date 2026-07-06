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
| `refresh_policy.py` | Tiers the universe: force-refreshes the "hot" set (open positions + live entry/earnings candidates) intraday while the long tail rides the daily pre-open warm-up. |
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
| `POST /api/scan/refresh` · `GET /api/scan/status` | Run the full-universe scan as a **detached server-side job** and poll it. The sweep keeps running even if the browser tab is backgrounded, switched, or closed; a returning client reads the memoized result warm. |
| `GET /api/entry-gate?ticker=ON` | The 4-level gate, pass/fail per level, verdict. |
| `GET /api/roll-suggestion?ticker=ON` | Suggested weekly short strike (stock − 1.5×ATR). |
| `GET /api/roll-options?ticker=ON` | Roll picker data: current short + live buyback, plus every expiration to ROLL_MAX_DTE with nearby strikes (choose week + strike). |
| `GET /api/earnings?ticker=ON` | Next earnings date (Alpha Vantage, day-cached; `&refresh=1` to force). Manual override via `metadata.earnings_overrides`. |
| `POST /api/execute` | Execute a CFM action (`buy_leap`/`sell_short`/`close_short`/`close_leap`/`roll_short`). Paper path logs immediately and returns `status:"filled"`; a live single-leg order returns `status:"working"` + `order_id`. |
| `GET /api/order-status?order_id=…` | Poll a live order. On fill it commits the execution (at the real fill price) and returns `filled`; `canceled`/`rejected` when the broker drops it; else `working`. |
| `POST /api/order-cancel` | Cancel a working order (`{order_id}`) at the broker and clear it. |
| `GET /api/positions` | Positions (LEAP/share/cap), capital summary, milestones. |
| `GET /api/coverage?ticker=ON` | Delta-coverage guardrail: LEAP vs short deltas, the 0.50 LEAP floor, and whether the long still covers the short. |
| `GET /api/theta-ledger` | Net juice (week/month/YTD) + extrinsic payback per position. |
| `GET /api/kill-switch` | Per-position RS3M vs SPY/Sector + exit signals. |
| `GET /api/daily-checklist` | Today's routine: regime, reserve, expiring shorts, LEAP DTE. |
| `GET/POST /api/state` | Read the full state; POST updates metadata. |
| `POST /api/refresh/hot` | Force-refresh the hot set (open positions + live entry/earnings candidates) now; the scheduler also runs it every `HOT_REFRESH_MINUTES` in market hours. `/api/data-health` reports the set + last run. |
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

**Live order lifecycle.** With `CFM_LIVE_TRADING=1` and Schwab connected, a
single-leg action places a real DAY LIMIT order (`buy_leap`→BUY_TO_OPEN,
`sell_short`→SELL_TO_OPEN, `close_short`→BUY_TO_CLOSE, `close_leap`→SELL_TO_CLOSE)
and parks it under `state.json` `pending_orders`; it is **not** recorded as an
execution until it actually fills. The UI toasts the submit, polls
`/api/order-status` for the fill, and auto-cancels via `/api/order-cancel` if it
doesn't fill within 3 seconds — so an unfilled, cancelled order leaves no trace.
Paper mode keeps committing immediately and just toasts the success.

### Authentication

A single-password gate guards the whole dashboard (every `/api` route and the
Schwab re-auth link). A successful login sets a signed, HttpOnly, Secure session
cookie that lasts **30 days**, so you sign in once per device.

Set one secret in production — a *hash* of your password, never the password
itself:

```bash
# generate the hash (paste your password at the prompt)
python -c "from werkzeug.security import generate_password_hash as g; print(g(input('password: ')))"
fly secrets set DASHBOARD_PASSWORD_HASH='pbkdf2:sha256:...'   # the value printed above
```

That's it — redeploy and the login screen appears. Notes:

- **No password configured → the gate is disabled** (open). This keeps local dev
  frictionless; the app is only unprotected if you never set the secret, so
  *always* set `DASHBOARD_PASSWORD_HASH` on Fly.
- The cookie-signing key is generated once and persisted to
  `DATA_DIR/.session_secret` (on the `/data` volume), so logins survive deploys
  and restarts. Override with `DASHBOARD_SECRET_KEY` if you prefer to manage it.
  Rotating it (or the volume) signs everyone out.
- Env vars: `DASHBOARD_PASSWORD_HASH` (preferred), `DASHBOARD_PASSWORD`
  (plaintext, local only), `DASHBOARD_SECRET_KEY` (optional),
  `DASHBOARD_COOKIE_INSECURE=1` (only if testing over plain http locally).

---

## Deploy to Fly.io

The root `Dockerfile` builds the frontend and runs Gunicorn. A persistent
volume at `/data` (`DATA_DIR=/data`) holds `state.json`, the parquet cache, and
the Schwab token across deploys.

```bash
fly launch
fly volume create data --region iad --size 1
fly secrets set SCHWAB_APP_KEY=… SCHWAB_APP_SECRET=… ALPHAVANTAGE_API_KEY=…
fly secrets set DASHBOARD_PASSWORD_HASH='pbkdf2:sha256:…'   # login gate — see Authentication
fly deploy && fly scale count 1
```

**Run exactly one machine.** `state.json` is a single-writer store and a Fly
volume attaches to one machine — `fly scale count 1`. Pushes to `master` deploy
via `.github/workflows/fly.yml`.

---

## Mobile app & push notifications

The dashboard is a **PWA**: install it to your Android home screen and it runs
full-screen with its own icon, and the alert engine can push notifications to
the phone's lock screen even when the app is closed. It stays private — the
whole app is behind your password gate, on your own Fly machine.

### Install on Android (home-screen app)

Open the dashboard in **Chrome** on Android, then menu (⋮) → **Install app** /
**Add to Home screen**. It launches standalone from then on. (A manifest,
service worker, and icons ship in `frontend/public/`; nothing to configure.)

### Native Web Push (this app sends the notifications)

Delivery is a self-contained alert **channel** (`webpush`, alongside `email`
and `ntfy`), keyed by a VAPID pair. **No setup required:** on first use the app
generates a keypair and persists it to `DATA_DIR/.vapid_keys.json` on the volume
(the same self-configuring pattern as the session-signing key), stable across
deploys. So push works out of the box — nothing to run from a phone.

Just deploy, then in the app: **Alerts → Settings → Push notifications (this
device) → Enable on this device**, allow the browser prompt, and hit **Send
test**. Each phone/browser registers once; subscriptions live in `state.json`
(`alerts.push_subscriptions`) and dead ones are pruned automatically. Do the
enable step **after installing to the home screen** — Android push is far more
reliable from the installed PWA.

**Optional — manage the keys yourself** (only if you'd rather set them
centrally, e.g. to share one keypair across environments). Generating them
regenerates the pair, which invalidates existing device subscriptions, so keep
them stable once set:

```bash
python scripts/gen_vapid_keys.py     # prints the three secrets below
fly secrets set VAPID_PUBLIC_KEY='…' VAPID_PRIVATE_KEY='…' VAPID_SUBJECT='mailto:you@example.com'
fly deploy
```

### ntfy (alternative / additional push, no VAPID)

The [ntfy](https://ntfy.sh) app is a zero-code path: install it, subscribe to a
**secret random topic**, and point the app at it. Privacy comes from the topic
name being unguessable.

```bash
fly secrets set ALERT_NTFY_TOPIC='cfm-<long-random-string>'   # optional: ALERT_NTFY_SERVER for self-hosted
```

Both channels can run at once; toggle either under **Alerts → Settings**.
Unconfigured channels fall back to the server log, so alerts are never silently
dropped.

### Dead-man's switch (page me if the scheduler goes quiet)

The alert scheduler is an in-process thread — if it wedges or the machine stops,
no alert fires and nothing says so. Point it at an external dead-man service so
its *silence* pages you:

```bash
fly secrets set HEALTHCHECK_URL='https://hc-ping.com/<your-uuid>'   # healthchecks.io (or any ping URL)
```

The scheduler pings that URL every tick while it's alive (a `/fail` ping on a
broken alert run); miss enough pings and the service alerts you. Optional:
`HEALTHCHECK_MIN_INTERVAL` (seconds between liveness pings, default 300).
Configure the check's period+grace to taste — e.g. period 1h / grace 1h catches
a wedge or stop within ~2h, any day. Inert when unset.

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
