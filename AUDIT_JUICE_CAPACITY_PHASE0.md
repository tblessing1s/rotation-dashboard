# AUDIT — Trailing Juice Capacity Metric (Phase 0)

**Scope:** the written audit required before any implementation of the shadow-mode
juice-capacity metric. No implementation code was written. Every claim below carries
a `file:line` citation; where the repo does not settle a question the answer is
marked **UNKNOWN — human verification required** rather than guessed.

**Baseline:** `python -m pytest backend -q` → **1259 passed** on
`claude/trailing-juice-capacity-metric-oulrt6` at audit time. (The container's
system `cryptography` was initially broken — `ModuleNotFoundError: No module named
'_cffi_backend'` — producing 12 unrelated collection/runtime failures; reinstalling
`cffi`/`cryptography` cleared them. Nothing in the tree was modified.)

---

## Headline findings (read these first)

Three facts change the shape of the work relative to the prompt's assumptions:

1. **The scan's juice number never touches an option chain.** It is a
   Black–Scholes price computed from the *cached daily OHLCV frame* at the
   ticker's trailing 20-day **realized** vol (`account_gate.juice_estimate`,
   `backend/account_gate.py:53-150`). No IV, no chain fetch, no clock, no state.
   → §0.1, §0.2.

2. **Therefore backfill is exact, not approximate.** Because the metric is a pure
   function of the frame, replaying it over `df.iloc[:i+1]` reproduces the *same
   function on the same inputs* — it is not an IV proxy standing in for something
   else. The prompt's option (c) is not a lossy approximation of (b); it is the
   metric itself, evaluated historically. This is empirically verified in §0.2(c).
   **Recommended primary path: (c) bar-replay backfill, with (a) forward
   accumulation as the ongoing mechanism.**

3. **The scan's strike is NOT regime-aware.** `juice_estimate` prices a flat
   `config.SHORT_ATR_MULT = 1.5` strike (`backend/config.py:473`,
   `backend/account_gate.py:89`). The regime × posture table
   (`config.STRIKE_TABLE`, `backend/config.py:583-587`) and the documented
   HARD_CFM_RULE multiples (`STRIKE_ATR_MULT_GREEN/YELLOW`,
   `backend/config.py:605-606`) drive the *option-chain drawer's* live suggestion
   only — and the two schemes disagree with each other, a divergence the repo
   already flags as a deliberately-deferred follow-up (`backend/config.py:596-603`).
   The prompt's "regime-appropriate strike (Green 1.5×ATR, Yellow 2.0×ATR)" is
   therefore **not** what the scan's GROSS/WK measures today. → §0.1, and the
   decision requested in **Open questions Q1**.

---

## 0.1 Current juice computation

### The formula

Single site: **`backend/account_gate.py:53-150`**, `juice_estimate(ticker, df)`.
Called once per scan row at **`backend/metrics/scorecard.py:506`**, its
`weekly_yield_pct` landing on the row as `juice_weekly_pct`
(`backend/metrics/scorecard.py:516`) — the column the UI labels **Gross/wk**
(`frontend/src/components/Scorecard.jsx:112-117`).

| Input | Value | Citation |
|---|---|---|
| Spot `S` | last daily close (or the live `price_override` when one was passed) | `account_gate.py:76`; override applied at `metrics/scorecard.py:462` |
| `atr_val` | Wilder ATR, 9 bars (`config.ATR_WINDOW = 9`) | `account_gate.py:77`; `indicators.py:71-86`; `config.py:373` |
| `hv` (σ) | **annualized trailing 20-day REALIZED vol**, `std(log returns) × √252 × 100` | `account_gate.py:78`; `indicators.py:169-180` |
| `r` | `config.RISK_FREE_RATE = 0.04` | `account_gate.py:86`; `config.py:472` |
| **Strike selection** | `k_short = indicators.short_strike(S, atr_val)` = `round((S − 1.5·ATR)·2)/2` — **flat 1.5×, no regime, no posture, no ITM% floor** | `account_gate.py:89`; `indicators.py:431-434`; `config.py:473` |
| **Expiry selection** | `t_week = 5/365` — a hard-coded constant, **not** a real expiration date and not the `WEEKLY_MIN_COMPARISON_DTE` logic the chain view uses | `account_gate.py:90`; cf. `config.py:616` |
| Option price | `indicators._bs_call_price(S, k_short, t_week, r, sigma)`, `q = 0` (dividend yield **not** passed) | `account_gate.py:91`; `indicators.py:481-484` |
| **Extrinsic extraction** | `extr_w = max(price_w − max(S − k_short, 0), 0)` — model price minus intrinsic, per share | `account_gate.py:92` |
| **Notional denominator** | shares mode: `extr_w / S × 100` → **covered-call yield on spot** (`config.LEGACY_LEAP_READONLY` is a hard `True`, `config.py` / verified at runtime) | `account_gate.py:103-109` |
| | legacy mode: `extr_w / leap_cost × 100`, with `net = gross − burn` | `account_gate.py:110-124` |

