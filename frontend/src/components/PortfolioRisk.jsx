import React from "react";
import { api } from "../api.js";
import { Card, Stat, Meter, Loading, money, fmt, useApi } from "./ui.jsx";

// One glance = "what is my book actually exposed to": aggregate delta (raw and
// SPY-beta-adjusted), theta/day, vega, capital vs cap, reserve, sector split.
export default function PortfolioRisk() {
  const { data, error, loading } = useApi(api.portfolioRisk, [], null);
  if (loading && !data) return <Card title="Portfolio risk"><Loading /></Card>;
  if (error) return <Card title="Portfolio risk"><p className="text-sm text-rose-400">{error}</p></Card>;

  const t = data?.totals || {};
  const cap = data?.capital || {};
  const sectors = data?.sector_exposure || [];
  if (!data?.positions?.length) return null;

  return (
    <Card title="Portfolio risk">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Δ dollars" value={money(t.delta_dollars)}
              sub={t.delta_dollars_spy_adj != null ? `${money(t.delta_dollars_spy_adj)} SPY-β adj` : "β unavailable"} />
        <Stat label="Θ / day" value={money(t.theta_per_day)}
              tone={t.theta_per_day >= 0 ? "text-emerald-300" : "text-rose-300"}
              sub="net decay collected" />
        <Stat label="Vega" value={money(t.vega)} sub="$ per vol point" />
        <Stat label="Deployed" value={money(cap.deployed)}
              tone={cap.deployed > cap.cap ? "text-rose-300" : "text-slate-100"}
              sub={`cap ${money(cap.cap)} (${fmt(cap.pct_of_cap, 0)}%)`} />
      </div>
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <div>
          <div className="mb-1 flex justify-between text-xs text-slate-400">
            <span>Reserve (2×ATR defensive)</span>
            <span className={cap.reserve_ok ? "text-emerald-300" : "text-rose-300"}>
              {money(cap.operating_cash)} cash vs {money(cap.reserve_required)} required
            </span>
          </div>
          <Meter
            pct={cap.reserve_required ? (cap.operating_cash / cap.reserve_required) * 100 : 100}
            tone={cap.reserve_ok ? "bg-emerald-500" : "bg-rose-500"}
          />
        </div>
        <div>
          <div className="mb-1 text-xs text-slate-400">Sector exposure (LEAP capital)</div>
          <div className="flex h-3 w-full overflow-hidden rounded-full bg-slate-800">
            {sectors.map((s, i) => (
              <div
                key={s.sector}
                title={`${s.sector}: ${money(s.capital)} (${s.pct}%)`}
                className={["bg-sky-500", "bg-emerald-500", "bg-amber-500", "bg-rose-500", "bg-violet-500", "bg-teal-500"][i % 6]}
                style={{ width: `${s.pct}%` }}
              />
            ))}
          </div>
          <div className="mt-1 flex flex-wrap gap-x-3 text-xs text-slate-500">
            {sectors.map((s) => <span key={s.sector}>{s.sector} {fmt(s.pct, 0)}%</span>)}
          </div>
        </div>
      </div>
      <div className="mt-4 overflow-x-auto border-t border-slate-800 pt-3">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase tracking-wide text-slate-500">
              <th className="py-1 pr-3">Ticker</th>
              <th className="py-1 pr-3">β</th>
              <th className="py-1 pr-3">Δ shares</th>
              <th className="py-1 pr-3">Δ $</th>
              <th className="py-1 pr-3">Δ $ (β adj)</th>
              <th className="py-1 pr-3">Θ/day</th>
              <th className="py-1 pr-3">Vega</th>
            </tr>
          </thead>
          <tbody>
            {data.positions.map((r) => (
              <tr key={r.ticker} className="border-t border-slate-800/60">
                <td className="py-1.5 pr-3 font-semibold text-slate-100">
                  {r.ticker}
                  {!r.greeks_complete && (
                    <span className="ml-1 text-xs text-amber-400" title="Some legs lacked a usable mark — greeks partial">*</span>
                  )}
                </td>
                <td className="py-1.5 pr-3 text-slate-300">{fmt(r.beta, 2)}</td>
                <td className="py-1.5 pr-3 text-slate-300">{fmt(r.delta_shares, 0)}</td>
                <td className="py-1.5 pr-3 text-slate-300">{money(r.delta_dollars)}</td>
                <td className="py-1.5 pr-3 text-slate-300">{money(r.delta_dollars_spy_adj)}</td>
                <td className={`py-1.5 pr-3 ${r.theta_per_day >= 0 ? "text-emerald-300" : "text-rose-300"}`}>{money(r.theta_per_day)}</td>
                <td className="py-1.5 pr-3 text-slate-300">{money(r.vega)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
