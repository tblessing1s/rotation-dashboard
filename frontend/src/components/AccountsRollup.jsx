import React from "react";
import { api } from "../api.js";
import { Card, Loading, ErrorState, money } from "./ui.jsx";

// "All accounts" — the multi-account monitor at the top of Overview.
//
// Every other surface answers for ONE book. This one answers the question you
// can only ask once there are several: where does each book stand, and which one
// needs me next. It reads /api/accounts/summary, which is served straight off
// the state files (no provider calls), so it stays a single cheap request.
//
// Renders nothing on a single-account install — there is nothing to compare.

function Cell({ children, className = "" }) {
  return <td className={`whitespace-nowrap px-3 py-2 ${className}`}>{children}</td>;
}

function Flag({ count, label, tone }) {
  if (!count) return null;
  return (
    <span className={`rounded-full px-1.5 py-0.5 text-[10px] font-semibold ${tone}`}>
      {count} {label}
    </span>
  );
}

export default function AccountsRollup({ activeId, refreshKey, onSelect }) {
  const [data, setData] = React.useState(null);
  const [error, setError] = React.useState(null);

  const load = React.useCallback(() => {
    api.accountsSummary()
      .then((r) => { setData(r); setError(null); })
      .catch((e) => setError(e.message));
  }, []);

  React.useEffect(() => {
    load();
    const id = setInterval(load, 5 * 60 * 1000);
    return () => clearInterval(id);
  }, [load, refreshKey]);

  if (error) return <Card title="All accounts"><ErrorState error={error} onRetry={load} /></Card>;
  if (!data) return null;              // first paint: don't push the ribbon down
  const rows = data.accounts || [];
  if (rows.length <= 1) return null;   // single account — nothing to compare

  const totals = data.totals || {};

  return (
    <Card
      title="All accounts"
      right={
        <span className="text-xs text-slate-500">
          {money(totals.this_week)} this week · {money(totals.capital_deployed)} deployed
        </span>
      }
    >
      <div className="-mx-2 overflow-x-auto">
        <table className="w-full min-w-[36rem] text-left text-sm">
          <thead>
            <tr className="text-[11px] uppercase tracking-wide text-slate-500">
              <th className="px-3 py-1.5 font-medium">Account</th>
              <th className="px-3 py-1.5 text-right font-medium">Open</th>
              <th className="px-3 py-1.5 text-right font-medium">Deployed</th>
              <th className="px-3 py-1.5 text-right font-medium">Week</th>
              <th className="px-3 py-1.5 text-right font-medium">Month</th>
              <th className="px-3 py-1.5 font-medium">Needs you</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800">
            {rows.map((r) => {
              const isActive = r.id === activeId;
              const theta = r.theta || {};
              return (
                <tr
                  key={r.id}
                  onClick={() => !isActive && onSelect?.(r.id)}
                  className={`transition ${isActive ? "bg-sky-500/5" : "cursor-pointer hover:bg-slate-800/60"}`}
                  title={isActive ? "Current account" : `Switch to ${r.label}`}
                >
                  <Cell>
                    <span className="flex items-center gap-2">
                      <span className={isActive ? "font-semibold text-sky-200" : "text-slate-200"}>
                        {r.label}
                      </span>
                      {isActive && (
                        <span className="rounded-full bg-sky-500/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-sky-300">
                          active
                        </span>
                      )}
                    </span>
                    <span className="block text-[11px] text-slate-500">
                      {r.broker_bound ? `Schwab ${r.broker_account_number}` : "no account linked"}
                      {r.exists ? "" : " · not started"}
                    </span>
                  </Cell>
                  <Cell className="text-right text-slate-200">{r.open_positions}</Cell>
                  <Cell className="text-right text-slate-300">{money(r.capital_deployed)}</Cell>
                  <Cell className="text-right text-emerald-300">{money(theta.this_week)}</Cell>
                  <Cell className="text-right text-slate-300">{money(theta.this_month)}</Cell>
                  <Cell>
                    <span className="flex flex-wrap gap-1">
                      <Flag count={r.active_alerts} label="alerts"
                            tone="bg-rose-500/15 text-rose-300" />
                      <Flag count={r.pending_orders} label="working"
                            tone="bg-amber-500/15 text-amber-300" />
                      <Flag count={r.open_proposals} label="to adopt"
                            tone="bg-sky-500/15 text-sky-300" />
                      {!r.active_alerts && !r.pending_orders && !r.open_proposals && (
                        <span className="text-xs text-slate-600">—</span>
                      )}
                      {r.error && <span className="text-xs text-rose-300">{r.error}</span>}
                    </span>
                  </Cell>
                </tr>
              );
            })}
          </tbody>
          <tfoot>
            <tr className="border-t border-slate-700 text-slate-400">
              <Cell className="text-xs uppercase tracking-wide">Total</Cell>
              <Cell className="text-right">{totals.open_positions}</Cell>
              <Cell className="text-right">{money(totals.capital_deployed)}</Cell>
              <Cell className="text-right text-emerald-300">{money(totals.this_week)}</Cell>
              <Cell className="text-right">{money(totals.this_month)}</Cell>
              <Cell />
            </tr>
          </tfoot>
        </table>
      </div>
      <p className="mt-2 text-xs text-slate-600">
        Click a row to switch books. Alerts, reconciliation and nightly backups run
        for every account whether or not it's the one on screen.
      </p>
    </Card>
  );
}
