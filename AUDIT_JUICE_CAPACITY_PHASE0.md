# Phase 0 audit — trailing juice capacity metric (prompt 1 of 2)

Date: 2026-08-24 · `TRAVIS_EXTENSION` · **No implementation. HARD STOP pending approval.**

Scope: audit only, per prompt 1 Phase 0. Every claim below carries a `file:line`
citation. Section 8 lists the seven things I could not resolve from the codebase
and that need a decision before Phase 1 can be written.

---

## Summary

The good news is bigger than expected: **the scan's juice number is already a pure
function of cached daily bars — no option chain is involved at any point.** That
single fact changes the answer to Phase 0.2. The "IV-proxy backfill" (option c)
isn't a proxy for the juice leg at all; replaying the existing function over
historical bar slices reproduces *exactly* the number the live scan would have
shown on that date, with no new approximation. A ~250-observation history is
recoverable today, offline, from data already on disk.

The bad news is a premise correction. Prompt 1 describes the current computation
as using a "regime-aware ATR multiplier: Green 1.5×ATR, Yellow 2.0×ATR". It does
not. The scan prices a **flat 1.5×ATR strike, regime-blind** — and the repo's
regime×posture table encodes a *third*, different scheme. Details in §1.2; the
consequence for what "capacity" means is in §8.1.

There is also already a partial juice history on disk that nobody planned as one
(§1.3), and a real dividend data path where the prompt expected none (§3).

---

## 1. Current juice computation (0.1)

### 1.1 The producer chain

| Step | Location |
|---|---|
| Universe sweep loop, one iteration per name | `backend/metrics/scorecard.py:861-880` |
| Per-name row build | `backend/metrics/scorecard.py:428` (`score_ticker`) |
| Juice math | `backend/account_gate.py:53` (`juice_estimate`) |
| Row keys assigned | `backend/metrics/scorecard.py:516-527` |
| Combined juice+dividend | `backend/metrics/scorecard.py:542-547` |

### 1.2 The exact formula

All of it in `backend/account_gate.py:74-108`:

```
S       = indicators.last(df)                  # last daily CLOSE (or price_override)
atr_val = indicators.atr(df)                   # Wilder ATR, window config.ATR_WINDOW = 9
hv      = indicators.hist_vol(df)              # 20d annualized realized vol, PERCENT
k_short = indicators.short_strike(S, atr_val)  # S - 1.5*ATR, rounded to $0.50
t_week  = 5 / 365.0                            # hardcoded 5 calendar days
price_w = indicators._bs_call_price(S, k_short, t_week, r=0.04, sigma=hv/100)
extr_w  = max(price_w - max(S - k_short, 0), 0)
gross   = extr_w / S * 100                     # SHARES mode: denominator is SPOT
net     = gross                                # shares don't decay: no burn
```

Component citations: `indicators.last` `backend/indicators.py:22`; `indicators.atr`
`backend/indicators.py:71`; `indicators.hist_vol` `backend/indicators.py:169` (std of
daily log returns × √252); `indicators.short_strike` `backend/indicators.py:431`;
`indicators._bs_call_price` `backend/indicators.py:481`; `config.RISK_FREE_RATE = 0.04`
`backend/config.py:472`; shares-mode branch `backend/account_gate.py:103-108`.

**Strike selection — premise correction.** `juice_estimate` calls
`indicators.short_strike(S, atr_val)`, which defaults to the flat
`config.SHORT_ATR_MULT = 1.5` (`backend/config.py:473`). It does **not** call
`indicators.short_strike_from_table` (`backend/indicators.py:437`), and it never
reads the regime. The scan's juice number is regime-blind.

The repo carries three different strike schemes, and the prompt's numbers match
the one that is explicitly *not* wired up:

| Scheme | Value | Where it is actually used |
|---|---|---|
| `SHORT_ATR_MULT` | flat 1.5× | **the scan juice number** — `backend/config.py:473` |
| `STRIKE_TABLE` | green 0.5× / yellow 1.0× / red 1.5× (conservative) | defend & roll-down selector only — `backend/config.py:583-589` |
| `STRIKE_ATR_MULT_GREEN/YELLOW` | 1.5× / 2.0× | **nothing** — documented policy, deliberately unapplied |

`backend/config.py:596-604` states this in terms: the live `STRIKE_TABLE` "encodes a
DIFFERENT, internally-consistent scheme … deliberately left for a separate,
reviewable change". So the 1.5/2.0 pair the prompt cites is a documented
`HARD_CFM_RULE` that no code path consumes today.

