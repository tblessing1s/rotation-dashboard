import React from "react";
import { api } from "../api.js";
import { Card, Pill, Spinner, pct, fmt, useApi } from "./ui.jsx";

function SectorBar({ sectors, selected, onSelect }) {
  const entries = Object.entries(sectors || {}).sort(
    (a, b) => (b[1].rs3m || -999) - (a[1].rs3m || -999),
  );
  return (
    <div className="mb-4 flex flex-wrap gap-2">
      <button
        onClick={() => onSelect("")}
        className={`rounded-lg border px-3 py-1.5 text-sm ${
          selected === "" ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-300" : "border-slate-700 text-slate-400 hover:text-slate-200"
        }`}
      >
        All
      </button>
      {entries.map(([etf, s]) => (
        <button
          key={etf}
          onClick={() => onSelect(etf)}
          title={`${s.name} · RS3M ${pct(s.rs3m)} · breadth ${fmt(s.breadth, 0)}%`}
          className={`flex items-center gap-2 rounded-lg border px-3 py-1.5 text-sm ${
            selected === etf ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-300" : "border-slate-700 text-slate-400 hover:text-slate-200"
          }`}
        >
          <Pill status={s.status}>{etf}</Pill>
          <span className="text-xs">{pct(s.rs3m)}</span>
        </button>
      ))}
    </div>
  );
}

export default function StockFilter({ onSelectStock }) {
  const [sector, setSector] = React.useState("");
  const sectorsQ = useApi(api.sectors, []);
  const stocksQ = useApi(() => api.stockFilter(sector), [sector]);

  return (
    <Card title="Stock Filter (Levels 2–4)" right={stocksQ.loading ? <span className="flex items-center gap-1.5 text-xs text-slate-500"><Spinner size="h-3 w-3" />scanning…</span> : null}>
      <SectorBar sectors={sectorsQ.data} selected={sector} onSelect={setSector} />
      {stocksQ.error && <p className="text-sm text-rose-400">{stocksQ.error}</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase tracking-wide text-slate-500">
              <th className="py-2 pr-3">Ticker</th>
              <th className="py-2 pr-3">Sector</th>
              <th className="py-2 pr-3">RS3M vs SPY</th>
              <th className="py-2 pr-3">RS3M vs Sector</th>
              <th className="py-2 pr-3">ATR%</th>
              <th className="py-2 pr-3">Consolidating</th>
              <th className="py-2 pr-3">Status</th>
              <th className="py-2 pr-3"></th>
            </tr>
          </thead>
          <tbody>
            {(stocksQ.data || []).map((row) => {
              const weak = row.rs3m_vs_sector != null && row.rs3m_vs_sector < 0;
              return (
                <tr key={row.ticker} className={`border-t border-slate-800 ${weak ? "bg-rose-500/5" : ""}`}>
                  <td className="py-2 pr-3 font-semibold text-slate-100">{row.ticker}</td>
                  <td className="py-2 pr-3 text-slate-400">{row.sector}</td>
                  <td className="py-2 pr-3">{pct(row.rs3m_vs_spy)}</td>
                  <td className={`py-2 pr-3 ${weak ? "text-rose-400" : "text-slate-200"}`}>{pct(row.rs3m_vs_sector)}</td>
                  <td className="py-2 pr-3 text-slate-400">{fmt(row.atr_pct, 1)}%</td>
                  <td className="py-2 pr-3">{row.consolidating == null ? "—" : row.consolidating ? "Yes" : "No"}</td>
                  <td className="py-2 pr-3">
                    <span title={row.blocked_by?.length ? `Blocked by: ${row.blocked_by.join(", ")}` : "All 4 gate levels pass"}>
                      <Pill status={row.status} />
                    </span>
                  </td>
                  <td className="py-2 pr-3">
                    <button
                      onClick={() => onSelectStock?.(row.ticker)}
                      className="rounded-md border border-slate-700 px-2 py-1 text-xs text-emerald-300 hover:bg-emerald-500/10"
                    >
                      Execute →
                    </button>
                  </td>
                </tr>
              );
            })}
            {!stocksQ.loading && (stocksQ.data || []).length === 0 && (
              <tr><td colSpan={8} className="py-6 text-center text-slate-500">No candidates.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
