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

**Computed automatically** (backend, from public market/economic data):
Level 1 macro inputs (VIX, breadth, Fed stance, growth, inflation), RSI, OBV
trend, volume ratio, MFI, RS3M, RS3M_MOM, MA21, and price-vs-MA21. RSI is
verified against Wilder's reference (70.46).

Level 1 uses `^VIX` from Yahoo Finance, breadth as the percent of a configured
ETF universe trading above its 50-day moving average, Fed stance from a current-conditions model using FRED DFF/CPI/GDP/unemployment,
inflation from FRED CPI YoY, and growth from FRED real-GDP momentum. All of
these fields remain editable so you can override the automatic readout.

**Manual** (your judgment / non-price data, entered on the Indicators tab):
Earnings revisions, valuation, credit, and the chart-reading toggles (bounces at
support, breakout, support defined).

On the Indicators tab, Level 1 macro values auto-fill on refresh and remain
editable. Other computed values appear as a small `calc … use ↵` chip next to
their field; you compare against thinkorswim and tap **use** to apply.

---

## Calibrating RS3M / RS3M_MOM to thinkorswim

Your thinkorswim RS3M studies are EMA-based and scaled to the numbers you know
(e.g. +500 / +884 / +1128). The backend now defaults to an EMA-smoothed
relative-strength approximation: it smooths the sector ETF and SPY closes, then
compares their 3-month returns. That should track EMA-based watchlists better
than the old raw close-to-close spread, but the **exact magnitude may still
differ** until the settings match your ThinkScript. These knobs in
`backend/config.py` let you line it up:

- `RS3M_METHOD` — `"ema"` for EMA-smoothed prices, or `"return_spread"` for the
  legacy raw return-spread formula.
- `RS3M_EMA_SPAN` — EMA length applied to both the sector ETF and SPY before the
  lookback return is measured.
- `RS3M_LOOKBACK` — trading days in the relative-strength window (63 ≈ 3 months).
- `MOM_SMOOTH` — EMA span applied to the RS3M series before momentum is taken.
- `MOM_SCALE` — multiplier on RS3M_MOM so its size matches your thinkorswim
  reading. Compare a few live values, then set this ratio.

Edit, save, restart the backend. Until calibrated, trust RS3M_MOM directionally
or paste TOS watchlist rows in the Rotation tab when TOS should be the source of
truth.

---


## Deploy to Fly.io

The repository includes a root-level `Dockerfile` so Fly can detect the app from
the GitHub repository root. The Docker build compiles the Vite frontend and then
runs the Flask backend with Gunicorn on Fly's `$PORT` (default `8080`).

From the repository root, launch or deploy with:

```bash
fly launch
fly deploy
```

Make sure `Dockerfile` and `.dockerignore` are committed and pushed to GitHub;
Fly's GitHub deployment will not see local-only files.

## Configuration

`backend/config.py` also holds the tracked symbols (`XLV`, `ILMN`), the
benchmark (`SPY`), the Level 1 breadth universe, cache freshness
(`CACHE_TTL_MINUTES`, default 15), and your capital / reserve figures. Change
symbols there and restart.

## Data source

Daily OHLCV via `yfinance` (no API key), plus FRED graph CSV downloads for Fed
funds, CPI, and real GDP (also no API key). Price history is cached to
`backend/.cache/` as parquet and refreshed at most every `CACHE_TTL_MINUTES`; the
macro snapshot is cached in memory for `MACRO_CACHE_TTL_MINUTES`. If a price
fetch fails, the last cached copy is used so the dashboard still loads.

## Notes

- This implements *your* framework's mechanical logic. It is not financial
  advice; the GO/WAIT verdicts are checklist outputs, not recommendations.
- Single-user, localhost only. Nothing is sent anywhere except the market-data
  request to Yahoo Finance.
