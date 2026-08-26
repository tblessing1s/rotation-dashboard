# Evaluation prompt — CFM rotation-dashboard

Paste everything below the line into a fresh chat.

---

You are a senior staff engineer doing an independent architecture and risk review. I am
going to describe a system in detail. You do **not** have access to the source code — every
fact below was established by reading the codebase directly, and I have marked which claims
are *measured* (I ran it) versus *described* (I read it). Treat the description as accurate
but not exhaustive, and tell me explicitly where you would need to see code to be confident.

Do not be diplomatic. I want the review a skeptical colleague would give, not a summary of
what I already said.

---

## 1. What the system is

A private, single-user trading dashboard for one mechanical options strategy. Not
multi-tenant, not a platform, not a backtester. One person operates it, with real money, as
a side activity alongside a full-time job.

**The strategy.** Hold 100 real shares of a strong, consolidating stock as the base leg.
Sell a weekly in-the-money covered call against those shares. Roll the short each week. The
premium collected net of buyback is called "juice" and is the income. The app's job is to
decide which names qualify, enforce discipline at the moment of execution, and keep an
auditable record of what the income actually was.

**A completed migration.** The app originally used a poor-man's-covered-call structure: the
base leg was a deep-ITM LEAP call (~0.90 delta, ~180 DTE) rather than real shares. That
structure is now permanently retired. A constant `LEGACY_LEAP_READONLY = True` is hard-coded
and deliberately *not* environment-overridable; the executor refuses every LEAP-opening
action. LEAP *derivation* code remains because the execution log is append-only and
historical LEAP fills must keep pricing forever, but there is no path back to opening one.

## 2. Shape and scale (measured)

| | |
|---|---|
| Backend | Python 3.11, Flask, pandas/numpy/pyarrow — 59,152 lines |
| — of which tests | 21,881 lines |
| Frontend | React 18 + Vite + Tailwind — 9,950 lines; exactly two runtime deps (react, react-dom); no router, no state library |
| Backend modules | 90 non-test modules, flat layout (modules import each other by bare name, no packages) |
| Test files | 81 files, 1,340 collected tests |
| API routes | 98, all in a single 2,022-line `app.py` |
| Largest module | `executor.py`, 4,341 lines |
| Persistence | No database. One JSON file (`state.json`) on a disk volume, plus a parquet cache of daily price bars |
| Providers | Schwab Trader API (primary: bars, quotes, option chains, order placement, fundamentals), Alpha Vantage (fallback: bars, quotes, earnings calendar). Nothing else. |
| Deployment | Fly.io. **One** machine, **one** 1 GB volume, `min_machines_running = 1` |
| Docs | ~12,000 lines of markdown: a changelog, ~15 "phase 0 audit" documents, decision records, runbooks |

## 3. Core architecture

### 3.1 Append-only log, everything else derived

`state.json` is the single source of truth. Its keys split three ways:

- **Immutable records** — `executions`, `order_events`, `recommendations`,
  `recommendation_overrides`, `ingested_transactions`. Appended, never rewritten. A reversed
  adoption is marked `reversed_by` and excluded from replay, but both the adoption and its
  reversal stay on the log for audit.
- **Derived** — theta ledger, extrinsic payback, roll ledger, closed cycles, accrual ledger,
  trust scoreboard, order state. A function `recompute_derived()` rebuilds all of these from
  the immutable records after *every* append. A full rebuild is claimed to be byte-stable.
- **Operator facts** — risk posture, cash balance, manual overrides, and the record of which
  monthly payouts were actually withdrawn.

Design rule stated in the codebase: prefer fixing the derivation over editing state.

### 3.2 Durability

- Writes serialize to a string first (so an unencodable value raises before any file is
  touched), then write to a temp file in the same directory, fsync it, `os.replace` over the
  target, then fsync the *directory* so the rename itself is durable. Guarded by a process
  lock; single-writer by design.
- A corrupt/unparseable `state.json` raises on load, logs CRITICAL with a pointer to the
  newest backup, and **refuses to start** rather than silently re-initializing empty state
  over a live trading record. The worker crashes.
- 21 additive schema versions. The migration runner snapshots the pre-migration file to a
  backups directory first and *aborts* if that snapshot cannot be written.
