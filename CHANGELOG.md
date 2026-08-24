# Changelog

## v2.13.0 — Trailing juice capacity (shadow, no authority)

`TRAVIS_EXTENSION`. The scan's weekly juice reading answers "what does this name
pay THIS week", which cannot tell a normally-juicy name in IV compression
(recoverable) from an instrument that is simply built low-vol (never
recoverable). Both read thin today. This adds the discriminator: the trailing
**median** of the combined weekly-equivalent yield — what the name has
demonstrated it *can* pay. A compressed name carries a high capacity beside a
low current reading; a structurally low-vol name carries both low.

**It has zero authority, and there is no switch that grants it any.** Capacity
does not gate, hide, rank, bench or reorder anything; it is computed, persisted
and displayed only. `test_juice_capacity` pins that with an AST check that no
module outside `metrics/scorecard.py` (the display row key) and `maintenance.py`
(the observation emitter) imports it, plus a byte-identical scan-output test at a
capacity far below any floor. Consuming it is a separate, reviewed change.

- **New:** `backend/juice_capacity.py` — the observation store
  (`DATA_DIR/juice_capacity_log.json`, standalone and append-only, out of
  `state.json` like `iv_history` / `regime_history`) and
  `juice_capacity_wk_pct(symbol)`, the median over the trailing
  `CAPACITY_WINDOW_DAYS`. Below `CAPACITY_MIN_OBS` distinct days it returns the
  `INSUFFICIENT_HISTORY` sentinel — never a provisional number, and a string
  rather than `None` so "not watched long enough" can't be confused with "cannot
  be priced".
- **Emission:** one observation per name per scan day, appended by the nightly
  sweep off rows it has already computed. No new provider call.
- **Bootstrap:** `scripts/backfill_juice_capacity.py`. Two offline sources, both
  tagged on every record so they stay distinguishable forever — `--seed`
  recovers real readings from `scan_rejection_log` (which has been persisting
  `combined_weekly_yield_pct` per candidate since v21), and `--backfill` replays
  the computation over cached daily bars.
- **Display:** a capacity row in the expanded scan card, carrying the same
  `NO AUTHORITY` badge as the structure and shadow-floor readouts. No scan-table
  column, no sorting or filtering by capacity.

Three things worth knowing:

- **The bar replay is exact, not approximate.** The juice number is computed
  entirely from daily bars — Wilder ATR to the strike, 20-day realized vol to
  sigma, Black-Scholes to the weekly extrinsic, over spot — with no option-chain
  input at any point. So replaying it against a historical bar slice reproduces
  the number the live scan *would have printed* that day. The HV-for-IV
  substitution people reach for as the error term is already the live metric's
  own convention. `HISTORY_DAYS = 400` bounds a replay at ~254 days, just under a
  full 252-day window.
- **Backfilled observations carry no dividend leg**, because no dividend-yield
  history exists anywhere in the tree. They record `combined == juice` with
  `dividend_known: False` — never a silent zero. For a dividend payer the
  dividend leg can be most of the combined number, so a backfill-heavy median
  *understates* that name's capacity. `capacity_detail` reports the per-source
  observation counts alongside the median rather than one opaque figure, so a
  future consumer can require live provenance before treating a low capacity as
  structural.
- **The strike is regime-blind, and capacity inherits that.** `juice_estimate`
  prices a flat `SHORT_ATR_MULT` (1.5×ATR) strike and never reads the regime —
  the documented `STRIKE_ATR_MULT_GREEN/YELLOW` pair has no consumer, and the
  live `STRIKE_TABLE` encodes a third scheme used only by the defend/roll
  selector (see the note at `config.py:596`). Capacity is therefore measured on
  exactly the basis the scan displays and the floors judge, which is the point;
  it is not "capacity at the regime-appropriate strike", and reconciling the
  strike schemes remains its own separate change.

Also: `scan_triggers.floor_for_profile` extracts the profile-aware floor
resolution that `shadow_floor` had inline, so the capacity readout and the
shadow floor can never quote different bars for one name. Behaviour unchanged.

## v2.12.0 — RS3M-vs-Sector removed completely

A stock's relative strength against its cap-weighted sector ETF is not a peer
comparison — XLK and friends are dominated by a handful of mega-caps, so the
figure largely answered "is this name keeping up with the three biggest
companies in its sector". It carried no reliable meaning and is gone from the
entry gate, the kill switch, ranking, display, telemetry and config. A
rules-based industry peer-basket benchmark is the planned replacement and is
**not** part of this change.

**This includes a deliberate loosening of a safety mechanism.** The kill
switch's RS3M-vs-Sector exit-now trigger is removed, with the cases it caught
and the SPY leg does not enumerated in
`docs/decision-2026-08-21-remove-sector-rs.md`. Read that before concluding the
absence is an oversight.

- **Removed:** the Level-3 entry veto (`stock_lights` veto 1); the kill switch's
  RED exit-now branch, its YELLOW thinning half, its `KILL_SWITCH_SECTOR` alert
  and exit reason, and its `KILL_RS_SECTOR` recommendation trigger; the
  suitability AVOID rule; the RS1M-vs-sector ranking key; the two-speed
  RS-vs-Sector shadow (scan table column, shadow-SCORE component, and the
  TURNING WATCH annotation); and every display, telemetry and config surface for
  the above.
- **Untouched:** RS3M-vs-SPY. The kill switch's confirm-on-close exit behaves
  identically — same input, same threshold, same wording. The two-speed RS **vs
  SPY** drawer readout stays.

Three things worth knowing:

- **There is no relative-strength entry veto any more.** RS3M-vs-SPY was never
  one: the "beats SPY" gate leg had been removed earlier, leaving
  `config.rs_vs_spy_min()` with no production caller. The sector veto was the
  only RS check the entry gate had. Entry now rests on the market regime, sector
  deterioration, the SYM four-light vote with its ATR/IVR and MA200 vetoes,
  structure entrability, the right spot, and the account overlay.
- **Share adds loosen too.** The YELLOW "thinning" leg lost its sector half, and
  `position_manager.can_add_shares` blocks adds on red **or** yellow — so the
  system will now permit adding to positions it previously refused. There is no
  way to keep a sector-based YELLOW once the figure is gone, and raising the SPY
  threshold to compensate would violate the parity requirement on that leg.
- **The shadow SCORE is recomposed and candidate ordering changed.** Dropping the
  RS component takes the quality weights 8.5 → 7.0, renormalized: SCORE stays
  0–10 but the remaining five components carry more of it, so a score is not
  comparable across this change (it has no authority, so it decides nothing).
  Ranking within GREENs now uses RS1M-vs-SPY for every name, where stocks
  previously ranked on RS1M-vs-sector.

Compatibility — historical records are readable and **were not touched**:

- `ExitReason.KILL_SWITCH_SECTOR` and `TriggerRule.KILL_RS_SECTOR` are **retired,
  not deleted**. Past closes and recommendation records carry those strings and
  the History tab and CSV export read them back, so both stay valid for reads
  (a new `exit_reasons.RETIRED` set keeps the exit reason inside `ALL`) and are
  removed only from the emitting paths and from `CLOSE_TIME`. No new record can
  be stamped with either.
- `recompute_derived` reaches entry snapshots only through
  `entry_context.summary`, a pure `.get()` read, so old snapshots carrying
  sector fields recompute correctly and silently. No migration, no rewrite.
- `SNAPSHOT_SCHEMA_VERSION` 3 → 4 and `scan_rejection_log` schema 3 → 4: new
  records stop carrying the sector fields; older ones keep theirs and stay
  readable by their own version tag.

A note on the rationale: the removal was originally also motivated by the belief
that the sector figure was an *approximation* (the difference of two RS-vs-SPY
values). That is not true — every site already computed the direct
`indicators.rs3m(stock, peer)` ratio, the codebase migrated off the
approximation earlier on purpose, and `SNAPSHOT_SCHEMA_VERSION = 3` exists to
mark that migration. The decision record says so explicitly rather than
preserving a wrong reason. The invalid-benchmark rationale stands on its own.

