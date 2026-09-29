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

// One line of the story. kind: "neutral" (a raw amount, no good/bad colour),
// "pnl" (signed, coloured), "sub" (an "of which" slice, indented + muted).
function Line({ label, value, kind = "pnl", bold, hint }) {
  const sub = kind === "sub";
  return (
    <div className={`flex items-baseline justify-between gap-4 py-0.5 ${sub ? "pl-5 text-xs text-slate-500" : "text-sm text-slate-300"}`}
         title={hint}>
      <span className={bold ? "font-semibold text-slate-100" : ""}>{sub ? "of which " : ""}{label}</span>
      {kind === "neutral"
        ? <span className="text-slate-200">{cash(value)}</span>
        : <Num v={value} bold={bold} />}
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div className="min-w-0">
      <div className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">{title}</div>
      {children}
    </div>
  );
}

// Original cost → current cost → premium (intrinsic / extrinsic) → buybacks →
// uncovered gap → position P/L, so it is clear where the loss came from.
function Story({ r }) {
  return (
    <div className="grid gap-4 rounded-lg bg-slate-950/40 p-3 sm:grid-cols-3">
      <Section title="1 · Underlying (shares)">
        <Line kind="neutral" label="Original cost of shares bought" value={r.shares_bought_cost} />
        <Line kind="neutral" label="Cost of shares already sold" value={r.shares_sold_cost}
              hint="Cost basis of the shares that have left" />
        <Line kind="neutral" label="Current cost of shares held" value={r.shares_held_cost} />
        {r.open && <Line kind="neutral" label="Current value at spot" value={r.shares_held_value} />}
        <Line label="Realized (sold / called away)" value={r.shares_realized} />
        <Line label="Open (value − cost)" value={r.shares_unrealized} />
        <Line label="Shares P/L" value={r.shares_total} bold />
        <Line kind="sub" label="uncovered gap" value={r.uncovered_gap}
              hint="Stock move while owned shares had no call on them (shadow-mode, no authority)" />
        <Line kind="sub" label="while covered" value={r.shares_while_covered} />
      </Section>
      <Section title="2 · Short-call premium">
        <Line kind="neutral" label="Total premium sold" value={r.premium_sold} />
        <Line kind="sub" label="intrinsic" value={r.premium_intrinsic}
              hint="Premium that was intrinsic when sold — the shares give this back if called" />
        <Line kind="sub" label="extrinsic" value={r.premium_extrinsic}
              hint="Premium that was time value when sold — the true 'juice'" />
        <Line label="Paid to buy back" value={-r.buyback_paid}
              hint="Assignment and expiry cost nothing here; the intrinsic is already in the share sale at the strike" />
        {r.open && <Line label="Open calls (cost to close now)" value={-r.open_call_mark} />}
        <Line label="Short-call P/L" value={r.calls_total} bold />
      </Section>
      <Section title="3 · Everything else → result">
        <Line label="Dividends received" value={r.dividends} />
        <Line label="Shares P/L" value={r.shares_total} />
        <Line label="Short-call P/L" value={r.calls_total} />
        <div className="mt-1 border-t border-slate-700 pt-1">
          <Line label="Position P/L" value={r.total} bold />
        </div>
      </Section>
    </div>
  );
}

function Row({ r, total, open, onToggle }) {
  const cls = total ? "border-t border-slate-700 font-semibold" : "border-t border-slate-800";
  return (
    <tr className={`${cls} ${onToggle ? "cursor-pointer hover:bg-slate-800/40" : ""}`}
        onClick={onToggle}>
      <td className="px-2 py-1.5 text-left text-slate-200">
        {onToggle && <span className="mr-1.5 text-xs text-slate-500">{open ? "▾" : "▸"}</span>}
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
  const [openT, setOpenT] = React.useState(null); // ticker whose story is expanded
  // Default to the biggest drag so the answer to "where is it coming from" is
  // on screen without a click.
  const first = data?.tickers?.[0]?.ticker;
  const shown = openT === null ? first : openT;
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
      <Card title="By position (worst first) — click a row for the full story">
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
              {rows.map((r) => (
                <React.Fragment key={r.ticker}>
                  <Row r={r} open={shown === r.ticker}
                       onToggle={() => setOpenT(shown === r.ticker ? "" : r.ticker)} />
                  {shown === r.ticker && (
                    <tr><td colSpan={7} className="px-2 pb-3"><Story r={r} /></td></tr>
                  )}
                </React.Fragment>
              ))}
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
