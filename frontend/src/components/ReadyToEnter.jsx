import React from "react";
import { api } from "../api.js";
import { Card, Pill, StaleBadge, Spinner, Loading, ErrorState, StockLights, fmt, useApi } from "./ui.jsx";

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

export default function ReadyToEnter({ onSelectStock, refreshKey }) {
  const { data, error, loading, reload } = useApi(api.scanReady, [refreshKey], null);
  const [showMisses, setShowMisses] = React.useState(false);
  // Which tickers are mid live-scan — the tiered poller doesn't quote off-deck
  // names, so a stale row can force its own live quote+bars pull and re-scan.
  const [rescanning, setRescanning] = React.useState(new Set());
  const [rescanError, setRescanError] = React.useState(null);

  const liveScan = React.useCallback(async (tickers) => {
    const set = new Set(tickers.map((t) => t.toUpperCase()));
    setRescanError(null);
    setRescanning((prev) => new Set([...prev, ...set]));
    try {
      await api.refreshReadyQuote(tickers);
      await reload(); // re-run the scan so fresh names move out of stale-blocked
    } catch (e) {
      setRescanError(e.message);
    } finally {
      setRescanning((prev) => {
        const next = new Set(prev);
        set.forEach((t) => next.delete(t));
        return next;
      });
    }
  }, [reload]);

  if (loading && !data) return <Card title="Ready to Enter"><Loading label="Scanning the universe…" /></Card>;
  if (error) return <Card title="Ready to Enter"><ErrorState error={error} onRetry={reload} /></Card>;

  const ready = data?.ready || [];
  const misses = data?.near_misses || [];
  // GO candidates refused because an input datum is stale beyond its tier
  // tolerance (STALE_BLOCKS_GO): unknown-fresh data blocks entry, never permits it.
  const staleBlocked = data?.stale_blocked || [];

  return (
    <Card
      title={`Ready to Enter${ready.length ? ` — ${ready.length}` : ""}`}
      right={
        <span className="flex items-center gap-2 text-xs text-slate-500">
          <StaleBadge
            stale={staleBlocked.length > 0}
            label={`${staleBlocked.length} stale-blocked`}
            title="GO candidates withheld — a data input is stale beyond its tier tolerance"
          />
          <span>Level 3 + 4 (GO) + Level 5 (Account &amp; Juice)</span>
        </span>
      }
    >
      {ready.length === 0 ? (
        <p className="text-sm text-slate-500">Nothing clears every level right now.</p>
      ) : (
        <div className="flex flex-wrap gap-2">
          {ready.map((r) => (
            <button
              key={r.ticker}
              onClick={() => onSelectStock?.(r.ticker)}
              title={`${r.sector || ""} · juice ${fmt(r.juice_weekly_pct, 2)}%/wk · lights 4/4 green · right spot ✓`}
              className="flex items-center gap-2 rounded-lg border border-emerald-600/50 bg-emerald-500/10 px-3 py-1.5 text-sm font-semibold text-emerald-300 hover:bg-emerald-500/20"
            >
              {r.ticker}
              {/* The four Genius lights (a ready name is 4/4 green + in the right spot). */}
              {r.lights ? <StockLights lights={r.lights} size="h-2.5 w-2.5" /> : null}
              <span className="text-xs font-normal text-emerald-400/80">{fmt(r.juice_weekly_pct, 2)}%/wk</span>
            </button>
          ))}
        </div>
      )}

      {staleBlocked.length > 0 && (
        <>
          <div className="mt-3 flex items-center justify-between">
            <span className="text-xs text-slate-500">Held on stale inputs — pull a live quote to re-check.</span>
            <button
              onClick={() => liveScan(staleBlocked.map((r) => r.ticker))}
              disabled={rescanning.size > 0}
              className="flex items-center gap-1.5 rounded-md border border-amber-600/50 px-2.5 py-1 text-xs font-semibold text-amber-300 hover:bg-amber-500/10 disabled:opacity-50"
            >
              {rescanning.size > 0 && <Spinner size="h-3 w-3" />}
              Live-scan all
            </button>
          </div>
          {rescanError && <p className="mt-1 text-xs text-rose-400">{rescanError}</p>}
          <ul className="mt-2 space-y-1">
            {staleBlocked.map((r) => {
              const busy = rescanning.has(r.ticker.toUpperCase());
              return (
                <li key={r.ticker} className="flex items-center gap-2 rounded-lg bg-amber-950/30 px-3 py-1.5 text-sm">
                  <Pill status="wait">{r.ticker}</Pill>
                  <StaleBadge
                    stale
                    title={(r.stale_inputs || [])
                      .map((s) => `${s.kind}: ${s.reason}${s.age_seconds != null ? ` (${Math.round(s.age_seconds)}s)` : ""}`)
                      .join(" · ")}
                  />
                  <span className="text-xs text-amber-300/80">
                    held — stale {(r.stale_inputs || []).map((s) => s.kind).join(", ")}
                  </span>
                  <button
                    onClick={() => liveScan([r.ticker])}
                    disabled={busy || rescanning.size > 0}
                    title="Force a live quote + bars pull for this name and re-check"
                    className="ml-auto flex items-center gap-1 rounded-md border border-slate-700 px-2 py-0.5 text-xs text-slate-200 hover:bg-slate-800 disabled:opacity-50"
                  >
                    {busy ? <Spinner size="h-3 w-3" /> : <span aria-hidden>↻</span>}
                    Live scan
                  </button>
                </li>
              );
            })}
          </ul>
        </>
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
                  {r.lights ? <StockLights lights={r.lights} size="h-2.5 w-2.5" /> : null}
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
