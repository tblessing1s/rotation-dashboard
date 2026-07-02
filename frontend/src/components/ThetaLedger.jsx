import React from "react";
import { api } from "../api.js";
import { Card, Stat, Meter, Loading, money, fmt, useApi } from "./ui.jsx";

export default function ThetaLedger() {
  const { data, error, loading } = useApi(api.thetaLedger, [], null);
  if (loading && !data) return <Card title="Theta Ledger"><Loading /></Card>;
  if (error) return <Card title="Theta Ledger"><p className="text-sm text-rose-400">{error}</p></Card>;

  const totals = data?.totals || {};
  const weeks = data?.weeks || [];
  const payback = data?.extrinsic_payback || {};
  const summary = data?.extrinsic_summary || {};
  const hurdle = summary.leap_extrinsic_at_entry || 0;
  const rollByTicker = data?.roll_ledger?.by_ticker || {};
  const rollTotals = Object.values(rollByTicker).reduce(
    (a, r) => ({ count: a.count + (r.count || 0), net: a.net + (r.net_total || 0), drag: a.drag + (r.drag_total || 0) }),
    { count: 0, net: 0, drag: 0 },
  );

  return (
    <div className="grid gap-4">
      <Card title="Net Juice (extrinsic sold − paid back)">
        <div className="grid grid-cols-3 gap-4">
          <Stat label="This week" value={money(totals.this_week)} tone="text-emerald-300" />
          <Stat label="This month" value={money(totals.this_month)} />
          <Stat label="YTD" value={money(totals.ytd)} />
        </div>
        {hurdle > 0 && (
          <div className="mt-4 grid grid-cols-3 gap-4 border-t border-slate-800 pt-4">
            <Stat label="LEAP extrinsic hurdle" value={money(hurdle)} sub="income needed to net positive" />
            <Stat label="Remaining to fill" value={money(summary.remaining_to_payback)}
                  tone={summary.income_positive ? "text-emerald-300" : "text-amber-300"} />
            <Stat label="Net income" value={money(summary.net_income)}
                  tone={summary.income_positive ? "text-emerald-300" : "text-rose-300"}
                  sub={summary.income_positive ? "income-positive ✓" : "still filling the LEAP"} />
          </div>
        )}
        {rollTotals.count > 0 && (
          <div className="mt-4 grid grid-cols-3 gap-4 border-t border-slate-800 pt-4">
            <Stat label="Rolls executed" value={rollTotals.count} sub="paired close+open tickets" />
            <Stat label="Roll net" value={money(rollTotals.net)}
                  tone={rollTotals.net >= 0 ? "text-emerald-300" : "text-rose-300"}
                  sub="credits − buybacks across all rolls" />
            <Stat label="Roll drag" value={money(rollTotals.drag)}
                  tone={rollTotals.drag < 0 ? "text-rose-300" : "text-slate-100"}
                  sub="debits paid on defensive rolls (whipsaw cost)" />
          </div>
        )}
      </Card>

      <Card title="Extrinsic Payback (fill the LEAP before it's 'real' income)">
        {Object.keys(payback).length === 0 && <p className="text-sm text-slate-500">No positions yet.</p>}
        <div className="space-y-4">
          {Object.entries(payback).map(([ticker, p]) => (
            <div key={ticker}>
              <div className="mb-1 flex justify-between text-sm">
                <span className="font-semibold text-slate-100">{ticker}</span>
                <span className="text-slate-400">
                  {money(p.collected_to_date)} / {money(p.leap_extrinsic_at_entry)} · {fmt(p.pct_complete, 0)}%
                </span>
              </div>
              <Meter pct={p.pct_complete} tone={p.pct_complete >= 100 ? "bg-emerald-400" : "bg-sky-500"} />
              <div className="mt-1 text-xs text-slate-500">
                {p.remaining_to_payback > 0
                  ? `${money(p.remaining_to_payback)} left to fill`
                  : "Filled — you're in profit mode."}
              </div>
            </div>
          ))}
        </div>
      </Card>

      <Card title="Per-week closes">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wide text-slate-500">
                <th className="py-2 pr-3">Week</th>
                <th className="py-2 pr-3">Ticker</th>
                <th className="py-2 pr-3">Extrinsic sold</th>
                <th className="py-2 pr-3">Paid back</th>
                <th className="py-2 pr-3">Net juice</th>
              </tr>
            </thead>
            <tbody>
              {weeks.map((w, i) => (
                <tr key={i} className="border-t border-slate-800">
                  <td className="py-2 pr-3 text-slate-300">{w.week}</td>
                  <td className="py-2 pr-3 font-semibold text-slate-100">{w.ticker}</td>
                  <td className="py-2 pr-3">{money(w.extrinsic_sold)}</td>
                  <td className="py-2 pr-3">{money(w.extrinsic_paid_back)}</td>
                  <td className="py-2 pr-3 text-emerald-300">{money(w.net_juice)}</td>
                </tr>
              ))}
              {weeks.length === 0 && <tr><td colSpan={5} className="py-6 text-center text-slate-500">No closes logged yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
