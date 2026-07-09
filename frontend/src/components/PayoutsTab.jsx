import React from "react";
import { api } from "../api.js";
import { Card, Stat, Pill, Loading, ErrorState, useApi } from "./ui.jsx";

// Monthly payout tracker: the income-withdrawal view over the juice ledger.
// Net juice per month is derived server-side from the close_short executions;
// this page adds the operator's payout bookkeeping — the current-month estimate,
// the last completed month's finalized payout, and a mark-as-paid record so the
// PAYOUT_READY alert can resolve once the money is withdrawn.

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
  unpaid: { status: "wait", label: "Awaiting payout" },
  paid: { status: "go", label: "Paid" },
};

function StatusPill({ status }) {
  const p = STATUS_PILL[status] || STATUS_PILL.unpaid;
  return <Pill status={p.status}>{p.label}</Pill>;
}

export default function PayoutsTab() {
  const { data, error, loading, reload } = useApi(api.payouts, [], null);
  const [busy, setBusy] = React.useState(null); // month currently being (un)marked
  const [actionError, setActionError] = React.useState(null);

  async function markPaid(month) {
    setBusy(month);
    setActionError(null);
    try {
      await api.markPayoutPaid(month);
      await reload();
    } catch (e) {
      setActionError(e.message || "Couldn't mark this month paid.");
    } finally {
      setBusy(null);
    }
  }

  async function unmarkPaid(month) {
    setBusy(month);
    setActionError(null);
    try {
      await api.unmarkPayoutPaid(month);
      await reload();
    } catch (e) {
      setActionError(e.message || "Couldn't undo this payout.");
    } finally {
      setBusy(null);
    }
  }

  if (loading && !data) return <Card title="Payouts"><Loading /></Card>;
  if (error) return <Card title="Payouts"><ErrorState error={error} onRetry={reload} /></Card>;

  const cur = data?.current || {};
  const prev = data?.previous || {};
  const totals = data?.totals || {};
  const history = data?.history || [];
  const prevReady = !prev.paid && prev.net_juice > 0;

  return (
    <div className="grid gap-4">
      {/* Headline cards: this month's estimate + last month's finalized payout */}
      <div className="grid gap-4 sm:grid-cols-2">
        <Card title="Est. payout — this month"
              right={<StatusPill status={cur.status} />}>
          <Stat
            label={cur.label}
            value={cash(cur.net_juice)}
            tone={tone(cur.net_juice)}
            sub={`${cur.closes || 0} short close${cur.closes === 1 ? "" : "s"} so far · still accruing`}
          />
          <p className="mt-3 text-xs text-slate-500">
            Net juice booked this month. This is an estimate — it keeps growing as
            you roll shorts, and finalizes when the month closes.
          </p>
        </Card>

        <Card title="Last month's payout"
              right={<StatusPill status={prev.status} />}>
          <Stat
            label={prev.label}
            value={cash(prev.paid && prev.paid_amount != null ? prev.paid_amount : prev.net_juice)}
            tone={tone(prev.net_juice)}
            sub={prev.paid
              ? `Marked paid${prev.paid_at ? ` · ${prev.paid_at.slice(0, 10)}` : ""}`
              : prev.net_juice > 0 ? "Finalized — ready to withdraw" : "No income this month"}
          />
          <div className="mt-3 flex items-center gap-2">
            {prev.paid ? (
              <button
                onClick={() => unmarkPaid(prev.month)}
                disabled={busy === prev.month}
                className="rounded-md border border-slate-700 px-3 py-1 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-50"
              >
                {busy === prev.month ? "Working…" : "Undo"}
              </button>
            ) : (
              <button
                onClick={() => markPaid(prev.month)}
                disabled={busy === prev.month || !(prev.net_juice > 0)}
                className="rounded-md border border-emerald-500/50 bg-emerald-500/10 px-3 py-1 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-40"
              >
                {busy === prev.month ? "Working…" : "Mark as paid"}
              </button>
            )}
          </div>
        </Card>
      </div>

      {prevReady && (
        <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200/90">
          <span className="font-semibold text-emerald-300">{prev.label} payout is finalized.</span>{" "}
          {cash(prev.net_juice)} of net income is ready to withdraw. You'll also get a
          push notification for this each month — mark it paid once the money's out.
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
          <Stat label="Awaiting payout" value={cash(totals.unpaid)}
                tone={totals.unpaid > 0 ? "text-amber-300" : "text-slate-100"}
                sub="finalized but not marked paid" />
        </div>
      </Card>

      {/* Month-by-month history */}
      <Card title="Monthly history">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wide text-slate-500">
                <th className="py-2 pr-3">Month</th>
                <th className="py-2 pr-3">Net income</th>
                <th className="py-2 pr-3">Closes</th>
                <th className="py-2 pr-3">Status</th>
                <th className="py-2 pr-3 text-right">Payout</th>
              </tr>
            </thead>
            <tbody>
              {history.map((m) => (
                <tr key={m.month} className="border-t border-slate-800">
                  <td className="py-2 pr-3 font-semibold text-slate-100">{m.label}</td>
                  <td className={`py-2 pr-3 ${tone(m.net_juice)}`}>
                    {cash(m.net_juice)}{m.estimated ? " ·est" : ""}
                  </td>
                  <td className="py-2 pr-3 text-slate-400">{m.closes || 0}</td>
                  <td className="py-2 pr-3"><StatusPill status={m.status} /></td>
                  <td className="py-2 pr-3 text-right">
                    {m.estimated ? (
                      <span className="text-xs text-slate-600">—</span>
                    ) : m.paid ? (
                      <button
                        onClick={() => unmarkPaid(m.month)}
                        disabled={busy === m.month}
                        className="rounded-md border border-slate-700 px-2.5 py-1 text-xs text-slate-400 hover:bg-slate-800 disabled:opacity-50"
                        title={m.paid_at ? `Marked paid ${m.paid_at.slice(0, 10)}` : "Marked paid"}
                      >
                        {busy === m.month ? "…" : "Undo"}
                      </button>
                    ) : (
                      <button
                        onClick={() => markPaid(m.month)}
                        disabled={busy === m.month || !(m.net_juice > 0)}
                        className="rounded-md border border-emerald-500/50 bg-emerald-500/10 px-2.5 py-1 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-40"
                      >
                        {busy === m.month ? "…" : "Mark paid"}
                      </button>
                    )}
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