Shares mode is the live path: `net_weekly_yield_pct == weekly_yield_pct`, `burn = 0`
(`account_gate.py:106-109`). So the scan's Gross/wk, Net/wk and covered-call yield
are all the same number today.

**Purity:** no I/O, no clock, no state, no provider call. Verified empirically —
see §0.2(c).

### Dividend leg (already wired)

`combined_weekly_yield(juice, annual_div_pct)` = `juice%/wk + annual_div_pct/52`
(`backend/scan_triggers.py:357-388`), attached to the row at
`backend/metrics/scorecard.py:542-545` as `combined_weekly_yield_pct` /
`dividend_weekly_pct` / `dividend_known`. `DIVIDEND_WEEKS_PER_YEAR = 52`
(`config.py:535`). The day-count mismatch (juice on a 7-calendar-day base, dividend
on 52 weeks) is ~1.8% *of the dividend leg* and is already pinned by
`test_dividend_profile` (`scan_triggers.py:363-368`).

### The second, unrelated juice path (do not confuse them)

`backend/option_chain.py:569-683` computes a **live-chain** weekly number for the
per-name drawer: regime-aware strike via `strike_policy.suggest_strike`
(`option_chain.py:577`, `strike_policy.py:46-51`), real expirations, real marks, and
`weekly_juice_estimate` denominated in **dollars against LEAP payback**
(`option_chain.py:677`), not %/wk. It also feeds `iv_history.record`
(`option_chain.py:606`). It is on-demand, per name, and does **not** produce the
scan's GROSS/WK. Only `juice_estimate` does.

### Is any computed juice value persisted today? **YES — partially.**

`backend/scan_rejection_log.py` already persists, per symbol per scan run:

- `juice_weekly_pct` (`scan_rejection_log.py:145`)
- `combined_weekly_yield_pct` (`:143`)
- `dividend_weekly_pct` (`:144`)
- `annual_dividend_yield_pct` (`:142`)
- `net_juice_weekly_pct` (`:128`), `income_profile` (`:141`), `iv_rank` (`:134`),
  `price` (`:118`), plus the shadow-floor verdict (`:146-150`)

**Shape:** `DATA_DIR/scan_rejection_log.json`, `{"symbols": {TICKER: [record, …]}}`,
each record `{date, scan_id, schema, …}` (`scan_rejection_log.py:33`, `:371-372`).
**Retention:** `config.SCAN_REJECTION_LOG_DAYS = 180` **distinct dates**
(`config.py:299`; trim at `scan_rejection_log.py:326-337`). **Writer:** exactly one
— `maintenance.nightly_refresh` (`maintenance.py:215-223`). **Idempotence:**
append-per-scan-run keyed on `scan_id`; re-writing the same `scan_id` replaces that
run's point (`scan_rejection_log.py:373-380`).

**What this does and does not give us.** It is a real, live juice series — but it is
a *by-product* store whose retention (180 days), key shape (one list per symbol,
mixed-schema records) and semantics (append-per-run, so a day can carry several
points) are tuned for gate calibration, not for a 252-day median. Reading the
capacity median directly out of it would (a) couple the capacity metric to a
retention window shorter than `CAPACITY_WINDOW_DAYS = 252`, (b) require de-duping
multiple same-day runs, and (c) make a future schema change to the rejection log a
silent change to capacity. **Recommendation: a dedicated store (§0.4), with the
rejection log used only as an optional one-time seed** — see §0.2(a).

---

## 0.2 Feasibility of the trailing median — three candidate sources, ranked

### (a) Forward accumulation — **AVAILABLE, and the ongoing mechanism**

Always feasible; the number is already computed for display, so persisting it costs
one JSON write per sweep and zero provider calls (§0.5).

**Storage shape** (matching `iv_history` / `regime_history`, see §0.4):

```
DATA_DIR/juice_capacity.json
{"symbols": {"ET": [{"date": "2026-08-24",
                     "juice_wk_pct": 0.14,
                     "dividend_wk_pct": 0.17,
                     "combined_wk_pct": 0.31,
                     "strike_used": 16.5,
                     "spot": 17.2,
                     "atr_mult": 1.5,
                     "regime": "green",
                     "source": "live"}, …]}}
```

