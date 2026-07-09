import React from "react";
import { api } from "../api.js";
import { Card, Stat, Pill, Loading, ErrorState, useApi } from "./ui.jsx";

// Monthly payout tracker: the income-withdrawal view over the juice ledger.
// Net juice per month is derived server-side from the close_short executions;
// this page adds the operator's payout bookkeeping. A month moves through
//   in progress → finalizable → finalized → paid
// where "finalizable" means its last short of the month has closed (no open
// short still expires in it) or the calendar month has ended — the moment the
// payout can be locked in. The PAYOUT_READY push fires at that point.

// Payouts want cents (a monthly income figure), unlike the whole-dollar `money`
// helper used for capital sizing elsewhere.
function cash(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  return Number(n).toLocaleString(undefined, {
    style: "currency", currency: "USD",
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
}

function tone(n) {
  if (n === null || n === undefined) return "text-slate-100";
  return n > 0 ? "text-emerald-300" : n < 0 ? "text-rose-300" : "text-slate-100";
}

const STATUS_PILL = {
  in_progress: { status: "caution", label: "In progress" },
  finalizable: { status: "ready", label: "Ready to finalize" },
  finalized: { status: "wait", label: "Finalized · unpaid" },
  paid: { status: "go", label: "Paid" },
  none: { status: "unknown", label: "No payout" },
};

function StatusPill({ status }) {
  const p = STATUS_PILL[status] || STATUS_PILL.none;
  return <Pill status={p.status}>{p.label}</Pill>;
}

function subline(m) {
  if (m.paid) return `Paid${m.paid_at ? ` · ${m.paid_at.slice(0, 10)}` : ""}`;
  if (m.finalized) return `Finalized${m.finalized_at ? ` · ${m.finalized_at.slice(0, 10)}` : ""} · awaiting payout`;
  if (m.finalizable) return "Last short closed — ready to finalize";
  if (m.status === "in_progress") return `${m.closes || 0} short close${m.closes === 1 ? "" : "s"} so far · still accruing`;
  return "No income this month";
}

// The action buttons for one month, driven by its state. `compact` renders the
// tight variant used inside the history table.
function PayoutActions({ m, busy, onFinalize, onMarkPaid, onUnfinalize, onUnmarkPaid, compact = false }) {
  const b = busy === m.month;
  const primary = compact
    ? "rounded-md border border-emerald-500/50 bg-emerald-500/10 px-2.5 py-1 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-40"
    : "rounded-md border border-emerald-500/50 bg-emerald-500/10 px-3 py-1 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-40";
  const ghost = compact
    ? "rounded-md border border-slate-700 px-2.5 py-1 text-xs text-slate-400 hover:bg-slate-800 disabled:opacity-50"
    : "rounded-md border border-slate-700 px-3 py-1 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-50";
  const label = (busyLabel, idle) => (b ? (compact ? "…" : busyLabel) : idle);

  if (m.paid) {
    return (
      <button onClick={() => onUnmarkPaid(m.month)} disabled={b} className={ghost}
              title={m.paid_at ? `Marked paid ${m.paid_at.slice(0, 10)}` : "Marked paid"}>
        {label("Working…", "Undo payout")}
      </button>
    );
  }
  if (m.finalized) {
    return (
      <div className="flex items-center justify-end gap-2">
        <button onClick={() => onMarkPaid(m.month)} disabled={b} className={primary}>
          {label("Working…", "Mark as paid")}
        </button>
        <button onClick={() => onUnfinalize(m.month)} disabled={b} className={ghost}>
          {label("…", "Undo")}
        </button>
      </div>
    );
  }
  if (m.finalizable) {
    return (
      <div className="flex items-center justify-end gap-2">
        <button onClick={() => onFinalize(m.month)} disabled={b} className={primary}>
          {label("Working…", "Finalize")}
        </button>
        <button onClick={() => onMarkPaid(m.month)} disabled={b} className={ghost}
                title="Finalize and mark paid in one step">
          {label("…", "+ Paid")}
        </button>
      </div>
    );
  }
  // in_progress or none — nothing to do yet.
  return <span className="text-xs text-slate-600">—</span>;
}

export default function PayoutsTab() {
  const { data, error, loading, reload } = useApi(api.payouts, [], null);
  const [busy, setBusy] = React.useState(null); // month currently mutating
  const [actionError, setActionError] = React.useState(null);

  function run(fn) {
    return async (month) => {
      setBusy(month);
      setActionError(null);
      try {
        await fn(month);
        await reload();
      } catch (e) {
        setActionError(e.message || "That didn't work — try again.");
      } finally {
        setBusy(null);
      }
    };
  }
  const onFinalize = run(api.finalizePayout);
  const onUnfinalize = run(api.unfinalizePayout);
  const onMarkPaid = run(api.markPayoutPaid);
  const onUnmarkPaid = run(api.unmarkPayoutPaid);
  const actions = { busy, onFinalize, onMarkPaid, onUnfinalize, onUnmarkPaid };

  if (loading && !data) return <Card title="Payouts"><Loading /></Card>;
  if (error) return <Card title="Payouts"><ErrorState error={error} onRetry={reload} /></Card>;

  const cur = data?.current || {};
  const prev = data?.previous || {};
  const totals = data?.totals || {};
  const history = data?.history || [];
  // Whichever recent month is finalizable-but-not-finalized is the one the push
  // is nudging about; surface it in the banner.
  const ready = [cur, prev].find((m) => m.finalizable && !m.finalized);

  return (
    <div className="grid gap-4">
      {/* Headline cards: this month's estimate + last month's payout */}
      <div className="grid gap-4 sm:grid-cols-2">
        <Card title="Est. payout — this month" right={<StatusPill status={cur.status} />}>
          <Stat label={cur.label} value={cash(cur.payout_amount)} tone={tone(cur.net_juice)}
                sub={subline(cur)} />
          <p className="mt-3 text-xs text-slate-500">
            Net juice booked this month. It's an estimate while shorts are still
            open — once the last short of the month closes, you can finalize it.
          </p>
          <div className="mt-3"><PayoutActions m={cur} {...actions} /></div>
        </Card>

        <Card title="Last month's payout" right={<StatusPill status={prev.status} />}>
          <Stat label={prev.label} value={cash(prev.payout_amount)} tone={tone(prev.net_juice)}
                sub={subline(prev)} />
          <div className="mt-3"><PayoutActions m={prev} {...actions} /></div>
        </Card>
      </div>

      {ready && (
        <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200/90">
          <span className="font-semibold text-emerald-300">{ready.label} payout is ready.</span>{" "}
          {cash(ready.net_juice)} of net income —{" "}
          {ready.month === cur.month ? "the last short of the month has closed" : "the month has closed"}.
          Finalize to lock it in; you'll also get a push notification for this each month.
        </div>
      )}

      {actionError && (
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
          {actionError}
        </div>
      )}

      {/* Roll-up totals */}
      <Card title="Totals">
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Stat label={`${totals.year || ""} income`} value={cash(totals.ytd)} tone={tone(totals.ytd)}
                sub="net juice year to date" />
          <Stat label="All-time income" value={cash(totals.all_time)} tone={tone(totals.all_time)}
                sub="net juice, every month" />
          <Stat label="Paid out" value={cash(totals.paid_out)}
                sub="withdrawn across all months" />
          <Stat label="Awaiting payout" value={cash(totals.awaiting)}
                tone={totals.awaiting > 0 ? "text-amber-300" : "text-slate-100"}
                sub="finalizable or finalized, not yet paid" />
        </div>
      </Card>

      {/* Month-by-month history */}
      <Card title="Monthly history">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wide text-slate-500">
                <th className="py-2 pr-3">Month</th>
                <th className="py-2 pr-3">Payout</th>
                <th className="py-2 pr-3">Closes</th>
                <th className="py-2 pr-3">Status</th>
                <th className="py-2 pr-3 text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {history.map((m) => (
                <tr key={m.month} className="border-t border-slate-800">
                  <td className="py-2 pr-3 font-semibold text-slate-100">{m.label}</td>
                  <td className={`py-2 pr-3 ${tone(m.net_juice)}`}>
                    {cash(m.payout_amount)}{m.estimated ? " ·est" : ""}
                  </td>
                  <td className="py-2 pr-3 text-slate-400">{m.closes || 0}</td>
                  <td className="py-2 pr-3"><StatusPill status={m.status} /></td>
                  <td className="py-2 pr-3">
                    <div className="flex justify-end">
                      <PayoutActions m={m} {...actions} compact />
                    </div>
                  </td>
                </tr>
              ))}
              {history.length === 0 && (
                <tr><td colSpan={5} className="py-6 text-center text-slate-500">
                  No income logged yet — payouts appear as short closes book net juice.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
