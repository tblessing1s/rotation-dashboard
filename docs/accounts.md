# Multiple accounts — one dashboard, several books

One Schwab login usually reaches several brokerage accounts (a taxable account,
an IRA, a spouse's account). The dashboard runs **one book per account**: each
has its own `state.json`, its own positions, execution log, ledgers, alerts and
orders, and can be bound to one of the brokerage accounts your login reaches.

The registry is `backend/accounts.py`; the store is `DATA_DIR/accounts.json`.

## Why one file per account, not one file with an account column

The execution log is append-only and every derived number — positions, the theta
ledger, extrinsic payback, the put ledger — is recomputed from it wholesale by
`logging_handler.recompute_derived()`. Two books sharing one log would make every
derived figure a blend of accounts that no later migration could unpick. So the
split is at the store, where it is total and unambiguous.

## Layout on the volume

| Account | Live store | Demo store | Backups |
| --- | --- | --- | --- |
| `primary` | `state.json` | `state.demo.json` | `backups/` |
| `<id>` | `state.<id>.json` | `state.demo.<id>.json` | `backups/<id>/` |

The primary account keeps the exact paths the single-account app used, so an
existing deployment **is already a valid one-account registry**: nothing to
migrate, and rolling back loses only `accounts.json`. There is no registry file
at all until you add a second account.

Per-account backup *directories* (rather than a filename prefix) keep rotation
honest: "keep the newest N in this directory" can never age out another book's
copies. Off-machine copies are named `<account>/state-<ts>.json` so a bucket or
inbox keeps them apart.

## Which book a call reads

`config.active_state_path()` delegates to the registry, so **every** state read
and write follows the active account with no per-call-site plumbing. The active
account is resolved in this order:

1. the request/job override — the `X-CFM-Account` header or `?account=` on an
   API call, or `accounts.use(id)` around a scheduler job;
2. the persisted `active` in `accounts.json`.

The override is a contextvar, reset in Flask's teardown: gunicorn reuses threads,
and a leaked selection would hand the next request someone else's book. An
unknown id is refused (404), never silently served from the primary book — the
one exception being the `/api/accounts` endpoints themselves, so a UI holding a
stale id can list accounts and recover.

Because the header travels with each request, two browser tabs can watch two
accounts at once; the persisted choice is what background jobs and a fresh
session read.

## Brokerage binding

An account may name the Schwab **account number** it trades. Orders, previews,
cancels, transaction ingestion, cash reads and reconciliation all resolve through
it (`accounts.broker_hash`, `schwab_api.select_account_node`):

- **Unbound** → the first linked account, exactly as before. Right for a
  single-account install.
- **Bound** → that account's hash, and *only* that one. A binding whose number
  isn't linked to the login **raises** rather than falling back — routing an
  order to the wrong account is the failure the binding exists to prevent.

Reconciliation reads the same node: a login's `/accounts` response carries every
linked account, and comparing one book against the union would report every
sibling account's position as an unexpected broker holding.

## What runs for every account

The scheduler (`alert_scheduler`) fans out over every non-archived account —
alerts, recommendation passes, pre-market and interval reconciliation,
transaction ingestion, the mandatory expiry-day put check, hot/tier refreshes,
nightly maintenance and backups — each inside `accounts.use(...)`. Cadence gates
stay global ("every N minutes, reconcile the books"), while registries keyed per
position are per account, since two books can hold the same ticker and strike.

Market-wide work (the daily full-universe scan sweep) stays a single pass; it is
the same universe whichever book is looking at it.

Alerts are qualified with the account label once more than one book exists
(`[CFM HIGH · IRA] …`), and their deep links carry `&account=<id>` so a tapped
push opens the book the alert is about. Push devices are shared: a phone is
registered once, and delivery reads the union across books.

## Lifecycle

- **Add** — Settings → Accounts. The state file is created lazily on first use,
  so a mistyped account leaves nothing behind.
- **Archive** — retires a book from the switcher and the scheduler while keeping
  its history. The primary account cannot be archived.
- **Remove** — refused while the book holds executions unless you confirm the
  purge, and even then the state file is **renamed aside**
  (`state.<id>.json.deleted-<ts>`), never unlinked. The primary account cannot be
  removed.

## API

| Route | What it does |
| --- | --- |
| `GET /api/accounts` | the registry + this request's binding |
| `POST /api/accounts` | register a book |
| `PATCH /api/accounts/<id>` | rename, (re)bind, archive |
| `DELETE /api/accounts/<id>[?purge=1]` | remove |
| `POST /api/accounts/active` | persist the operator's choice |
| `GET /api/accounts/summary` | every book on one screen |
| `GET /api/accounts/broker-accounts` | the brokerage accounts this login reaches |

`/api/accounts/summary` is read straight off the state files — no provider calls
— so the "All accounts" monitor on Overview stays one cheap request however many
books there are.
