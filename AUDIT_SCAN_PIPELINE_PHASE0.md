> **Implementation status (post-audit).** The hard stop was lifted and all four
> phases are implemented on this branch — 1003 backend tests pass, frontend builds:
> - **P1** — `scan_triggers.py` (gate-complete verdict + calendar/conditional/
>   estimated/safety triggers, a READ of the already-computed gate — no re-eval);
>   Fixture D (`early_advance_extended`) proves READY-structure ⇒ WATCH, binding L4;
>   `/api/scan/ready` L4-blind filter + stale docstring fixed, L5 overlay triggers;
>   rejection log persists the structured binding + spot price (Q9).
> - **P2** — Scorecard BENCH filter (derived VIEW, `row.bench`), "path to READY"
>   drawer line, bench/~Nd verdict tag, the pipeline throughput strip; Ready-to-Enter
>   near-miss L5 path.
> - **P3** — `scan_diff.py` (nightly transition diff) + `scan_diff_log.py`
>   (append-only, Q9) wired into `maintenance.nightly_refresh`; 5 `SCAN_*` alert
>   types fanned through the existing notifier + per-type toggles; `?tab=Scan&ticker=`
>   deep-link focuses the row. Quiet hours confirmed absent → descoped.
> - **P4** — `universe_screen.py` (offline Finviz approximation: Perf-Quarter/RSI/
>   avg-vol from bars; market-cap + optionability reported/descoped, never guessed)
>   + `candidate_universe.py` (weekly candidate list + append-only change log +
>   sector-diversity report). SHADOW: `CFM_UNIVERSE_SCREEN` gates consumption
>   (default off, current universe = fallback). Orphan `universe.json.tmp` reaped.
>
> Every DO-NOT held: no rebuild of the shipped classifier/genius/gate; no parallel
> rules engine; no fifth verdict value; no executor/recommendation/kill-switch/
> regime-constant changes; no new provider integrations (market cap/optionability
> descoped with reasons); all new thresholds `PROPOSED_DEFAULT`.

# Phase 0 Audit — Scan Pipeline: From Snapshot to Pipeline

**Scope:** turn the scan from "what is READY today" into "what will be READY, when,
and will it tell me" — (1) forward-looking trigger conditions, (2) a near-ready
bench view, (3) daily transition diff + push alerts, (4) wider universe intake.
**Audit only — no code changed in this pass.** Every citation is `file:line`
against branch `claude/scan-pipeline-triggers-ciwizz`. All new thresholds proposed
below are `PROPOSED_DEFAULT`. Hard stop after this document.

Builds on the shipped scan restructure — `structure_classifier`, `symbol_genius`,
`scan_verdict.compose_verdict`, Level 3.5, the Level-2 veto, rejection / binding-
constraint capture, and the nightly dwell shadow-log. Those are **not** rebuilt here.

---

## TL;DR — the precondition is NOT met. Fix it first.

The AAPL "READY + fails entry gate level 4" report is **real and still present.**
The canonical scan verdict does **not** consume the full L1–L5 gate. It composes
only three inputs — the invisible market regime, the Symbol-Genius color, and the
structure entrability cell:

```
metrics/scorecard.py:478
composed = scan_verdict.compose_verdict(regime_color, sym["color"],
                                        cls["base_stage"], cls["inst_flow"])
row["verdict"] = composed["verdict"]        # scorecard.py:485 — the canonical scan verdict
```

`compose_verdict` (`scan_verdict.py:62-88`) places exactly those three inputs on
the worst-wins ladder. It **never sees**: the Level-2 sector veto, the Level-3
stock-lights vetoes (IVR / vs-sector RS), the **Level-4 right-spot / extension**
checks, or **any Level-5** account/earnings/sector-slot/juice check. So a name can
be `verdict == READY` while `screening.entry_gate` reports `cleared_level < 4` and
while Level 5 would block at Execute.

The bug reproduces on a **single row**: `row["verdict"]` can be `"READY"`
(`scorecard.py:485`) at the same time `row["suitability"]` is `"AVOID"` with reason
`"fails entry gate level 4"` (`scorecard.py:527-533`). The verdict ignores L4; the
demoted `suitability` catches it; they disagree in the same payload.

