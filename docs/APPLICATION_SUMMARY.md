# CFM Dashboard — Application Summary

A single, self-contained tour of the Cash Flow Machine (CFM) dashboard: what it
is, how it's built, what's tested, and — the reason this doc exists — a frank
inventory of **holes and areas to improve**. It's written to be read cold by
someone reviewing the system for gaps, so the last section is the point; the
rest is the context that makes it legible.

Snapshot at time of writing: version `2.1.0`, ~13k lines of backend Python
(30 modules), 24 React components, 25 backend test files (~330 test functions,
**0 frontend tests**), state schema **v9**.

---

## 1. What it is

A focused, single-strategy trading dashboard for a **poor-man's covered call
(PMCC / diagonal)**:

- Buy a **deep-ITM LEAP call** (~0.90 delta, ~180 DTE) in a strong, consolidating
  stock — this is the deployed capital.
- Sell **weekly ITM short calls** against it (strike ≈ stock − 1.5×ATR), roll
  weekly, collect "juice" (extrinsic).
- Track extrinsic payback until each position is "in profit mode," manage the
  diagonal's delta coverage, and exit on a binary kill-switch signal.

The operator works a day job, so the app's job is to **compute the mechanical
signals, notify when a rule trips, and prepare the order tickets** — it
recommends and stages; the human transmits. It is explicitly *not* an autopilot
and *not* financial advice; GO/WAIT verdicts are checklist outputs.

Two hard operating principles shape everything:

1. **`state.json` is the single source of truth.** Execution records are
   immutable and append-only; every ledger, meter, and summary is *derived* from
   them and never hand-maintained. Exactly one writer (one Fly.io machine, one
   persistent volume at `/data`).
2. **Paper by default.** `CFM_LIVE_TRADING=1` is the only switch that lets an
   order reach the broker; without it, executions are captured against live
   prices and logged but nothing is transmitted (the honest paper path).

---

## 2. The strategy, as encoded

The system is a pipeline: **scan → gate → execute → track**.

**Entry gate (5 levels, stop on first fail):**

1. **Market regime green** — SPY breadth positive, VIX calm. (`screening.regime`)
2. **Sector strong** — RS3M vs SPY > +10%, breadth > 60%, ATR expanding.
3. **Stock beats peers** — RS3M vs SPY > +5%, RS3M vs Sector > 0%.
4. **Consolidating, not breaking** — low ATR%, price near MA21.
5. **Account & Juice** (`account_gate.py`) — is the *account* ready and does the
   *trade* pay? Blocking checks: cash reserve (2×ATR defensive reserve across the
   book), position limit (≤2), capital cap (~$38K), sector concentration (≤1 per
   sector), juice adequacy (weekly extrinsic ÷ LEAP cost ≥ ~1.88%/wk). Warnings:
   juice-rich (event pricing), earnings-in-cycle. Enforced server-side in
   `executor.execute`; a blocking failure rejects the entry (HTTP 400) unless a
   typed `override_reason` is logged onto the immutable record.

Levels 1–4 are "right stock, right tape"; Level 5 is "right account, worth it."

**Weekly management:**

- **Strike selection** is a regime × posture table (`strike_policy.py`,
  `config.STRIKE_TABLE`): market regime (green/yellow/red) × operator posture
  (aggressive/conservative) → an (ATR multiplier, minimum ITM% floor) pair. The
  strike used is whichever candidate sits further below spot (max protection
  wins). Posture is an operator-editable, per-store, persisted setting.
- **75% buyback rule** — a short decayed ≥75% with >2 DTE shows ROLL NOW and
  fires an alert; roll early to capture the juice.
- **Defend / roll-down** — when the underlying closes below a short strike, the
  app recommends a defensive roll (new strike from the regime table), estimates
  net credit/debit, and stages it.
- **Rolls** are recorded as paired executions sharing a `roll_id` + `roll_reason`
  (scheduled | 75%-rule | defend | earnings | kill-switch-exit), feeding a
  derived roll-cost / whipsaw ledger. In live mode a roll is **one atomic
  two-leg NET_CREDIT/NET_DEBIT ticket** — no legging risk.

