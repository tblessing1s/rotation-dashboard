# Backtesting Engine

Configure a custom day-trading **setup**, replay it across 5-minute history, and
get a fully auditable trade log with summary stats. It lives on the **Backtest**
tab of the dashboard and is backed by the same Schwab API connection and SQLite
datastore as everything else.

> The engine reads the datastore only — it never contacts a provider while
> running, so a backtest is fast and repeatable. Pulling missing intraday
> history is a separate, explicit step (**Backfill**), in keeping with the
> dashboard's "providers are only touched on purpose" rule.

---

## How it works

For each ticker × trading day in the range:

1. **Yesterday's levels** (`Y-High` / `Y-Low`) come from the prior *daily* bar,
   and **ATR** from the daily series — both strictly *before* the day (no
   look-ahead).
2. The day's 5-minute candles are walked in order, inside the configured **time
   window**, after dropping the first *N* candles if asked.
3. The first candle that matches the **setup** (e.g. price dips to Y-Low and
   closes back above it, with a volume spike) becomes a signal.
4. **Entry** = candle close (or the level itself on "immediate touch").
   **Stop** = per the stop-placement rule. **Target** = `entry ± risk × R`.
5. Later candles are stepped through to see whether the **target or stop** is
   hit first (if both are touched in one 5-minute candle, the stop is assumed to
   fill first — the conservative read).
6. The trade is logged with **market context**: SPY direction and the ticker's
   sector-proxy direction at entry (price-so-far vs the session open).

One trade per ticker per day: the first valid setup is taken; if a **skip
condition** (SPY/sector down) blocks it, that is logged as a `Skip` so you can
see what the filter cost you.

Summary stats: total trades, wins, losses, skips, win-rate %, average win (R),
average loss (R), and expectancy per trade.

---

## Using the UI

1. Open the **Backtest** tab.
2. Set tickers (quick chips for AMD / HOOD / HIMS / CVNA), a date range, and the
   setup / entry / skip / risk-reward / stop / time-window options.
3. Click **Run Backtest**. If sessions are missing from the datastore you'll get
   a coverage warning — click **Backfill from Schwab** (or use **Run +
   auto-backfill**, which pulls then runs in one shot).
4. Review the summary cards and the trade log. Filter by ticker, outcome, or
   date, and **Export CSV** for the filtered rows.
5. Optionally **Save config** under a name and reload it later.

Times are **exchange-local (America/New_York)**. `09:30–11:00` is the first 90
minutes of the regular session.

---

## API

All endpoints accept/return JSON. The config can be sent bare or wrapped as
`{ "config": { … } }`.

| Method & path | Purpose |
|---|---|
| `POST /api/backtest/run` | Run a backtest. Body may include `"autoBackfill": true`. |
| `POST /api/backtest/coverage` | Report which `(symbol, date)` sessions are missing. |
| `POST /api/backtest/backfill` | Pull missing intraday history from Schwab → Yahoo. |
| `POST /api/backtest/export` | `{ "trades": [...] }` → CSV download. |
| `GET/POST/DELETE /api/backtest/configs` | List / save / delete named configs. |

### Input

```json
{
  "tickers": ["AMD", "HOOD", "HIMS", "CVNA"],
  "date_range": { "start": "2026-05-15", "end": "2026-06-14" },
  "setup_conditions": { "type": "support_resistance_bounce", "use_yesterday_levels": true, "proximity_pct": 0.3 },
  "entry_rules": { "volume_multiplier": 2.0, "entry_timing": "candle_close" },
  "skip_conditions": { "skip_first_n_candles": 0, "skip_if_spy_down": false, "skip_if_sector_down": false },
  "risk_reward": 2,
  "stop_logic": "atr_divided_by_2",
  "stop_params": { "fixed_distance": 0.5, "buffer_pct": 0.1, "atr_period": 14 },
  "time_window": { "start_time": "09:30", "end_time": "11:00" }
}
```