**Closing this is P1 item zero**, with Fixture D (AAPL 7/16 shape) as its guard.
Everything else in this pipeline is meaningless until READY means "will pass Execute."

---

## Q1 — Verdict completeness

### The two evaluations, side by side

| | Canonical scan verdict | Full gate | Execute (server-enforced) |
|---|---|---|---|
| Where | `scan_verdict.compose_verdict` @ `scorecard.py:478` | `screening.entry_gate` @ `screening.py:420-547` | `executor._enforce_account_gate` @ `executor.py:1639-1663` |
| L1 regime | ✅ `regime_color` | ✅ `screening.py:449` | ❌ not enforced |
| L2 sector veto | ❌ | ✅ `screening.py:459-469` | ❌ |
| L3 stock lights (+vetoes) | ⚠️ *proxy only* via Symbol Genius | ✅ `screening.py:487-495` | ❌ |
| L3.5 structure | ✅ entrability cell | ✅ `screening.py:506-517` | ❌ |
| **L4 right-spot / extension** | ❌ **(the AAPL gap)** | ✅ `screening.py:519-536` | ❌ |
| **L5 account & juice** | ❌ | ❌ (gate stops at L4) | ✅ `executor.py:439-440` |

Three facts fall out of this table:

1. **The row verdict is L1 + a symbol proxy + L3.5 only.** The "L3 proxy" is not
   the real L3: Symbol Genius (`symbol_genius.py:84-131`) is four lights
   (close>SMA50, **SMA50>SMA200**, SAR, ROC10) with **no vetoes and no right-spot**
   (`symbol_genius.py:123-124`), whereas real L3 `stock_lights` uses EMA21>SMA50
   plus the IVR and vs-sector-RS vetoes and folds the right-spot. So even L3 is
   only approximated, and L2/L4/L5 are absent entirely.

2. **The full gate `entry_gate` computes L1–L4 but stops before L5**
   (`screening.py:545` — `verdict = "READY TO ENTER" if cleared == 4 else "WAIT"`).
   It is **not** consumed by the canonical verdict. Despite `scan_verdict.py:5`
   claiming "the entry gate … call `compose_verdict`," the gate re-derives
   `cleared_level` independently and **never calls** `compose_verdict` (the only
   production caller is `scorecard.py:478` — confirmed by grep across `backend/`).
   The gate **is** passed into `score_ticker` (`scorecard.py:416`) but is used only
   for `gate_cleared_level`, for lifting lights/right-spot detail onto the row
   (`scorecard.py:417-428`), and for the demoted `suitability` via
   `_failed_stock_gate_level` (`scorecard.py:340-360, 527`). It does **not** reach
   the verdict.

3. **Execute's only server-side hard gate is L5** (`executor.py:439-440` →
   `_enforce_account_gate`, `executor.py:1639-1663`, overridable with
   `override_reason`). L1–L4 are **advisory** — enforced by the operator reading
   the scan, never re-checked server-side at order time. So "READY = will pass
   Execute" has two failure modes: (a) the operator trusts a READY that L4 fails,
   and (b) L5 blocks at Execute for a name the scan called READY.

### `/api/scan/ready` does not rescue it

`/api/scan/ready` (`app.py:135-230`) filters `r.get("verdict") == "READY"`
(`app.py:167`) — the same L4-blind canonical verdict — then layers L5 via
`account_gate.evaluate_many` (`app.py:168`) and splits ready / near_misses on the
L5 pass (`app.py:221`). **L4 is never consulted in this path either.** The
docstring (`app.py:137-148`) is **stale**: it claims the shortlist clears "Level 3
… Level 4 … AND Level 5" and evaluates L5 "only for tickers the Scorecard already
verdicts GO," but the code filters on the canonical `verdict` (which excludes L4),
not on `suitability == "GO"`. A name extended >1 ATR (L4 fail) with a green
regime + green SYM + `EARLY_ADVANCE × ACCUMULATING` structure is `verdict==READY`,
passes the L5 filter, and lands in `ready`.