**Expiry selection.** There isn't one. `t_week = 5/365` is a hardcoded 5-calendar-day
tenor (`backend/account_gate.py:90`) — no expiration date is chosen, and
`weeklies.has_weeklies(t)` (`backend/metrics/scorecard.py:874`) is carried on the row
for display but does not feed this math.

**Notional denominator.** Spot (`extr_w / S`), i.e. a covered-call yield on the
100-share base (`backend/account_gate.py:106`, `:138`). The legacy branch divides by
LEAP cost instead (`:121`) and is dead under `config.LEGACY_LEAP_READONLY`.

### 1.3 Is any juice history persisted? **Yes — more than expected**

Live chain quotes are **not** used: `juice_estimate` reads only cached daily frames
(`backend/account_gate.py:74-78`). There is no stored historical chain anywhere.

But a per-symbol juice series is *already being persisted*, as a side effect of the
gate-calibration telemetry. `backend/scan_rejection_log.py:104-147` records, per
symbol per scan run:

- `juice_weekly_pct` — `:145`
- `combined_weekly_yield_pct` — `:143`
- `dividend_weekly_pct` — `:144`
- `net_juice_weekly_pct` — `:128`
- plus `shadow_floor_measured_pct` / `_pct` / `_pass` — `:147-150`

**Shape:** `DATA_DIR/scan_rejection_log.json`, `{"symbols": {TICKER: [record, …]}}`,
each record stamped `{date, scan_id, schema}` (`backend/scan_rejection_log.py:34`,
`:371`). **Retention:** newest 180 *distinct dates*
(`config.SCAN_REJECTION_LOG_DAYS = 180`, `backend/config.py:299`; trim at
`backend/scan_rejection_log.py:328-338`). **Writer:** the nightly maintenance sweep
only — `backend/maintenance.py:215-224`. **Read API:** `series(ticker)` at
`backend/scan_rejection_log.py:206`.

Two properties of this store matter for the capacity metric and are easy to miss:

1. **Retention is 180 dates, not 252.** A capacity window of
   `CAPACITY_WINDOW_DAYS = 252` cannot be served from it as configured.
2. **It is append-per-scan-run, not one-per-day** (`backend/scan_rejection_log.py:340-352`).
   A regime flip re-fingerprints the scan cache and forces a second sweep the same
   day (`backend/scan_cache.py:121-128`), so that date contributes two records. A
   median taken over raw records would silently overweight volatile days — exactly
   the days where juice is least representative. See §8.4.

---

## 2. Feasibility of the trailing median — three sources ranked (0.2)

### (a) Forward accumulation — feasible, and already half-built

Storage shape and writer would mirror `scan_rejection_log` / `iv_history` (§4).
Emission point in §5. Costs nothing beyond a JSON append per nightly sweep. This is
the permanent live feed regardless of what else we do.

Standalone, it means `juice_capacity_wk_pct` returns `INSUFFICIENT_HISTORY` for the
first `CAPACITY_MIN_OBS = 20` trading days after deploy — i.e. **prompt 2 cannot
start for ~4 weeks minimum**, and a 252-day median isn't meaningful for a year.

### (b) Historical option chains — **no, and one UNKNOWN**

**Schwab: no.** The only chain endpoint is `OPTION_CHAIN_URL` =
`marketdata/v1/chains` (`backend/schwab_api.py:37`), wrapped at
`backend/schwab_api.py:365-392`. Its `from_date`/`to_date` params filter which
**expirations** come back (`:377-380`), not an as-of date — the response is always
today's book. No historical-chain endpoint exists in the client, and none is
documented in the repo.

**Alpha Vantage: UNKNOWN — flagging rather than guessing.** The repo's client
implements exactly four functions: `daily_bars` (`backend/alpha_vantage.py:66`),
`earnings_calendar` (`:114`), `overview` (`:127`), `global_quote` (`:133`). No
options function of any kind. Per the prompt's instruction not to guess endpoint or
field names: whether Alpha Vantage's paid tier exposes a usable historical-options
series, at what history depth, and on which plan is **not determinable from this
repo** and needs human verification against their current docs and the account's
plan. I have not assumed it either way.

### (c) Bar-derived backfill — **recommended primary, and it is not really a proxy**