**Emission point:** the completion of a full-universe `_compute_scorecard`
(`backend/metrics/scorecard.py:829-885`) and/or alongside
`scan_rejection_log.record_scan` in `maintenance.nightly_refresh`
(`maintenance.py:215-223`). See §0.5 for why that boundary and not `score_ticker`.

**Time to usefulness:** `CAPACITY_MIN_OBS = 20` observations ≈ 20 trading days ≈ 4
calendar weeks from first deploy, if emitting once per trading day.

**Optional seed:** ~180 days of `juice_weekly_pct` / `combined_weekly_yield_pct`
already sit in `scan_rejection_log.json` for whatever names have been swept since
that store went live. A one-time import (de-duped to one point per date, stamped
`source: "seed_rejection_log"`) would clear the min-obs guard immediately for those
names. **Recommend offering this as a separate, explicitly-invoked seeder, not as
part of the emission path** — it inherits the rejection log's per-run duplication
and mixed-schema history, and its provenance must stay distinguishable forever.

### (b) Historical option chains — **NOT AVAILABLE in-repo; partly UNKNOWN**

**Schwab.** The client exposes exactly four market-data endpoints
(`backend/schwab_api.py:35-38`): `pricehistory`, `quotes`, `chains`, `instruments`.
`get_option_chain` (`schwab_api.py:365-392`) sends `symbol`, `contractType`,
`strikeCount`, `includeUnderlyingQuote`, and optionally `expirationDate` /
`fromDate` / `toDate`. **`fromDate`/`toDate` filter which EXPIRATIONS are returned,
not an as-of quote date** — they are expiry-window bounds, and the marks come back
live. There is no as-of/historical parameter anywhere in the client, and no chain
snapshot is ever persisted (`grep` for a chain store returns nothing; the only
chain-derived persistence is the single ATM IV point at `option_chain.py:606`).
→ **No Schwab backfill.** Whether Schwab's API offers a historical-chain product the
repo simply doesn't implement is **UNKNOWN — human verification required.**

**Alpha Vantage.** The client implements four functions (`backend/alpha_vantage.py`):
`TIME_SERIES_DAILY` (`:66`), `EARNINGS_CALENDAR` (`:114`), `OVERVIEW` (`:127`),
`GLOBAL_QUOTE` (`:133`). **No options function of any kind.** Alpha Vantage's public
product list is not documented anywhere in this repo, so whether a
`HISTORICAL_OPTIONS` endpoint exists, what its history depth is, and whether the
account's key tier can reach it are all **UNKNOWN — human verification required.**
Even if it exists it would need a new adapter, a new budget line
(`backend/data_budget.py`), and — critically — it would be measuring a **different
quantity** from the live metric (real market marks vs. HV-priced BSM), so its
observations could not be pooled into the same median without biasing it. That
alone argues against (b) as the primary path even if the endpoint turns out to exist.

### (c) Bar-replay backfill — **AVAILABLE, EXACT, and the recommended primary**

The prompt frames (c) as "approximate historical achievable juice from historical
price bars + a historical IV series … state what error that introduces." **There is
no IV in the live computation to approximate.** `juice_estimate` prices the weekly
short at trailing realized vol from the same bars (`account_gate.py:78, 85`). So the
"proxy" *is* the metric. Replaying it over a frame prefix yields the number the scan
would have shown on that date, not an estimate of it.

**BSM engine interface:** `indicators._bs_call_price(S, K, T, r, sigma, q=0.0)`
(`backend/indicators.py:481-484`), on `_d1` (`:469-470`) and `_norm_cdf` (`:461-462`).
The put-IV substitution convention for deep-ITM calls (`implied_vol_put`,
`indicators.py:517-537`) is used only where a **provider mark** must be inverted to
a vol — the LEAP delta path. `juice_estimate` never inverts a mark: it feeds HV
straight in as σ. So the substitution convention is **not on this code path at all**
and introduces no error here.

**Empirical verification** (run against `fixtures/structure/early_advance_low_juice.parquet`,
270 bars):

```
full-frame  : spot 129.9  strike 129.5  extr/sh 0.079  juice 0.06%/wk  hv 2.0
prefix idx 100 (2023-05-22): juice 0.06%/wk  hv 2.7
prefix idx 150 (2023-07-31): juice 0.06%/wk  hv 2.5
prefix idx 200 (2023-10-09): juice 0.06%/wk  hv 2.8
prefix idx 269 (2024-01-12): juice 0.06%/wk  hv 2.0   ← equals the full-frame value
```