`test_sector_rs_removed.py` asserts the removal positively — that the sector
trigger cannot fire on any input, that no live sector-RS identifier survives
(checked via the AST, so the explanatory comments don't mask a real one), and
that the retired constants still read back off historical records.

## v2.11.0 — Level-4 chart structure (shadow) + a phase-aware volume check

Level 4 measured quietness but not **structure**. All three of its live checks
are local reads — ATR% of price, ATR/ATR_5EMA, and extension above MA21 in ATR
units — so two structurally opposite charts produced identical gate readings: a
tight coil under the highs after an advance, and a name that ran months ago,
rolled over, and now drifts mid-range under a flattening MA21. That is why
READY/WATCH kept landing on spots a human would not call compelling.

- **Four new structure metrics, SHADOW ONLY** (`backend/chart_structure.py`):
  `dist_from_high_pct` (126-bar, 252-bar for display), `ma21_slope` (ATR/bar, so
  names are comparable), `tightness` (15-bar coil range over the prior 60-bar
  advance, with a separately calibrated ceiling per denominator), and
  `higher_lows` (3-bar pivots over 30 bars). Plus
  `structure_score`, the count in their constructive bands.
  **Zero authority**: nothing is appended to the `blocks` list that carries
  verdict authority, nothing is a check in `stock_lights._right_spot_from`,
  nothing reaches the shadow SCORE's ranking inputs, and there is deliberately no
  config switch that would grant any of it. Graduating a metric is a future code
  change contingent on the logged calibration — the discipline the weekly-juice
  floor is held to.
- **The thin-volume CAUTION is now phase-aware.** Inside a consolidation, low
  volume is supply drying up, not thin participation — and the same lens already
  penalized ATR *expansion*, so the two CAUTIONs were pulling opposite directions
  on one chart. `VOLUME_RATIO_MIN` is **unchanged at 0.8**; only where it applies
  changed. Outside a consolidation the behavior is byte-identical. The phase flag
  is a pure read of the already-computed Level-4 check results and fails closed.
  This is a real behavior change, not display-only: `suitability` gates the
  recommendation pool (`recommendation_runner`), the internal queue
  (`queue_state`) and the intraday hot set (`refresh_policy`).
- **Calibration logging.** `scan_rejection_log` schema 3 persists the metrics, the
  score and its denominator, the tightness basis and applied ceiling, and the phase flag per candidate
  per scan; `summary()` crosstabs `structure_score` against the verdict reached
  *without* it. New `structure_labels` store + `GET|POST /api/scan/structure-label`
  records Travis's manual compelling / not-compelling calls (append-only, curl-able,
  no UI needed) so the two halves can be joined. Deliberately out of `state.json`:
  a subjective label is telemetry, not a trading fact.
- **Display.** A `STRUCT n/4` chip on the row plus the four sub-values on the
  expanded row, in the violet "observation" styling the shadow SCORE and
  shadow-ruleset chip use, with an explicit NO AUTHORITY badge — never the
  emerald/amber palette the blocking gate outputs use.

Three things worth knowing:

- **A partial read is `n/k`, never `n/4`.** With 400 calendar days ≈ 275 trading
  bars, the 252-bar display leg has ~23 bars of headroom and will genuinely be
  unmeasurable in production. It reports `insufficient_data` — never a silent 0,
  which would read as a failure it never measured.
- **`dist_from_high_pct` is split-guarded by signature, not magnitude.** Schwab
  bars are split-adjusted, Alpha Vantage `TIME_SERIES_DAILY` is not, and neither
  is dividend-adjusted. A ratio test can't work — an unadjusted 2:1 split and a
  real 50% drawdown give the identical ratio — so a window straddling a
  split-sized *single-bar* drop is reported unmeasurable instead.
- **`tightness` carries a SEPARATE threshold per basis.** When the prior window
  didn't advance the denominator falls back to summed true range, which is path
  length and always >= the range it spans — so the two bases are not on one scale
  and one ceiling cannot bar both. Measured over a synthetic population spanning
  drift, amplitude, period and noise, the shared 0.35 admitted **100%** of
  atr_sum-basis charts against 60.2% of advance-basis ones: a bar everything
  clears is not a bar. The atr_sum ceiling is now **0.05**, corroborated two
  independent ways that agree to within a few thousandths — random-walk scale
  (`0.35 / sqrt(60)` = 0.045; measured median range/atr_sum on non-advancing
  windows 0.131 vs the theoretical 0.129) and pass-rate matching against the
  advance basis (0.049 -> 60.3% vs 60.2%). The applied ceiling travels with the
  ratio on the row and in the log, so a calibration pass can never read a value
  against the wrong bar or pool the two populations.

Unchanged and asserted: `VOLUME_RATIO_MIN`, the MFI 40–60 band, `ATR_MOMENTUM_MAX`,
every Level-4 threshold, and WATCH/BENCH semantics. `AUDIT_LEVEL4_STRUCTURE_PHASE0.md`
carries the full pre-implementation audit.

## v2.10.1 — Universe edits no longer re-scan the whole universe

Adding tickers made the Scan tab time out. Root cause was three compounding
issues, all fixed here.

- **A universe edit invalidated every cached row.** `scan_cache.fingerprint()`
  hashed the full ticker list, so adding ONE name changed the key and discarded
  all ~500 rows — the cost was proportional to the universe, not to the edit. The
  next Scan then recomputed everything synchronously inside
  `GET /api/scan/scorecard`, where the client's 60s abort (`api.js`) was waiting.
  Gunicorn's timeout is 600s, so the server kept going and usually finished; the
  browser just gave up first.
  The universe is now **out of the fingerprint**, which keeps only the genuinely
  global inputs (regime, demo/live, row schema). A universe change is handled by
  row set instead: `scan_cache.reusable()` serves the rows still in the universe,
  reports which names have none, and the sweep computes **only those** and merges
  them in. Removals cost nothing. Measured on a 522-name universe: adding 26
  tickers now computes 26 rows instead of 548.
- **`sector_data._clear_caches()` deleted the day's sweep on every edit**, which
  would have defeated the above on its own. Split into `screening.clear_memo()`
  (universe change — drop the short-TTL memo, keep the disk sweep) and
  `screening.clear_cache()` (demo/live switch — drop both, since a sweep computed
  against the other data source must never be replayed).
- **Undeterminable weeklies probes were never cached.** `has_weeklies` pinned
  only `True`/`False`, so a name whose option chain can't be read — a typo, a dead
  listing, an optionless symbol — was re-probed on *every* sweep, forever. Those
  are the slowest probes there are (a live chain call plus its retry/backoff), so
  the worst names were also the most expensive ones, permanently. `None` is now
  pinned on a short `WEEKLIES_UNKNOWN_TTL` (1h, env-tunable): one probe per sweep
  at most, while a transient outage still clears on its own.

Also:

- **New names are warmed off-request.** `POST /api/universe/add` and
  `/api/universe/sync` now kick a detached `screening.start_background_warm()`
  that fetches daily bars and probes weeklies for the added names, so a fresh
  ticker is never cold when a sweep reaches it. The response doesn't wait.
- **`patch_rows` gained a universe-membership guard** — a refreshed row is written
  back only if its ticker is still in the universe, so a refresh in flight during a
  removal can't resurrect a dropped row.

Upgrade note: the fingerprint change means the first scan after deploy is a
one-time full sweep (the stored blob's old key can't match). It re-caches
immediately.

## v2.10.0 — Entry-gate recalibration, shadow-first

Three defects in the entry gate, addressed behind a `GATE_RULESET` flag that
**defaults to `legacy`** — nothing below carries blocking authority until a human
flips it. Both rulesets are computed on every scan and their divergence is
recorded per name per run.

- **SYM vote: mandatory-core 3-of-4** (`stock_lights.verdict_proposed`). The
  4-of-4 requirement gave every one of four correlated lights unilateral veto
  power over an entry — Parabolic SAR most consequentially, since its seed is
  path-dependent on the frame's first bar, so an entry could hinge on how much
  history a frame happened to carry. Under the proposed rule `close > SMA50` stays
  **mandatory** (it is the light that maps to the Consider-Going-To-Cash exit
  level, keeping the entry veto set aligned with the exit trigger set) and any
  `SYM_MIN_GREEN_LIGHTS` of four clears. SAR is still computed, voted and
  displayed; it simply can no longer block alone. The market (SPY) regime vote and
  its yellow dwell are **untouched** — they have their own `>=3` rule already.
