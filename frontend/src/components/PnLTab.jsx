import React from "react";
import { api } from "../api.js";
import { Card, Stat, Loading, ErrorState, useApi } from "./ui.jsx";

// Where the money is made and lost: the underlying (shares) vs the short
// covered calls, per ticker and overall. Computed server-side (pnl_breakdown.py).
// "Uncovered gap" is a slice INSIDE the shares column — the stock move while
// owned lots had no call — so it is shown as its own column, never summed in.

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

function Num({ v, bold }) {
  return <span className={`${tone(v)} ${bold ? "font-semibold" : ""}`}>{cash(v)}</span>;
}

const TH = "px-2 py-1.5 text-right text-xs font-medium uppercase tracking-wide text-slate-500";

function Row({ r, total }) {
  const cls = total ? "border-t border-slate-700 font-semibold" : "border-t border-slate-800";
  return (
    <tr className={cls}>
      <td className="px-2 py-1.5 text-left text-slate-200">
        {r.ticker === "ALL" ? "All positions" : r.ticker}
        {!total && !r.open && <span className="ml-2 text-xs text-slate-600">closed</span>}
      </td>
      <td className="px-2 py-1.5 text-right"><Num v={r.shares_total} /></td>
      <td className="px-2 py-1.5 text-right"><Num v={r.calls_total} /></td>
      <td className="px-2 py-1.5 text-right"><Num v={r.total} bold /></td>
      <td className="px-2 py-1.5 text-right"><Num v={r.realized} /></td>
      <td className="px-2 py-1.5 text-right"><Num v={r.unrealized} /></td>
      <td className="px-2 py-1.5 text-right" title="Stock move while owned shares had no call on them (included in Shares)">
        <Num v={r.uncovered_gap} />
      </td>
    </tr>
  );
}

export default function PnLTab() {
  const { data, error, loading, reload } = useApi(api.pnlBreakdown, [], null);
  if (loading && !data) return <Card title="P/L"><Loading /></Card>;
  if (error) return <Card title="P/L"><ErrorState error={error} onRetry={reload} /></Card>;
  const t = data?.totals || {};
  const rows = data?.tickers || [];
  const worst = rows.find((r) => r.total < 0);
  return (
    <div className="grid gap-4">
      <Card title="Overall P/L — underlying vs short calls">
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Stat label="Total" value={cash(t.total)} tone={tone(t.total)}
                sub={`${cash(t.realized)} realized · ${cash(t.unrealized)} open`} />
          <Stat label="Underlying (shares)" value={cash(t.shares_total)} tone={tone(t.shares_total)}
                sub={`${cash(t.shares_realized)} realized · ${cash(t.shares_unrealized)} open`} />
          <Stat label="Short calls" value={cash(t.calls_total)} tone={tone(t.calls_total)}
                sub={`${cash(t.calls_realized)} realized · ${cash(t.calls_unrealized)} open`} />
          <Stat label="Lost while uncovered" value={cash(t.uncovered_gap)} tone={tone(t.uncovered_gap)}
                sub="inside Underlying, not added" />
        </div>
        {worst && (
          <p className="mt-3 text-xs text-slate-500">
            Biggest drag: {worst.ticker} at {cash(worst.total)}
            {worst.uncovered_gap < 0 && ` — ${cash(worst.uncovered_gap)} of it while uncovered`}.
          </p>
        )}
      </Card>
      <Card title="By position (worst first)">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className={`${TH} text-left`}>Ticker</th>
                <th className={TH}>Shares</th>
                <th className={TH}>Calls</th>
                <th className={TH}>Total</th>
                <th className={TH}>Realized</th>
                <th className={TH}>Open</th>
                <th className={TH}>Uncovered gap</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => <Row key={r.ticker} r={r} />)}
              <Row r={t} total />
            </tbody>
          </table>
        </div>
        <p className="mt-3 text-xs text-slate-500">
          Calls = premium collected minus cash paid to buy back (assignment/expiry cost nothing here — the
          intrinsic given away is already in the share sale at the strike). Open = marked to live spot and
          call mark. Uncovered gap is shadow-mode, no authority.
        </p>
      </Card>
    </div>
  );
}