**Anchoring requirement.** Wilder ATR is an EWM seeded from the first bar
(`indicators.py:84`), so it is prefix-causal **only across prefixes sharing bar 0** —
the same canonical-start rule `regime_history.backfill` states explicitly and pins
in tests (`regime_history.py:190-196`; `config.py:377-385`). Measured drift from a
shifted start at 150 bars of warm-up is ~7e-10 (negligible in value, but **not
bit-identical**), so the replay must anchor at `df.iloc[:i+1]` from index 0, never a
rolling sub-window. `hist_vol` is window-local and unaffected
(`indicators.py:169-180`, verified identical under a shifted start).

**Precedent to copy:** `backend/calibration.py:46-73` (`collect_rows`) already walks
`df.iloc[:i+1]` over cached frames re-running the scorecard as-of each date, and
`regime_history.backfill` (`regime_history.py:160-210`) already persists a replayed
series with a per-record `backfilled: True` marker.

**Depth available:** `config.HISTORY_DAYS = 400` calendar days ≈ **~275 trading
bars** cached per symbol (`config.py:385`, `data_handler.py:134-136`). Minus the
20-bar HV warm-up plus the 9-bar ATR warm-up, a backfill yields roughly **~250
usable observations** — right at `CAPACITY_WINDOW_DAYS = 252`, and comfortably past
`CAPACITY_MIN_OBS = 20`. Note the caveat already recorded at `config.py:383-384`: a
cache filled under an older, shorter window keeps serving shorter frames until
refreshed, so a cold-ish cache yields fewer observations. The min-obs guard handles
that correctly by construction.

**Two real errors the backfill DOES introduce** (both must be disclosed on the record):

1. **Dividend anachronism.** `dividends.cached_annual_yield_pct` is a
   *point-in-time* day-cached scalar with **no history**
   (`backend/dividends.py:149-192`; cache `dividends_cache.json`, TTL 24h,
   `dividends.py:29-30`). A backfilled observation would necessarily carry
   *today's* yield attached to a past date. For the structural-vs-transient question
   this is nearly harmless (a midstream MLP's yield is stable, which is the whole
   point), but it is an anachronism and must be marked. **Recommendation:** stamp
   backfilled records `dividend_source: "current_yield_anachronistic"` and keep the
   juice and dividend legs separable in the record so a later pass can recompute the
   combined figure under a better dividend history if one ever exists.
2. **Regime anachronism, only if the strike becomes regime-aware.** Today's scan
   strike is flat 1.5× (§0.1), so there is nothing to anachronise. If Q1 below is
   answered "make it regime-aware," the backfill would need the published regime
   as-of each date — which **does exist**: `regime_history.py` stores it per day and
   can itself backfill from cached SPY bars (`regime_history.py:160-210`). So the
   dependency is satisfiable, but it makes the capacity backfill depend on the
   regime store being backfilled first.

### Recommendation

**Primary: (c) bar-replay backfill + (a) forward accumulation**, as one mechanism
with two sources. Backfilled records carry `source: "backfill_bar_replay"` and are
distinguishable from `source: "live"` forever, per the `regime_history` precedent.

**Fallback: (a) alone.** If the backfill is deemed too much surface for this prompt,
forward accumulation plus the `CAPACITY_MIN_OBS` guard is sufficient — the prompt
explicitly permits it, and prompt 2 treats `INSUFFICIENT_HISTORY` as unsuppressible,
so nothing behaves wrongly during the ~4-week accrual.

**Do not pursue (b).** No endpoint in-repo, and even if one exists its observations
measure a different quantity (real marks vs. HV-priced BSM) and cannot be pooled
into the same median without biasing it.

**Tradeoff, stated plainly:** (c) buys ~250 observations on day one instead of ~20 in
four weeks, at the cost of a replay loop over ~500 names × ~250 bars (roughly the
same shape as `calibration.collect_rows`, which is already an accepted offline cost)
and the two disclosed anachronisms above. If the operator would rather eyeball the
metric against real names *before* trusting a replayed median, (a)-only is the more
conservative sequencing and delays prompt 2 by about a month.

---

## 0.3 Dividend data availability — **a real path EXISTS; do NOT stub**

The prompt's recommendation to stub assumes no data path. There is one, and it is
already on the scan row.

- **Yields:** `backend/dividends.py`. Sources: Schwab fundamentals `divYield`
  (`dividends.py:102-112`, via `schwab_api.get_instrument_fundamental`
  `schwab_api.py:394-408`), then Alpha Vantage `OVERVIEW.DividendYield`
  (`dividends.py:113-119`, `alpha_vantage.py:127`). Manual override in
  `state.metadata.dividend_overrides` always wins (`dividends.py:96-100`).
  Day-cached in `DATA_DIR/dividends_cache.json` (`dividends.py:28-29`).