- Nightly rotating backups taken under the write lock, plus one copy shipped off-machine
  (email attachment or S3). A restore CLI and a written recovery runbook exist.

### 3.3 Pure cores, impure shells

A repeated pattern: the decision logic is a pure function with the clock injected, and a thin
impure shell does the I/O. Applied to verdict composition, forward-looking triggers, the
time-of-day execution gate, the recommendation engine, the tiered data scheduler, the order
lifecycle state machine, theta-burn math, and the structure classifier. This is why the whole
suite runs offline with no credentials.

## 4. The decision pipeline

Six stages:

1. **Ingest** — daily OHLCV per symbol (Schwab, then Alpha Vantage), cached to parquet. If
   both providers fail but a cached frame exists, the stale frame is returned *visibly aged*
   rather than nothing.
2. **Classify** — two deliberately independent reads per name. (a) A shared four-light
   "Genius" engine (close vs slow MA, fast vs slow MA, Parabolic SAR, ROC momentum) applied
   fractally to the market index and to each stock. (b) A structure classifier reading where
   a name sits in its base→advance→decline cycle and whether volume shows institutional
   accumulation or distribution. The lights judge trend; the classifier judges structure.
   They are expected to disagree on the cases that matter — e.g. a name whose trend lights
   are green while its base is topping.
3. **Compose a verdict** — worst-signal-wins over regime color + per-name color + structure
   entrability, then folded together with *every failing gate block*, so that READY means
   "will pass execution". (This fixed a real bug where a name read READY on the scan tab and
   then failed a gate level at the order ticket.) Vocabulary: READY / CAUTION / WATCH /
   BLOCKED. A safety "no" forces BLOCKED; a merely-waiting condition degrades to WATCH. The
   module also emits one machine-readable trigger per non-READY block, typed as CALENDAR
   (a deterministic date), CONDITIONAL (a predicate on observable state), ESTIMATED (a crude
   days-to-trigger, always labeled), or SAFETY (carries no trigger). This renders a
   "path to READY" line per name.
4. **Gate** — five entry levels plus three orthogonal execution guards (section 5).
5. **Execute** — one funnel function captures the price at execution time and appends an
   immutable record.
6. **Derive and monitor** — `recompute_derived()`, then scheduled alerts and recommendations
   push to the operator's phone.

## 5. The gate stack

**Entry levels (stop on first fail):**

| Level | Question | Representative checks |
|---|---|---|
| L1 Regime | Is the tape green? | Four-light vote on SPY: ≥3 green → GREEN, 2/2 → YELLOW, ≥3 red → RED. YELLOW carries a mandatory 3-trading-day dwell so it can't flap. RED blocks every entry. |
| L2 Sector | Is the sector leading? | Sector relative strength vs SPY, breadth > 60%, ATR expanding; breadth below 40% is a collapse veto. |
| L3 Stock | Does the name beat peers? | 3-month relative strength vs SPY > +5%; per-name four lights (4/4 green = GREEN, 3 = watchlist only); plus vetoes. |
| L4 Right spot | Coiled, not extended? | ATR% of price, ATR vs its 5-EMA, extension above the 21-day MA in ATR units (≤ 1.5). |
| L5 Account & juice | Is the account ready and does the trade pay? | Cash reserve (2×ATR per open position), max open positions, deployed-capital cap, sector concentration cap, weekly-juice adequacy, per-position lot-cost ceiling. |

L5 is enforced **server-side inside the executor**, not at the HTTP route. A blocking failure
rejects with HTTP 400 unless the payload carries an explicit `override_reason` — which is
then written onto the immutable execution record along with the list of checks it overrode.
Overrides are possible but never invisible.

**Three orthogonal guards on transmission:**

- **Execution window** (pure, clock-injected). The first 30 minutes after the open and the
  last 15 before the close are blocked for order transmission; entries additionally wait 60
  minutes on the reasoning that entries are never urgent. *Alerts still fire immediately* —
  only orders are deferred. One narrow gap-emergency exception exists for defensive rolls and
  kill-switch exits, and it is fail-closed (a filling gap never unlocks it). A rule
  `NO_MARKET_ORDERS_AT_OPEN` is absolute, emergency included. The gate correctly models
  half-day sessions (the close blackout keys off the *actual* close).
