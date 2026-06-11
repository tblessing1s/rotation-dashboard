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

- **The request path never contacts a provider.** If every provider is down,
  the dashboard still serves the last good values — visibly aged, never wrong.
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

Formulas for every computed value (and known differences vs thinkorswim) are
documented in [FORMULAS.md](FORMULAS.md). **When a backend value disagrees
with thinkorswim, thinkorswim is the source of truth** — type the TOS value
into the field; it is stored as a timestamped manual override that beats
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
| **FRED** | Fed funds, CPI, real GDP, unemployment | none |

Every stored row is tagged with its source, and the UI shows it in the
staleness tooltip. Yahoo is explicitly the labeled last resort.

### Schwab setup (one-time, ~10 minutes)

1. Create an app at https://developer.schwab.com (Trader API — Individual),
   callback URL `https://127.0.0.1`.
2. Run `python backend/cli.py schwab-auth`, follow the printed steps (log in,
   approve, paste the redirect URL back).
3. It prints the `fly secrets set …` command with all three values.

**Schwab refresh tokens expire every 7 days.** When that happens the
dashboard keeps working — ingestion falls back to Yahoo and the Data issues
panel shows a "Schwab auth failed" notice — until you re-run `schwab-auth`
and update the secret. Without Schwab credentials the app runs Yahoo-only.

---

## Deploy to Fly.io

The root `Dockerfile` builds the frontend and runs Gunicorn on Fly's `$PORT`.

```bash
fly launch                  # first time
fly volumes create data --size 1   # persistent volume for the datastore
fly secrets set SCHWAB_APP_KEY=… SCHWAB_APP_SECRET=… SCHWAB_REFRESH_TOKEN=…
fly secrets set INGEST_TOKEN=$(openssl rand -hex 16)   # protects POST /api/ingest
fly deploy
```

Mount the volume at `/data` and set `DATA_DIR=/data` in `fly.toml` so the
SQLite datastore and your saved inputs survive deploys and machine restarts.

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

**Manual** (your judgment / non-price data, on the Indicators tab): earnings
revisions, valuation, credit, chart-reading toggles — and any Level 1 field
you choose to override (marked MANUAL with its timestamp until cleared).

## Configuration

`backend/config.py`: tracked symbols (XLV + AAPL), benchmark (SPY), breadth
universe, validation bands, cross-check tolerances, RS3M calibration knobs
(`RS3M_METHOD`, `RS3M_EMA_SPAN`, `RS3M_LOOKBACK`, `MOM_SMOOTH`, `MOM_SCALE` —
see FORMULAS.md), staleness threshold for the catch-up runner, and capital /
reserve figures.

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