- **The scan-safe read:** `dividends.cached_annual_yield_pct(ticker, state)` —
  **cache-only, never fetches**, returns annual **percent**, and returns `None`
  (not `0`) when unresolved (`dividends.py:179-192`, on
  `cached_yield_with_source` `:149-177`). This is the one place the decimal→percent
  conversion and the "unknown is not zero" rule live.
- **Already on the row:** resolved once per ticker per sweep by
  `screening.resolve_profile_detail` (`backend/screening.py:562-588`), threaded into
  `score_ticker` (`metrics/scorecard.py:465-471`) and combined at
  `metrics/scorecard.py:542`.
- **Declared/realized distributions (per-payment):** `backend/dividend_calendar.py`
  defines an explicit adapter contract — `ex_date`, `pay_date`, `amount` (per share,
  per payment, **never annualized, never a yield**), `frequency`, `source`
  (`dividend_calendar.py:11-45`) — consumed by `dividends.next_dividend` /
  `cached_dividend` (`dividends.py:274-324`).

**Consistency with the accrual-ledger rule.** The prompt requires the dividend leg
to use "realized/declared distributions only." The existing `dividend_weekly_pct` is
derived from a **trailing/quoted annual yield ÷ 52**, which is a provider
fundamentals figure, not a realized-distribution sum. Two options:

- **(i) Reuse `combined_weekly_yield` verbatim** (`scan_triggers.py:357-388`).
  Pro: the capacity metric and the shadow floor then measure *exactly* the same
  quantity, so they can never disagree — which is the same "one place per side"
  discipline `CLAUDE.md` mandates for the ×100 factor. Con: the yield is quoted, not
  strictly realized.
- **(ii) Build a realized-distribution weekly rate** from `dividend_calendar`
  amounts. Pro: strictly satisfies "realized/declared only." Con: it is a **new
  dividend computation**, and there is no per-symbol distribution *history* store —
  `cached_dividend` holds the *next* event, not a trailing series. Building one is a
  dividend fetcher, which §1.6 explicitly puts out of scope.

**Recommendation: (i).** Reuse `combined_weekly_yield` unchanged, record
`dividend_known` alongside so an unresolved yield is never persisted as a confident
zero, and record juice and dividend legs separably so a future realized-distribution
series can be swapped in without invalidating the stored juice history. Flag the
quoted-vs-realized gap on the record (`dividend_basis: "quoted_annual_yield"`) so it
is visible rather than assumed. **Do not build a dividend fetcher in this prompt.**

The stub path (`dividend_wk_pct = 0` + `DIVIDEND_STUBBED`) should still be
implemented as the **unresolved-yield branch** — it is exactly the `dividend_known:
False` case — but it will be the exception, not the default.

---

## 0.4 Persistence model

### Where it belongs: a standalone `DATA_DIR` telemetry store, NOT `state.json`

The repo's rule is explicit and consistently applied: `state.json` is the
append-only **trading record**; anything DERIVED from market data lives in a
separate store under `DATA_DIR` and is **not** rebuilt by
`logging_handler.recompute_derived()` (which keys off executions only). Stated at
`scan_rejection_log.py:17-22`, `regime_history.py:3-9`, `iv_history.py:11-16`,
`burn_marks.py:17-20`. Capacity observations are derived market data → same
treatment.

### The pattern to match

| Store | File | Key shape | Per-day rule | Retention |
|---|---|---|---|---|
| `iv_history` | `DATA_DIR/iv_history.json` (`iv_history.py:27`) | `{TICKER: [{date, iv}]}` | **last write of the day replaces that day's point** (`:77-81`) | `_MAX_POINTS = 260` (`:28`) |
| `regime_history` | `DATA_DIR/regime_history.json` (`regime_history.py:31`) | `{"records": [{date, backfilled, …}]}` | last write of the day replaces (`:108-115`) | `REGIME_HISTORY_DAYS` (`:116`) |
| `symbol_genius_history` | `DATA_DIR/symbol_genius_history.json` | per-ticker daily color (`:157-163`) | one point/day | `SYMBOL_GENIUS_HISTORY_DAYS = 90` (`config.py:295`) |
| `scan_rejection_log` | `DATA_DIR/scan_rejection_log.json` (`:33`) | `{"symbols": {TICKER: [rec]}}` | **append per SCAN RUN**, idempotent per `scan_id` (`:373-380`) | 180 **distinct dates** (`config.py:299`) |
| `structure_labels` | `DATA_DIR/structure_labels.json` (`structure_labels.py:39`) | — | — | — |
| `burn_marks` | `DATA_DIR/burn_marks.json` (`burn_marks.py:17-19`) | weekly marks | — | — |

