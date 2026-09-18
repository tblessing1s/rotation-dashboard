# Rotation Dashboard — Cash Flow Machine (CFM)

A focused trading dashboard for the **Cash Flow Machine** strategy:
**scan markets → filter stocks → execute trades → auto-log executions → track
positions.** Buy 100 real shares of strong, consolidating stocks (or get paid
to wait for a better price via a cash-secured put) and sell weekly covered
calls against them — collecting extrinsic premium every week while the shares
carry the position.

```
  Schwab (primary) ─┐
                    ├─►  data_handler  ──►  parquet cache (DATA_DIR/cache)
  Alpha Vantage  ───┘         │
                              ▼
        indicators (RS3M · ATR · MA · RSI · breadth · structure)
                              ▼
   screening (regime · veto set · composite SCORE · entry route)
                              ▼
           Flask API  ◄──►  state.json (per-account source of truth)
                              ▼
                     React + Tailwind UI
```

Data sources are **Schwab + Alpha Vantage only**. Every execution is captured
(stock price + premium + timestamp) and appended to `state.json`; the theta
ledger and extrinsic-payback meters are *derived* from those records, never
hand-maintained. The dashboard supports **multiple accounts**, each with its
own state file and Schwab binding — see [`docs/accounts.md`](docs/accounts.md).

---

## The CFM system

**Base leg is 100 real shares, not a LEAP.** There is no diagonal, no long
option leg, and no LEAP surface in the UI — `config.LEGACY_LEAP_READONLY` is a
hard `True` so the execution log can keep pricing historical LEAP fills, but
new entries are shares-only.

**Entry isn't a stop-on-first-fail gate anymore.** `screening.entry_gate`
evaluates a thin **veto set** (`scan_verdict.VETOES` — the exit mirrors plus
hard account constraints: RED regime, below MA50/MA200, no weeklies, stale
data, account limits) — that's the only thing that can block a name. Every
eligible candidate is then **ranked** by a composite SCORE
(`scan_score.compute_score`, shadow mode — see below) built from relative
strength, sector context, chart structure, and volatility-normalized
extension. An eligible name also gets an advisory **entry route**:

- **Near/at the MA21 zone** → buy the shares now.
- **Extended above it** → sell a weekly cash-secured put struck at the MA21
  zone instead — get paid to wait for the price the strategy would rather
  pay; assignment turns it into the same shares + covered-call position.
