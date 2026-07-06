import React from "react";
import { api } from "../api.js";
import { Card, Pill, Loading, ErrorState, fmt, useApi } from "./ui.jsx";

// Ready-to-enter shortlist: tickers that clear the Scorecard's GO verdict
// (Level 3 beats peers + Level 4 consolidating + the scorecard's own
// CFM-suitability rules) AND the Level 5 Account & Juice gate, right now.
// Level 1 (regime) / Level 2 (sector) are deliberately excluded — same as the
// Scorecard verdict — so this stays useful even on a yellow/red tape; RED
// still hard-blocks actual execution regardless of what's listed here.

const REASON_LABELS = {
  cash_reserve: "cash reserve",
  position_limit: "position limit",
  capital_limit: "capital cap",
  sector_concentration: "sector cap",
  juice_adequacy: "juice too thin",
};

function reasonList(l5) {
  return (l5?.blocking_failures || []).map((id) => REASON_LABELS[id] || id).join(", ");
}

export default function ReadyToEnter({ onSelectStock }) {
  const { data, error, loading, reload } = useApi(api.scanReady, [], null);
  const [showMisses, setShowMisses] = React.useState(false);

  if (loading && !data) return <Card title="Ready to Enter"><Loading label="Scanning the universe…" /></Card>;
  if (error) return <Card title="Ready to Enter"><ErrorState error={error} onRetry={reload} /></Card>;

  const ready = data?.ready || [];
  const misses = data?.near_misses || [];

  return (
    <Card
      title={`Ready to Enter${ready.length ? ` — ${ready.length}` : ""}`}
      right={<span className="text-xs text-slate-500">Level 3 + 4 (GO) + Level 5 (Account &amp; Juice)</span>}
    >
      {ready.length === 0 ? (
        <p className="text-sm text-slate-500">Nothing clears every level right now.</p>
      ) : (
        <div className="flex flex-wrap gap-2">
          {ready.map((r) => (
            <button
              key={r.ticker}
              onClick={() => onSelectStock?.(r.ticker)}
              title={`${r.sector || ""} · juice ${fmt(r.juice_weekly_pct, 2)}%/wk`}
              className="flex items-center gap-2 rounded-lg border border-emerald-600/50 bg-emerald-500/10 px-3 py-1.5 text-sm font-semibold text-emerald-300 hover:bg-emerald-500/20"
            >
              {r.ticker}
              <span className="text-xs font-normal text-emerald-400/80">{fmt(r.juice_weekly_pct, 2)}%/wk</span>
            </button>
          ))}
        </div>
      )}

      {misses.length > 0 && (
        <>
          <button
            onClick={() => setShowMisses((s) => !s)}
            className="mt-3 text-xs text-slate-500 hover:text-slate-300"
          >
            {showMisses ? "Hide" : "Show"} near misses — cleared 3 &amp; 4, blocked on Level 5 ({misses.length})
          </button>
          {showMisses && (
            <ul className="mt-2 space-y-1">
              {misses.map((r) => (
                <li key={r.ticker} className="flex items-center gap-2 rounded-lg bg-slate-950/60 px-3 py-1.5 text-sm">
                  <Pill status="avoid">{r.ticker}</Pill>
                  <span className="text-xs text-slate-500">{r.sector}</span>
                  <span className="ml-auto text-xs text-rose-300">{reasonList(r.level5)}</span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </Card>
  );
}