All six share the same mechanics: module-level `_lock = threading.RLock()`, `_load()`
that returns an empty skeleton on any `OSError`/`ValueError`, `_save()` that writes
`f"{PATH}.tmp.{os.getpid()}"` then `os.replace` (atomic), and a best-effort writer
that **never raises into its caller** (`scan_rejection_log.py:385-386`;
`regime_history.py:129-130`).

### Recommendation

New module **`backend/juice_capacity.py`**, `DATA_DIR/juice_capacity.json`, shaped
`{"symbols": {TICKER: [obs]}}` like `scan_rejection_log`, but with **`iv_history`'s
per-calendar-day last-write-wins rule** (`iv_history.py:77-81`) rather than
`scan_rejection_log`'s append-per-run.

**Why last-write-wins and not append-per-run.** The median is over *dates*, not over
scan runs. Under `SCAN_WARM_INTERVAL_MINUTES = 4` (`config.py:398`) a busy day can
produce many sweeps; appending each would let one heavily-rescanned day outvote
twenty quiet ones and silently bias the median. One observation per symbol per
calendar day is the semantically correct grain, and it is what §1.5's idempotence
test should assert.

**Retention:** `CAPACITY_RETENTION_DAYS` ≥ `CAPACITY_WINDOW_DAYS = 252`, trimmed by
distinct date (reuse the `_trim_to_days` idea at `scan_rejection_log.py:326-337`).
Proposed `280` (`PROPOSED_DEFAULT`) so the 252-day window never sits flush against
the trim boundary.

### Recompute-from-history story

`juice_capacity_wk_pct(symbol)` must be a **pure function over the persisted
observations** — load, filter to the trailing `CAPACITY_WINDOW_DAYS` by date,
project `combined_wk_pct`, `statistics.median`. **No running median, no cached
aggregate, no mutable counter anywhere in the store.** The stored records are
immutable once written (the only permitted mutation is same-day replacement before
the day closes, and the trim). This mirrors `iv_history.iv_rank`
(`iv_history.py:91-118`), which recomputes rank from the raw series on every call
and returns `None` below `_MIN_POINTS = 20` — note that its 20-point floor is the
same number the prompt proposes for `CAPACITY_MIN_OBS`, which is a welcome
consistency rather than a coincidence to hide.

**Recomputability test (§1.5) is therefore trivially satisfiable:** hand-write a
fixture JSON, call the function, assert the median; assert the file's bytes are
unchanged after the read.

---

## 0.5 Scan-loop integration points

### The loop

`backend/metrics/scorecard.py:829-885`, `_compute_scorecard(names, …)`:
prefetch frames (`:846`) → resolve profile + dividend once per ticker (`:866-870`) →
`screening.entry_gate` (`:872`) → `score_ticker` (`:876-882`) → sorted rows.

Wrapped by `scorecard(tickers=None, force=False)` (`:886-961`), which is **cached at
two levels**: `scan_cache` on disk keyed by `(scan_day, fingerprint(regime_color))`
(`scan_cache.py:162-192`, `:103-111`) and a short-TTL memo in `screening._cached`.
**`_compute_scorecard` runs cold at most once per scan-day per regime**, plus
explicit `force=True` rescans and incremental merges for newly-added names
(`scorecard.py:919-943`).

### Where the emission belongs

**Recommended: at the completion of a full-universe `_compute_scorecard`** (after
`rows.sort(...)`, `scorecard.py:882-883`), guarded to the full sweep, **and** as a
best-effort call in `maintenance.nightly_refresh` next to
`scan_rejection_log.record_scan` (`maintenance.py:215-223`) so the day's point is
guaranteed even if no user ever opened the Scan tab.

**Why not inside `score_ticker`.** `score_ticker` is called from many subset paths —
a single-ticker entry snapshot, a per-stock refresh, the recommendation runner. A
write there would mean a JSON load/save per name (≈500 per sweep, on the request
path) and would let a one-ticker refresh write a "scan observation" that the daily
sweep never made. The day-idempotent store makes a duplicate write *harmless*, but
the cost and the semantic muddle are both avoidable. `scan_rejection_log` already
made exactly this call: it is written once per sweep from `maintenance`, never from
`score_ticker`.

### **No new API calls — confirmed**

Every input the observation needs is already on the row before emission:

