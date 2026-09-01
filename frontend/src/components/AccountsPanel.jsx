import React from "react";
import { api } from "../api.js";
import { Card, ErrorState, Loading, money } from "./ui.jsx";

// Settings → Accounts. Add a book, point it at one of the brokerage accounts
// this Schwab login can reach, rename it, archive it, and (rarely) remove it.
//
// The binding is the load-bearing field: it decides which brokerage account this
// book's orders, transactions, cash and reconciliation use. An unbound book
// keeps the historical behaviour — the first linked account — which is right for
// a single-account install and wrong the moment there are two, so a second
// account without a binding is called out below.

// Account-number control. A picker alone is a dead end when Schwab's enumeration
// comes back short — the operator can SEE the account in Schwab and still have no
// way to bind it here. So this is a typed field with the enumerated numbers as
// suggestions: pick one when the list has it, type one when it doesn't.
//
// Typing a number Schwab doesn't return is allowed but flagged: the order path
// resolves the number through /accounts/accountNumbers and refuses when it isn't
// there (accounts.broker_hash), so a wrong number fails loudly at the ticket
// rather than routing a trade to the wrong account.
function AccountNumberField({ value, onChange, brokerAccounts, listId, ownId }) {
  const known = (brokerAccounts || []).some((b) => b.account_number === value);
  const takenBy = (brokerAccounts || []).find(
    (b) => b.account_number === value && b.bound_to && b.bound_to !== ownId)?.bound_to;
  return (
    <label className="text-xs text-slate-400">
      Schwab account
      <input
        value={value}
        onChange={(e) => onChange(e.target.value.trim())}
        list={listId}
        placeholder="Leave blank to use the first account on this login"
        className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm text-slate-100"
      />
      <datalist id={listId}>
        {(brokerAccounts || []).map((b) => (
          <option key={b.account_number} value={b.account_number}>
            {b.masked}{b.bound_to ? ` — used by ${b.bound_to}` : ""}
          </option>
        ))}
      </datalist>
      {value && !known && (
        <span className="mt-1 block text-amber-300">
          Schwab didn't list this number for your login. You can save it, but orders
          from this book will be refused until Schwab returns it — reconnect and tick
          the account on the consent screen.
        </span>
      )}
      {takenBy && (
        <span className="mt-1 block text-amber-300">Already used by “{takenBy}”.</span>
      )}
    </label>
  );
}

