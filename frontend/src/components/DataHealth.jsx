import React from "react";
import { api } from "../api.js";
import { Card, Light, Loading, fmt, useApi } from "./ui.jsx";

// Data health: last-successful fetch per source + cache staleness, so a silent
// provider failure is visible instead of quietly serving stale frames.

const SOURCE_LABELS = {
  schwab_bars: "Schwab daily bars",
  schwab_quote: "Schwab quotes",
  alpha_vantage_bars: "Alpha Vantage bars (fallback)",
  alpha_vantage_quote: "Alpha Vantage quotes (fallback)",
};

export default function DataHealth() {
  const { data, error, loading, reload } = useApi(api.dataHealth, [], 120000);
  const [refreshing, setRefreshing] = React.useState(false);

  async function refresh() {
    setRefreshing(true);
    try {
      await api.maintenanceRefresh();
      await reload();
    } finally {
      setRefreshing(false);
    }
  }

  if (loading && !data) return <Card title="Data health"><Loading /></Card>;
  if (error) return <Card title="Data health"><p className="text-sm text-rose-400">{error}</p></Card>;

  const providers = data?.providers || {};
  const sources = providers.sources || {};
  const ages = data?.ohlcv_cache_age_hours || {};
  const tok = data?.schwab_token || {};

  return (
    <Card
      title="Data health"
      right={
        !data?.demo && (
          <button onClick={refresh} disabled={refreshing}
                  className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50">
            {refreshing ? "Refreshing…" : "Refresh earnings/dividends"}
          </button>
        )
      }
    >
      {data?.demo ? (
        <p className="text-sm text-slate-500">Demo mode — synthetic cache, providers unused.</p>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <div className="mb-1 text-xs uppercase tracking-wide text-slate-500">Last successful fetch</div>
            <ul className="space-y-1 text-sm">
              {Object.entries(SOURCE_LABELS).map(([key, label]) => (
                <li key={key} className="flex items-center gap-2">
                  <Light status={sources[key] ? "green" : "yellow"} size="h-2.5 w-2.5" />
                  <span className="text-slate-300">{label}</span>
                  <span className="ml-auto text-xs text-slate-500">
                    {sources[key] ? `${sources[key].at.replace("T", " ")} (${sources[key].symbol})` : "no success this session"}
                  </span>
                </li>
              ))}
            </ul>
            {providers.fallback_events > 0 && (
              <p className="mt-1 text-xs text-amber-300">
                Alpha Vantage covered for Schwab {providers.fallback_events} time(s) this session.
              </p>
            )}
            <p className="mt-2 text-xs text-slate-500">
              Schwab token: {tok.status || "missing"}
              {tok.daysLeft != null && ` · ${fmt(tok.daysLeft, 1)}d left`}
            </p>
          </div>
          <div>
            <div className="mb-1 text-xs uppercase tracking-wide text-slate-500">OHLCV cache age (key symbols)</div>
            <ul className="space-y-1 text-sm">
              {Object.entries(ages).map(([sym, age]) => (
                <li key={sym} className="flex items-center gap-2">
                  <Light status={age == null ? "red" : age <= 30 ? "green" : "yellow"} size="h-2.5 w-2.5" />
                  <span className="text-slate-300">{sym}</span>
                  <span className="ml-auto text-xs text-slate-500">{age == null ? "no cache" : `${fmt(age, 1)}h`}</span>
                </li>
              ))}
            </ul>
            <p className="mt-2 text-xs text-slate-500">
              Earnings cache: {data?.earnings_cache?.entries ?? 0} entries
              {data?.earnings_cache?.oldest_fetched_at && ` · oldest ${data.earnings_cache.oldest_fetched_at.slice(0, 16).replace("T", " ")}`}
              <br />
              Dividends cache: {data?.dividends_cache?.entries ?? 0} entries
              {data?.dividends_cache?.oldest_fetched_at && ` · oldest ${data.dividends_cache.oldest_fetched_at.slice(0, 16).replace("T", " ")}`}
            </p>
          </div>
        </div>
      )}
    </Card>
  );
}
