import React from "react";
import { api } from "../api.js";
import { Card, Light, Stat, Pill, fmt, useApi } from "./ui.jsx";

const REGIME_COPY = {
  green: "Market good — clear to hunt entries.",
  yellow: "Caution — tighten criteria, no fresh risk.",
  red: "Wait — regime risk-off, stand down.",
};

export default function RegimeScanner({ onStatus }) {
  // Level 1 market regime. Refreshes every 5 minutes per the routine.
  const { data, error, loading } = useApi(api.regime, [], 5 * 60 * 1000);

  React.useEffect(() => {
    if (data?.status) onStatus?.(data.status);
  }, [data, onStatus]);

  if (loading && !data) return <Card title="Market Regime">Loading…</Card>;
  if (error) return <Card title="Market Regime"><p className="text-rose-400 text-sm">{error}</p></Card>;

  const r = data || {};
  return (
    <Card
      title="Market Regime (Level 1)"
      right={<Pill status={r.status}>{r.status}</Pill>}
    >
      <div className="flex items-center gap-4">
        <Light status={r.status} size="h-10 w-10" />
        <div>
          <div className="text-lg font-semibold text-slate-100">{r.status?.toUpperCase()}</div>
          <div className="text-sm text-slate-400">{REGIME_COPY[r.status] || ""}</div>
        </div>
      </div>
      <div className="mt-5 grid grid-cols-3 gap-4">
        <Stat label="Breadth" value={r.breadth != null ? `${fmt(r.breadth, 0)}%` : "—"} sub="above 50-DMA" />
        <div title={r.vix == null && r.vix_error ? r.vix_error : ""}>
          <Stat
            label="VIX"
            value={fmt(r.vix, 1)}
            sub={r.vix == null ? (r.vix_error ? "unavailable — hover for why" : "index level") : "index level"}
            tone={r.vix == null ? "text-slate-500" : "text-slate-100"}
          />
        </div>
        <Stat label="SPY trend" value={(r.spy_trend || "—").toUpperCase()} sub={`vs MA21 ${fmt(r.spy_dist_ma21, 1)}%`} />
      </div>
    </Card>
  );
}