function AccountRow({ account, summary, brokerAccounts, isActive, onPatch, onDelete, onSelect }) {
  const [editing, setEditing] = React.useState(false);
  const [label, setLabel] = React.useState(account.label);
  const [number, setNumber] = React.useState(account.broker_account_number || "");
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const [confirmDelete, setConfirmDelete] = React.useState(false);

  const isPrimary = account.id === "primary";

  async function save() {
    setBusy(true); setErr(null);
    try {
      await onPatch(account.id, { label, broker_account_number: number });
      setEditing(false);
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  }

  async function patch(body) {
    setBusy(true); setErr(null);
    try { await onPatch(account.id, body); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  }

  async function remove(purge) {
    setBusy(true); setErr(null);
    try { await onDelete(account.id, purge); }
    catch (e) { setErr(e.message); setConfirmDelete(false); } finally { setBusy(false); }
  }

  const unbound = !account.broker_account_number;

  return (
    <div className="py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-slate-200">{account.label}</span>
            {isActive && (
              <span className="rounded-full bg-sky-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase text-sky-300">
                active
              </span>
            )}
            {account.archived && (
              <span className="rounded-full bg-slate-700/60 px-2 py-0.5 text-[10px] font-semibold uppercase text-slate-400">
                archived
              </span>
            )}
          </div>
          <div className="mt-0.5 text-xs text-slate-500">
            {account.broker_account_number
              ? `Schwab account …${String(account.broker_account_number).slice(-4)}`
              : "No brokerage account linked — orders use the first account on this login."}
            {summary && (
              <> · {summary.open_positions} open · {money(summary.capital_deployed)} deployed</>
            )}
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {!account.archived && !isActive && (
            <button onClick={() => onSelect(account.id)} disabled={busy}
                    className="rounded-full border border-sky-500/40 bg-sky-500/10 px-2.5 py-1 text-xs font-semibold text-sky-300 hover:bg-sky-500/20 disabled:opacity-50">
              Switch to
            </button>
          )}
          <button onClick={() => setEditing((v) => !v)} disabled={busy}
                  className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50">
            {editing ? "Cancel" : "Edit"}
          </button>
          {!isPrimary && (
            <button onClick={() => patch({ archived: !account.archived })} disabled={busy}
                    className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50">
              {account.archived ? "Unarchive" : "Archive"}
            </button>
          )}
          {!isPrimary && (
            <button onClick={() => setConfirmDelete(true)} disabled={busy}
                    className="rounded-full border border-rose-500/40 bg-rose-500/10 px-2.5 py-1 text-xs font-semibold text-rose-300 hover:bg-rose-500/20 disabled:opacity-50">
              Remove
            </button>
          )}
        </div>
      </div>

      {!isPrimary && unbound && !account.archived && (
        <p className="mt-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-xs text-amber-200">
          This book has no brokerage account linked, so its orders would go to the
          first account on your Schwab login — the same one as your main book. Link
          it below before trading from it.
        </p>
      )}

      {editing && (
        <div className="mt-3 grid gap-2 rounded-lg border border-slate-800 bg-slate-900/40 p-3 sm:grid-cols-2">
          <label className="text-xs text-slate-400">
            Name
            <input value={label} onChange={(e) => setLabel(e.target.value)}
                   className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm text-slate-100" />
          </label>
          <AccountNumberField value={number} onChange={setNumber}
                              brokerAccounts={brokerAccounts}
                              listId={`broker-accounts-${account.id}`} ownId={account.id} />
          <div className="sm:col-span-2">
            <button onClick={save} disabled={busy}
                    className="rounded-full border border-emerald-500/40 bg-emerald-500/10 px-3 py-1.5 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-50">
              {busy ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      )}

      {confirmDelete && (
        <div className="mt-3 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
          <p>
            Remove <span className="font-semibold">{account.label}</span>? Its book is
            kept on the volume either way — a removal with trades sets the state file
            aside (never deletes it), so the record survives.
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button onClick={() => remove(false)} disabled={busy}
                    className="rounded-full border border-slate-600 bg-slate-800 px-2.5 py-1 font-semibold text-slate-200 disabled:opacity-50">
              Remove if empty
            </button>
            <button onClick={() => remove(true)} disabled={busy}
                    className="rounded-full border border-rose-500/50 bg-rose-500/20 px-2.5 py-1 font-semibold text-rose-200 disabled:opacity-50">
              Remove and set the book aside
            </button>
            <button onClick={() => setConfirmDelete(false)} disabled={busy}
                    className="rounded-full border border-slate-700 px-2.5 py-1 font-semibold text-slate-400 disabled:opacity-50">
              Cancel
            </button>
          </div>
        </div>
      )}

      {err && <p className="mt-2 text-xs text-rose-300">{err}</p>}
    </div>
  );
}

export default function AccountsPanel({ registry, summary, activeId, onSelect, onChanged }) {
  const [broker, setBroker] = React.useState(null);
  const [brokerBusy, setBrokerBusy] = React.useState(false);
  const [adding, setAdding] = React.useState(false);
  const [label, setLabel] = React.useState("");
  const [number, setNumber] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);

  const [reconnecting, setReconnecting] = React.useState(false);

  // Re-run the Schwab consent flow from HERE, where the missing account is
  // noticed — the account selection happens on Schwab's screen, so a reconnect
  // is the only way to widen what the API can see.
  async function reconnect() {
    setReconnecting(true);
    try {
      const { authorize_url } = await api.schwabAuth();
      window.location.href = authorize_url;
    } catch (e) {
      setErr(e.message);
      setReconnecting(false);
    }
  }

  const loadBroker = React.useCallback(() => {
    setBrokerBusy(true);
    return api.brokerAccounts()
      .then((r) => setBroker(r))
      .catch((e) => setBroker({ accounts: [], count: 0, error: e.message }))
      .finally(() => setBrokerBusy(false));
  }, []);

  React.useEffect(() => { loadBroker(); }, [loadBroker, registry]);
  const brokerAccounts = broker?.accounts || [];

  async function create() {
    setBusy(true); setErr(null);
    try {
      await api.createAccount({ label, broker_account_number: number || null });
      setLabel(""); setNumber(""); setAdding(false);
      await onChanged();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  }

  async function patch(id, body) {
    await api.updateAccount(id, body);
    await onChanged();
  }

  async function remove(id, purge) {
    await api.deleteAccount(id, purge);
    await onChanged();
  }

  if (!registry) return <Card title="Accounts"><Loading /></Card>;

  const rows = registry.accounts || [];
  const summaryById = Object.fromEntries((summary?.accounts || []).map((r) => [r.id, r]));

  return (
    <Card
      title="Accounts"
      right={<span className="text-xs text-slate-500">{rows.length} / {registry.max_accounts}</span>}
    >
      <p className="text-sm text-slate-400">
        Each account is its own book — its own positions, execution log, ledgers and
        alerts — and can be pointed at one of the brokerage accounts your Schwab
        login reaches. Switching accounts switches every tab at once.
      </p>

      {/* What Schwab actually returns for this login. Shown always, not only on
          failure: "the picker is empty" and "Schwab reports one account" are
          different problems, and only this line tells them apart. */}
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
        <span className="text-slate-500">
          {broker
            ? `Schwab lists ${brokerAccounts.length} account${brokerAccounts.length === 1 ? "" : "s"} for this login`
            : "Reading your Schwab accounts…"}
        </span>
        <button onClick={loadBroker} disabled={brokerBusy}
                className="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50">
          {brokerBusy ? "Checking…" : "Recheck"}
        </button>
      </div>
      {broker?.error && (
        <p className="mt-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-xs text-amber-200">
          {broker.error}
        </p>
      )}
      {broker && brokerAccounts.length < 2 && (
        <div className="mt-2 text-xs text-slate-500">
          <p>
            Expecting another account here? Schwab's API returns only the accounts
            your app authorization covers — which is chosen on the consent screen,
            not by what your login can see on schwab.com. Reconnect and tick EVERY
            account you want to trade from here, then Recheck.
          </p>
          <p className="mt-1">
            If the other account sits under a different Schwab login (linked only
            for viewing), this token can't reach it at all — that needs its own
            connection. You can still type its number below; orders are refused
            until Schwab returns it, so a typo can't misroute a trade.
          </p>
          <button onClick={reconnect} disabled={reconnecting}
                  className="mt-2 rounded-full border border-sky-500/40 bg-sky-500/10 px-2.5 py-1 font-semibold text-sky-300 hover:bg-sky-500/20 disabled:opacity-50">
            {reconnecting ? "Opening Schwab…" : "Reconnect Schwab"}
          </button>
        </div>
      )}

      <div className="mt-2 divide-y divide-slate-800">
        {rows.map((a) => (
          <AccountRow key={a.id} account={a} summary={summaryById[a.id]}
                      brokerAccounts={brokerAccounts} isActive={a.id === activeId}
                      onPatch={patch} onDelete={remove} onSelect={onSelect} />
        ))}
      </div>

      {adding ? (
        <div className="mt-3 grid gap-2 rounded-lg border border-slate-800 bg-slate-900/40 p-3 sm:grid-cols-2">
          <label className="text-xs text-slate-400">
            Name
            <input value={label} onChange={(e) => setLabel(e.target.value)}
                   placeholder="e.g. Roth IRA"
                   className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm text-slate-100" />
          </label>
          <AccountNumberField value={number} onChange={setNumber}
                              brokerAccounts={brokerAccounts}
                              listId="broker-accounts-new" ownId={null} />
          <div className="flex gap-2 sm:col-span-2">
            <button onClick={create} disabled={busy || !label.trim()}
                    className="rounded-full border border-emerald-500/40 bg-emerald-500/10 px-3 py-1.5 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-50">
              {busy ? "Adding…" : "Add account"}
            </button>
            <button onClick={() => { setAdding(false); setErr(null); }} disabled={busy}
                    className="rounded-full border border-slate-700 px-3 py-1.5 text-xs font-semibold text-slate-400 disabled:opacity-50">
              Cancel
            </button>
          </div>
          {err && <p className="text-xs text-rose-300 sm:col-span-2">{err}</p>}
        </div>
      ) : (
        <button onClick={() => setAdding(true)}
                className="mt-3 rounded-full border border-slate-700 bg-slate-800/60 px-3 py-1.5 text-xs font-semibold text-slate-300 hover:bg-slate-800">
          + Add account
        </button>
      )}

      <p className="mt-3 text-xs text-slate-600">
        A new account starts as an empty book. Nightly backups, alerts,
        reconciliation and transaction ingestion run for every account.
      </p>
      {err && !adding && <ErrorState error={err} />}
    </Card>
  );
}