- **Level 4: contracting → not expanding** (`stock_lights.atr_momentum_max`).
  Requiring ATR/ATR_5EMA ≤ 1.0 systematically selected premium-poor charts, since
  contracting ATR compresses the very extrinsic the strategy sells — the gate
  disagreeing with itself. The proposed ceiling is `L4_ATR_EXPANSION_MAX` (1.05).
  ATR% and extension are unchanged, and the shadow SCORE's own ATR band is a
  separate constant, so the rank still rewards contraction while only the veto
  relaxes.
- **Rejection log extended to schema 2** (`scan_rejection_log.py`). Now records
  both rulesets' verdicts and their divergence, the **full per-level gate result**
  (every level, recorded past the first failure — stop-on-first-fail still governs
  the verdict), the lowest `first_failing_level` (distinct from `binding`, which is
  the most *decisive* block), the SYM light breakdown, the core-light state and the
  raw ATR ratio — enough to replay a rule change against history without refetching
  bars. Writes became **append-per-scan-run**: each run carries a `scan_id` and
  appends, so a same-day re-run no longer overwrites the earlier record; retention
  is now by distinct date rather than record count. Re-writing the same `scan_id`
  is still idempotent.
- **Divergence surfaced in the UI** (`Scorecard.jsx`): a non-blocking pill in the
  Scan tab's pipeline row reads `shadow ruleset: N/M diverge`, with the ruleset in
  force and the most common verdict transitions in its tooltip. `/api/scan/rejection-stats`
  gains `ruleset_divergences`, `ruleset_divergence_rate` and `ruleset_divergence_pairs`.

Known consequences, recorded deliberately rather than fixed here:

- The Level-3 veto `atr_expanding_high_ivr` is **not** in this change's scope, so
  an expanding-ATR name at IVR ≥ 90 is still blocked at Level 3 regardless of the
  Level-4 relaxation — exactly the high-IV names the relaxation targets.
- The blocking juice floor is disabled under shares-primary
  (`scan_triggers.juice_floor_block` returns `None`), and the share-denominated
  `shadow_floor` never blocks. So relaxing Level 4 removes a constraint with **no
  armed downstream income constraint** to take its place. Re-arming a
  share-denominated floor is separate tracked work.
- The XLK July 6th fixture cannot flip: its last bar measures 0/4 green with the
  core light red, so the mandatory-core rule is strictly unable to admit it.

Full Phase 0 findings, with citations: `AUDIT_ENTRY_GATE_RECALIBRATION_PHASE0.md`.

## v2.9.0 — Scan cached per trading day; Scorecard crash fix; error boundaries

Follow-up to v2.8.0's shares-primary migration (PR #255), which shipped the LEAP
removal but left the Scan tab unopenable.

- **Fix: "Show full universe scorecard" blanked the whole app.** `Scorecard` read
  `showPricedOut` from its `useApi` dependency array one line above the `const`
  that declares it. The deps array is evaluated during render, so the read hit the
  temporal dead zone and threw `ReferenceError: Cannot access 'showPricedOut'
  before initialization` on **every** render. With no error boundary anywhere,
  React tore down the entire tree — a black page, no nav, no message. Introduced
  by the affordability filter (`ed7a596`); the scorecard has been unopenable since.
- **Error boundaries** (`ui.jsx`, `App.jsx`, `index.jsx`): a render throw now
  degrades to a single error card with the message, a "Try again" that remounts the
  subtree, and the component stack in the console. `App` wraps the tab content
  (keyed on the view, so the nav survives and switching tabs clears the error);
  `index.jsx` wraps the shell as a last resort.
- **Scan: one full-universe sweep per trading day** (`scan_cache.py`, new). The
  ~500-name sweep was memoized only in process memory on a 5-minute TTL and
  re-warmed every 4 minutes, recomputing the universe dozens of times a day over
  daily bars that `data_handler` refreshes every 12 hours — and an in-memory memo
  dies with the process, so every Fly auto-stop handed the next visitor a cold
  ~22s sweep on the request path. The sweep is now persisted to the volume once per
  **trading day**, keyed on the day whose CLOSED bars it covers so it always runs
  outside trading hours on final bars. Weekends and holidays replay the last
  session. Measured: a restarted machine goes from 24,185 ms to **24 ms**.
  The key also folds in the ticker universe, the market regime and demo/live mode,
  so a change that would make a cached row *wrong* re-scans rather than serving it.
- **Per-stock refreshes persist independently.** Refreshing one stock or sector
  writes those rows back into the day's sweep, so a refreshed name survives a
  reload while every other row stays exactly as the sweep computed it. Each patched
  row carries `refreshed_at` + `price_source` so a row fresher than the sweep around
  it says so. Held positions ride this automatically via the kill-switch RS3M pass
  (3x per session). The nightly sweep re-scans everything and overwrites the lot.
- **Rescan forces.** The operator's Rescan button bypasses both cache layers;
  scheduled warm-ups never force. Scan freshness now counts the day cache, so
  opening the tab after idle minutes no longer force-sweeps.

## v2.8.0 — Dividend income profile + position builder (state schema v21)

**PROVENANCE — `TRAVIS_EXTENSION`, not a CFM rule.** The CFM source methodology
(Mark Yegge) explicitly *prefers* volatile stocks because they carry more juice,
and warns against "safe" low-volatility names. The dividend sleeve added here is
Travis's extension, made economically viable only by the shares-primary model:
with real shares as the base leg the dividends are actually collected, which was
never true under a LEAP. It is implemented as a **separate, clearly-labeled income
profile that never blends into or weakens the juice-engine verdict logic** —
`JUICE_ENGINE` behavior is regression-locked byte-for-byte
(`test_dividend_profile.py`, tests 1/2/9). Audit: `audit-dividend-profile-v1.md`.

- **Income profile** (`income_profile.py`): `JUICE_ENGINE` (default, backfilled
  onto every existing position) | `DIVIDEND_COMPOUNDER`. Anything that is not an
  explicit dividend tag normalizes to `JUICE_ENGINE`, so the sleeve is entered only
  by intent, never by omission or a typo. A position's profile is **stamped at
  entry and never re-derived** — a yield print cannot move an open position's peer
  group or floor underneath it.
- **Entry gate — dividend branch** (`screening`, `stock_lights`, `kill_switch`,
  `metrics/scorecard`): a parallel branch mirroring the ETF waiver pattern. It
  changes **exactly two things**: the RS3M comparison benchmark for the *sector*
  leg (`DIVIDEND_PEER_BENCHMARK`, default SCHD; VYM/NOBL configurable) and which
  shadow floor a candidate is measured against. Trend quality is **not** waived —
  the Genius four-light vote (per-stock and market regime), consolidation-near-MA21,
  the ATR posture, the RSI band, the earnings-window exclusion and the YELLOW
  watchlist lockout are identical for both profiles. **The RS3M-vs-SPY kill-switch
  leg is untouched** and retains full exit authority. A payer lagging its *dividend*
  peers is still vetoed at Level 3 (the AAPL lesson, covered for the new branch).
  Entry snapshots now record `rs3m_vs_sector_benchmark` beside the existing
  `rs3m_vs_sector_method`, so "direct against what?" can never be ambiguous.
- **Combined weekly-equivalent yield, SHADOW ONLY** (`scan_triggers.shadow_floor`):
  `juice %/wk + (trailing annual dividend % ÷ 52)`, with the two components kept
  separable everywhere they are displayed. Floors: `JUICE_ENGINE` 0.75%/wk on juice
  alone; `DIVIDEND_COMPOUNDER` 0.5%/wk combined, plus a `JUICE_BELOW_SLIPPAGE`
  sub-floor requiring the juice component to still clear the estimated round-trip
  spread cost. **Zero blocking authority, and no config switch exists that would
  grant any** — `shadow`/`blocking` are literals, not config reads, and the
  observation is deliberately never appended to the block list `compose_row_verdict`
  derives authority from. Pass/fail is logged per candidate per day into the
  existing `scan_rejection_log`, which is the evidence a future graduation decision
  would rest on. Honest magnitude, pinned by test: a 3%/yr payer contributes only
  ~0.06%/wk — the *lower floor*, not the dividend, does most of the admitting work.