This is the finding that matters. Because §1.2 shows the juice number is computed
entirely from `(close, high, low)` bars via ATR + HV + Black-Scholes, with **no
chain input at all**, replaying `juice_estimate` against a historical bar slice
does not *approximate* the historical number — it *is* the number the live scan
would have printed on that date, bit for bit.

The prompt anticipated an error term from substituting HV for IV. That substitution
is already in the live metric (`backend/account_gate.py:78, 85, 91`): the scan has
always priced the weekly short at trailing realized vol. `backend/iv_history.py:4-8`
says so explicitly — juice adequacy "prices the weekly short at the stock's trailing
*realized* vol, so it can't see whether THIS week's implied vol is rich or cheap".
So a bar-replay backfill introduces **zero new error relative to the live series**.
It reproduces the live series' own convention. The HV-for-IV gap is real, but it is
a property of the metric being backfilled, not of the backfill.

**Precedents for both halves of the mechanism already exist:**

- *Replay over sliced frames*: `backend/calibration.py:46-72` walks
  `df.iloc[: i + 1]` at `step` trading days and recomputes the scorecard at each
  as-of date. Same idiom, same offline/parquet-only discipline (`:11-12`).
- *Persisting a backfill with a provenance marker*:
  `backend/regime_history.py:160-200` backfills the live store from cached bars and
  stamps every synthesized record `"backfilled": True` (`:200`, and the flag in
  `record()` at `:103-107`). This is precisely prompt 1.2's
  `source: backfill_<method>` requirement, already solved once in this codebase.
  `:161-167` also states the justification test — a full replay is legitimate
  *because the data is derived and recomputable*, unlike entry-context snapshots.
  Juice capacity passes that same test.

**Depth available.** `config.HISTORY_DAYS = 400` calendar days of bars per symbol
(`backend/config.py:385`, fetched at `backend/data_handler.py:136`) ≈ **~275 trading
bars**. `juice_estimate` needs ATR (10 bars) and HV (21 bars) warm-up, so ~254 as-of
dates are replayable. That covers `CAPACITY_WINDOW_DAYS = 252` — with essentially no
margin. See §8.3.

**The one genuine gap: the dividend leg cannot be backfilled.** `dividends` caches a
single current yield per ticker with a 24h TTL (`backend/dividends.py:29`,
`:54-70`) — there is no yield history, and no dividend-history provider call in the
repo. So backfilled observations can carry only the juice leg. They must record
`combined == juice` with an explicit marker, never a silent zero — the same
"unknown is not zero" rule the codebase already enforces at
`backend/dividends.py:179-191` and `backend/scan_triggers.py:387`.

### Recommendation

**Primary: (c) bar-derived backfill, at deploy, once.** ~254 observations per name
immediately, no provider calls, no new approximation, reusing two established
patterns. It makes prompt 2 startable on day one instead of in a month.

**Permanent feed: (a) forward accumulation**, one observation per name per scan day.

**(b): leave UNKNOWN.** Don't build against it; if Alpha Vantage turns out to expose
historical options, it becomes a later refinement, not a dependency.

**Tradeoff, stated plainly:** backfilled observations are juice-only and inherit the
HV-for-IV convention. A dividend payer's backfilled capacity therefore *understates*
its combined capacity — for ET, the dividend leg is most of the number. That is
directionally dangerous for prompt 2 in exactly one way: it would make dividend
payers look *more* structurally suppressible than they are. Mitigations: keep
`source` on every observation, and have prompt 2's classifier either exclude
backfilled points for `DIVIDEND_COMPOUNDER` names or require live observations
before a structural suppression can bind. Recommend the latter, but it is prompt
2's call — flagging it here because the choice constrains the observation schema,
which *is* this prompt's surface.

---

## 3. Dividend data availability (0.3)

**A real path exists — the prompt's expected "no path, stub it" is not the case.**

- `backend/dividends.py` — per-ticker annual yield. `yield_for` (`:123`) fetches,
  `cached_annual_yield_pct` (`:179`) is the cache-only read in percent.
- Sources: Schwab fundamentals `divYield` (percent) via
  `get_instrument_fundamental` (`backend/schwab_api.py:394-403`), falling back to
  Alpha Vantage `OVERVIEW.DividendYield` (decimal) — `backend/dividends.py:102-121`.
