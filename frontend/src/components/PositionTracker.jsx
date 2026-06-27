import React from "react";
import { api } from "../api.js";
import { Card, Stat, Meter, Pill, Loading, money, fmt, pct, useApi } from "./ui.jsx";

export default function PositionTracker() {
  const { data, error, loading } = useApi(api.positions, [], null);
  if (loading && !data) return <Card title="Positions"><Loading /></Card>;
  if (error) return <Card title="Positions"><p className="text-sm text-rose-400">{error}</p></Card>;

  const positions = data?.positions || [];
  const cap = data?.capital || {};
  const ms = cap.milestones || {};

  return (
    <div className="grid gap-4">
      <Card title="Capital">
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Stat label="Deployed" value={money(cap.capital_deployed)} />
          <Stat label="Reserve req." value={money(cap.reserve_required)} tone={cap.reserve_ok ? "text-slate-100" : "text-rose-300"} />
          <Stat label="Operating cash" value={money(cap.operating_cash)} />
          <Stat label="Juice YTD" value={money(cap.juice_ytd)} tone="text-emerald-300" />
        </div>
        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          {["half_nut", "quit_safe"].map((k) => (
            ms[k] && (
              <div key={k}>
                <div className="mb-1 flex justify-between text-sm">
                  <span className="text-slate-300">{k === "half_nut" ? "Half-nut ($/mo)" : "Quit-safe ($/mo)"}</span>
                  <span className="text-slate-400">{money(ms[k].current)} / {money(ms[k].target)}</span>
                </div>
                <Meter pct={ms[k].pct} tone="bg-emerald-500" />
              </div>
            )
          ))}
        </div>
      </Card>

      {positions.length === 0 && <Card>No open positions.</Card>}
      {positions.map((p) => {
        const leap = p.leap || {};
        const sh = p.shares || {};
        return (
          <Card key={p.ticker} title={`${p.ticker} · ${p.sector || ""}`} right={<Pill status={p.status === "active" ? "green" : "unknown"}>{p.status}</Pill>}>
            <div className="grid gap-4 sm:grid-cols-3">
              <div>
                <div className="text-xs uppercase tracking-wide text-slate-500">LEAP</div>
                <div className="text-sm text-slate-200">{leap.contracts || 0} × {fmt(leap.strike, 0)}C · {leap.dte ?? "—"} DTE</div>
                <div className="text-xs text-slate-500">intrinsic {money(leap.intrinsic)} · extrinsic {money(leap.extrinsic)}</div>
              </div>
              <div>
                <div className="text-xs uppercase tracking-wide text-slate-500">Shares ({sh.count || 0}/{sh.cap || 500})</div>
                <Meter pct={sh.pct_to_cap} tone={sh.locked ? "bg-amber-500" : "bg-sky-500"} />
                <div className="mt-1 text-xs text-slate-500">{sh.locked ? "Cap reached — rotate to a new stock." : `${fmt(sh.pct_to_cap, 0)}% to cap`}</div>
              </div>
              <div>
                <div className="text-xs uppercase tracking-wide text-slate-500">Stock</div>
                <div className="text-sm text-slate-200">{fmt(p.stock_price, 2)}</div>
                <div className="text-xs text-slate-500">{(p.short_calls || []).length} open short(s)</div>
              </div>
            </div>
          </Card>
        );
      })}
    </div>
  );
}