- **YELLOW regime, or a BLOCKED chart structure** → shares only (a put commits
  capital a week out on a tape or a chart that doesn't support it).
- **RED regime** → blocked entirely; no route offered.

**Weekly routine:** roll the short covered call (strike = stock − ATR ×
regime/posture table below, see "Weekly short strike selection") to next
week, log the extrinsic sold and paid back. The **Positions** tab rolls a
short in place — pick the same or a different week, and the same or a
different strike (e.g. deep-ITM into earnings) — as a single `roll_short`
action.

**Earnings:** each open position surfaces its next earnings date (Positions
tab and the Overview action items) and flags it inside `EARNINGS_WARN_DAYS`
so the short can be rolled deep-ITM for protection or the position exited
entirely before the report.

**Kill switch:** RS3M vs SPY turns negative on a confirmed close → exit within
1–2 days. (The earlier RS3M-vs-Sector trigger was removed — see
[`docs/decision-2026-08-21-remove-sector-rs.md`](docs/decision-2026-08-21-remove-sector-rs.md).)

**Weekly short strike selection** (`backend/strike_policy.py`): the ATR
multiplier and minimum ITM% floor come from a table keyed by market regime
(green/yellow/red) **and** the operator's risk posture
(aggressive/conservative), toggled next to the demo/live switch. Whichever
candidate (ATR-based or ITM%-based) sits further below spot wins — max
protection.

**Size:** accumulate up to `config.SHARE_CAP` (500) shares per stock on
pullbacks, capped at `PER_POSITION_CAP_USD` ($15K) per name; open a new stock
only when the current one maxes out or a new slot is free
(`MAX_CFM_POSITIONS`, 2 concurrent names by default) within
`MAX_DEPLOYED_CAPITAL` ($38K) total.

**Shadow mode — computed but not authoritative.** Several signals are
computed, displayed, and logged with **zero blocking authority** pending
real-data calibration: the weekly-juice floor, the composite SCORE, the gate
ruleset replay, the chart-structure metrics, and the trailing juice capacity.
None of them can veto an entry; they inform the ranking and the operator's
judgment only, shown in the UI as violet "NO AUTHORITY" badges.

**Recommendation trust layer.** The engine emits explicit ENTER/ROLL/DEFEND/
EXIT recommendations *before* you act (never automated — see
[`docs/trust-layer.md`](docs/trust-layer.md)), then measures — from immutable
records only — how often the operator follows them and whether the resulting
order lifecycle matched. The **Recommendations** tab shows live picks; the
trust scoreboard tracks the agreement rate that would eventually justify
automation.

---

## Architecture

### Backend (`backend/`, Python Flask)

Flat module layout — modules import each other by bare name
(`import logging_handler`), not as a package. Some of the load-bearing pieces:

| Module | Responsibility |
|---|---|
| `app.py` | Flask app + all CFM routes; serves the built frontend. |
| `config.py` | Thresholds, calibration, capital figures, DATA_DIR-aware paths. |
| `accounts.py` | Multi-account registry; resolves the active `state.<id>.json` + Schwab binding. |
| `sector_data.py` | Parses the root-level `tickers_by_sector.txt` into the sector universe. |
| `indicators.py` | RS3M, ATR, MA, RSI, breadth, consolidation, strike spacing. |
| `structure_classifier.py` | Level-4 chart-structure classification (stage, institutional flow). |
| `data_handler.py` | Daily OHLCV (Schwab → Alpha Vantage) with a parquet cache. |
| `refresh_policy.py` | Tiers the universe: force-refreshes the "hot" set (open positions + live candidates) intraday. |
| `schwab_api.py` | Market data, quotes, option chains, order execution. |
| `alpha_vantage.py` | Daily OHLCV + quotes fallback. |
| `screening.py` | Regime, veto set, ranking inputs, entry route (shares vs. cash-secured put). |
| `scan_verdict.py` / `scan_score.py` | The veto set and the composite ranking SCORE (shadow mode). |
| `executor.py` | Execute buy_shares/sell_short/close_short/roll_short/put_opened/put_closed/put_assigned; capture + auto-log. |
| `position_manager.py` | Share-cap accumulation progress, capital + milestones. |
| `recommendation_engine.py` / `trust_derive.py` | The recommendation trust layer. |
| `logging_handler.py` | `state.json` I/O; derives the theta ledger + payback meters. |
| `kill_switch.py` | Per-position RS3M-vs-SPY monitoring and exit signals. |
| `units.py` | The one place the ×100 shares-per-contract factor lives on the backend. |

### Frontend (`frontend/src/`, React + Tailwind)

`App.jsx` drives the tabs:

- **Overview** — the landing digest: regime, action items, the book,
  positions glance, juice + payback (one `/api/overview` call).
- **Recommendations** — live ENTER/ROLL/DEFEND/EXIT picks from the trust
  layer, plus the trust scoreboard.
- **Scan** (`ScanProgress` + `ReadyToEnter`, full `Scorecard` behind a
  collapse) — find an entry.
- **Positions** (`PositionTracker` incl. per-card kill-switch strip,
  `PortfolioRisk` collapsed to headlines) — manage the book.
- **History** (`HistoryTab` incl. the theta ledger + per-week closes) — review
  results.
- **Payouts** (`PayoutsTab`) — the monthly income-withdrawal view. The payout
  is the **leftover**: net juice collected, shown with the full breakdown.
  Covers this month's estimate, last month's payout, month-by-month history,
  and a per-month finalize → paid record.
- **Day Trade** (`DayTradePanel`) — the separate intraday screener/tracker.
- **Calibration** (`ShadowCalibration`) — the shadow-mode metrics' replay
  view (see "Shadow mode" above).
- **Settings** (`SettingsTab`: demo/posture toggles, `LiveTradingSwitch`,
  `AlertsPanel`, `DataHealth`, `AccountsPanel`) — low-frequency controls and
  admin.

**Execute** (`ExecuteTab` — entry gate + order ticket) is a flow, not a tab:
it opens from a Ready-to-Enter pick, a position card, or Scan's "check any
ticker" button, with a ← Back button to return.

---

## API

The full route list lives in `backend/app.py` (~90 routes across scan,
execute, positions, recommendations, day-trade, accounts, and reconciliation).
The core CFM loop:

| Route | Purpose |
|---|---|
| `GET /api/overview` | One-call landing payload: regime + positions/capital + theta totals/payback + kill-switch, pre-joined (sections fail independently). |
| `GET /api/regime` | Market regime: status (green/yellow/red), breadth, VIX, SPY trend. |
| `GET /api/sectors` | Per-sector RS3M, breadth, ATR-expanding, status. |
| `GET /api/stock-filter?sector=XLK` | Candidates with RS3M vs SPY, ATR%, consolidating, status. |
| `POST /api/scan/refresh` · `GET /api/scan/status` | Run the full-universe scan as a **detached server-side job** and poll it. |
| `GET /api/scan/scorecard` | The composite SCORE + verdict per candidate (shadow mode). |
| `GET /api/scan/ready` | Ready-to-enter shortlist: veto set clear **and** Level 5 (account/juice), right now. |
| `GET /api/entry-gate?ticker=ON` | The veto set + ranking inputs + entry route for one ticker. |
| `GET /api/roll-suggestion?ticker=ON` | Suggested weekly short strike (regime × posture table). |
| `GET /api/roll-options?ticker=ON` | Roll picker data: current short + live buyback, plus every expiration to ROLL_MAX_DTE with nearby strikes. |
| `GET /api/earnings?ticker=ON` | Next earnings date (Alpha Vantage, day-cached; `&refresh=1` to force). |
| `POST /api/execute` | Execute a CFM action (`buy_shares`/`sell_shares`/`sell_short`/`close_short`/`roll_short`/`put_opened`/`put_closed`/`put_assigned`). Paper path logs immediately and returns `status:"filled"`; a live order returns `status:"working"` + `order_id`. |
| `GET /api/order-status?order_id=…` | Poll a live order. On fill it commits the execution (at the real fill price) and returns `filled`; `canceled`/`rejected` when the broker drops it; else `working`. |
| `POST /api/order-cancel` | Cancel a working order (`{order_id}`) at the broker and clear it. |
| `GET /api/positions` | Positions (shares, cap progress), capital summary, milestones. |
| `GET /api/theta-ledger` | Net juice (week/month/YTD) per position. |
| `GET /api/payouts` | Monthly payout tracker: current-month estimate + last-month payout + history + totals. |
| `GET /api/kill-switch` | Per-position RS3M vs SPY + exit signals. |
| `GET /api/recommendations` · `GET /api/trust-scoreboard` | The recommendation trust layer's live picks and its agreement scoreboard. |
| `GET /api/daytrade/*` | The separate intraday screener (universe, signals, trades, budget). |
| `GET/POST /api/state` | Read the full state; POST updates metadata (operator escape hatch, no UI). |
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
cd backend && pip install -r requirements.txt -c constraints.txt && python app.py
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
`{"CSCO": 0.03}`; a value > 1 is read as a percent).

