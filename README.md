# Rotation Dashboard

Your two-strategy (CFM / APP) institutional rotation system. A Python backend
ingests market data on a schedule into a SQLite datastore and computes all
indicators there; a React frontend renders the 4-level decision system,
checklists, exit alarms, positions, and P&L.

This dashboard gates real capital deployment, so the data pipeline is built
around one rule: **the UI never silently shows stale or wrong data.** Every
value carries its as-of date and a freshness dot; if the regime inputs go
stale, the Level 1 banner says DEGRADED DATA instead of a confident
RISK-ON/RISK-OFF.

---

## Architecture

```
        scheduled ingestion (cron / CLI / catch-up thread)
                          │
   Schwab Trader API ──►  │   ◄── FRED (DFF, CPIAUCSL, GDPC1, UNRATE)
   Yahoo (fallback)  ──►  │
                          ▼
              validation (validation.py)
        bad bars → quarantine table (with reason)
                          ▼
            SQLite datastore (backend/data/rotation.db)
        bars · macro series · snapshots · overrides · runs
                          ▼
        indicator + macro snapshots recomputed at ingest
                          ▼
              Flask API (reads datastore ONLY)
                          ▼
                    React frontend
```

Key properties:

- **The request path never contacts a provider** for market data. If every
  provider is down, the dashboard still serves the last good values — visibly
  aged, never wrong. (The one deliberate exception is the user-triggered
  Positions → *Sync from Schwab* button, which reads your brokerage account
  live; it touches no market data and writes nothing to the datastore.)
- **Append-only history.** A bad fetch can never delete or overwrite good
  data. Corrections land as new rows; reads resolve the best row per date
  (manual > schwab > yahoo, then newest fetch).
- **Validation before write.** Bars with null/negative prices, high < low,
  negative volume, or absurd moves (±25% vs prior close; ±100% for ^VIX —
  configurable in `config.py`) are quarantined with the reason and surfaced
  in the UI's Data issues panel.
- **Cross-checks.** When both providers are available, the regime inputs
  (^VIX, SPY) are compared across them; divergence beyond tolerance is
  flagged instead of silently trusting one source.
- **Staleness is measured in trading days** (`market_calendar.py`): Friday's
  data is fresh all weekend and through Monday's session; NYSE holidays don't
  count. Green dot = covers the last completed session, yellow = 1 session
  behind, red = 2+ behind.

Formulas for every computed value are documented in [FORMULAS.md](FORMULAS.md).
Defaults are tuned for Schwab/thinkorswim daily bars (63-bar RS3M, Wilder RSI,
and SimpleMovingAvg(21)). **When a backend value still disagrees with your
custom thinkorswim study, thinkorswim is the source of truth** — type the TOS
value into the field; it is stored as a timestamped manual override that beats
ingested data until you tap **auto ↻**.

---

## Run it locally

Requirements: Python 3.10+, Node 18+ (first run only, to build the frontend).

```bash
./start.sh        # macOS / Linux
start.bat         # Windows
```

Open **http://localhost:5179**. Stop with `Ctrl+C`.

## CLI

```bash
cd backend
python cli.py ingest --now          # force one ingestion cycle
python cli.py ingest --symbols XLV  # targeted run for specific symbols
python cli.py status                # per-symbol freshness report
python cli.py status --json         # same, machine-readable
python cli.py schwab-auth           # mint a Schwab refresh token (see below)
```

`status` shows, per symbol: last bar date, close, source (schwab/yahoo), and
whether that's current / 1 day behind / stale relative to the last completed
NYSE session — plus FRED series freshness, open quarantine items, and the
last ingestion run. The same report is at `GET /api/data-status`.

---

## Data sources

| Source | Used for | Credentials |
|---|---|---|
| **Schwab Trader API** (primary) | daily OHLCV — same feed as thinkorswim | `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_REFRESH_TOKEN` |
| **Yahoo Finance** (fallback) | daily OHLCV when Schwab is unavailable | none |
| **FRED** | Fed funds, CPI, real GDP, unemployment | `FRED_API_KEY` (recommended) |

Every stored row is tagged with its source, and the UI shows it in the
staleness tooltip. Yahoo is explicitly the labeled last resort.

**FRED:** ingestion prefers the official FRED API and falls back to the keyless
graph CSV. The keyless endpoint has started returning HTTP 403 to programmatic
requests, which leaves Fed policy / growth / inflation blank and shows the
regime gate as **DEGRADED DATA**. Get a free key at
https://fred.stlouisfed.org/docs/api/api_key.html and set it:

```sh
fly secrets set FRED_API_KEY=…
```

### Schwab setup (one-time, ~10 minutes)

1. Create an app at https://developer.schwab.com (Trader API — Individual).
   Register your deployed callback as the Callback URL:
   `https://<your-app>.fly.dev/auth/schwab/callback`
   (keep `https://127.0.0.1` too if you want the CLI bootstrap flow).
2. Set the app credentials: `fly secrets set SCHWAB_APP_KEY=… SCHWAB_APP_SECRET=…`
3. Visit `https://<your-app>.fly.dev/auth/schwab`, log in to Schwab, approve.
   The refresh token is stored in the datastore automatically and an ingest
   kicks off immediately — no secret to copy.

(Alternative bootstrap: `python backend/cli.py schwab-auth` mints a token
locally and prints the `fly secrets set SCHWAB_REFRESH_TOKEN=…` command. A
datastore token from `/auth/schwab` always beats the env secret.)