- **Accrual ledger + position builder** (`accrual.py`): a per-position balance fed
  by exactly two whitelisted sources — realized extrinsic at short-call **cycle**
  close, and `dividend_income` receipts. **Roll-down credits are excluded by
  construction** (a roll leg carries a `roll_id` and is rejected in
  `credit_for`): crediting a deferred intrinsic obligation is the Martingale trap,
  scaling the book up precisely as a thesis deteriorates. Unrealized juice and every
  intrinsic component are likewise excluded. At threshold
  (`price × 100 × (1 + 2%)`) a `LOT_ADD_READY` alert fires — **blocked-with-reason**
  when the Level 5 gate or a reconciliation freeze would refuse the add. **No
  auto-execution**: an executed add is an operator-confirmed `buy_shares` carrying a
  `lot_add` stamp, so it traverses the same freeze / Level-5 / execution-window /
  spread path as any other new risk. Accrued cash is CASH, never exposure — it
  changes no covered-call math until a whole lot is actually bought.
- **Ex-dividend calendar contract** (`dividend_calendar.py`): the internal data
  contract is now explicit (ex-date vs pay date; **per-share, per-payment** amounts,
  never annualized, never a yield; never substitute a missing date), with Alpha
  Vantage and a fixture-backed stub wired behind it. The **Schwab adapter is a
  documented TODO that returns None on purpose** — its field names and units are
  unconfirmed against a live account, and a wrong guess is indistinguishable from
  "this stock pays no dividend", which would silently disarm the assignment guard.
- **Early-assignment guard**: the existing `ASSIGNMENT_RISK` dividend escalation now
  carries the typed code `EARLY_ASSIGNMENT_RISK` and defense-level styling on the
  position card. Deliberately **not** a second alert type — it is the same condition
  the extrinsic-vs-dividend guard has always raised, and forking the taxonomy would
  double-alert for one event.
- **UI**: JUICE/DIV sleeve badge on the scan table and position cards; a
  combined-yield column whose components split on hover; a per-position accrual
  progress bar ("$X / $Y toward next lot") with its source breakdown; a
  shadow-floor calibration panel labeled NO AUTHORITY; ex-div dates and
  roll-before-ex wording on the position card. Gross premium is never presented as
  income anywhere — the juice half is extrinsic only.
- Migration v20→v21 (additive: `income_profile` backfilled to `JUICE_ENGINE`;
  `accrual_ledger` seeded empty — never back-filled, which would fabricate a
  compounding record that never happened). Executions untouched; append-only holds.
- Config: `DIVIDEND_PEER_BENCHMARK(S)`, `DIVIDEND_PROFILE_MIN_YIELD_PCT`,
  `DIVIDEND_WEEKS_PER_YEAR`, `COMBINED_YIELD_FLOOR_WK`,
  `MIN_WEEKLY_EXTRINSIC_AFTER_SLIPPAGE_PS`, `LOT_ADD_BUFFER_PCT`;
  `SHARES_JUICE_FLOOR_PCT` recalibrated 1.5 → 0.75 and **wired for the first time**
  (it had no consumer anywhere in the tree).

### Affordability — the scan only shows lots you can actually buy

A shares-primary entry buys a **whole 100-share lot**, so a name whose lot costs
more than the dry powder available right now is not a candidate at any conviction.
The scan now filters those out by default.

- **One bar, derived from the numbers Level 5 already gates on**
  (`position_manager.capital_summary.max_lot_cost`): the tighter of `deployable`
  (itself the tighter of the deployed-capital headroom and the cash above the
  defensive reserve) and `PER_POSITION_CAP_USD`. The scan therefore cannot show a
  name the Execute gate would then reject on size, and vice versa.
- **Rows carry `lot_cost`** (spot × 100) — computed in the sweep, which stays pure
  and account-free so it remains memoized and shared across requests. The
  affordability *comparison* happens at the API boundary where the account context
  lives, mirroring the existing Level-5 overlay in `/api/scan/ready`.
- **Nothing vanishes silently.** `/api/scan/scorecard` reports the bar, the count,
  and `priced_out_tickers`; `/api/scan/ready` reports `priced_out` with how much
  each name is over by. `?include_unaffordable=1` returns them, still annotated. A
  new Lot-cost column shows the number and marks an over-budget row.
- **An UNKNOWN is never treated as unaffordable.** A lot cost that can't be priced
  stays visible — hiding a name we merely failed to price would be an invisible
  exclusion. And because `state.metadata.operating_cash` defaults to 0, a zero is
  ambiguous between "no money" and "never configured": the filter reads it as the
  latter and **deactivates entirely**, with a banner saying so. Without that a
  fresh book would show an empty scan and look broken rather than broke.
- A free position slot is deliberately NOT part of the bar — that is the
  `position_limit` gate's job and it already surfaces as a near-miss with a path.
  A full book should still show the pipeline it will draw from.

### Also in this release — two Level 5 gate gaps closed

Found by the Phase 0 audit and fixed with explicit approval. Both are pre-existing
defects in the shares-primary path, independent of the dividend sleeve:

- **`buy_shares` was never gated.** `executor.execute` ran the Level 5 account gate
  only for `buy_leap` / `open_position_atomic`, so every shares entry reached the
  book with cash reserve, position limit, deployed-capital cap, sector concentration
  and the earnings-in-cycle block **all unenforced** — and `_buy_shares` read an
  `_account_gate` key nothing ever set, so an override logged an empty
  `failed_checks` list. Shares entries are now gated on a round-lot basis, sized by
  the actual fill price.
- **The round-lot SIZE-BLOCK never fired.** No caller passed `position_type`, so
  `PER_POSITION_CAP_USD` was dead in production. Now armed on the shares path.
- **Sub-lot share entries are refused** (`shares_entry_lots`): a `buy_shares` qty
  that is not a whole multiple of 100 is rejected at the operator boundary, because
  a fragment can never be sold against. Broker-side odd lots remain bookable through
  the reconciliation `adjustment` path, which carries no size rule.

## v2.7.0 — Broker execution ingestion + reconciliation freeze gating (state schema v19)

The reconciliation core the roll incident demanded: the app now reads Schwab's
own transaction record as ground truth and reconciles out-of-band trades back
into `state.json`. Built as **ingest-to-confirm** so it satisfies the ingestion
spec without reversing the "no silent adopt-external-trade" safety stance. Audit:
`AUDIT_LIFECYCLE_RECONCILIATION.md`; notes:
`IMPLEMENTATION_NOTES_LIFECYCLE_RECONCILIATION.md`.

- **Transaction ingestion** (`transaction_ingest.py`, `schwab_api.get_transactions`):
  pulls the Schwab transactions feed, dedupes by transaction id (idempotent
  re-runs), and classifies each broker execution — **matched** fills confirm the
  app's own orders (`source: app`); **out-of-band** trades with no app order
  (e.g. the manual ToS roll) surface as one-click adoption proposals
  (`source: broker_manual`) with economics taken verbatim from the broker record.
  Multi-leg orders sharing a Schwab orderId link into one logical action.
  `executor.adopt_broker_trade` books an adopted proposal through the same
  builders app fills use; `recompute_derived` then rebuilds ledgers/positions —
  no derived value patched directly (`NO_AUTO_REMEDIATION`,
  `INGESTION_IS_GROUND_TRUTH`).
- **Reconciliation freeze gating** (`reconcile.freeze_status`): while the book
  diverges from the broker (or holds an unbalanced leg), recommendation
  generation is now blocked entirely (previously the freeze only blocked order
  submission). Minutes-based market-hours staleness (`RECONCILE_STALE_MINUTES`)
  degrades action-capable panels; a `RECONCILE_INTERVAL_MINUTES` scheduler runs
  reconcile + ingestion during market hours (+ once after close).
- **Unbalanced exposure direction** (`_leg_imbalance_exposure`): a leg-imbalanced
  fill now names the direction — orphaned new short (potentially NAKED, urgent) vs
  orphaned buyback (under-written, safe).
