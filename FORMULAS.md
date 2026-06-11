# Indicator formulas

Exact definitions of every value the dashboard computes, as implemented in
`backend/indicators.py` and `backend/macro.py`. All indicators are computed
**inside scheduled ingestion** from bars stored in SQLite — never in the
request path. Where a definition is known to differ from thinkorswim, that is
called out; **the TOS value remains the source of truth when you use the
manual override fields.**

All price series are daily closes from the canonical stored bars (best
provider per date: schwab > yahoo). Schwab daily bars are the primary target,
so the defaults use common thinkorswim daily-study settings where applicable.

---

## RS3M — 3-month relative strength vs SPY

Matches the supplied Thinkorswim formula:

```
rs      = close_sym[t] / close_spy[t]
past    = rs[t-63]
RS3M    = (rs / past - 1) × 100
```

- Lookback is `RS3M_LOOKBACK = 63` **trading rows**, the standard daily-bar
  approximation for three trading months. Code: `rs3m_series()`.
- Dates where either series has no close are dropped before alignment.
- Schwab daily bars are preferred by ingestion, so these calculations run on the
  same OHLCV fields that can be pulled from Schwab.
- Config knobs `RS3M_METHOD="ema"`, `RS3M_METHOD="return_spread"`,
  `RS3M_EMA_SPAN`, `MOM_SMOOTH`, and `MOM_SCALE` remain available for legacy or
  custom calibration. The default `RS3M_METHOD="ratio"` is the supplied study.
  Manual overrides are still stored with `source="manual"` and beat ingested values.

## RS3M_MOM — relative-strength acceleration

Matches a thinkorswim momentum column that compares the current RS3M plot to
the same plot from the momentum lag ago. With the default 63-bar RS3M lookback
and 5-bar momentum lag:

```
rs           = close / close("SPY")
RS3M_current = ((rs[t]   / rs[t-63]) - 1) × 100
RS3M_prior   = ((rs[t-5] / rs[t-68]) - 1) × 100
RS3M_MOM     = if RS3M_prior != 0 then
                 (RS3M_current - RS3M_prior) / RS3M_prior × 100
               else 0
```

Code: `rs3m_momentum_from_closes()`. `RS3M_MOM_PAST_END_LAG = 68`
means the momentum lag is `68 - RS3M_LOOKBACK`, or 5 bars with the defaults.
`RS3M_MOM_PAST_LOOKBACK` remains in the config/API payload for backward
compatibility but is not used by the TOS-compatible momentum calculation.

`rs3mTrend` is "up" when today's RS3M_MOM is above yesterday's recomputation,
"down" if below, otherwise "flat".

## RSI — 14-period Wilder average

```
deltas        = daily close changes
seed_gain     = mean(positive deltas over the first 14 changes)
seed_loss     = mean(|negative deltas| over the first 14 changes)
avg_gain[t]   = (avg_gain[t-1] × 13 + gain[t]) / 14
avg_loss[t]   = (avg_loss[t-1] × 13 + loss[t]) / 14
RSI           = 100 - 100 / (1 + avg_gain / avg_loss)    # 100 if avg_loss = 0
```

Code: `rsi()`. Default `RSI_METHOD = "wilder"`, matching thinkorswim's
default RSI study. `RSI_METHOD = "simple"` remains available for the earlier
plain latest-14-change average.

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

Matches the supplied Thinkorswim formula:

```
VolumeRatio = volume[t] / SMA(volume, 20)[t] × 100
```

Code: `volume_ratio()`. The denominator includes the current bar, as a daily
Thinkorswim `MovingAverage(AverageType.SIMPLE, volume, 20)` value does.

## VolumeAccel — today vs 5-day average volume

Matches a thinkorswim-style volume acceleration column:

```
VolumeAccel = volume[t] / SMA(volume, 5)[t] × 100
```

Code: `volume_acceleration()`. The denominator includes the current bar, just
like thinkorswim daily moving-average studies.

## Accumulation/Distribution Line

```
mfm = ((close - low) - (high - close)) / (high - low)
mfv = mfm × volume
ADL = cumulative_sum(mfv)
```

Code: `accumulation_distribution()` and `accumulation_distribution_trend()`.
The trend field reports `rising`, `flat`, or `falling` by comparing the latest
ADL to its EMA20 and five-bar slope.

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
MA21 = mean(close[t-20 .. t])   # 21 daily bars
```

Code: `moving_average()` / `compute_all()`. Default `MA21_METHOD = "sma"`,
matching thinkorswim's `SimpleMovingAvg(21)`. `MA21_METHOD = "ema"` preserves
the earlier exponential behavior. `priceAboveMA21 = close > MA21`.

---

## Level 1 macro inputs

### VIX
Latest stored VIX ETF proxy close. The default proxy is `VIXY`, and `VIX_PROXY_SYMBOL` can override it (for example to `VXX`).

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
