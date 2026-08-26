import React from "react";
import { api } from "../api.js";
import { Card, Pill, StaleBadge, Spinner, Loading, ErrorState, StockLights, ChartLink, fmt, useApi } from "./ui.jsx";

// The RANKED entry shortlist. The scan is a thin hard floor plus a ranker: a name
// is here because it cleared every veto, and its POSITION is its rank. The rank
// blocks nothing — a low-scoring name is still listed, near the bottom, because
// "here is the best available and it is not very good" is a different and more
// useful claim than the empty list the old serial filter produced.
//
// THE PRESSURE GUARD (§1.5) is this component's main job, and it is why the
// header reads "N eligible of M evaluated" before anything else:
//
//   * zero eligible renders as a NORMAL outcome, not an error or an empty state;
//   * nothing is auto-selected, pre-filled, or visually flagged as the action to
//     take — #1 gets no accent, no border, no "recommended" chip;
//   * a rank is NEVER shown without its absolute score beside it. "Best available"
//     and "good" are different claims and this panel must not be able to imply
//     the second while showing the first.

const REASON_LABELS = {
  regime_red: "regime RED",
  rs3m_vs_spy: "RS3M vs SPY negative",
  close_below_ma50: "below the 50-day",
  close_below_ma200: "below the 200-day",
  line_in_the_sand: "below your line",
  earnings_in_cycle: "earnings in cycle",
  no_weeklies: "no weeklies",
  untradeable_spread: "spread too wide",
  stale_inputs: "stale inputs",
  cash_reserve: "cash reserve",
  position_limit: "position limit",
  capital_limit: "capital cap",
  sector_concentration: "sector cap",
  round_lot_size: "lot size",
};

const label = (id) => REASON_LABELS[id] || id;

