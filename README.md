# Rotation Dashboard (local app)

Your two-strategy (CFM / APP) institutional rotation system, running locally.
A Python backend fetches market data and computes indicators server-side (no
browser CORS limits, cached to disk); a React frontend renders the 4-level
decision system, checklists, exit alarms, positions, and P&L.

Everything you enter — manual thinkorswim inputs and positions — is saved to
`backend/state.json` and reloads automatically.

---

## Requirements

- **Python 3.10+**
- **Node.js 18+** (only needed the first time, to build the frontend)

## Run it

**macOS / Linux**
```bash
./start.sh
```

**Windows**
```bat
start.bat
```

The first run creates a Python virtual environment, installs dependencies, and
builds the frontend. Subsequent runs skip straight to launching. When it's up,
open **http://localhost:5179** in your browser.

To stop: `Ctrl+C` in the terminal.

---

## What's automated vs. manual

**Computed automatically** (backend, from daily price/volume history):
RSI, OBV trend, volume ratio, MFI, RS3M, RS3M_MOM, MA21, price-vs-MA21.
RSI is verified against Wilder's reference (70.46).

**Manual** (your judgment / non-price data, entered on the Indicators tab):
Fed stance, growth, inflation, breadth, earnings revisions, valuation, credit,
and the chart-reading toggles (bounces at support, breakout, support defined).

On the Indicators tab, each computed value appears as a small `calc … use ↵`
chip next to its field. Nothing overwrites your numbers automatically — you
compare against thinkorswim and tap **use** to apply.

---

## Calibrating RS3M / RS3M_MOM to thinkorswim

Your thinkorswim RS3M studies are EMA-based and scaled to the numbers you know
(e.g. +500 / +884 / +1128). The backend's RS3M uses a transparent
percentage-spread formula, so the **raw magnitude won't match** out of the box —
but the direction and turning points will. Three knobs in
`backend/config.py` let you line it up:

- `RS3M_LOOKBACK` — trading days in the relative-strength window (63 ≈ 3 months).
- `MOM_SMOOTH` — EMA span applied to RS3M before momentum (raise to smooth,
  matching an EMA-based study).
- `MOM_SCALE` — multiplier on RS3M_MOM so its size matches your thinkorswim
  reading. Compare a few live values, then set this ratio.

Edit, save, restart the backend. (Until calibrated, trust RS3M_MOM
directionally, or keep entering those two fields manually.)

---


## Deploy to Fly.io

The repository includes a root-level `Dockerfile` and `fly.toml` so Fly can
detect and deploy the app from the GitHub repository root. The Docker build
compiles the Vite frontend and then runs the Flask backend with Gunicorn on
Fly's `$PORT` (default `8080`), matching `fly.toml`'s `internal_port`.

From the repository root, launch or deploy with:

```bash
fly launch
fly deploy
```

Make sure `Dockerfile`, `fly.toml`, and `.dockerignore` are committed and pushed
to GitHub; Fly's GitHub deployment will not see local-only files.

## Configuration

`backend/config.py` also holds the tracked symbols (`XLV`, `ILMN`), the
benchmark (`SPY`), cache freshness (`CACHE_TTL_MINUTES`, default 15), and your
capital / reserve figures. Change symbols there and restart.

## Data source

Daily OHLCV via `yfinance` (no API key). Cached to `backend/.cache/` as parquet
and refreshed at most every `CACHE_TTL_MINUTES`. If a fetch fails, the last
cached copy is used so the dashboard still loads.

## Notes

- This implements *your* framework's mechanical logic. It is not financial
  advice; the GO/WAIT verdicts are checklist outputs, not recommendations.
- Single-user, localhost only. Nothing is sent anywhere except the market-data
  request to Yahoo Finance.