- **UI**: ingestion panel with a `broker_manual` badge + one-click Adopt +
  "Ingest now"; `/api/ingestion`, `/api/ingestion/adopt`,
  `/api/reconcile/freeze-status`.
- Config: `RECONCILE_INTERVAL_MINUTES`, `RECONCILE_STALE_MINUTES`,
  `NO_AUTO_REMEDIATION`, `INGESTION_IS_GROUND_TRUTH`, `INGESTION_LOOKBACK_DAYS`.
- Migration v18→v19 (additive: `ingested_transactions` + `ingestion`).

## v2.6.0 — Recommendation engine + trust scoreboard + execution fidelity ledger (state schema v17)

The trust layer that must exist before any automated execution is permitted.
The app now (a) commits to specific, actionable recommendations BEFORE the
operator acts, (b) measures agreement between its recommendations and the
operator's actual actions, and (c) grades whether every order lifecycle behaved
exactly as specified. Automation eligibility is a derived, per-action-type,
display-only readout — **no automated order submission exists anywhere in this
version**, and while post-fill reconciliation is `NOT_YET_IMPLEMENTED` no
action type may graduate. Operator doc: `docs/trust-layer.md`.

- **Recommendation records** (`recommendations`, append-only, immutable):
  every scheduled alert slot also runs an evaluation pass emitting, per open
  position, either an actionable recommendation (EXIT / DEFEND / ROLL_OUT,
  with a full proposed ticket: legs, strikes, NET limit, minimum acceptable
  net credit, max slippage vs mid) or an explicit `ALL_CLEAR` — silence is not
  a valid output. Coded trigger rules (`rec_types.TriggerRule`), frozen
  `input_snapshot` (incl. condition-first-true dates for timeliness),
  `valid_until` expiry, and supersession chains.
- **Same-code-path invariant**: the engine
  (`recommendation_engine.evaluate`) is a PURE function over a frozen market
  snapshot + injected clock — the exact function a future automation switch
  would call — and it reuses the existing single sources of truth rather than
  forking them: `strike_policy` for every proposed strike, a newly extracted
  pure `kill_switch.classify` core, `circuit_breaker.evaluate(df=...)`,
  `position_manager.whipsaw_status` / `enrich_short` / a new shared
  `delta_coverage` core (the `DELTA_UNCOVERED` alert now calls the same core).
  The impure shell (`recommendation_runner.py`) owns providers/clock/state.
- **Resolution matching** (derived in `recompute_derived`, never
  hand-entered): executions match the latest open, valid recommendation of the
  same action type on the same position (`source_rec_id` passthrough from the
  UI makes it exact); dismissals carry coded override reasons
  (`DISAGREE_TIMING/STRIKE/ACTION`, `EXTERNAL_INFO`, `DISCIPLINE_LAPSE`,
  `OTHER`+note) as append-only override records; expiries and **coverage
  misses** (an action with no matching recommendation — the loudest failure)
  are synthesized. Pre-activation history (`metadata.trust_layer_since`) and
  out-of-scope mechanics (LEAP rolls, scale-ins, leg repairs, adjustments) are
  excluded by rule.
- **Execution fidelity ledger** (`order_fidelity`, derived + retained past the
  order_events cap): per live ticket — `LIFECYCLE_LEGAL` (replayed against the
  now data-encoded legal transition graph in `order_lifecycle`),
  `SLIPPAGE_IN_BOUND` (reusing `slippage.py`'s exact math against the ticket's
  own bound), `NO_ORPHAN_LEG` (incl. the fill-during-cancel race),
  `CANCEL_CONFIRMED_DEAD` (a cancel that never confirms terminal fails after a
  deadline), and `RECONCILED_CLEAN` = `NOT_YET_IMPLEMENTED` (never a silent
  pass). Paper tickets are graded on what a paper fill can express, flagged
  paper. Failures page via new `ORDER_FIDELITY_FAIL` / `TRUST_COVERAGE_MISS`
  alerts; new actionable recommendations push via the existing notifier.
- **Trust scoreboard** (`trust_scoreboard`, derived; `GET
  /api/trust-scoreboard` + Settings-tab panel): coverage, precision (+ override
  breakdown), timeliness (emission lag + late-after-action flags), fidelity
  pass rate, and per-action-type graduation status with the failing criterion
  named. Criteria: `GRAD_MIN_LIVE_CYCLES`=10, `GRAD_MIN_WEEKS` 8/16/16/26
  (ENTER never eligible), override rate <= 0.10 with zero `DISAGREE_ACTION`
  (PROPOSED_DEFAULT); zero coverage misses, 100% fidelity, reconciliation
  green (HARD, in code).
- **UI**: recommendation cards on each position (proposed ticket, trigger,
  validity countdown, one-tap Execute into the existing flow / Dismiss with a
  forced coded reason), open-recommendation count in the Overview digest, and
  the Trust Scoreboard panel with coverage misses and fidelity failures
  rendered loud.
- **Schema v17** (pre-migration snapshot as always): adds `recommendations`,
  `recommendation_overrides`, `order_fidelity`, `metadata.trust_layer_since`;
  `recommendation_resolutions` + `trust_scoreboard` are derived keys.
- **Offline test suite** (53 new tests): the XLK July-6th labeled failure case
  regression-locked (real scorecard path must block, engine must emit NO
  ENTER), the AAPL laggard -> `KILL_RS_SECTOR` EXIT on first pass, ALL_CLEAR
  emission, coverage-miss synthesis, stale/superseded/overridden matching,
  timeliness lag + late-after-action, graduation math (miss / under-cycles /
  reconciliation-blocked, each with the named reason), fidelity lifecycles
  (clean two-leg, fill-during-cancel orphan + page, unconfirmed cancel,
  out-of-bound slippage), crash recovery (open recs survive restart, no
  duplicate claims in-window), and migration idempotency.

## Risk-path math hardening

The app's *accounting* math was already honest; three places where a
bookkeeping-safe simplification leaked into a live **risk** decision (defend,
kill switch, assignment) are now corrected, and three more flagged items get
permanent verification tests. **No strategy rule, threshold, or trigger level
changed — only the inputs to them.** Payout/ledger outputs are untouched. Full
audit in `AUDIT_RISK_PATH.md`.

### Risk paths now run on honest inputs

- **Unclamped capture on the defend view.** The short-capture meter clamps/floors
  at 0% for payout accounting (an IV spike must never book as negative income) —
  correct there, but it hid an *underwater* short leg from the management view.
  `enrich_short` now also emits a signed `extrinsic_captured_pct_raw` and an
  `extrinsic_above_entry` flag; the position card surfaces the raw figure and an
  "extrinsic above entry (IV event)" indicator, and a new LOW-severity
  `EXTRINSIC_ABOVE_ENTRY` alert fires when a short's extrinsic rises >25% above
  entry. The clamped payout figure is unchanged.
- **Direct sector RS for the kill switch (and gate + scorecard).** RS3M-vs-sector
  was the *difference* of two RS-vs-SPY figures; it is now the true direct ratio
  `rs3m(stock, sector_etf)` over the same 63-day lookback everywhere — the same
  `indicators.rs3m` with a different benchmark (no fork), at zero extra cache
  cost. The kill switch's thinning band no longer lags on large sector moves. The
  entry-context snapshot records `rs3m_vs_sector_method` (snapshot schema v2→v3,
  additive; old snapshots still load).
- **Dividend-adjusted greeks on the assignment path.** The real continuous yield
  `q` (existing `dividends` cache; `q_source` logged, `0` fallback explicit) now
  flows through the delta-coverage guardrail, `portfolio_risk._leg_greeks` (book
  delta / beta-adjusted leverage), and the live `leap_health` roll-timing numbers
  (matching the stored burn marks, which already used q). The dividend-assignment
  trigger's extrinsic is the live quote (already q-aware via the market); when
  there is *no* quote — off-hours before ex-div, where it went silent — it now
  falls back to a q-aware Black-Scholes extrinsic so the escalation still fires.

### Verified and pinned

- **Day-count convention documented and pinned.** Juice/week and burn/week are on
  one shared 7-calendar-day base (θ ÷365 calendar × 7); a permanent worked-example
  test (`test_net_juice_day_count_convention_is_pinned`) encodes it end-to-end so
  it can't drift. θ's ÷365 is unchanged.