| Field | Already computed at | Provider call? |
|---|---|---|
| `juice_wk_pct` | `metrics/scorecard.py:516` (`account_gate.juice_estimate`, `account_gate.py:53`) | **No** — pure over the cached frame |
| `dividend_wk_pct` | `metrics/scorecard.py:543-544` | **No** — `cached_annual_yield_pct` is cache-only by contract (`dividends.py:149-160`) |
| `combined_wk_pct` | `metrics/scorecard.py:542-543` | **No** |
| `strike_used` | `juice_estimate(...)["short_strike"]` (`account_gate.py:129`) — currently discarded, not put on the row | **No** — one extra dict key |
| `regime` | `_current_regime_color()`, read **once per sweep** (`scorecard.py:823-828`), threaded to every row (`scorecard.py:880`) | **No** — itself memoized |
| `source` | literal | — |

The only code change needed on the compute side is surfacing `short_strike` (and
optionally `spot`) from the `est` dict already in hand at `metrics/scorecard.py:506`
onto the row. Everything else is a read.

**Frame prefetch** already happens for the whole universe (`scorecard.py:846`), so a
bar-replay backfill (§0.2c) also needs no new provider calls — it reads the same
parquet cache `calibration.collect_rows` reads (`calibration.py:59-61`).

---

## 0.6 Display surfaces

**Component:** `frontend/src/components/Scorecard.jsx`.

- **Expanded row drawer:** rendered by `ScoreRow` at `:479-579` under
  `{expanded && (...)}`. Order inside: binding constraint (`:484-501`) → path to
  READY (`:503-510`) → Genius lights (`:512-522`) → the `Readout` grid ending in
  **`Suitability`** (`:522-560`, the `Suitability` cell at `:559`) → suitability
  reasons (`:562-566`) → notes (`:570-574`) → **`<StructureShadow row={row} />`**
  (`:575`).
- **Recommended insertion point:** a sibling of `StructureShadow`, immediately after
  it at `:575`. That places it directly below SUITABILITY, in the shadow band of the
  drawer, exactly where the prompt asks.
- **Style precedent to copy:** `StructureShadow` (`:362-414`) — a `border-t
  border-slate-800 pt-2` block, a `text-[10px] uppercase tracking-wide
  text-slate-500` heading, a violet observation chip
  (`bg-violet-500/15 text-violet-300`, `:379-382`) and the grey **`NO AUTHORITY`**
  chip (`bg-slate-500/15 text-slate-400`, `:383-385`). The identical `NO AUTHORITY`
  chip also appears on `ShadowFloorLog` (`:974-977`). The comment at `:167-171`
  states the palette rule explicitly: **violet = observation, never the
  emerald/amber/rose the blocking gate uses.**
- **Insufficient-history precedent:** `StructureShadow`'s partial-read footnote
  (`:408-412`) — "Partial read — not enough history for …" — is the exact register
  for `CAPACITY: insufficient history (12 obs)`.
- **Scan table (no change, per §1.3):** the juice columns are `Gross/wk` (`:112-117`),
  `Combined/wk` (`:118-145`) and `Net/wk` (`:146-154`); the shadow `Struct` column
  is at `:166+`. No capacity column is to be added.
- **Data plumbing:** none needed. The drawer renders straight off `row`, and the row
  is serialized wholesale by `/api/scan/scorecard` (`backend/app.py:126-153`,
  `out["results"]` at `:148`). A new row key appears in the UI automatically.
- **Which floor to display next to capacity.** The row carries **two** different
  "floor" numbers and they disagree: `juice_target_pct` (`metrics/scorecard.py:527`,
  from `account_gate.weekly_yield_target_pct`, `account_gate.py:189-212`) is
  **LEAP-denominated** — 1.88%/wk for a growth name, 1.0% for an ETF — while
  `shadow_floor.floor_pct` (`metrics/scorecard.py:548`) is the **share-denominated**
  0.75% (`config.SHARES_JUICE_FLOOR_PCT`, `config.py:498`) or 0.5% for a
  `DIVIDEND_COMPOUNDER` (`COMBINED_YIELD_FLOOR_WK`, `config.py:538`). Capacity is a
  share-notional number, so it must be shown against **`row.shadow_floor.floor_pct`**,
  never `juice_target_pct`. (The prompt's "floor 0.70%" is the low end of the
  0.7–0.8% band whose midpoint the constant encodes as 0.75.)
- **Optional second surface:** a capacity rollup could later join `ShadowFloorLog`
  (`:960-1032`, fed by `/api/scan/rejection-stats`, `app.py:921-934`). **Out of scope
  for this prompt** — §1.3 asks for the row readout only.

---

## Open questions for the operator (blocking Phase 1 sign-off)