- **Spread quality.** A trailing bid-ask baseline per contract, sampled from quotes the data
  layer already fetches — no new polling. A current spread more than 2× baseline requires an
  explicit operator acknowledgement carrying the estimated excess slippage. Below 5 samples
  there is *no baseline* rather than a fabricated average.
- **Reconciliation freeze.** A position whose recorded state has diverged from the broker is
  frozen: new-risk actions rejected with HTTP 409 until a human resolves the diff. Closing
  actions and compensating adjustments always pass — you can always get out.

Plus a **resubmission lock**: no new order for the same position intent until the prior one
is confirmed terminal at the broker. The lock is persisted, so a crash mid-cancel cannot
orphan a working broker order invisibly or let a double order through.

**Exit rules**, each owning its definition in one module:

- *Kill switch* — 3-month relative strength vs SPY turning negative on a confirmed close
  means exit within 1–2 days.
- *Circuit breaker* — a 15% drawdown from entry, OR three consecutive closes below the 50-day
  MA, OR a single close below the 200-day, OR the operator's own stored line-in-the-sand;
  whichever trips first.
- *Whipsaw guard* — catches what neither of the above sees: three defensive roll-downs within
  four weeks, or cumulative roll cost past 5% of position capital, recommends **exit** rather
  than another defensive roll.

**A deliberate loosening, for you to judge.** A former exit trigger — relative strength vs the
name's own *sector* ETF — was removed system-wide in August 2026. The stated reasoning: a
cap-weighted sector ETF is dominated by a few mega-caps, so it isn't a genuine peer
comparison. A decision document enumerates the cases the rule used to catch and no longer
does, and the code states outright that this was a loosening of a safety mechanism, not an
oversight. The stated replacement plan is a rules-based industry peer basket, not yet built.

## 6. "Shadow mode" — the most distinctive architectural commitment

Five substantial features are fully built, computed on every scan, displayed in the UI, and
persisted to telemetry stores — and are read by **nothing** in the decision path:

| Feature | What it would decide, if it had authority |
|---|---|
| Composite quality SCORE (0–10) | Rank order among entrable names |
| Weekly-juice floor | Reject names whose premium is too thin |
| Level-4 chart-structure metrics | Distinguish a real coil from mid-range drift near a flattening MA |
| Trailing juice capacity (a rolling median) | Separate transient IV compression from structurally low-volatility instruments |
| Per-gate rejection telemetry | Nothing — pure observability, by design |

The load-bearing invariant: nothing from a shadow module is ever appended to the `blocks`
list that feeds verdict composition — that list is what carries authority. This is **enforced
by test, not convention**: one test pins a fixture whose value sits far below the shadow
floor and asserts the resulting scan row is *byte-identical* to one computed with the feature
disabled; it then greps every gate, verdict and executor module for any import of the store,
and asserts no boolean config constant exists that could act as an authority switch. There is
deliberately no such switch — graduating a shadow metric is a reviewed code change.

The stated rationale: every threshold in these modules is a guess, and shadowing accumulates
the data that would calibrate them before any of it could influence a real trade.