### Account context at scan time — available, just not folded in

L5 needs account state (open positions for the sector slot, cash for the reserve).
`account_gate.evaluate_many` (`account_gate.py:394-407`) loads state **once**
(`account_gate.py:405`) and `resolve_operating_cash` (`account_gate.py:159-183`)
reads the **same live Schwab balance Execute uses**. So the account context Execute
uses **is** reachable in a bulk sweep — it is simply not wired into the row verdict.
Note the memoization tension: the market-driven scorecard sweep is cached
account-independently on purpose (`scorecard.py:592-597` — "doesn't depend on the
operator's own account state"). Folding L5 into the memoized verdict would break
that. **Recommended split (P1):** canonical verdict = full **L1–L4** (market +
structure, all already computed in the gate handed to `score_ticker`, still
memoizable); **L5** folded as a per-request account overlay where `/api/scan/ready`
already calls `evaluate_many`, and surfaced on the row as the binding constraint
when it is the worst blocker. This matches "same account context Execute uses"
without making the market sweep account-dependent.

### Fixture D (AAPL 7/16 shape) — data needs

`EARLY_ADVANCE × ACCUMULATING` + RS RISING + SYM green, but **extended >1.5 ATR
above MA21 with ATR expanding** (L4 fail), earnings 14d (L5), sector slot occupied
(L5) ⇒ **VERDICT ≠ READY, binding constraint = L4** (earliest failing; L4 < L5).
No such fixture exists — `backend/fixtures/structure/` has `early_advance_accum`,
`topping_distribution`, `turning_recovery`, but nothing extended-yet-entrable-
structure. Fixture D must build a bar series where structure classifies
`EARLY_ADVANCE`/`ACCUMULATING` yet `atr_extension > config.SPOT_ATR_EXTENSION_MAX`
(`scorecard.py:48-54`, `screening.py:530`) and `atr_momentum > 1.0`
(`screening.py:527`), plus a mocked `earnings.next_earnings` at +14d and an
injected open position in the same sector for the slot check.

---

## Q2 — Binding-constraint → trigger mapping

### What the gate exposes today

- **Structured per-check results already exist in both gates** — they are just not
  what the row verdict / binding constraint reads:
  - `entry_gate` returns `levels[].checks[]`, each `{label, value, pass}`
    (`screening.py:411-413`, e.g. L4 checks at `screening.py:523-533` carry the
    ATR%, ATR-momentum, and extension **observed values**).
  - `account_gate.evaluate` returns `checks[]`, each `{id, label, pass, blocking,
    detail}` (`account_gate.py:234-236, 375-382`) with observed numbers in
    `detail` (earnings dict, sector already-held list, weekly yield, etc.).
- **The binding constraint today is a STRING from the 3-input composition only.**
  `scan_rejection_log.binding_constraint(row)` (`scan_rejection_log.py:71-84`)
  returns the first non-`rs:` entry of `verdict_reasons`, which `compose_verdict`
  formats as `"<input>:<LEVEL>"` (`scan_verdict.py:81-82`) — e.g. `regime:BLOCKED`,
  `symbol:WATCH`, `structure:BLOCKED`. It can therefore **never** name L2/L4/L5 as
  the binding constraint, because those never enter `compose_verdict`.

**Answer to "can the gate return a structured failure without re-evaluation":**
**Yes.** The structured `(level, check_id, observed_values)` is already computed —
`entry_gate.levels[].checks[]` is built during the sweep (gate handed to
`score_ticker` at `scorecard.py:416`) and `account_gate.checks[]` is built in the
`/api/scan/ready` overlay. Triggers must **annotate that single evaluation**, not
re-run a parallel rules engine. P1 should thread the structured first-fail (with
values) onto the row so the binding constraint and the trigger share one source.

### Every current blocking check, classified

| Gate | Check | Citation | Kind |
|---|---|---|---|
| L1 | regime published == green | `screening.py:448-449` | **conditional** (`regime == GREEN`); dwell floor → **calendar** |
| L1 | yellow-dwell earliest transition | `config.py:191` `GENIUS_YELLOW_DWELL_DAYS=3` | **calendar** (dwell start + 3 trading days) |
| L2 | sector RS1M vs SPY not negative | `screening.py:461-462` | **conditional** (`sector RS1M > 0`) |
| L2 | sector breadth not collapsing | `screening.py:463-464` | **conditional** |
| L2 | sector not under distribution | `screening.py:465-466` | **safety** (distribution = "no", no trigger) |
| L3 | 4 stock lights green | `screening.py:487-493` | **conditional** (per-light predicates) |
| L3 | IVR volatility veto | `stock_lights` (via `screening.py:314`) | **conditional** (IVR below threshold) |
| L3 | vs-sector RS veto | `stock_lights` | **conditional** (`RS3M vs sector > 0`) |
| L3.5 | structure entrable | `screening.py:506-517`, `structure_classifier.py:387-406` | TOPPING/DECLINING/DISTRIBUTING/INSUFFICIENT → **safety**; `BASING×EARLY_INTEREST` WATCH → **conditional/estimated** (needs EARLY_ADVANCE) |
| L4 | ATR% ≤ 5.0 | `screening.py:524-526`, `config.py:248` | **conditional** |
| L4 | ATR contracting/flat ≤ 1.0 | `screening.py:527-529`, `config.py:267` | **estimated** (ATR-contraction rate) |
| L4 | extension ≤ 1.5 ATR above MA21 | `screening.py:530-532`, `config.py:266` | **estimated** (distance to MA21 at drift) |
| L5 | cash_reserve ≥ 2×ATR book | `account_gate.py:281-290` | **conditional** (account state) |
| L5 | position_limit | `account_gate.py:294-297` | **conditional** (a slot frees) |
| L5 | capital_limit | `account_gate.py:302-306` | **conditional** |
| L5 | **sector_concentration** | `account_gate.py:312-317` | **conditional** (`no open position in <sector>` — the "slot opens" trigger) |
| L5 | juice_adequacy | `account_gate.py:327-334` | **conditional** (structural; weak-trigger) |
| L5 | **earnings_in_cycle** | `account_gate.py:349-362` | **calendar** (see Q3) |

Kinds map cleanly onto the design's calendar / conditional / estimated / safety
taxonomy. The three **estimated** ones (L4) are exactly where the "days-to-trigger"
approximations live; all estimation constants stay `PROPOSED_DEFAULT`.

---

## Q3 — Earnings & calendar data

- **Source & horizon.** Earnings come from Alpha Vantage `EARNINGS_CALENDAR`
  (`earnings.py:72-84` → `alpha_vantage.earnings_calendar`, `alpha_vantage.py:114-124`,
  default `horizon="3month"`, up to `12month`), cross-checked against Schwab
  fundamentals (`earnings.py:94-127`), day-cached to `DATA_DIR/earnings_cache.json`
  (`earnings.py:30-31`, TTL 24h), manual override wins. **Earnings are known ~3
  months ahead** — ample for a calendar trigger. `cached_earnings`
  (`earnings.py:180-193`) is cache/override-only and never hits a provider (safe
  for the bulk sweep — already used at `scorecard.py:448`).

- **The earnings "buffer" is NOT a discrete constant — do not invent one.** The
  gate blocks the **entire planned cycle window**: `earnings_in_cycle` fails when
  `0 <= days_until <= config.CYCLE_WEEKS_MAX * 7` (`account_gate.py:358-359`), i.e.
  a **56-day** block (`CYCLE_WEEKS_MAX = 8`, `config.py:944`, **HARD_CFM_RULE**).
  Consequence for the calendar trigger: an earnings-blocked name's deterministic
  eligible date is **the trading day after the report** — once `days_until` goes
  negative, `next_earnings` returns the *following* report (~90d out) and the check
  clears. There is **no** "earnings + N-day settle buffer" today. If a post-report
  settle buffer is desired for the trigger (Fixture D's "eligible ~8/2" implies
  one), it is a **new `PROPOSED_DEFAULT`**, flagged as such — not an existing rule.
  The only fixed earnings day-counts that exist are for position flagging/staleness,
  not the entry gate: `EARNINGS_WARN_DAYS=7` (`config.py:460`),
  `EARNINGS_STALE_DAYS=4` / `EARNINGS_CONFLICT_DAYS=3` (`config.py:473-474`,
  PROPOSED_DEFAULT).

- **Regime-dwell calendar.** `GENIUS_YELLOW_DWELL_DAYS = 3` (`config.py:191`,
  **HARD_CFM_RULE**) → earliest regime transition = dwell start + 3 trading days.

---

## Q4 — Bench derivability

- **Bench derives purely from `(verdict, binding-constraint kind)` — no new state.**
  The row already carries `verdict` (`scorecard.py:485`) and `verdict_reasons`
  (`scorecard.py:486`); `binding_constraint` is a pure read
  (`scan_rejection_log.py:71-84`). BENCH = "non-READY, would be READY but for a
  calendar/conditional/estimated (non-safety) blocker." It is a **filtered view**,
  not a fifth verdict value — `compose_verdict` output is untouched.
- **Caveat (depends on P1):** with today's L4/L5-blind verdict, bench cannot tell a
  name waiting on an L4 calendar/conditional blocker from a safety "no," because the
  verdict never records L4/L5. Bench membership becomes correct only once the P1
  fix threads the structured L1–L5 first-fail (and its kind) onto the row.
- **Where the view lives (frontend).** `Scorecard.jsx`: the combined filter
  predicate `filtered` (`Scorecard.jsx:449`), the `FILTERS` toolbar
  (`Scorecard.jsx:371, 486-487`), the client-side `counts` fold
  (`Scorecard.jsx:438-442`), and the sort in `sortRows` (`Scorecard.jsx:161-187`).
  Bench is a new `useMemo` beside `filtered` + a toggle button in the toolbar, and
  a bench sort key (calendar first, then EST ascending, then SCORE/JUICE desc). The
  per-row "path to READY" line renders in the inline drawer (`Scorecard.jsx:291-366`,
  where `verdict_reasons` already render at `:301`) or the ticker cell (`:253`).

---

## Q5 — Diff & alert plumbing

- **Nightly job pattern (the template to copy).** `alert_scheduler` ticks every 30s
  (`alert_scheduler.py:33, 325-327`) and fires `maintenance.nightly_refresh()` once
  per calendar day after `MAINTENANCE_ET` = `"17:30"` ET (`config.py:576`,
  gated by `maintenance_due` at `alert_scheduler.py:153-176`). Inside it, three
  derived stores are written the same way a diff log would be:
  regime dwell (`maintenance.py:184-190`), Symbol-Genius shadow-log
  (`maintenance.py:198-204`), and **`scan_rejection_log.record_scan`**
  (`maintenance.py:213-217`) which appends today's full-universe sweep to
  `DATA_DIR/scan_rejection_log.json` (`scan_rejection_log.py:35, 145-171`) —
  append-only, idempotent per day, single nightly writer.
- **Where yesterday's scan state is read from.** `scan_rejection_log` **already
  persists** per-symbol-per-day: `verdict`, `binding_constraint`, `score`,
  `rs_state/level/slope`, `net_juice`, `base_stage`, `inst_flow`, `sym`,
  `sector_rs1m`, `iv_rank` (`scan_rejection_log.py:87-104`), readable via
  `series(ticker)` (`scan_rejection_log.py:110-112`). So the day-over-day diff
  (BENCH→READY, any→READY, BASE→TOPPING, INST→DISTRIBUTING, RS→FADING/FALLING,
  new `BASING+EARLY_INTEREST`) is derivable from the newest two records **already
  stored** — no new snapshot artifact needed for those fields. Diff **events** must
  be a **separate append-only log** (not the rejection log, which is
  last-write-wins per day — `scan_rejection_log.py:163-167`).
- **Notifier / webpush fan-out entry points.** `notifier.dispatch(alerts, settings,
  dry_run)` (`notifier.py:136-168`) → `WebPushNotifier.send` (`notifier.py:105-118`)
  → `webpush.send` (`webpush.py:209-247`). Payload `_payload`
  (`webpush.py:192-206`): `{title, body, severity, count, tickers, tag, url}`. Fan
  out new events through this **existing** path — no new infrastructure.
- **Per-event-type enable/disable EXISTS.** `alerts.get_settings` →
  `enabled`/`channels` (`alerts.py:1039-1046`), enforced at `alerts.py:1057`,
  patched by `update_settings` (`alerts.py:1179-1192`, validated against
  `ALERT_TYPES` — 33 types at `alerts.py:33-67`). Frontend precedent: per-type
  checkbox grid + per-channel toggles in `AlertsPanel.jsx:90-109`. New scan-event
  types slot into `ALERT_TYPES` and inherit the toggle UI for free.
- **Quiet hours — DOES NOT EXIST. Descope.** No quiet-hours / snooze / DND anywhere
  in backend or frontend (confirmed by grep). The only time-gating is the scheduler
  slot schedule, not per-notification suppression. Per the design's "cite; don't
  build if absent" — this is a **descope note**, not P3 work.
- **Deep-link to a scan row — partial precedent, needs a new intent path.** The push
  → tab flow works: service worker `push` + `notificationclick` navigate
  `data.url` (`frontend/public/sw.js:23-62`); `App.jsx:89-109` parses URL params and
  `?tab=` deep-links land on a tab. But `goToAction` routes **only to Positions**
  (`App.jsx:80-84`), and `Scorecard` takes **no intent prop** (`App.jsx:217`,
  `Scorecard.jsx:373`). A "deep-link to symbol's scan row" needs a new
  `scanIntent`-style path analogous to `positionIntent` — the alert sets
  `url = /?tab=Scan&ticker=XYZ`, and Scorecard consumes an intent to open/scroll the
  row. The `action_url` builder (`alerts.py:90-105`) is the place to add the scan URL.

---

## Q6 — State discipline

| Proposed artifact | Fits derived / single-writer rules? | Notes |
|---|---|---|
| **Trigger annotations** (per scan run, not persisted) | ✅ | Pure, like `verdict_reasons` (`scorecard.py:504-507`); computed in `score_ticker`, never stored. |
| **Diff events log** (append-only) | ✅ | New store under `DATA_DIR`, single nightly writer — copies the `scan_rejection_log` template (`scan_rejection_log.py:1-25, 54-65`). Must be **append-only** (distinct from the last-write-wins rejection log). Not in `state.json`, not rebuilt by `recompute_derived`. |
| **Universe list + change log** (append-only) | ✅ | `universe.json` is already an editable store on the volume (`config.UNIVERSE_PATH`, `sector_data.py:1-18, 147-169`). A derived *candidate* universe + change log = new append-only stores under `DATA_DIR`. |

**No new artifact touches the `state.json` schema.** (Push subscriptions already
live in `state.json` under `alerts.push_subscriptions` — `webpush.py:132-133` — but
that is existing, not new.) One cleanup flag: an orphaned `universe.json.tmp.uej4hh_8`
exists in `backend/` — a stray atomic-write temp (`sector_data.py:97-101` →
`logging_handler._atomic_write` `logging_handler.py:170-204`); the startup temp
sweeper (`logging_handler.py:212-224`) does not cover `UNIVERSE_PATH`, so it was not
reaped. Harmless, worth a one-line broom in P4.

---

## Q7 — Universe data availability (Finviz-screen approximation)

| Criterion | Computable offline from cached bars? | Citation |
|---|---|---|
| **Perf Quarter > +15%** | ✅ `indicators.roc(df, ~63)` | `indicators.py:311-325` |
| **RSI 50–70** | ✅ `indicators.rsi` | `indicators.py:55-68` |
| **avg volume > 500K** | ✅ data present, **needs a small new pure helper** (no `avg_vol` fn today) | `Volume` column in bars; no helper in `indicators.py` |
| **mid/large cap** | ❌ **needs a NEW provider read** | see below |
| **optionable / weekly chain** | ❌ **live Schwab probe**, cached 7d | `weeklies.py:80-129`, `config.py:455` |

- **Market cap is not ingested anywhere.** No market-cap / shares-outstanding /
  float field is cached or read in the backend (grep-confirmed). Schwab
  `get_instrument_fundamental` (`schwab_api.py:332-342`) is fetched live and only
  `divYield` is read. Alpha Vantage `overview()` (`alpha_vantage.py:127-130`)
  **does** expose `MarketCap`, but the codebase reads only `DividendYield` from it
  (`dividends.py:85, 193`). **Cost:** a budgeted per-symbol AV call (could piggyback
  on the already-wired `overview()`), **not** offline-from-bars. → **descope-eligible**:
  approximate size with an avg-dollar-volume floor from bars, or accept the AV cost
  and log it. Do not guess Schwab fields.
- **Optionability requires a live chain probe.** `has_weeklies`
  (`weeklies.py:112-129`) resolves override → cache → `_detect` (a tiny live
  option-chain fetch, `weeklies.py:80-109`, needs Schwab), cached 7d
  (`config.py:455`), overridable via `metadata.weeklies_overrides`. Not static, not
  from bars — but the 7-day cache + prefetch (`weeklies.py:132-139`) means a weekly
  refresh amortizes it.
- **Universe & fallback.** Universe is a repo seed (`tickers_by_sector.txt`)
  promoted to an editable `DATA_DIR/universe.json` store that **self-heals from the
  seed** if lost (`sector_data.py:8-9, 55-77, 147-169`) — the baked-in list is
  already the safety-net/fallback the design wants. `all_tickers()` /
  `constituents()` / `sector_etfs()` at `sector_data.py:283-304`.
- **Weekly refresh job — reuse existing detached-thread machinery.**
  `screening.start_background_scan` spawns a deduped detached daemon thread
  (`screening.py:99-114`) with `scan_status` polling (`screening.py:123-131`); the
  full sweep is `warm_scan_cache` (`screening.py:35-66`). **No weekly scheduler slot
  exists** — the pattern to imitate is `burn_marks.weekly_due()`, an ISO-week gate
  run *inside* the nightly job (`maintenance.py:221-227`). A weekly universe refresh
  = an ISO-week gate in `nightly_refresh` that kicks the detached full-universe
  screen, sharing the same job machinery. **Sector-diversity report** = a fold over
  the surviving candidates by `sector_data.sector_for` (`sector_data.py:318-319`),
  emitted into findings after the screen.

---

## Q8 — Throughput metrics

The header counts (READY now / eligible ≤14d / beyond) are a **client-side fold over
row verdicts + triggers**, the same shape as the existing `counts` fold
(`Scorecard.jsx:438-442`, rendered into the filter buttons at `Scorecard.jsx:472`).
**No separate computation.** One dependency: rows carry no "days-to-eligible" field
today (`earnings_days` exists at `Scorecard.jsx:354`, but no `days_to_ready`), so the
"≤14d" bucket needs the per-row trigger's estimated/calendar eligible-date from
P1/P2 to be present on the row. Header renders in the Card `right` slot
(`Scorecard.jsx:477-479`) or a banner above the toolbar (`Scorecard.jsx:486`).

---

## Q9 — Retrospective capture gaps (design-only — do NOT build the analysis)

The deferred miss-analysis asks two questions: *did L4-blocked names resolve into
entries we missed?* and *did skipped FADING names keep rising?* To answer them later
without schema regret, these must be captured **now** (P1/P3), because they are not
recoverable after the fact:

1. **Structured binding constraint (P1).** Today `binding_constraint` is a string
   from `compose_verdict`'s three inputs (`scan_rejection_log.py:71-84`) and can
   never say "L4" or "L5." Once P1 threads the structured `(level, check_id,
   observed_values)` onto the row, persist **that** in the rejection log so
   "L4-blocked names" is queryable. **Gap today.**
2. **Forward price/return anchor.** `scan_rejection_log` records no spot price or
   forward return (`scan_rejection_log.py:87-104`), so "did it keep rising after we
   skipped it" is unanswerable — there is nothing to join a later price against.
   Add a per-record close/price so a future pass computes forward returns. **Gap.**
3. **Diff-events log with timestamps (P3).** BENCH→READY and any→READY transitions,
   timestamped per symbol, are what "resolved into entries we missed" joins against.
   The append-only diff log (Q5) is the artifact; ensure it records the transition
   date and the pre/post verdict. **Built in P3 — must include timestamps.**
4. **Entry linkage.** "Entries we missed" = diff events (any→READY) **not** followed
   by an open in the executions ledger for that symbol. Derivable if both are
   timestamped per symbol; no new capture beyond (3) + the existing immutable
   executions log. **No new work, just confirm the join key (ticker + date) exists.**

Close (1)–(3) as they are built; do not build the analysis itself.

---

## Phase plan (hard stop after this document)

**P1 — Verdict completeness + structured failures + trigger classification (pure).**
- *Item zero:* make the canonical verdict consume the full gate. Recommended:
  verdict = L1–L4 (from the gate already computed in `score_ticker`, memoizable),
  with L5 folded as a per-request account overlay where `/api/scan/ready` already
  runs `evaluate_many`; surface the earliest structured first-fail (level, check_id,
  observed values) as the binding constraint. Do **not** add a fifth verdict value;
  do **not** rebuild `compose_verdict` — extend its inputs / annotate its output.
- **Fixture D** (AAPL 7/16: `EARLY_ADVANCE×ACCUM` + RS RISING + SYM green, extended
  >1.5 ATR / ATR expanding, earnings +14d, sector slot occupied ⇒ VERDICT ≠ READY,
  binding = L4). Fix `/api/scan/ready`'s stale docstring + L4-blind filter.
- Structured gate failure (no re-evaluation — annotate `entry_gate.levels[].checks[]`
  + `account_gate.checks[]`). Pure trigger classifier (calendar/conditional/
  estimated/safety) over that single evaluation. New estimation constants =
  `PROPOSED_DEFAULT`. Persist the structured binding in the rejection log (Q9 gap 1)
  and add a per-record spot price (Q9 gap 2).

**P2 — Trigger rendering + bench view + throughput header.**
- "Path to READY" line in the row/drawer (`Scorecard.jsx:291-366`). Bench filtered
  view + sort beside `filtered` (`Scorecard.jsx:449`) and the toolbar
  (`Scorecard.jsx:486`). Throughput header as a client fold (Q8), consuming the
  per-row eligible-date from P1.

**P3 — Snapshot diff + alert events + Settings toggles + deep links.**
- Nightly diff (ISO the dwell-shadow-log slot, `maintenance.py`) reading the newest
  two `scan_rejection_log` records; emit events through `notifier.dispatch`. New
  `ALERT_TYPES` entries inherit the per-type toggle UI (`alerts.py:33-67`,
  `AlertsPanel.jsx:90-109`). Append-only diff-events log under `DATA_DIR` with
  timestamps (Q9 gap 3). New `scanIntent` deep-link path into Scorecard (Q5). Quiet
  hours **descoped** (absent — Q5). Retrospective stays deferred; its capture gaps
  close here.

**P4 — Universe intake + sector-diversity report.**
- Weekly ISO-week gate in `nightly_refresh` kicking the detached full-universe screen
  (`screening.start_background_scan` machinery). Offline criteria from bars
  (Perf-Quarter via `roc`, RSI, avg-volume helper); market-cap + optionability
  reported with cost, **descope-eligible** if the AV/chain calls aren't justified.
  Append-only candidate universe + change log; current universe = fallback.
  Sector-diversity fold over survivors. Reap the orphan `universe.json.tmp` (Q6).

### DO-NOT (held throughout)
No rebuild of the shipped classifier/genius/verdict/gate modules except the P1
verdict-completeness fix. No parallel rules engine — annotate the one gate
evaluation. No fifth verdict value — BENCH is a derived view. No auto-enter /
auto-size / executor / recommendation / kill_switch / circuit_breaker / regime-
constant changes — alerts inform, the operator acts. No new notification
infrastructure or provider integrations without the audit-justified need above. No
guessed Schwab fields, no live broker calls in tests — pure functions + offline
fixtures with mocked clock/provider. No hand-edited state — new artifacts are
append-only derived. Every new threshold / estimation constant = `PROPOSED_DEFAULT`.