- Manual override in state metadata `dividend_overrides` — `backend/dividends.py:96-100`.
- Cached in `DATA_DIR/dividends_cache.json`, 24h TTL — `backend/dividends.py:28-29`.
- **Already wired into the scan**: `screening.resolve_profile_detail`
  (`backend/screening.py:562-588`, yield at `:585`) → `_compute_scorecard`
  (`backend/metrics/scorecard.py:866-868`) → `score_ticker` → the combined yield at
  `backend/metrics/scorecard.py:542-547`.
- The weekly-equivalent conversion already exists and is pinned:
  `scan_triggers.combined_weekly_yield` (`backend/scan_triggers.py:357-392`),
  `combined = juice%/wk + annual_div% / DIVIDEND_WEEKS_PER_YEAR` where
  `DIVIDEND_WEEKS_PER_YEAR = 52` (`backend/config.py:535`). Its docstring
  (`backend/scan_triggers.py:365-374`) documents the 52-vs-51.07 day-count choice and
  says `test_dividend_profile` pins it.
- Declared-event data also exists — `backend/dividend_calendar.py:129-141`
  (`ex_date`, `pay_date`, `amount`, `frequency`), merged from fixture / Alpha Vantage
  / Schwab adapters (`:170-217`) — but it carries only the **next** event, no history.

**One mismatch to surface.** Prompt 1 specifies the dividend leg use "realized/declared
distributions only". What exists is a **quoted trailing annual yield from a
provider fundamentals field** — not a sum of realized distributions. The declared
path (`dividend_calendar`) is the closer fit to the stated rule but has no history,
so it cannot produce a trailing series either.

**Recommendation:** consume the existing `cached_annual_yield_pct` and reuse
`combined_weekly_yield` verbatim — do not build a dividend fetcher (prompt 1.6), and
do not stub to zero when a real number is available. Record the provenance honestly
on the observation (`dividend_source`), and mark, don't zero, an unresolved yield —
`dividend_known: False` already carries that distinction at
`backend/scan_triggers.py:387`. Whether a quoted yield satisfies the
realized/declared rule is Travis's call, not mine; see §8.6.

---

## 4. Persistence model (0.4)

**The pattern is unambiguous: a standalone append-only JSON store under `DATA_DIR`,
never `state.json`.** `backend/scan_rejection_log.py:15-20` states the rule directly —
like `symbol_genius_history` / `regime_history` / `iv_history`, this is DERIVED
telemetry, "kept in a standalone append-only store under `DATA_DIR` — NOT in
state.json and NOT rebuilt by `recompute_derived`".

Instances of the pattern, all with the same shape (module-level `LOG_PATH` under
`config.DATA_DIR`, `threading.RLock`, `_load`/`_save` with atomic
tmp-then-`os.replace`, best-effort writes that never raise into the caller):

| Store | Path constant | Retention |
|---|---|---|
| `scan_rejection_log` | `:34` | 180 dates — `config.py:299` |
| `iv_history` | `:27` | 260 points — `iv_history.py:28` |
| `regime_history` | — | 400 days — `config.py:212` |
| `symbol_genius_history` | — | 90 days — `config.py:295` |
| `structure_labels`, `scan_diff_log` | — | — |

`state.json` is the append-only **executions** ledger plus state derived from it;
`recompute_derived` (`backend/logging_handler.py:790`) rebuilds positions and the
theta/payback ledgers off executions. A market-observation series has no executions
behind it and does not belong there. Schema is at v21
(`backend/migrations.py:332`) — a capacity store needs no migration, which is
another point in favor of the standalone store.

**Recompute-from-history.** Satisfied by construction if the store holds only
observations: `juice_capacity_wk_pct(symbol)` becomes a pure fold (median) over the
persisted list, computed on read. No running median, no mutable aggregate. This
matches `iv_history.iv_rank` (`backend/iv_history.py:93-117`), which likewise stores
raw points and computes rank on read.

**Idempotency — the convention differs between the two nearest analogs, so it must
be chosen deliberately:**

- `iv_history.record` — **one point per calendar day, last write wins**
  (`backend/iv_history.py:60-63`).
- `scan_rejection_log.record_scan` — **append per scan run**, idempotent only on a
  retry of the same `scan_id` (`backend/scan_rejection_log.py:340-352`, `:373-379`).

**Recommendation: follow `iv_history` (one observation per symbol per scan day).**
A median is a distributional statistic; letting a regime-flip day contribute two or
three points biases it toward turbulent sessions. `scan_rejection_log` is
append-per-run for a different and valid reason — it is a *calibration event log*
where each run's verdict is itself the datum.