**Coverage guardrail (the diagonal's core discipline):** the LEAP delta must
hold a **0.50 floor** (below it, it stops behaving like a stock proxy — roll
deeper ITM), and the long's total delta must stay **≥ the short's** (else the
position is effectively uncovered on an up-move). Greeks are **recomputed**
(skew-aware, dividend-adjusted, Black-Scholes-Merton) because Schwab's own chain
greeks are unreliable.

**LEAP lifecycle (the long leg's discipline):** roll the LEAP when DTE < 90 or
extrinsic runway < 4 weeks; a net-maintenance number (trailing juice − weekly
LEAP burn) flags positions bleeding faster than they earn; a delta-velocity
early warning fires before the 0.50 floor. Payback continuity is preserved
across a LEAP roll (linked `leap_roll_id`) but resets on a true exit + re-entry.

**Kill switch (binary exit):** RS3M vs Sector negative → exit immediately; RS3M
vs SPY negative on a confirmed close → exit within 1–2 days.

**Exit & learning loop:** each closed buy_leap→close_leap window produces an
immutable **closed-cycle record** (dates, days held, capital, gross juice, roll
net/drag, LEAP P&L, return % vs the 15–25% target, exit reason, and the
scorecard snapshot frozen at entry). The History tab aggregates these; a
**calibration harness** (`calibration.py`) replays the scorecard over cached
history against forward 4-/8-week returns to upgrade tunable thresholds from
guess to measured.

---

## 3. Architecture

```
  Schwab (primary) ─┐
                    ├─►  data_handler  ──►  parquet cache (DATA_DIR/cache)
  Alpha Vantage  ───┘         │
                              ▼
        indicators (RS3M · ATR · MA · RSI · breadth · BSM greeks)
                              ▼
     screening (regime · sectors · stock filter · 4-level gate) + account_gate (L5)
                              ▼
           Flask API  ◄──►  state.json (single source of truth)
                              ▼
                     React + Tailwind PWA
```

### Backend (`backend/`, Python Flask + Gunicorn)

Thin HTTP controllers over domain modules. ~64 API routes in `app.py`; nearly
every route is a try/except wrapper delegating to a module below.

| Module | Responsibility |
|---|---|
| `app.py` | Flask app, ~64 routes, serves the built frontend, starts the scheduler + a startup corruption check at import. |
| `executor.py` | The biggest module. Executes all CFM actions, captures + auto-logs, drives the live pending→poll→commit/cancel order lifecycle, and applies reconciliation resolutions (append-only, corrected forward). |
| `logging_handler.py` | `state.json` atomic I/O + `recompute_derived` — rebuilds the theta ledger, payback meters, roll ledger, closed cycles, wash-sale flags, and per-position lifecycle fields from the execution log. |
| `reconcile.py` | The last safety layer: verifies `state.json` matches what Schwab actually holds. Detects divergence, freezes the position, alerts — never auto-rewrites. |
| `screening.py` | Regime, sector strength, stock filter, the 4-level gate, the daily checklist; short-TTL memoized; detached background scan. |
| `account_gate.py` | Level 5 (Account & Juice); also resolves live operating cash and suggests circuit breakers. |
| `option_chain.py` | Auto-picks the LEAP + weekly short with live pricing; recomputes greeks; delta-coverage; roll picker. |
| `indicators.py` | RS3M, ATR, MA, RSI, breadth, consolidation, and the BSM greeks / implied-vol (bisection) home. |
| `position_manager.py` | Position enrichment: intrinsic/extrinsic split, 75%/defend/assignment signals, capital + milestones. |
| `alerts.py` + `alert_scheduler.py` | 16-condition alert engine (dedup/resolve/cap) + an in-process daemon scheduler firing at ET slots. |
| `notifier.py` / `webpush.py` / `heartbeat.py` | Pluggable delivery (email · ntfy · web-push · log fallback) + a dead-man's-switch heartbeat. |
| `schwab_api.py` / `alpha_vantage.py` / `data_handler.py` | Providers (Schwab primary, AV fallback) + parquet-cached daily OHLCV with health tracking. |
| `refresh_policy.py` | Tiers the universe: force-refreshes a small "hot" set intraday, long tail rides the pre-open warm-up. |
| `backups.py` / `maintenance.py` / `migrations.py` | Rotating + off-machine backups, nightly data refresh, versioned additive state migrations (with pre-migration snapshots). |
| `config.py` / `metrics/thresholds.py` | Every threshold in one place, each labeled `HARD_CFM_RULE` (strategy) or `PROPOSED_DEFAULT` (tunable). |
| `metrics/scorecard.py` | Numeric CFM-suitability lens → GO/CAUTION/AVOID verdict. |
| `kill_switch.py` / `portfolio_risk.py` / `leap_policy.py` / `strike_policy.py` | Exit signals, book-level greeks, LEAP lifecycle, strike table. |
| `dividends.py` / `earnings.py` / `iv_history.py` / `weeklies.py` / `universe_health.py` | Cached first-class data (dividends, earnings, IV rank, weekly-optionability, dead-ticker vetting). |
| `auth.py` | Single-password gate + signed session cookie. |
| `calibration.py` / `history.py` / `fill_verify.py` / `version.py` | Threshold calibration, closed-cycle aggregation/export, live-fill verification, build identity. |

### Frontend (`frontend/src/`, React 18 + Vite + Tailwind)

Single-page tabbed dashboard — no router, no state library, no TypeScript. Eight
tabs: **Overview** (landing / action items), **Scan**, **Execute**, **Theta**,
**Kill Switch**, **Positions**, **History**, **Checklist**.

- **Service layer:** one `api.js` fetch wrapper (~55 endpoints, session cookie,
  60s timeout, 401→login event); `useApi` hook (poll + backoff retry, but only on
  transient errors); `orderFlow.submitOrder` (single funnel, one moving toast);
  `tradeMode.jsx` (paper/live badge + live-order confirm gate, safety-biased —
  an unresolved state confirms as live).
- **Coordination:** nonce-based remounts (`execNonce` after an execution,
  `scanNonce` after a background scan) and `window` CustomEvents (`auth-required`,
  `cfm-action` for alert deep-links) instead of a shared store.
- **PWA:** installable to an Android home screen; a deliberately minimal service
  worker (no offline caching — stale auth-gated data would mislead) that handles
  Web Push and deep-link notification clicks.
- **Heaviest components:** `OptionChainModal` (chain viewer + auto-detecting
  order ticket), `PositionTracker` (the management surface with lazily-fetched
  per-position sub-panels), `DataHealth` (ops dashboard: reconcile status, fill
  verify, universe check, candidate vetting).

---

## 4. Operational model & durability

The single-writer invariant is the load-bearing design decision, and the
durability story is genuinely strong:

- **Atomic writes** — serialize to string first, temp file → fsync → `os.replace`
  → fsync the directory. A crash mid-write leaves the old file or the new one,
  never a truncated one.
- **Refuse-to-reinitialize** — a corrupt `state.json` makes the app refuse to
  start rather than silently overwrite a live record.
- **Backups** — nightly rotating local backups (30 kept) + one off-machine copy
  (email attachment or optional S3) + pre-migration snapshots (kept forever). A
  backup failure self-alerts.
- **Reconciliation** — an independent last line that checks state against the
  broker; a divergence freezes the position (blocks new-risk actions with HTTP
  409, always allows closing) and alerts; resolution is a compensating
  append-only `adjustment`, never an edit.
- **Dead-man's-switch** — the scheduler pings an external service every tick, so
  its *silence* (a wedged thread or stopped machine) pages the operator.
- **Deploy** — Fly.io, one machine (`scale count 1`, `--ha=false`), volume at
  `/data`; CI on push to `master` via `.github/workflows/fly.yml`.

---

## 5. Test & quality posture

**Backend: strong where it matters.** ~330 test functions across 25 files, all
offline (mocked providers, rigged synthetic state, migration tests). The
safety-critical core is well covered: `reconcile.py` (35 tests), durability
(`test_durability.py`), the account gate (28), the alert engine (18), the
scorecard (27), LEAP lifecycle (17), position management, strike policy, refresh
policy, portfolio risk. `test_cfm.py` (54 tests) covers indicators + the
execute→ledger flow end to end.

At time of writing, running the suite in a clean environment yields **324 passed,
5 failed** — and all 5 failures are `test_webpush.py`/one dividend test failing
purely because the sandbox can't load the native `cryptography`/`cffi` module,
not because of a code defect. On a normally provisioned machine the suite is
green.

**The gaps (see §6):** `app.py` (the whole HTTP surface), `auth.py` (the security
gate), `schwab_api.py` (OAuth/token/order paths beyond header hygiene), and
`option_chain.py` have thin-to-no *direct* coverage, and there are **zero
frontend tests**.

---

## 6. Holes & areas to improve

Grouped by severity. Each item names the location so it can be triaged directly.
Nothing here is a showstopper — the system is mature and carefully built — but
these are the honest soft spots.

### 6a. Bugs / correctness

- **Alert ID collision (concrete latent bug).** `alerts.py` mints
  `id = f"alert_{len(log_list)+1:04d}"` while the log is capped at 500 entries.
  Once the log rotates, `len+1` can re-mint an ID that a still-active alert
  already holds; `acknowledge` matches by ID and could ack the wrong record. The
  `:04d` format also overflows past 9999. **Fix:** a monotonic counter persisted
  in state (or a UUID), decoupled from log length.
- **Non-atomic Schwab token write.** `schwab_api.py` writes
  `schwab_token.json` with a plain open/write — the *only* non-atomic write in a
  codebase that is otherwise rigorous about atomic saves. A crash mid-write can
  corrupt the token file and force a manual re-auth. **Fix:** route it through
  the same temp→fsync→replace path as `state.json`.
- **`resolve_operating_cash` has a hidden write side-effect.** A function named
  "resolve" (read-shaped, called from `capital_summary` / portfolio risk /
  checklist) persists a fresh live cash read back into `state.json`. It's
  intentional (keeps all readers agreeing) but surprising — a read path mutating
  state is a footgun for future callers. **Fix:** at minimum rename/annotate; ideally
  separate the read from the persist.
- **Kill switch fails open.** If the cached SPY or sector frame is missing, both
  RS numbers go None and the switch reads **green** — the one guardrail whose job
  is to say "get out" goes quiet exactly when data is broken. Reconcile-stale and
  fill-verify by contrast fail *loud*. This fail-open/fail-loud split across the
  codebase is deliberate but uneven and worth a conscious policy decision.

### 6b. Security & secrets

- **Auth gate is open when unconfigured.** `auth.py` disables protection
  entirely if neither `DASHBOARD_PASSWORD_HASH` nor `DASHBOARD_PASSWORD` is set —
  frictionless for local dev, but a production deploy that forgets the secret
  exposes every `/api` route and every trading action with no warning. And
  `auth.py` has **zero test coverage.** **Fix:** fail closed in production (e.g.
  refuse to start without a password when not localhost), and add tests.
- **Plaintext-password fallback in prod.** `DASHBOARD_PASSWORD` (plaintext) is
  accepted anywhere, discouraged only in a docstring.
- **CORS is wide open.** `CORS(app)` with no origin restriction. Low risk behind
  the password gate + same-origin cookie, but there's no reason to allow all
  origins on a single-user app.
- **Self-configuring secrets silently break on a read-only FS.** Both the VAPID
  keypair (`webpush.py`) and the session-signing key (`auth.py`) generate-once,
  persist-0600, fall back to in-memory. If persistence silently fails, a restart
  regenerates them — logging every device/session out (push subscriptions die
  permanently). **Fix:** surface a loud warning/alert when the in-memory fallback
  is hit.
- **Email backups ship full trading history in cleartext.** The nightly
  off-machine copy can attach the entire `state.json` (positions + execution log)
  to an unencrypted SMTP email up to 5 MB. Prefer the S3 path, or encrypt.
- **AV API key travels in the URL query string** (`alpha_vantage.py` uses
  `urllib`), so it can leak into any request log along the path.

### 6c. Single points of failure / ops

- **One machine, one volume, one region (`iad`).** By design (single-writer), but
  it means no automatic failover, and volume loss between nightly off-machine
  copies risks up to ~24h of state. Recovery is a documented manual runbook —
  good — but it *is* manual.
- **Off-machine backup can silently be "none."** If neither SMTP nor S3 is
  configured, the nightly job just logs that no copy left the machine. Easy to
  run for months with backups that never leave the single volume. **Fix:** treat
  "no off-machine target configured" as a warned/alerted state, not a quiet log.
- **Schwab refresh token dies every 7 days with no programmatic renewal** and
  requires a fresh browser login. This is a recurring manual chore and a single
  point of trading-connectivity failure; the OAuth/refresh path is essentially
  untested. The `TOKEN_EXPIRY` alert exists precisely because of this — but the
  chore itself can't be automated against Schwab's current API.
- **Scheduler is a single in-process daemon thread.** Correct given the
  single-writer constraint, but any wedge stops all alerts; the heartbeat
  dead-man switch is the only backstop, and it's optional (inert unless
  `HEALTHCHECK_URL` is set). **Recommend** making the heartbeat effectively
  mandatory in production.
- **Holidays aren't modelled** — a market-holiday scheduler run evaluates
  unchanged state and fires nothing (benign, documented, but means a holiday can
  mask a "should have noticed" edge case).

### 6d. Testing gaps

- **Zero frontend tests.** 24 components including two ~600-line management
  surfaces (`OptionChainModal`, `PositionTracker`) that build the exact order
  payloads sent to the broker — untested. `buildPayload`'s per-action price
  encodings (LEAP per-contract×100 vs short per-share) are the highest-value
  thing to unit-test; a bug there mis-prices a real order. **Recommend** at least
  Vitest coverage of `buildPayload`, `describeOrder`, and `orderFlow`.
- **The HTTP edge is untested.** No `test_app.py`; routing, error handling, and
  most of the ~64 endpoints are only exercised incidentally. Combined with the
  untested `auth.py`, the two most externally-exposed surfaces have the least
  coverage.
- **`schwab_api.py` order/token paths untested** beyond header hygiene — the code
  that talks to real money is thin on tests (understandably hard to mock, but
  worth investing in).

### 6e. Performance / scale (slow-burning, not urgent)

- **`recompute_derived` is O(executions) and runs on every append/save.** Every
  execution reloads and rewrites the full state and re-derives every ledger. Fine
  today; it will slow measurably as the execution log grows over years. **Fix
  eventually:** incremental derivation or a periodic compaction/snapshot of
  derived state.
- **Heavy `load_state`/`save_state` churn per execution.** Many executor
  functions reload state several times within a single action. Correct under the
  lock, just not cheap.
- **Positions tab request burst.** Several sub-panels (coverage, defend, LEAP
  estimate) fire independent per-position fetches; with several open positions
  that's a burst of parallel requests on tab open. Each degrades gracefully, but
  a batched endpoint would be tidier.
- **Unbounded in-memory dicts** — `data_handler._symbol_locks` and the weeklies
  thread pool grow/live for the process lifetime. Negligible for a long-lived
  single process, but noted.

### 6f. Dead code & loose ends

- **Latent live-order path.** `orderFlow.js` polling + `api.orderStatus/cancelOrder`
  and the whole `"working"` branch are documented as wired only when live
  placement is enabled; in the current paper-first deployment the backend returns
  `"filled"`, so this path is effectively unexercised. The **3-second
  auto-cancel** is also aggressive for a real resting limit order and has a
  fill-vs-cancel race — revisit before going live.
- **Dead frontend exports:** `api.ivRank`, `api.state`, `api.saveState`, and
  `api.earnings` have no call sites; `pct` is imported but unused in
  `Overview.jsx`.
- **`DailyChecklist` "done" checkboxes are local-only** and reset on refresh — by
  design ("read-only computed status"), but easy to mistake for a bug.
- **Legacy config constants** (`LEAP_ROLL_DTE`, `SHORT_ATR_MULT`) remain in
  `config.py` after being superseded by the strike table / lifecycle floors.

### 6g. Dependency & build hygiene

- **Loose dependency pinning.** Backend deps are lower-bound only (`flask>=3.0`,
  etc.) with no lockfile/hashes — non-reproducible builds and supply-chain drift.
  `gunicorn` is installed only in the Dockerfile, not `requirements.txt`, so
  local `python app.py` and the container diverge. **Fix:** pin exact versions /
  add a lockfile; add gunicorn to requirements.
- **Single-worker assumption is implicit.** The whole single-writer safety model
  depends on one Gunicorn worker (the Dockerfile uses `--threads 8`, implicitly
  one worker). Adding `--workers >1` — an innocent-looking perf tweak — would
  silently break the in-process lock. Worth a loud comment at the CMD and,
  ideally, a runtime assertion.

---

## 7. What's genuinely strong (so a review doesn't "fix" it)

- The **derived-not-stored** discipline — every ledger rebuilt from an immutable
  log — makes the whole system auditable and self-healing.
- The **durability layer** (atomic writes, refuse-to-reinit, layered backups,
  pre-migration snapshots, a documented recovery runbook) is better than most
  production systems this size.
- **Reconciliation** as an independent last line, with a freeze that blocks
  new risk but never traps an exit, is a thoughtful safety design.
- **Config provenance** (`HARD_CFM_RULE` vs `PROPOSED_DEFAULT`) plus the
  offline calibration harness means the strategy's knobs are honest about which
  are rules and which are guesses.
- **Graceful degradation** — price math returns `None` offline instead of
  raising, demo mode has full parity, a failed broker fetch never masquerades as
  an empty account.

The short version for a reviewer: the **safety-critical core is solid and well
tested; the soft spots are at the edges** — the HTTP/auth surface, the frontend,
the not-yet-exercised live-order path, and a handful of concrete small bugs
(alert-ID collision, non-atomic token write, fail-open kill switch).