`entry_timing`: `candle_close` | `immediate_touch`.
`stop_logic`: `atr_divided_by_2` | `fixed_distance` | `just_beyond_level`.
`sector_map` (optional) overrides the per-ticker sector proxy; otherwise it
defaults from `config.ENTRY_CANDIDATE_PROXY` (e.g. AMD → XLK).

### Output

```json
{
  "summary": {
    "total_trades": 50, "wins": 20, "losses": 30, "skips": 0,
    "win_rate_percent": 40.0, "avg_win_r": 2.0, "avg_loss_r": -1.0,
    "expectancy_per_trade": 0.2
  },
  "trades": [
    {
      "date": "2026-05-28", "ticker": "AMD", "level_type": "Y-Low",
      "volume_spike": true, "direction": "Long", "entry_time": "09:45",
      "entry_price": 105.86, "stop_price": 99.5, "target_price": 118.58,
      "exit_price": 118.60, "exit_time": "10:20", "outcome": "Win",
      "r_result": 2.0, "spy_direction": "Down", "sector_direction": "Down", "notes": ""
    }
  ],
  "coverage": { "complete": true, "missing": [], "perSymbol": { } },
  "warnings": []
}
```

---

## Data

The backtester needs **5-minute intraday bars**, which live in the new
`intraday_bars` table (append-only, source-priority resolved — same model as the
daily `bars` table). Schwab's price-history endpoint supplies them
(`frequencyType=minute`); Yahoo is the fallback but only serves ~60 days of
intraday history and is rate-limited.

If a date range has no stored bars for a symbol, the run reports a coverage gap
rather than failing; **Backfill** fetches and stores the missing sessions. The
backfill pulls each ticker plus its sector proxy and SPY (needed for market
context). A Schwab token that has lapsed surfaces as a per-symbol error with the
re-authorization hint, exactly like the rest of the dashboard.

### Backfill / verify from the CLI

Schwab is only contacted where its secrets live (the Fly machine). To pull or
check 5-minute history directly — handy for the first backfill or to confirm the
live Schwab path — `fly ssh console` onto the app and run:

```bash
cd backend
python cli.py backtest-coverage --symbols AMD,HOOD --start 2026-05-15 --end 2026-06-14
python cli.py backtest-backfill --symbols AMD,HOOD --start 2026-05-15 --end 2026-06-14
```

`backtest-backfill` prints a per-symbol report (`rowsWritten`, `source`, and any
`error`) and lists the provider chain it used — `["schwab", "yahoo"]` when the
Schwab secrets are present, `["yahoo"]` otherwise.

> Schwab serves a limited window of minute history (recent months), so very old
> date ranges may come back empty even with valid credentials — verify with
> `backtest-coverage` after a backfill.

---

## Extending it

The walk-forward loop is fixed; the *rules* are pluggable registries in
`backend/backtest.py`:

```python
@register_setup("opening_range_break")
def _detect_orb(candle, *, levels, avg_volume, cfg):
    # return {"direction", "level", "level_type", "volume_spike"} or None
    ...

@register_stop("percent_of_entry")
def _stop_pct(direction, *, level, entry, atr, cfg):
    pad = entry * cfg["stop_params"]["pct"] / 100
    return entry - pad if direction == "Long" else entry + pad
```

A new setup type or stop style is a single isolated function; `validate_config`
picks it up automatically and the UI's dropdowns are the only other place to add
a label.

---

## Notes & limitations

- One trade per ticker per day (first qualifying setup).
- Same-candle target+stop resolves as a **stop** (conservative).
- Unfilled trades at session end are marked to the last close (`r_result` from
  the realized move) and noted "closed at session end".
- Times and stored candles are **America/New_York**.
- Not financial advice — this replays mechanical rules over historical bars.

See `backend/test_backtest.py` for worked examples with hand-checked
entry/stop/target/outcome values.