**Recommendation on reuse vs. new store: new store**
(`DATA_DIR/juice_capacity_log.json`), for three reasons — the 180-vs-252 retention
conflict (§8.3), the per-run-vs-per-day conflict just described, and lifecycle
independence (prompt 2 will read capacity on every scan; coupling it to the
gate-calibration log means the two can't be trimmed or reshaped separately). The
existing `scan_rejection_log` history is still valuable as a **one-time seed** —
up to 180 dates of real `combined_weekly_yield_pct` for names already swept, read
through `series()` (`backend/scan_rejection_log.py:206`) and stamped
`source: "seed_scan_rejection_log"`.

---

## 5. Scan-loop integration points (0.5)

**The loop:** `backend/metrics/scorecard.py:861-880` — `for t in names:` building one
row per ticker via `score_ticker` (`:428`). Called from `scorecard()` (`:886`), whose
full-universe path is disk-cached one sweep per trading-day epoch
(`backend/scan_cache.py:103-118`).

**Two candidate emission points, and the obvious one is wrong:**

1. *Inside `score_ticker`* — fires only when a row is genuinely computed. The cached
   path returns early at `backend/metrics/scorecard.py:927-929` without recomputing
   anything, and the incremental path (`:934-950`) recomputes only newly-added names.
   Emission here would be **irregular** — dense on force-rescan days, absent on
   cache-hit days. Bad input for a median.

2. *Alongside `record_scan` in the nightly sweep* — `backend/maintenance.py:215-224`
   already calls `scorecard_metrics.scorecard(None)`, takes `sweep["results"]`, and
   writes per-symbol telemetry keyed on the sweep's `as_of` as the run identity.
   **Recommended.** One observation per name per trading day, the same cadence and
   the same writer discipline as every other telemetry store, reusing the memoized
   sweep. It is a ~5-line insertion next to an existing call that already holds
   exactly the rows needed.

**No new API calls — confirmed.** `juice_estimate` takes the already-fetched frame
as an argument (`backend/account_gate.py:74-75`) and touches no provider;
`_compute_scorecard` prefetches all frames once up front
(`backend/metrics/scorecard.py:841`); the dividend read is cache-only by explicit
design (`backend/dividends.py:149-158`, "never a fetch … calling `yield_for` across
them on a cold cache would fire one provider request per ticker"). The observation
is a pure byproduct of numbers the sweep has already computed, exactly as the prompt
assumed.

**Backfill entry point:** a `backfill()` function on the new module, run once, shaped
on `regime_history.backfill` (`backend/regime_history.py:160-200`) — no-op when
history is present unless `force`.

---

## 6. Display surfaces (0.6)

**Component:** `frontend/src/components/Scorecard.jsx`.

- **Expanded-card readout grid** — `:520-559`, a grid of `<Readout label= value= />`
  ending with `Suitability` at `:559`. The juice figures render in the same drawer.
- **The NO AUTHORITY pattern to mirror** — `StructureShadow`, `:362-400`: a
  bordered block (`:376`), a violet metric chip (`:381-383`), and a separate slate
  `NO AUTHORITY` badge (`:383-385`). Its header comment (`:358-361`) states the
  intent — styled "as an observation … rather than as gate output". Second instance:
  `ShadowFloorLog` at `:961`, chip at `:974-976`.
- `fmt()` renders `null` as `"—"`, so an `INSUFFICIENT_HISTORY` capacity needs an
  explicit branch to print `insufficient history (12 obs)` rather than a bare dash.

**Recommendation:** one `CAPACITY` row inside the existing drawer adjacent to the
juice readouts, carrying the `NO AUTHORITY` badge — not a new collapsible section.
Prompt 1.3's format (`CAPACITY: 0.31%/wk (floor 0.70%) · 41 obs`) fits a single
`Readout` with a `title` tooltip for provenance (obs count, live-vs-backfilled
split). Per prompt 1.3, no scan-table column and no sort/filter.

---

## 7. Authority check (1.4) — how it will be verified

The invariant is already enforced elsewhere by convention and is greppable: the
load-bearing rule is that nothing shadow is ever appended to the `blocks` list that
feeds `compose_row_verdict` (`backend/scan_triggers.py:346-356`,
`backend/metrics/scorecard.py:546-547`). Capacity must follow the same discipline —
additive row keys only, consumers limited to display, the telemetry store, and
tests. Noting it here so Phase 1 carries the check rather than discovering it.

---

## 8. Open decisions — these need Travis, not me

**8.1 "Regime-appropriate strike" does not describe the current number.**
Prompt 1 defines capacity as "achievable weekly juice at the regime-appropriate
strike" and cites Green 1.5× / Yellow 2.0×. The scan computes a flat, regime-blind
1.5× (§1.2), and prompt 1.6 forbids changing strike selection. Three options:
(i) record capacity on the flat-1.5 basis — capacity then measures exactly what the
scan displays and what the floor is compared against, which I **recommend**;
(ii) compute observations at a regime-aware strike, which makes capacity
incomparable to the displayed juice and to `shadow_floor`; (iii) reconcile the
strike schemes first — out of scope for both prompts and already flagged as its own
reviewable change at `backend/config.py:596-604`. Recommending (i), and noting that
the phrase in the prompt should then be retired so the metric isn't later read as
something it isn't.

**8.2 `JUICE_FLOOR_WK_PCT` does not exist** (prompt 2 §1.1 cites it as an existing
constant to reuse). Three floors exist, and which applies is profile-dependent:
`JUICE_FLOOR_WK = 1.5` gross, and the only one with blocking authority
(`backend/config.py:356`, enforced `backend/scan_triggers.py:337`);
`SHARES_JUICE_FLOOR_PCT = 0.75`, shadow (`backend/config.py:498`);
`COMBINED_YIELD_FLOOR_WK = 0.5`, shadow, dividend compounders
(`backend/config.py:538`). `shadow_floor` picks between the latter two on profile
(`backend/scan_triggers.py:451-464`). Prompt 1's display line ("floor 0.70%") matches
none of them exactly. Which floor the capacity readout displays against is a Phase 1
input.

**8.3 252 vs. what's actually available.** `CAPACITY_WINDOW_DAYS = 252` exceeds both
the seed store's retention (180 dates, `backend/config.py:299`) and, by ~2 bars, the
replayable bar depth (~254 as-of dates from `HISTORY_DAYS = 400`,
`backend/config.py:385`). Options: raise `HISTORY_DAYS`; set the window to 180; or
keep 252 and accept that it is only fully populated for names with a complete bar
history. Recommend keeping 252 as the *ceiling* and letting the observation count
speak for itself in the display — but it should be a decision, not an accident.

**8.4 `CAPACITY_MIN_OBS = 20` — twenty observations or twenty days?** If seeded from
`scan_rejection_log` (append-per-run, `backend/scan_rejection_log.py:340-352`), a name
can reach 20 records in ~4 calendar days across re-sweeps. The guard exists to make
the number statistically meaningful, which argues for distinct days. Recommend the
guard count **distinct observation dates**, and that the store enforce one
observation per symbol per scan day (§4).

**8.5 The GDDY Aug 21 fixture does not exist.** Both prompts require "canonical
fixtures: XLK July 6th and GDDY Aug 21 pass unmodified". XLK July 6 exists —
`backend/fixtures/regime/xlk_july6_rollover.parquet`, asserted in
`test_recommendation_engine.py:283-333`, `test_gate_ruleset.py:50`,
`test_chart_structure.py:429`, `test_shares_migration.py:388`,
`test_stock_lights.py:82`. **GDDY appears nowhere in the repo** — zero hits across
all `.py`/`.md`/`.jsx`. Either it is from work that hasn't landed, or it's a
different name. I have not invented a stand-in.

**8.6 Quoted yield vs. realized/declared distributions** — §3. The available data is
a provider-quoted trailing yield; the prompt specifies realized/declared. Accept the
quoted yield with honest provenance (recommended, and it's what the scan already
displays), or descope the dividend leg to zero-with-marker until a declared-history
path exists.

**8.7 Backfilled dividend legs are structurally absent** — §2(c). Constrains the
observation schema, so it needs answering in this prompt even though the consequence
lands in prompt 2: should a structural suppression be allowed to bind on a capacity
median composed mostly of juice-only backfilled points? Recommend no — require a
minimum count of *live* observations before capacity can support suppression — but
the schema must carry enough provenance to make that rule expressible, which is why
it's here.

---

## Deliverable status

Audit complete. **No implementation code written. HARD STOP** pending approval of
the recommendations in §2 (backfill path), §4 (store + idempotency), §5 (emission
point), and answers to §8.