- **Payback state-machine validation.** `validate_payback` flags the three silent
  corruption modes (dangling LEAP roll, orphan roll-buy, `legs_remaining`
  mismatch) so a mislabeled execution log can no longer produce a
  plausible-but-wrong payback target; surfaced on `payback_reconciliation` (never
  raises into recompute). A full-cycle fixture asserts the meter at every
  transition, plus mutation negatives.
- **SAR causality property test.** Parabolic SAR (and the full four-light
  published regime) computed on history truncated at date D equals the value at D
  from the full-history run, for every D over a year of fixture bars — the
  invariant the regime backfill relies on. A boundary test documents that the
  guarantee holds only for prefixes sharing the earliest bar, which the backfill
  now makes explicit.

## Order lifecycle: entry order type + broker-side cancel/retry state machine

Two entry-path fixes, both fully exercisable offline (mocked broker + mocked
clock — no order is ever auto-sent to the live broker as part of this work).

**Entry order type.** The live entry was already ONE atomic two-leg NET_DEBIT
diagonal (buy-to-open the deep-ITM LEAP + sell-to-open the weekly short on one
ticket); the gap was that `build_net_order` hardcoded `complexOrderStrategyType`
/`duration` while the roll routed them through config. The entry now reads its own
provenance-tagged constants (`ENTRY_COMPLEX_STRATEGY_TYPE` / `ENTRY_ORDER_DURATION`)
so entry and roll can't silently disagree — `CUSTOM`/`DAY` today, with DIAGONAL a
`[LIVE-VERIFY]` swap. The standalone `buy_leap`/`sell_short` actions stay for
scale-in and leg repair; a fresh two-leg entry routes atomic (UI default).

**Cancel is broker-first, with an explicit state machine.** Cancels already sent
`DELETE` to Schwab before clearing local state and confirmed the async cancel; this
change makes the whole lifecycle explicit and closes the resubmission/partial gaps:

- **Explicit coded states** (`order_lifecycle.py`, pure functions):
  `SUBMITTED → WORKING → { FILLED | CANCEL_REQUESTED → PENDING_CANCEL →
  { CANCELED | FILLED_DURING_CANCEL | PARTIAL_FILL_CANCELED } | REJECTED | EXPIRED }`,
  plus a non-terminal `LOCKED_UNKNOWN` hard lock.
- **Fill-during-cancel** (a fill that lands after the DELETE): the fill is
  reconciled into state, the order is NOT retried, and a CRITICAL alert fires — the
  position is unexpectedly live.
- **Partial fill on cancel**: recorded as a distinct `PARTIAL_FILL_CANCELED` state
  that freezes the position for defensive review, trips the delta-coverage
  guardrail review, and alerts. The app flags; it never auto-fixes an unbalanced
  position.
- **Resubmission gate** (`NO_RESUBMIT_BEFORE_TERMINAL`): a per-position-intent lock
  persisted in `state.json` (survives restart). A new live order for an intent may
  only be sent once the prior order is confirmed terminal at the broker AND
  reconciled; `MAX_RESUBMIT_ATTEMPTS` per session then stops with an alert. This is
  IN ADDITION to the Level-5 account gate, kill switch, and reconciliation freeze —
  none are weakened.
- **DELETE failure handling**: if the DELETE is refused because the order already
  filled, the fill is reconciled; if it's refused while the order is still WORKING,
  the cancel retries per the bounded poll policy and, if exhausted, the position is
  hard-locked (`LOCKED_UNKNOWN`) — no resubmit ever while the broker state is
  unknown.
- **Startup reconciliation**: on app start, every locally non-terminal order is
  re-polled against the broker before any new order activity is allowed for its
  position; an unreachable order hard-locks its position so a crash mid-cancel can't
  orphan a working broker order invisibly.
- **Every transition is an append-only event** in `state.json` (`order_events`,
  with prior/new coded state + raw broker status); `recompute_derived()` derives the
  current `order_state` from the log — order state is never a mutated field.

### What changed

- **`backend/order_lifecycle.py`** (new): the coded-state vocabulary,
  `map_broker_status()`, `is_terminal()`, and the `check_resubmit()` invariant — all
  pure functions, no I/O.
- **`backend/executor.py`**: resubmit gate + per-intent lock on the live entry
  placers; `CANCEL_REQUESTED`/`PENDING_CANCEL`/`PARTIAL_FILL_CANCELED`/
  `FILLED_DURING_CANCEL`/`LOCKED_UNKNOWN` on the cancel path; config-driven bounded
  cancel polling; `reconcile_pending_orders_on_startup()`.
- **`backend/schwab_api.py`**: `build_net_order` takes `complex_strategy_type` /
  `duration` (defaults unchanged for exit / LEAP roll).
- **`backend/config.py`**: `ENTRY_COMPLEX_STRATEGY_TYPE`, `ENTRY_ORDER_DURATION`,
  `ORDER_FILL_TIMEOUT_SEC`, `CANCEL_POLL_INTERVAL_SEC`, `CANCEL_POLL_MAX_ATTEMPTS`,
  `MAX_RESUBMIT_ATTEMPTS`, `REPRICE_ON_RETRY` (`"none"` — never silently chase
  price), `NO_RESUBMIT_BEFORE_TERMINAL` (all provenance-tagged).
- **`backend/logging_handler.py`**: `order_events` / `order_locks` stores,
  `append_order_event`, `get`/`save_order_lock`, `list_pending_orders`, and
  `order_state` derivation in `recompute_derived`.
- **`backend/alerts.py`**: `ORDER_FILLED_DURING_CANCEL`, `ORDER_PARTIAL_FILL_CANCELED`,
  `ORDER_STATE_UNKNOWN`, `ORDER_RESUBMIT_EXHAUSTED` alert types.
- **`backend/app.py`**: `/api/execute` maps `ResubmitLockedError` to HTTP 409;
  startup runs order reconciliation after the durability check.
- **Migration v16** seeds the additive `order_events` / `order_locks` stores.
- **`backend/test_order_lifecycle.py`** (new): the pure state machine + the ten
  lifecycle branches (clean cancel, fill/partial during cancel, DELETE-error races,
  rejection, crash/startup reconcile, lock-held + max-attempts, golden entry JSON) —
  all offline with a mocked broker and clock.

## Payout = juice − LEAP burn (the leftover)

The monthly payout now nets out the **LEAP's weekly extrinsic burn**, so the
headline figure is the *leftover* an operator can actually take rather than the
raw juice: `payout = net juice collected − LEAP extrinsic burn`. The burn is the
REALIZED weekly extrinsic decay from the burn marks (`burn_marks.py`, same
whole-position dollars as the juice ledger), summed over the month and clamped so
a roll or IV spike that grows extrinsic can't masquerade as income.

### What changed

- **`backend/burn_marks.py`**: `monthly_realized_burn()` — realized LEAP extrinsic
  burn per calendar month, summed across tickers from consecutive marks'
  extrinsic drops (negatives clamped to 0 — burn is only ever a cost).
- **`backend/payouts.py`**: every month now carries `net_juice`, `leap_burn`,
  `burn_tracked`, and `net_payout` (the leftover); the payout headline and the
  finalize/paid snapshots are the leftover, with the juice/burn breakdown frozen
  alongside. Totals gain YTD juice and YTD LEAP burn. When a month has no burn
  marks yet the payout degrades cleanly to juice-only and says so.
- **`PAYOUT_READY` alert** now headlines the leftover with the juice − burn
  breakdown.
- **Frontend**: the Payouts cards, history table (Juice / LEAP burn / Leftover
  columns), and totals show the breakdown; the Overview glance headlines the
  leftover with the juice − burn sub.

## Monthly payout tracking

Income is booked as **net juice** (premium sold − buyback) on every short close,
but the dashboard had no month-by-month view of it and no notion of the operator
*paying themselves out* each month. A new **Payouts** tab tracks that: the
current month's estimated payout, the previous month's payout, the full monthly
history, and a per-month finalize → paid record — plus a push alert the moment a
month's payout can be finalized so it doesn't get forgotten.

