# Emergency exit runbook — "Schwab is down and the kill switch just fired"

Every trade in this system goes through the app by design. But the app trades
through **one broker API** behind a **7-day refresh token**, and an "exit
immediately" event (kill switch, circuit breaker, whipsaw exit) does not wait
for the plumbing to be healthy. This page is the procedure for the case the
machinery can't act for you. **Derive it now — an emergency is the wrong time.**

CFM deliberately uses **alerts over resting stop orders** (an alert you act on,
not a stop the market can run). The tradeoff is explicit: the exit depends on
*you* acting. When the app can't place the order, you act at the broker directly
and let reconciliation adopt the trade afterward. That path already exists in the
machinery (see [`reconciliation.md`](reconciliation.md)); this is the written
procedure for it.

---

## When this applies

You have an exit-now signal (a CRITICAL `KILL_SWITCH_*`, `CIRCUIT_BREAKER`, or
`WHIPSAW_EXIT` alert, or your own read of the position) **and** the app cannot
transmit the order. Common causes:

- **Schwab's API is down** — order placement 5xx/times out, `/api/config` shows
  Schwab unhealthy, live orders won't leave `working`.
- **The refresh token lapsed** — Schwab tokens die at ~7 days and need a fresh
  browser login; `TOKEN_EXPIRY` warns at day 5. If it lapsed, market data goes
  dark and orders can't authenticate.
- **The app/machine itself is unreachable** — deploy in progress, Fly machine
  stopped, network partition. (The dead-man's switch should already be paging
  you; see the README.)

The account is still yours regardless of the app's health. **Trade it directly.**

---

## The procedure

### 1. Exit at Schwab directly

Log into Schwab (web, mobile, or thinkorswim) and place the exit yourself.

- **Close both legs of the diagonal.** A CFM position is a short weekly call over
  a deep-ITM LEAP. Buy-to-close the short **and** sell-to-close the LEAP.
- **Never leave a naked short.** If you can't close both on one ticket, close the
  **short first**, then the LEAP — never the other way around.
- **Never exercise the LEAP to cover.** It is a deep-ITM call carrying real time
  value; exercising throws that extrinsic away. Sell it to close. (This is the
  same rule as the short-stock playbook in `reconciliation.md`.)
- If the signal is a single short you're rolling defensively (not a full exit),
  buy-to-close the short and sell the replacement week per your defend strike.

**Write down every fill**: leg, strike, expiration, quantity, price, and the
timestamp. You need these to reconcile.

### 2. Restore the connection when you can

- **Token lapsed:** re-authorize — Schwab card → **Reconnect** — as soon as
  Schwab's login is reachable. This revives market data and reconciliation.
- **API outage:** nothing to do but wait for Schwab; you've already exited, so
  you're flat and out of risk while it's down.

### 3. Let reconciliation adopt the trade

Once Schwab is connected again, reconciliation (pre-market slot, nightly, or
Checklist → Data health → **Reconcile now** / `POST /api/reconcile`) compares
`state.json` against the broker. Your manual exit shows up as a divergence:

- the closed short/LEAP → `MISSING_AT_BROKER`,
- an assigned leg → `SHORT_STOCK_DETECTED` or missing shares.

The affected position is **frozen** (`NEEDS REVIEW`) and `RECONCILE_DIRTY` fires.
This is expected — it's the app noticing the trade you made by hand.

### 4. Commit the truth forward with a compensating adjustment

Open the frozen position → **Reconciliation review**, and for each diff record a
compensating **`adjustment`** (leg EQUITY/OPTION + strike, signed `qty Δ`, and a
**typed reason** — e.g. *"manual exit at Schwab during API outage 2026-07-06,
short 132 BTC @0.05, LEAP 90 STC @41.10"*). Include the price so the P&L folds
into the derived ledgers.

The adjustment is an **append-only execution** — history is never edited, only
corrected forward — that links to the diff (`linked_diff_id`), clears it, and
lifts the freeze once the position's diffs are all resolved. A worthless weekly
expiry uses the one-click **Book expiry at $0.00** path instead. See
[`reconciliation.md`](reconciliation.md) § *Resolving a diff* for the field
details.

After the adjustments, the position reads flat (or reshaped), the ledgers reflect
the real fills, and the freeze is gone.

---

## Prevention (so you rarely reach step 1)

- **Keep the token fresh.** Re-auth on the `TOKEN_EXPIRY` alert (day 5), not at
  day 7 in the middle of an event. A lapsed token is the most common way the app
  goes dark right when you need it.
- **Know the exit shape before you need it.** The Positions tab shows each leg,
  strike, and quantity — that's exactly what you'll type into Schwab under
  pressure. Glance at it when an amber signal (kill-switch yellow, delta velocity)
  first appears, not when it goes red.
- **Have your Schwab login handy on the phone.** The whole point of the direct
  path is that it doesn't depend on this app.
- **Watch the dead-man's switch.** If the scheduler goes silent (machine stopped,
  thread wedged), `HEALTHCHECK_URL` pages you — that silence can be the first
  sign the app can't act for you (README → *Dead-man's switch*).

---

## Related runbooks

- [`reconciliation.md`](reconciliation.md) — the state-vs-broker check and the
  compensating-adjustment mechanics this procedure relies on.
- [`recovery.md`](recovery.md) — restoring `state.json` from backup if the store
  itself is the problem.
