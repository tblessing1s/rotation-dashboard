import React from "react";
import { api } from "../api.js";
import { Card, Pill, Light, Loading, pct, useApi } from "./ui.jsx";

export default function KillSwitchMonitor() {
  const { data, error, loading } = useApi(api.killSwitch, [], 5 * 60 * 1000);
  if (loading && !data) return <Card title="Kill Switch"><Loading /></Card>;
  if (error) return <Card title="Kill Switch"><p className="text-sm text-rose-400">{error}</p></Card>;

  const positions = data?.positions || [];
  return (
    <Card title="Kill Switch — RS3M monitor">
      {positions.length === 0 && <p className="text-sm text-slate-500">No open positions to monitor.</p>}
      <div className="space-y-3">
        {positions.map((p) => (
          <div
            key={p.ticker}
            className={`rounded-lg border p-3 ${p.alert ? "border-rose-500/50 bg-rose-500/5" : "border-slate-800"}`}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <Light status={p.status} />
                <span className="font-semibold text-slate-100">{p.ticker}</span>
                <Pill status={p.status} />
              </div>
              <div className="text-sm text-slate-400">
                vs SPY <span className={p.rs3m_vs_spy < 0 ? "text-rose-400" : "text-slate-200"}>{pct(p.rs3m_vs_spy)}</span>
                {"  ·  "}
                vs Sector <span className={p.rs3m_vs_sector < 0 ? "text-rose-400" : "text-slate-200"}>{pct(p.rs3m_vs_sector)}</span>
              </div>
            </div>
            <div className={`mt-2 text-sm ${p.alert ? "text-rose-300" : "text-slate-400"}`}>{p.suggested_action}</div>
          </div>
        ))}
      </div>
    </Card>
  );
}