A month moves through **in progress → finalizable → finalized → paid**. It
becomes *finalizable* — the point its income is locked in — the moment its **last
short of the month closes** (no open short leg still expires in it, so rolling the
final weekly into a next-month expiry flips it immediately), or when the calendar
month ends, whichever comes first. Finalizing snapshots the amount; marking paid
records the withdrawal.

### What changed

- **`backend/payouts.py`** (new). Net juice per calendar month is **derived**
  from the immutable `close_short` executions (same figure the theta ledger keys
  off) — never stored. The only thing persisted is the operator's payout
  bookkeeping: finalized/paid flags, timestamps, the **amounts snapshotted** at
  each step (frozen against later execution corrections), and an optional note.
  The finalizable signal reads the open `short_calls`' expirations (with an
  open_date+dte fallback for paper legs). `view()` returns the current-month
  estimate, the last month's payout, the month-by-month history, and roll-up
  totals (YTD / all-time / paid out / awaiting payout).
- **`PAYOUT_READY` alert** (`backend/alerts.py`). Fires once a month's payout can
  be finalized — its last short of the month has closed, or the month ended —
  with net income earned and not yet finalized: "July 2026 payout ready: $110.00
  net income — its last short of the month has closed." Scoped to the current +
  previous month so it reminds without spamming the back-history, auto-resolves
  when finalized, rides the existing notifier channels (Web Push / ntfy / email),
  and deep-links to the Payouts tab.
- **API**: `GET /api/payouts`, `POST /api/payouts/finalize`,
  `POST /api/payouts/unfinalize`, `POST /api/payouts/mark-paid`
  (`{month, amount?, note?}`; finalize/pay refuse a month still earning juice),
  `POST /api/payouts/unmark-paid`.
- **Frontend**: a new **Payouts** tab (`frontend/src/components/PayoutsTab.jsx`)
  with the est-this-month / last-month cards, totals, and a monthly history table
  with inline finalize / mark-paid / undo. App gains a `?tab=…` deep link so the
  payout push tap lands on the tab. The **Overview** landing shows a compact
  payout glance — this month's estimated payout + last month's — fed by a new
  `payouts` section on `/api/overview` (no extra call), linking to the tab.
- **Migration v15** seeds the additive `payouts` store; net juice stays derived,
  so no income data is copied. Covered end to end by `backend/test_payouts.py`.

## Genius four-light market regime (dwell + secondary indicators)

The market regime (**GREEN / YELLOW / RED**, Level 1 of the entry gate) is no
longer a single breadth + VIX rule. It is now the CFM course's **Genius System**:
four binary indicator "lights" on SPY daily bars, voted to a condition and held
against flapping by a **yellow dwell**. The traffic light is decided by the four
lights + the dwell **only**; breadth and VIX are kept as **secondary,
informational indicators** shown alongside the regime for the operator's own read
(they no longer change the light), and SPY's MA21 up/down trend is dropped
entirely.

### What changed

- **`backend/regime_genius.py`** (new, pure — no I/O, no clock; bars/timestamp/
  prior-series passed in). The four lights (each GREEN when bullish):
  1. close vs slow MA, 2. fast MA vs slow MA, 3. Parabolic SAR vs close,
  4. momentum (ROC) vs zero. **Vote** (`HARD_CFM_RULE`): ≥3 GREEN → GREEN, 2/2 →
  YELLOW, ≥3 RED → RED. Every intermediate is returned as a **decision trace**
  (each light + its values, the raw vote, the dwell state, the secondary
  breadth/VIX indicators, and the published regime).
- **New indicators** (`backend/indicators.py`): `ema`, `roc`, and a from-scratch
  **Wilder `parabolic_sar`** (no TA library) — unit-tested against a
  hand-computed fixture.
- **Yellow dwell** (`HARD_CFM_RULE`, `GENIUS_YELLOW_DWELL_DAYS = 3`): once the
  published regime turns YELLOW it holds YELLOW for a minimum of 3 **trading**
  days (the bar/record sequence, not calendar days) regardless of the raw vote —
  the course's anti-flap rule. Every day records both **`raw_condition`** (today's
  vote) and **`published_regime`** (after the dwell) so calibration sees both.
- **Secondary indicators**: breadth and VIX are **informational only** — they do
  **not** determine the traffic light. Each is reported with its value, a
  reference level (`BREADTH_CONFIRM_MIN_PCT`, `VIX_ELEVATED_THRESHOLD = 25`), and
  a confirming/diverging flag, purely as extra context the operator can weigh.
  (This replaces the earlier downgrade-only veto design per operator direction —
  breadth/VIX must not set the light.)
- **Published vs raw**: the entry gate (Level 1) and the regime-change alert
  consume only the **published** regime; raw four-light flaps never reach them.
- **Persistence** (`backend/regime_history.py`, `DATA_DIR/regime_history.json`):
  one full decision trace per trading day. This is **derived** telemetry
  (recomputable from cached SPY bars, like `iv_history.json`), so it is **not** an
  immutable execution and is **not** rebuilt by `recompute_derived`. Appended once
  per day by nightly maintenance and **backfillable** from cached parquet bars.
- **Entry-context snapshot** (`SNAPSHOT_SCHEMA_VERSION 1 → 2`): the regime section
  now carries the full four-light decision trace. **Additive** — older v1
  snapshots stay valid and still load.
- **Alerts**: a new deduped **`REGIME_CHANGE`** alert fires once per *published*
  transition (keyed on the from→to pair), never on raw flaps.
- **Calibration** (`backend/calibration.py`): `regime_series` /
  `regime_param_compare` / `regime_vs_cycles` recompute the historical raw-vote /
  published series under **alternative parameter sets** from cached bars, for
  offline comparison against realized cycle outcomes. Comparison-only — **no
  auto-tuning**.
- **Parameters are calibration-tunable defaults**: all four indicator parameter
  sets read from provenance-tagged `config.GENIUS_*`. The course fixes the
  indicator *types* and the vote/dwell logic (`HARD_CFM_RULE`); the parameters
  (MA lengths 50/21, SAR 0.02/0.20, ROC(10)) are `PROPOSED_DEFAULT`.
- **Frontend** (read-only): the Overview `RegimeHero` shows the four lights, the
  raw vote, the dwell status ("YELLOW — day 2 of 3 minimum"), and — neutrally, as
  secondary context — any diverging breadth / elevated VIX; the SPY stat is
  removed. The ribbon weather tooltip surfaces the raw vote and dwell day when
  they differ from the published regime.
- **Tests**: per-light units, the hand-computed SAR fixture, all 16 vote
  combinations, the dwell edge cases (hold-through-day-3, day-4 release, re-yellow
  inside the window, raw-crash held, cold start), that breadth/VIX are secondary
  (never change the light), and **labeled synthetic parquet regression fixtures**
  (`backend/fixtures/regime/`):
  a sustained confirmed-green hold, a distribution rollover degrading
  GREEN→YELLOW→RED in order, and a boundary whipsaw whose 1-day raw-green blip the
  dwell absorbs.

### Strike-policy regime wiring — audit finding (scoped follow-up)

The live roll ticket showing "**1×ATR, conservative**" in a YELLOW tape was **not**
a broken wiring: `strike_policy.suggest_strike()` already consumes the regime
status (now the dwell-adjusted **published** regime) and looks it up in
`config.STRIKE_TABLE`. The `1.0×ATR` figure is the literal `yellow`/`conservative`
cell. That table encodes a *shallower-when-safe → deeper-when-dangerous* scheme
(conservative green 0.5×, yellow 1.0×, red 1.5×) that predates — and contradicts —
the documented policy of **1.5× ATR in GREEN, 2.0× in YELLOW** (RED blocks entry).

The documented multiples are now present as `HARD_CFM_RULE` constants
(`STRIKE_ATR_MULT_GREEN = 1.5`, `STRIKE_ATR_MULT_YELLOW = 2.0`). Reconciling the
`STRIKE_TABLE` to them changes calibrated numbers for **both** postures and the
RED defend/roll-down rows, so it is deliberately left as a **separate, reviewable
change** rather than bundled into this regime work. No strike behaviour changed
here beyond the regime feeding it now being the published (dwell-adjusted) regime.