**Schwab refresh tokens expire every 7 days and cannot be renewed
programmatically — Schwab requires a fresh browser login.** The dashboard
tracks the token's age: the Data issues panel warns when ≤2 days remain and
shows a **Re-authorize Schwab** button that repeats step 3 in one click. If
the token lapses anyway, nothing breaks — ingestion falls back to Yahoo until
you re-authorize. Without Schwab credentials the app runs Yahoo-only.

#### Account sync (positions, on top of market data)

The same three secrets also power the Positions tab's **Sync from Schwab**
button, which pulls your live holdings and trade history via Schwab's
*Accounts & Trading* API (`/trader/v1/accounts…`) instead of importing a CSV.
This is a **different Schwab product** than the market-data feed: in your app at
https://developer.schwab.com, the app must be approved for
**Accounts and Trading Production** (not just Market Data). If it isn't, market
data keeps flowing but the sync button returns an HTTP 401/403 with a note to
enable that product. No new credentials are needed — it reuses the existing
key/secret/refresh token.

The sync is **on-demand and user-triggered** (`POST /api/account/sync`); it is
the only API route that contacts a provider at request time, because account
data has no place in the scheduled market datastore. Synced trades land in the
positions ledger, so open/close history and estimated P&L work without any CSV
imported data. Set each open position's *current net value* to mark it to market
(or read the live market value from the account snapshot panel).

---

## Deploy to Fly.io

The root `Dockerfile` builds the frontend and runs Gunicorn on Fly's `$PORT`.

```bash
fly launch                  # first time
fly volume create data --region iad --size 1   # persistent volume for the datastore
fly secrets set SCHWAB_APP_KEY=… SCHWAB_APP_SECRET=… SCHWAB_REFRESH_TOKEN=…
fly secrets set FRED_API_KEY=…                         # free key, keeps macro inputs auto-filling
fly secrets set INGEST_TOKEN=$(openssl rand -hex 16)   # protects POST /api/ingest
fly deploy
fly scale count 1           # one machine — see note below
```

Mount the volume at `/data` and set `DATA_DIR=/data` in `fly.toml` so the
SQLite datastore and your saved inputs survive deploys and machine restarts.

**Run exactly one machine.** This app is a single-writer SQLite + `state.json`
store, and a Fly volume attaches to only one machine. If you let Fly run its
default of 2 machines you need 2 volumes, and the two copies of your data will
silently diverge. Keep one machine (`fly scale count 1`) bound to the single
`data` volume.

If a deploy fails with:

```
Error: Process group 'app' needs volumes with name 'data' to fulfill mounts
defined in fly.toml; Run `fly volume create data -r REGION -n COUNT` ... iad=2
```

it means no volume exists yet (and Fly is trying to place 2 machines). Create
one volume and pin to a single machine:

```bash
fly volume create data --region iad --size 1
fly deploy
fly scale count 1
```

### Scheduled ingestion

Ingestion is triggered by `POST /api/ingest?wait=1` (Bearer `INGEST_TOKEN`).
Schedule it twice on trading days — after the close and a pre-open catch-up:

```bash
# 21:30 UTC ≈ 30 min after the NYSE close; 11:00 UTC pre-open catch-up
fly machine run --schedule "30 21 * * 1-5" curlimages/curl -- \
  curl -fsS -X POST -H "Authorization: Bearer $INGEST_TOKEN" \
  "https://YOUR-APP.fly.dev/api/ingest?wait=1"
```

There is also a belt-and-braces catch-up: if the app wakes up and the newest
successful ingest is older than `INGEST_STALE_AFTER_HOURS` (6h), it kicks a
background run — API requests are never blocked on providers.

---

## What's automated vs. manual

**Computed at ingest** (from stored bars + FRED): Level 1 macro (VIX,
breadth, Fed stance, growth, inflation), RS3M, RS3M_MOM, RSI, OBV trend,
volume ratio, volume acceleration, MFI, MA21, price-vs-MA21.

**Synced from your account** (Positions tab → **Sync from Schwab**): your live
holdings snapshot (symbol, qty, average price, market value, open P/L) and the
last year of trade fills, normalized into the Schwab-only positions ledger.
Synced fills merge with and de-duplicate against existing Schwab fills, while
legacy CSV-imported rows are removed from the positions view. (Requires the
Schwab app to also be approved for the **Accounts and Trading** product — see
below.)

**Manual** (your judgment / non-price data, on the Indicators tab): earnings
revisions, valuation, credit, chart-reading toggles — and any Level 1 field
you choose to override (marked MANUAL with its timestamp until cleared).

## Configuration

`backend/config.py`: tracked symbols (XLV + AAPL), benchmark (SPY), breadth
universe, validation bands, cross-check tolerances, RS3M calibration knobs
(`RS3M_METHOD`, `RS3M_EMA_SPAN`, `RS3M_LOOKBACK`, `MOM_SMOOTH`, `MOM_SCALE`),
RSI/MA defaults (`RSI_METHOD`, `MA21_METHOD` — see FORMULAS.md), staleness
threshold for the catch-up runner, and capital / reserve figures.

## Tests

```bash
python -m pytest backend -q
```

Covers the indicator formulas against reference fixtures, validation rules,
trading-day staleness (weekends/holidays), provider fallback order, and an
end-to-end ingestion cycle including garbage-data and provider-outage cases.

## Notes

- This implements *your* framework's mechanical logic. It is not financial
  advice; the GO/WAIT verdicts are checklist outputs, not recommendations.
- Single-user. External calls are limited to the configured market-data
  providers (Schwab/Yahoo) and FRED, during ingestion only.