**Q1 — Strike basis for capacity. (Material; please decide.)**
The prompt specifies capacity "at the regime-appropriate strike (Green 1.5×ATR,
Yellow 2.0×ATR)". The scan's juice uses a **flat 1.5×ATR with no regime input**
(`account_gate.py:89`, `config.py:473`), and the repo's live regime table encodes a
*third*, differently-shaped scheme (conservative green 0.5×, yellow 1.0×, red 1.5×,
`config.py:583-587`) whose reconciliation with the documented HARD_CFM_RULE
multiples is an explicitly deferred work item (`config.py:596-603`).

- **(A) Match the scan exactly — flat 1.5×ATR.** Capacity then measures the same
  thing the displayed GROSS/WK measures, capacity vs. the current reading is an
  apples-to-apples comparison, and prompt 2's suppression logic rests on a number
  the operator can verify by eye against the scan table. Backfill needs no regime
  history. **Recommended.**
- **(B) Make capacity regime-aware per the HARD_CFM_RULE multiples.** More faithful
  to the strategy document, but capacity would then be denominated differently from
  the GROSS/WK shown beside it — a name could read "capacity 0.85, current 0.42"
  partly because the two used different strikes. It also drags the deferred
  strike-policy reconciliation into this prompt, and makes the backfill depend on
  `regime_history` being backfilled first.

I recommend **(A)**, and that the observation record store `atr_mult: 1.5` and
`regime` (the regime is already in hand, §0.5) so a future switch to (B) is a
re-derivation over retained inputs rather than a lost history.

**Q2 — Backfill: yes or no?** §0.2 recommends yes (path (c)); the prompt permits
(a)-only. This is the single biggest scope lever in Phase 1 and it decides whether
prompt 2 can start in days or in ~4 weeks.

**Q3 — Seed from `scan_rejection_log`?** ~180 days of `juice_weekly_pct` /
`combined_weekly_yield_pct` already exist there (§0.1). Import as
`source: "seed_rejection_log"`, or leave it and rely on Q2's answer?

**Q4 — Dividend basis.** §0.3 recommends reusing `combined_weekly_yield`'s quoted
annual yield ÷ 52 (marked `dividend_basis: "quoted_annual_yield"`) rather than
building a realized-distribution series, which would be a dividend fetcher and is
out of scope per §1.6. Confirm that reading of the "realized/declared only" rule.

**Q5 — The "GDDY Aug 21" canonical fixture does not exist in this repo.**
`fixtures/regime/xlk_july6_rollover.parquet` is present and is pinned by
`test_stock_lights.py:84`, `test_gate_ruleset.py:46-78`, `test_dividend_profile.py:104`,
`test_chart_structure.py:432-450` and `test_recommendation_engine.py:293-305`. There
is **no GDDY artifact** anywhere (`grep -ri gddy` finds it only as a ticker in
`tickers_by_sector.txt:17`). Options: (i) pin the XLK July-6 fixture plus the
existing `fixtures/structure/early_advance_low_juice.parquet` — the PNC-shaped
low-vol name (`fixtures/structure/build_fixtures.py:194-208`), which is precisely
the structural-low-juice case this metric exists to characterize — or (ii) supply
the GDDY bars/date and a new fixture will be built. **(i) recommended**; it is the
better regression pin for *this* feature and needs nothing from outside the repo.

---

## Constants proposed (all `TRAVIS_EXTENSION` + `PROPOSED_DEFAULT`)

| Constant | Value | Note |
|---|---|---|
| `CAPACITY_WINDOW_DAYS` | 252 | per the prompt |
| `CAPACITY_MIN_OBS` | 20 | per the prompt; equals `iv_history._MIN_POINTS` |
| `CAPACITY_RETENTION_DAYS` | 280 | new — window plus headroom so the trim never clips the window |
| `CAPACITY_STRIKE_ATR_MULT` | `config.SHORT_ATR_MULT` (1.5) | only if Q1 → (A); a named alias so a future switch is one edit |

## Authority contract (restated, to be enforced in review)

Nothing in Phase 1 may be appended to the `blocks` list passed to
`scan_triggers.compose_row_verdict` (`scan_triggers.py:520-559`) — that list is what
carries verdict authority (`scan_triggers.py:344-355`, `metrics/scorecard.py:536-539`,
`chart_structure.py:17-25`). No consumer outside display, telemetry and tests. No
config switch that could grant authority. `row["verdict"]`, `row["bench"]`,
`row["triggers"]`, `row["path_to_ready"]` and `row["eligible_days"]`
(`metrics/scorecard.py:631-637`) must be byte-identical with the feature present and
absent — which is exactly what §1.5's no-authority test pins.

---

**END OF PHASE 0 — HARD STOP.** No implementation until this audit is approved and
Q1–Q5 are answered.