// Route badge — advisory only. This says how the entry would be made, never that
// it should be. No order is constructed from it anywhere.
function RouteBadge({ route }) {
  if (!route?.route) return null;
  const put = route.route === "CASH_SECURED_PUT";
  return (
    <span
      title={put
        ? `Extended ${fmt(route.detail?.extension_atr, 2)} ATR above MA21 (bar ${fmt(route.detail?.threshold, 2)}) — sell a weekly put at the MA21 zone and be paid to wait`
        : `Within ${fmt(route.detail?.threshold, 2)} ATR of MA21 — the price is available today`}
      className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
        put ? "bg-violet-500/15 text-violet-300" : "bg-slate-700/60 text-slate-300"
      }`}
    >
      {put ? "put" : "shares"}
    </span>
  );
}

export default function ReadyToEnter({ onSelectStock, refreshKey, scanRunning }) {
  const { data, error, loading, reload } = useApi(
    () => (scanRunning ? Promise.resolve(null) : api.scanReady()),
    [refreshKey, scanRunning], null);
  const [showBlocked, setShowBlocked] = React.useState(false);
  const [rescanning, setRescanning] = React.useState(new Set());
  const [rescanError, setRescanError] = React.useState(null);

  const liveScan = React.useCallback(async (tickers) => {
    const set = new Set(tickers.map((t) => t.toUpperCase()));
    setRescanError(null);
    setRescanning((prev) => new Set([...prev, ...set]));
    try {
      await api.refreshReadyQuote(tickers);
      await reload();
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

  if (loading && !data) return <Card title="Entry candidates"><Loading label="Scanning the universe…" /></Card>;
  if (error) return <Card title="Entry candidates"><ErrorState error={error} onRetry={reload} /></Card>;

  // A sweep still running renders as a LOADING state and never as an empty list:
  // "the scan has not finished" and "nothing is eligible" are different facts,
  // and showing the first as the second would read as a verdict never reached.
  if (scanRunning || !data || data.scan_pending) {
    return (
      <Card title="Entry candidates">
        <Loading label="Scanning the universe… results appear when the sweep lands." />
      </Card>
    );
  }

  const eligible = data?.eligible || [];
  const blocked = data?.blocked || [];
  const counts = data?.eligible_of_evaluated || { eligible: eligible.length, evaluated: 0 };
  const stale = blocked.filter((r) => (r.blocked_by || []).includes("stale_inputs"));

  return (
    <Card
      title="Entry candidates"
      right={
        // The pressure-guard headline. Deliberately the most prominent thing in
        // the header and deliberately neutral in tone at zero.
        <span className="flex items-center gap-2 text-xs">
          <span className="font-semibold tabular-nums text-slate-200">
            {counts.eligible} eligible
          </span>
          <span className="text-slate-500">of {counts.evaluated} evaluated</span>
        </span>
      }
    >
      {eligible.length === 0 ? (
        // NOT an error state. A day with nothing eligible is a normal day.
        <p className="text-sm text-slate-400">
          Nothing is eligible today. That is a normal outcome — the veto set is
          thin, so an empty list means the market, not the filter.
        </p>
      ) : (
        <ul className="space-y-1">
          {eligible.map((r) => (
            <li
              key={r.ticker}
              // Uniform styling for every rank. #1 gets no accent, no highlight
              // and no "take this" affordance — see the pressure guard above.
              className="flex items-center gap-2 rounded-lg bg-slate-950/60 px-3 py-1.5 text-sm hover:bg-slate-900/60"
            >
              <span className="w-6 shrink-0 text-right text-xs tabular-nums text-slate-500">
                {r.rank}
              </span>
              <button
                onClick={() => onSelectStock?.(r.ticker)}
                className="font-semibold text-slate-100 hover:text-sky-300"
              >
                {r.ticker}
              </button>
              <ChartLink ticker={r.ticker} size="h-3.5 w-3.5" />
              {/* The absolute score ALWAYS travels with the rank. */}
              <span
                title="Rank score 0–10. A rank orders the eligible; it does not make a name good."
                className="rounded bg-slate-800 px-1.5 py-0.5 text-xs font-semibold tabular-nums text-slate-200"
              >
                {fmt(r.score, 1)}
              </span>
              <RouteBadge route={r.route} />
              {r.lights ? <StockLights lights={r.lights} size="h-2.5 w-2.5" /> : null}
              <span className="ml-auto text-xs tabular-nums text-slate-400">
                {fmt(r.juice_weekly_pct, 2)}%/wk
              </span>
              <span className="text-xs text-slate-600">{r.sector}</span>
            </li>
          ))}
        </ul>
      )}

      {stale.length > 0 && (
        <div className="mt-3 flex items-center justify-between">
          <span className="text-xs text-slate-500">
            {stale.length} held on stale inputs — pull a live quote to re-check.
          </span>
          <button
            onClick={() => liveScan(stale.map((r) => r.ticker))}
            disabled={rescanning.size > 0}
            className="flex items-center gap-1.5 rounded-md border border-amber-600/50 px-2.5 py-1 text-xs font-semibold text-amber-300 hover:bg-amber-500/10 disabled:opacity-50"
          >
            {rescanning.size > 0 && <Spinner size="h-3 w-3" />}
            Live-scan all
          </button>
        </div>
      )}
      {rescanError && <p className="mt-1 text-xs text-rose-400">{rescanError}</p>}

      {blocked.length > 0 && (
        <>
          <button
            onClick={() => setShowBlocked((s) => !s)}
            className="mt-3 text-xs text-slate-500 hover:text-slate-300"
          >
            {showBlocked ? "Hide" : "Show"} blocked — a veto stopped these ({blocked.length})
          </button>
          {showBlocked && (
            <ul className="mt-2 space-y-1">
              {blocked.map((r) => (
                <li key={r.ticker} className="flex items-center gap-2 rounded-lg bg-slate-950/60 px-3 py-1.5 text-sm">
                  <Pill status="avoid">{r.ticker}</Pill>
                  <ChartLink ticker={r.ticker} size="h-3.5 w-3.5" />
                  <span className="text-xs text-slate-500">{r.sector}</span>
                  {(r.blocked_by || []).includes("stale_inputs") && <StaleBadge stale />}
                  {/* The veto that stopped it. There is deliberately no override
                      control here — the structural vetoes carry no override path. */}
                  <span className="ml-auto text-xs text-rose-300">
                    {(r.blocked_by || []).map(label).join(", ")}
                  </span>
                  {(r.blocked_by || []).includes("stale_inputs") && (
                    <button
                      onClick={() => liveScan([r.ticker])}
                      disabled={rescanning.has(r.ticker.toUpperCase()) || rescanning.size > 0}
                      title="Force a live quote + bars pull for this name and re-check"
                      className="flex items-center gap-1 rounded-md border border-slate-700 px-2 py-0.5 text-xs text-slate-200 hover:bg-slate-800 disabled:opacity-50"
                    >
                      {rescanning.has(r.ticker.toUpperCase()) ? <Spinner size="h-3 w-3" /> : <span aria-hidden>↻</span>}
                      Live scan
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </Card>
  );
}