### Schwab setup

1. Create a Trader API app at https://developer.schwab.com. Register the
   callback `https://<your-app>.fly.dev/auth/schwab/callback`.
2. `fly secrets set SCHWAB_APP_KEY=… SCHWAB_APP_SECRET=…`
3. Visit `https://<your-app>.fly.dev/auth/schwab`, approve, and the refresh
   token is stored automatically (in `DATA_DIR/schwab_token.json`, or
   `schwab_token.account-<id>.json` for a non-primary account with its own
   Schwab login).

Schwab refresh tokens expire every 7 days and require a fresh browser login;
`/api/config` reports the token's status. For live order placement (off by
default), set `CFM_LIVE_TRADING=1` — otherwise executions are captured against
live prices and logged but no order is transmitted (the honest paper path).

**Live order lifecycle.** With `CFM_LIVE_TRADING=1` and Schwab connected, a
single-leg action places a real DAY LIMIT order (`buy_shares`→BUY,
`sell_short`→SELL_TO_OPEN, `close_short`→BUY_TO_CLOSE, `roll_short`→a single
two-leg NET_CREDIT/NET_DEBIT ticket) and parks it under `state.json`
`pending_orders`; it is **not** recorded as an execution until it actually
fills. The UI toasts the submit, polls `/api/order-status` for the fill, and
auto-cancels via `/api/order-cancel` if it doesn't fill within the configured
window — so an unfilled, cancelled order leaves no trace. Paper mode keeps
committing immediately and just toasts the success (clearly labeled RECORDED,
never "filled" — nothing was sent to the broker).

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
volume attaches to one machine — `fly scale count 1`. Pushes to `master`
deploy via `.github/workflows/fly.yml`, which runs the backend (pytest) and
frontend (vitest) test suites first — a red suite blocks the deploy.

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

### Emergency exit — "Schwab is down and the kill switch just fired"

All trading goes through the app by design, but the app trades through one broker
API on a 7-day token. If that token lapsed or Schwab's API is down during an
exit-now event, exit at Schwab directly and let reconciliation adopt the trade
via a compensating adjustment. The written procedure is
[`docs/emergency-exit.md`](docs/emergency-exit.md) — read it before you need it.

---

## Tests

```bash
python -m pytest backend -q       # backend — ~2,000 tests, offline, no provider keys needed
cd frontend && npm test           # frontend — Vitest, pure-module coverage (units, orderFlow, recWhy)
```

The backend suite covers the indicator formulas, sector parsing, the veto
set/scoring, and the execute → theta-ledger → extrinsic-payback flow end to
end. Both suites run in CI (`.github/workflows/fly.yml`) and must pass before
a push to `master` deploys.

---

This implements a mechanical framework. It is not financial advice; the
GO/WAIT verdicts are checklist outputs, not recommendations.