Related convention: **every** constant in the config module is labeled with its provenance —
`HARD_CFM_RULE` (a stated strategy rule; changing it changes the strategy),
`PROPOSED_DEFAULT` (a tunable guess pending calibration), or `TRAVIS_EXTENSION` (explicitly
the operator's own addition, not part of the source methodology).

## 7. Orders, broker truth, and the trust layer

**Design constraint, confirmed by the operator:** all trading on this account goes through
this app. Any divergence between the broker and recorded state is therefore an anomaly —
assignment, expiry, partial fill, corporate action, or a bug — never a legitimate external
trade. There is no adopt-external-trade flow; the response to a diff is freeze, alert, human
resolves.

- **Live order path.** A single-leg action builds a DAY LIMIT order, parks it under
  `pending_orders`, and commits an execution **only on fill**, at the real fill price. An
  unfilled, cancelled order leaves no trace. A roll is one two-leg net-credit/net-debit ticket
  — no legging risk. Order prices are `Decimal`, rounded to the instrument's valid tick,
  never a bare `round(x, 2)`.
- **Order lifecycle** is an explicit state machine over broker statuses, with every transition
  appended to a log from which current order state is derived.
- **Manual recovery path** for an order whose broker outcome isn't confirmed: it can recover a
  missing order id by matching recent orders, and it never auto-retries a submission. UNKNOWN
  stays "confirming"; a rejection carries the broker's verbatim reason.
- **Reconciliation** compares recorded state against actual broker holdings. Pure core over a
  parsed broker view; the fetch wrapper isolates failures so a failed call cannot masquerade
  as an empty account. Classifications include missing-at-broker, unexpected-at-broker,
  quantity mismatch, short-stock-detected (highest severity — an assignment), a benign
  expired-worthless carve-out, and per-field economic divergence (cost basis, extrinsic) added
  later to catch drift that an existence-plus-quantity comparison read as clean forever.
- **Transaction ingestion** pulls the broker's transaction feed as ground truth for fill
  economics, deduped by broker transaction id so re-runs are idempotent. A broker execution
  with no matching app order surfaces as a *proposed* adoption for the operator to accept —
  never auto-booked — and adoptions can be reversed.

**The trust layer.** A recommendation engine commits to an explicit, timestamped claim
*before* the operator acts. Its evaluate function reads nothing but its arguments — no
provider access, no clock read, no state load, no network; every impure input arrives frozen
in a snapshot built by a separate shell. The stated design intent: this is the exact code path
a future automation switch would call, and supervised vs autonomous operation would differ
*only* in what the caller does with the returned records. Today there is no submit path at all.

Emission policy: one dominant action per open position per pass (exits dominate defends,
defends dominate rolls); other fired triggers are preserved as secondary. A pass over a
healthy position emits an explicit ALL_CLEAR — **silence is not a valid output**, which makes
a missing recommendation detectable rather than ambiguous.

Grading: each execution is matched to the latest open recommendation of the same action type
on the same position whose validity window contains the execution instant. A superseded,
overridden or expired recommendation never matches. An execution with *no* match synthesizes
a COVERAGE_MISS — the engine failed to commit before the operator acted — and an open
ALL_CLEAR does not excuse it. Scope is bounded by rule: mechanical rolls, scale-ins, leg
repairs and reconciliation adjustments never synthesize misses, and executions predating the
engine's activation timestamp are excluded.

## 8. Alerts and the operator loop

The operator has a day job, so "exit immediately" is only a real rule if the phone buzzes.

An **in-process daemon thread** runs the scheduler — chosen because the volume attaches to
exactly one machine and the state file is single-writer, so a separate scheduled machine could
never share the data directory. It ticks every 30 seconds and fires each Eastern-time slot
once per day, catching up after a restart rather than skipping.

| Slot (ET) | Why |
|---|---|
| 08:30 | Pre-market — overnight assignments have materialized and the operator can act calmly |
| 09:40, 09:50 | Post-open gap cadence. Open-to-10:00 was a 30-minute blind spot; since the strategy uses alerts rather than resting stops, this cadence *is* the tripwire |
| 10:00, 12:30, 15:30 | Fixed intraday anchors |
| 16:15 | Post-close. The kill switch's confirmed-close rule and an end-of-day circuit-breaker breach are only evaluable after the close; without this slot their earliest fire would be next morning, turning "exit immediately" into "exit at tomorrow's open" |
| 17:30 | Nightly maintenance — earnings/dividend caches, cash-balance sync, rotating backups |

~15 alert conditions, each tagged with rule provenance: kill switch, circuit breaker, delta
coverage, defend, whipsaw, assignment risk, 75%-decay buyback, earnings window, stale earnings
date, expiry Friday, broker-token expiry, stale data, plus scan-transition events. Each fires
*once* when it trips, refreshes while it stays true, and auto-resolves when it clears.
Delivery fans out to Web Push (self-generating VAPID keys persisted to the volume), ntfy, and
email; anything unconfigured falls back to the process log so an alert is never silently
dropped.

**The scheduler's own silence is monitored.** An in-process thread that wedges produces no
alert and says nothing. An optional external dead-man's-switch URL is pinged every tick; miss
enough pings and that service pages the operator. Inert when unset.

## 9. Data layer

The universe is ~500 names; fetching all of them continuously isn't affordable, so symbols
are tiered. Tier assignment is driven entirely by data — an open position lands in Tier 0, an
on-deck entry candidate in Tier 1, a name passing the hard gates in Tier 2, everything else
Tier 3. No symbol is hardcoded.

- **Escalation.** A per-symbol defense escalation (price crossing a defense level) or a global
  market escalation (index or held sector ETF moving >1% intraday) promotes freshness to a
  30-second cadence, decaying after an hour without re-trigger.
- **Budget shedding.** Every provider call is counted by day/provider/tier/kind. As a provider
  approaches its daily limit, cheap tiers shed first — Tier 3, then Tier 2, then Tier 1
  cadence — and **never Tier 0**. Open-position monitoring is never sacrificed.
- **Staleness blocks GO.** A hard rule: a GO verdict the operator would act on is not emitted
  on stale inputs. *Unknown* freshness reads as stale — it never permits action. Blocked names
  are surfaced in their own bucket, never silently dropped.
- **Scan caching.** The full universe sweep is persisted once per *trading day*, keyed to roll
  just after the close, so a restart re-reads the last sweep instead of handing the next
  visitor a cold 500-name computation on the request path. A universe edit re-scans only the
  names that changed.

One subtlety: the scan's juice estimate never touches an option chain — it prices a
Black-Scholes weekly short at trailing 20-day *realized* volatility off the cached daily
frame. That is why a historical backfill of the capacity metric can be an exact
reconstruction rather than an IV proxy, and why no historical-chain data provider is needed.

## 10. Operational posture

- **Auth.** A single shared password gates every API route. A werkzeug password *hash* is the
  supported production config; a signed, HttpOnly, Secure, SameSite=Lax cookie lasts 30 days.
  The cookie-signing key self-generates to a 0600 file on the volume so logins survive deploys.
  Failed logins sleep 0.75s to throttle brute force. **When no password is configured the gate
  is disabled entirely** (documented, intentional, for local development).
- **Live trading is off by default** behind an environment flag plus a persisted runtime
  toggle. Without it, executions are captured against live prices and logged, but nothing is
  transmitted to the broker — described in the code as "the honest paper path".
- **Broker credentials.** The refresh token expires every 7 days and requires a browser login;
  an alert fires at day 5.
- **Destructive ops.** A "reset book" endpoint requires an environment flag *and* login *and*
  a typed confirmation string. Off by default.
- **Emergency path.** A written procedure for exiting at the broker directly when the token
  has lapsed or the API is down, with reconciliation adopting the trade afterwards via a
  compensating adjustment.
- **Demo mode.** A fully separate synthetic store and price cache, so every feature is
  exercisable with no provider keys and no risk to the live record.

## 11. Measured verification

I ran the suite in a clean virtualenv on Python 3.11 with no credentials and no network access
to either provider:

- With dependencies installed per the requirements file **minus** the optional web-push
  package: **1,338 passed, 2 failed in 76.26 seconds.** Both failures were in the web-push
  test module and both stemmed from a missing optional sub-dependency (`cryptography`), not
  from a code defect.
- After installing that one package, the web-push module ran **12 passed in 0.11s**, making
  the full suite **1,340 / 1,340 green**.
- The suite passed on **pandas 3.0.5 and numpy 2.4.6**, far beyond the `pandas>=2.0` /
  `numpy>=1.24` floors declared in the requirements file.

What the suite covers: indicator formulas against pinned regression fixtures; every pure
decision core; the execute → ledger → payback flow end to end; migrations; reconciliation;
transaction ingestion; atomic rolls; kill switch; write durability; and the shadow-mode
invariants. A scriptable mock broker client replays every endpoint; no live broker call is
ever made.

What it does not cover: **zero frontend tests** (~10,000 lines of React, no test runner
configured); no broker integration test; and no CI verification (see below).

## 12. Findings I have already identified

Do not simply re-derive these. **Challenge them** — tell me where I've mis-weighted severity,
where I'm wrong, and what I missed entirely.

1. **The primary docs describe the retired strategy.** The README (338 lines) and the overview
   doc (462 lines) both present the LEAP diagonal as live — entry gate, API table, architecture
   diagram, delta-coverage guardrail. Neither mentions the shares-primary migration once. It is
   recorded only in the agent-instructions file and the changelog.
2. **Nothing verifies the suite before deploy.** The CI workflow deploys to production on every
   push to the default branch. There is no test step in any workflow — I grepped both. 1,340
   tests that run in 76 seconds are not being run.
3. **A clean `pip install -r requirements.txt` fails.** The web-push package pulls a
   sub-dependency needing a C toolchain; in a clean container the build fails and, because pip
   installs atomically, takes the entire install down with it including pandas. A session hook
   works around it with a fallback; a human following the README hits the wall. Related: the
   two web-push tests *fail* rather than *skip* when the optional package is absent.
4. **Single machine, single volume, single point of failure.** Deliberate and argued — the
   single-writer constraint is real and the alternatives are worse for a JSON store — but if the
   machine is down no alerts fire, and if the volume is lost everything since the last
   off-machine backup is gone.
5. **279 broad `except Exception` handlers in non-test backend code.** Most are deliberate and
   commented (best-effort telemetry that must never block an execution, independent section
   failure in the landing payload). Defensible in an operator tool where a degraded read beats
   a blank screen, but a large surface on which a genuine bug reads as graceful degradation.
6. **Aggregate surface area.** 90 modules, 98 routes in one file, a 4,341-line executor, 21
   schema versions, five telemetry stores outside the main state file, five features in shadow
   mode awaiting calibration. Each individually justified, often with a written audit document.
   The question is whether one person can still hold it, and whether the shadow backlog is
   converging or accumulating.
7. **Auth fails open when unconfigured.** Documented and intentional for local dev, but a
   fail-open default on an app that can transmit real orders. Mitigated by live trading being
   separately gated.
8. **No linter, no formatter, no frontend tests.** No ruff/flake8/black/eslint config anywhere;
   the agent-instructions file says so explicitly and tells contributors to match surrounding
   style. Style consistency is in fact good, but nothing mechanically catches an unused import
   or a dead branch.

---

## What I want from you

Work through these in order. Be specific and concrete; cite the section number you're
reasoning from. Where a judgement depends on something I haven't told you, say what you'd need
and why it changes the answer.

1. **Challenge my findings.** Which of the eight are mis-severitised in either direction? Which
   are wrong? What did I miss?

2. **Attack the architecture.** The central bet is "append-only log + full recomputation on
   every write, in a single JSON file, single writer, single machine." Where does that bet
   break? Consider: file growth over years of executions, recompute cost as the log grows,
   partial-failure modes the atomic-write contract doesn't cover, and the recovery story if the
   volume is lost between nightly backups.

3. **Attack the safety model.** This system can transmit real orders. Walk the paths an
   incorrect or duplicate order could still reach the broker despite the freeze, the
   resubmission lock, the execution window, and the spread gate. Where is the weakest link —
   and is it in the code, in the broker interaction, or in the operator's own loop?

4. **Judge the shadow-mode discipline.** Is deferring five built features from the decision path
   pending calibration genuine engineering rigour, or a mechanism for shipping without ever
   having to defend a threshold? What would convince you either way? What is the concrete cost
   of holding them in this state for another year?

5. **Judge the correctness risk in the money math.** The income figure is derived, not stored:
   per-close economics are re-derived from stored facts on every recompute so a stale stored
   figure can't leak in; the ×100 contract multiplier lives in exactly one module per side; and
   date-to-expiration bucketing is centralised so two views can't disagree about which week a
   fill landed in. Given only this, where would you expect a money bug to actually live?

6. **The human single point of failure.** Every guardrail terminates in a push notification and
   a human tap; there is no automated exit, by design. The trust layer exists explicitly so a
   future automation switch could reuse the same code path. Is the current supervised design
   the right call for this operator, and what would have to be true before flipping that switch
   would be responsible?

7. **Rank the work.** Give me an ordered list of what to fix, with an honest cost estimate for
   each and an explicit statement of what risk each one buys down. Then tell me which items I
   should decline to do, and why.

8. **The verdict.** In one paragraph: is this a well-engineered system carrying real money, an
   over-engineered hobby project, or something else? Say which, and defend it.