## Weekly theta burn & net-juice accounting

The per-position juice accounting no longer treats the LEAP's **total** entry
extrinsic as a cost to be paid off. The LEAP is held ~8 weeks and exited/rolled
around 130–140 DTE, so only the extrinsic **consumed during the hold window** is
a true cost — the rest is recovered when the LEAP is sold (minus slippage). The
headline per-position metric is now **net juice/week = juice collected/week −
theta burn/week**, and the entry queue ranks on it.

### What changed

- **`burn_projection()`** (new `backend/burn.py`) — the burn is the **difference of
  two Black-Scholes model prices**: the LEAP's model extrinsic at the current DTE
  minus its model extrinsic at the planned exit DTE (same spot & IV), divided by
  the weeks in that window. Never a straight-line proration of total extrinsic
  (`HARD_CFM_RULE BURN_IS_MODEL_DIFF`). Guard rails: auto-extends the window when
  a position is held past plan; floors burn at zero with a `low_extrinsic_flag`
  on deep-ITM drift; adds an explicit round-trip **exit-slippage** term.
- **`planned_exit_dte`** is now per-position state (default `PLANNED_EXIT_DTE = 135`),
  seeded onto existing positions by a forward-only migration (**schema v13 → v14**).
  All burn math keys off this, not off LEAP expiration.
- **Net juice is the headline** (`NET_JUICE_IS_HEADLINE`): `leap_health`, the
  portfolio income rollup (Overview), and the entry-queue ranking
  (`/api/scan/ready`, `queue_state`) all use net juice/week via one shared
  function — the queue and the position view can never disagree. This naturally
  penalizes high-IV candidates (more extrinsic bought → more burn) with no
  separate rule. The legacy `extrinsic_payback` meter is kept as a secondary
  capital-recovery view.
- **Weekly burn marks + divergence** (`backend/burn_marks.py`, telemetry in
  `DATA_DIR/burn_marks.json`, recorded by nightly maintenance at end-of-week):
  realized-vs-projected burn is queryable per position and book-wide — a live
  verification harness for the pricing model. Persistent divergence past
  `BURN_DIVERGENCE_WARN_PCT` surfaces a soft warning badge.
- **Frontend**: a per-position Theta-burn panel (Juice/wk · Burn/wk with a
  trend arrow · Net/wk), a coverage meter with threshold coloring, a weekly
  juice-vs-burn bar view (realized full-opacity, projected lighter), a
  hold-extension readout, and staleness/model-drift badges — all reusing existing
  Tailwind/flex-div primitives (no new chart library).

**Finding (documented in `IMPLEMENTATION_NOTES.md`):** for a real deep-ITM
0.90-delta LEAP the Black-Scholes extrinsic decay is **front-loaded**, so the
spec's "model burn < straight-line proration" and "extending the hold raises
burn/wk" assumptions (ATM-theta intuition) are inverted. The feature's actual
value prop — held-window burn ≈ ⅓ of total entry extrinsic — is confirmed and is
what the tests assert.

## Atomic spread roll orders (short-call roll)

The weekly short-call roll now completes the spec for **atomic** execution: a
live roll transmits ONE Schwab two-leg complex order (buy-to-close the old short
+ sell-to-open the new short) at a single NET_CREDIT / NET_DEBIT limit, so the
pair fills as a unit or not at all — no legging risk, one net crossing instead of
two. The atomic order construction, single `pending_orders` entry, and
per-leg-fill commit already existed; this change closes the remaining gaps.

### What changed

- **Feature flag** `ATOMIC_ROLLS_ENABLED` (default `True`). When off — or when the
  operator explicitly confirms after a rejection — the roll uses the legacy
  **legged** path (two independent single-leg orders, which carry legging risk).
  The legacy path is never a silent fallback.
- **`roll_group_id`** is stamped on both roll legs (equal to the ledger's
  `roll_id`), so a legged pair and an atomic pair are ledger-identical. A
  forward-only migration (schema v11 → v12) backfills it on historical roll
  executions.
- **Per-leg fill allocation is marked** on each execution (`roll_alloc_method`):
  `broker_per_leg` when Schwab reports per-leg fill prices, `proportional_to_mid`
  when it reports only a net (the net is split by the reference mids captured at
  ticket time), or `mid` for paper.
- **Partial fills** (multi-contract rolls) are booked as whole spread units; the
  remainder stays pending until it fills or cancels. All partials of one order
  share one `roll_group_id`.
- **Leg imbalance is a hard stop.** If Schwab ever reports a leg-imbalanced fill
  (one leg filled, the other not) at a terminal state, the position is **frozen**
  (`needs_review`) and a **CRITICAL `ROLL_LEG_IMBALANCE` alert** fires. No
  execution is written and nothing is auto-corrected (`ROLL_LEG_IMBALANCE_ACTION`).
- **Rejection surfaces a reason and an explicit legged-fallback offer** (behind a
  `confirm_leg_manually` confirmation) — never an automatic fallback.
- **Net roll slippage** is measured per roll (realized net vs the reference net
  mid) in `slippage.roll_report` and recorded per roll receipt in `fill_verify`.
- `ROLL_ORDER_DURATION` and `ROLL_COMPLEX_STRATEGY_TYPE` are now config constants
  (see below).

### Paper-economics shift (R4)

Paper fills are booked at the quoted **mid** and were never haircut on the
immutable ledger (the slippage haircut has always been a report-only caveat), so
this change does **not** alter booked paper roll prices. What it changes is the
**accounting model**: a paper roll is now treated as **one net crossing**
(`PAPER_ROLL_HAIRCUT_CROSSINGS = 1`) rather than the old illustrative two-per-leg
round-trip factor. Net roll slippage is reported as a single net figure per roll
instead of doubling a per-leg haircut. **Historical paper comparisons that relied
on the two-crossing round-trip figure will shift slightly** (roll economics look
marginally better under the single-net-crossing model). Booked ledger prices are
unchanged, so realized theta / payback / roll-ledger numbers do not move.

### Items requiring live verification (flagged, not assumed)

These depend on real Schwab behavior and are marked `LIVE_VERIFY` in the code /
audit. Confirm against a live account before production reliance:

1. **`complexOrderStrategyType` enum.** Defaults to `CUSTOM` (the safe superset
   for any strike/expiry call pair). Schwab also documents `DIAGONAL` (different
   expiry) / `VERTICAL` (same expiry); the exact enum its spread-approval logic
   wants is unverified. Configurable via `ROLL_COMPLEX_STRATEGY_TYPE`.
2. **Per-leg fill-price reporting.** The `broker_per_leg` allocation assumes
   Schwab populates per-leg `price` on a complex fill. When it doesn't, the code
   falls back to `proportional_to_mid` off the placement limit — verify which
   path real fills take.
3. **Partial-fill unit behavior.** Whole-spread-unit partial fills and the exact
   `filledQuantity` / per-leg `quantity` fields on a working complex order are
   assumed from the schema, not observed. Verify the partial-fill quantity
   reporting drives the imbalance/partial logic correctly.

### Config constants (provenance-tagged, see `backend/config.py`)

| Constant | Value | Provenance |
|---|---|---|
| `ATOMIC_ROLLS_ENABLED` | `True` | PROPOSED_DEFAULT — feature flag |
| `ROLL_ORDER_DURATION` | `"DAY"` | HARD_CFM_RULE — unfilled = canceled, no trace |
| `ROLL_NET_PRICE_SOURCE` | `"reference_net_mid"` | HARD_CFM_RULE — consistent with fill_verify |
| `ROLL_COMPLEX_STRATEGY_TYPE` | `"CUSTOM"` | PROPOSED_DEFAULT / LIVE_VERIFY |
| `ROLL_LEG_IMBALANCE_ACTION` | `"freeze"` | HARD_CFM_RULE — never auto-correct |
| `PAPER_ROLL_HAIRCUT_CROSSINGS` | `1` | PROPOSED_DEFAULT — single net crossing |

### Scope guard

LEAP-roll paths, the kill-switch, circuit-breaker, entry-gate, and strike-policy
logic are untouched. `state.json` changes are additive with a forward-only
migration. No new third-party dependencies.
