# Indicator formulas

Exact definitions of every value the dashboard computes, as implemented in
`backend/indicators.py` and `backend/macro.py`. All indicators are computed
**inside scheduled ingestion** from bars stored in SQLite — never in the
request path. Where a definition is known to differ from thinkorswim, that is
called out; **the TOS value remains the source of truth when you use the
manual override fields.**

All price series are daily closes from the canonical stored bars (best
provider per date: schwab > yahoo).

---

## RS3M — 3-month relative strength vs SPY

```
sym_ret = (close_sym[t] / close_sym[t-90] - 1) × 100      # 90 daily rows
spy_ret = (close_spy[t] / close_spy[t-90] - 1) × 100
RS3M    = sym_ret - spy_ret
```

- Lookback is `RS3M_LOOKBACK = 90` **trading rows**, not calendar days
  (~4.3 calendar months). Code: `rs3m_series()`.
- Dates where either series has no close are dropped before alignment.

**Known difference vs thinkorswim:** your TOS RS3M studies are EMA-based and
scaled (readings like +500/+884/+1128). This raw return-spread produces values
in single/low-double-digit percent. The two agree directionally but not in
magnitude. Config knobs `RS3M_METHOD="ema"`, `RS3M_EMA_SPAN`, `MOM_SCALE`
exist to approximate TOS, but **when they diverge, trust thinkorswim and use
the override fields** — overrides are stored with `source="manual"` and always
beat ingested values.

## RS3M_MOM — relative-strength acceleration

```
window  = latest 10 RS3M readings (RS3M_MOM_WINDOW = 10)
avg     = mean(window)
RS3M_MOM = ((RS3M[t] - avg) / |avg|) × 100
```

Code: `rs3m_momentum()`. Same thinkorswim caveat as RS3M: magnitudes are not
comparable to your TOS study; the sign/trend is. `rs3mTrend` is "up" when
today's RS3M_MOM is above yesterday's recomputation, "down" when below.

## RSI — 14-period, simple averages

```
deltas    = last 14 daily close changes
avg_gain  = sum(positive deltas) / 14
avg_loss  = sum(|negative deltas|) / 14
RSI       = 100 - 100 / (1 + avg_gain / avg_loss)        # 100 if avg_loss = 0
```

Code: `rsi()`. This is the **simple-average** RSI. thinkorswim's default
`RSI` study uses Wilder smoothing (`WildersAverage`), which reacts more
slowly; values typically differ by a few points. Verified against the
project's reference fixture (70.46 on Wilder's classic series is the Wilder
value; the simple version is asserted in tests at its own fixture values).

## OBV trend

```
OBV[t]   = OBV[t-1] + sign(close[t] - close[t-1]) × volume[t]
signal   = EMA20(OBV)
slope    = OBV[t] - OBV[t-5]
"rising"  if OBV > signal and slope > 0
"falling" if OBV < signal and slope < 0
"flat"    otherwise
```

Code: `obv_trend()`.

## VolumeRatio — today vs 20-day average

```
VolumeRatio = volume[t] / mean(volume[t-20 .. t-1]) × 100
```

Code: `volume_ratio()`. The denominator excludes today.

## VolumeAccel — 5-day vs previous 5-day average volume

```
VolumeAccel = mean(volume[t-4 .. t]) / mean(volume[t-9 .. t-5]) × 100
```

Code: `volume_acceleration()`.

## MFI — Money Flow Index, 14 periods

```
tp        = (high + low + close) / 3
raw_flow  = tp × volume
pos_sum   = sum of raw_flow over the last 14 rows where tp rose
neg_sum   = sum of raw_flow over the last 14 rows where tp fell
MFI       = 100 - 100 / (1 + pos_sum / neg_sum)          # 100 if neg_sum = 0
```

Code: `mfi()`.

## MA21

```
MA21 = EMA(close, span=21, adjust=False)   — the LAST value
```

Code: `compute_all()`. **Note: this is an exponential MA, not a simple MA.**
If your thinkorswim chart uses `SimpleMovingAvg(21)`, the values will differ
slightly (EMA hugs price more closely). `priceAboveMA21 = close > MA21`.

---

## Level 1 macro inputs

### VIX
Latest stored `^VIX` close (Schwab `$VIX` when available, else Yahoo `^VIX`).

### Breadth
Percent of the configured ETF universe (`BREADTH_SYMBOLS`: SPY, QQQ, IWM,
^NYA + 11 sector ETFs) whose close is **above its 50-day simple MA**:

```
breadth = round(100 × count(close > SMA50) / count(universe))
```

Code: `macro.breadth_from_bars()`. This is a proxy, not the NYSE %-above-50dma
statistic — with 15 members it moves in ~6.7% steps.

### Inflation
CPI year-over-year from FRED `CPIAUCSL`:
`(CPI[latest] / CPI[latest-12 months] - 1) × 100`. Code: `inflation_from_cpi()`.

### Growth
Real GDP momentum from FRED `GDPC1`. QoQ annualized:
`((GDP[q] / GDP[q-1])^4 - 1) × 100`; "accelerating"/"slowing" when it differs
from the prior quarter's reading by more than ±0.5pp, else "stable".
Code: `growth_from_gdp()`.

### Fed policy
Scored model over FRED `DFF` (fed funds), `CPIAUCSL`, `GDPC1`, `UNRATE`:
hawkish/dovish conditions are tallied (rate trend over 63 trading days ±0.25,
CPI YoY vs 3.0%/2.6%, CPI 3-month trend ±0.2pp, growth firm/slowing,
unemployment ≤4.2 tight / ≥4.5 or +0.3pp softening, real policy rate ≥1.0
restrictive / ≤0.25 accommodative). `score = hawkish - dovish`;
`≥ +2 → "hawkish"`, `≤ −2 → "dovish"`, else `"holding"`.
Code: `macro.classify_fed_policy()`.

---

## Staleness (Phase 4)

A value's `as_of` is compared to the **last completed NYSE trading session**
(`backend/market_calendar.py`, hardcoded NYSE holidays 2024–2028):

- **fresh** — covers the last completed session (Saturday data is not stale
  on Sunday)
- **yellow** — exactly 1 trading session behind
- **red** — 2+ sessions behind, or the value's latest fetch was quarantined

FRED-derived fields (fed/growth/inflation) use ingestion age instead — a CPI
print being a month old is normal; what matters is that ingestion keeps
succeeding (fresh ≤36h, yellow ≤96h, red beyond).
