import React from "react";
import { api } from "../api.js";
import { Card, Spinner, ErrorState, Stat, useApi } from "./ui.jsx";

// Dry-powder cash-secured-put shadow sleeve (csp_dry_powder.py) — a second,
// distinct income sweep run nightly on idle cash that cannot fund a new full
// CFM position. SHADOW ONLY: it never places an order, never touches
// state.json or positions. Until this view existed the sweep ran nightly
// with nowhere to look at what it found.

const asPct = (r) => (r == null ? null : `${r.toFixed(1)}%`);
const asMoney = (n) => (n == null ? "—" : `$${Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 })}`);

const TIER_LABEL = { quality: "Quality (clears today's live gate)", general: "General (extended, not held, has weeklies)" };

function TradeRow({ t, resolved }) {
  const outcome = t.outcome;
  return (
    <tr className="border-t border-slate-800 text-slate-200">
      <td className="py-1.5 pr-3 font-mono">{t.ticker}</td>
      <td className="py-1.5 pr-3 text-[11px] text-slate-400">{TIER_LABEL[t.tier] ? t.tier : t.tier || "—"}</td>
      <td className="py-1.5 pr-3 font-mono text-[11px] text-slate-400">{t.opened_date}</td>
      <td className="py-1.5 pr-3 font-mono text-[11px] text-slate-400">{t.expiration} ({t.dte}d)</td>
      <td className="py-1.5 pr-3 text-right font-mono">{t.strike}</td>
      <td className="py-1.5 pr-3 text-right font-mono text-slate-400">{t.abs_delta != null ? t.abs_delta.toFixed(2) : "—"}</td>
      <td className="py-1.5 pr-3 text-right font-mono">{t.contracts}</td>
      <td className="py-1.5 pr-3 text-right font-mono">{asMoney(t.collateral)}</td>
      <td className="py-1.5 pr-3 text-right font-mono font-semibold text-emerald-300">
        {t.annualized_yield_pct != null ? `${t.annualized_yield_pct}%` : "—"}
      </td>
      {resolved && (
        <td className="py-1.5 text-right">
          {outcome?.assigned ? (
            <span
              title={outcome.classification === "would_pass_standard_entry"
                ? "Assigned; today's live gate would still call this a standard entry."
                : outcome.classification === "entered_on_weakness_flag_for_review"
                ? "Assigned; today's live gate would NOT call this a standard entry — flagged for manual review."
                : "Assigned."}
              className="rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-300"
            >
              assigned
            </span>
          ) : (
            <span className="rounded border border-emerald-500/40 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-300">
              expired
            </span>
          )}
        </td>
      )}
    </tr>
  );
}

function TradeTable({ trades, resolved }) {
  if (!trades?.length) {
    return (
      <p className="text-[11px] text-slate-500">
        {resolved ? "No trades have reached expiration yet." : "No open shadow positions right now."}
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[720px] text-sm">
        <thead>
          <tr className="text-left text-[10px] uppercase tracking-wide text-slate-500">
            <th className="py-1 pr-3">Ticker</th>
            <th className="py-1 pr-3">Tier</th>
            <th className="py-1 pr-3">Opened</th>
            <th className="py-1 pr-3">Expiration</th>
            <th className="py-1 pr-3 text-right">Strike</th>
            <th className="py-1 pr-3 text-right">|Δ|</th>
            <th className="py-1 pr-3 text-right">Contracts</th>
            <th className="py-1 pr-3 text-right">Collateral</th>
            <th className="py-1 pr-3 text-right">Annualized</th>
            {resolved && <th className="py-1 text-right">Outcome</th>}
          </tr>
        </thead>
        <tbody>
          {trades.map((t, i) => (
            <TradeRow key={`${t.ticker}-${t.opened_date}-${t.expiration}-${i}`} t={t} resolved={resolved} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

const WINDOWS = [
  { label: "All", value: null },
  { label: "30d", value: 30 },
  { label: "90d", value: 90 },
];

export default function DryPowderSleeve() {
  const [days, setDays] = React.useState(null);
  const { data, error, loading, reload } = useApi(
    () => api.cspDryPowderSummary(days), [days], null);

  return (
    <Card
      title="Dry-powder CSP sleeve"
      right={
        <div className="flex items-center gap-2">
          <span className="rounded bg-violet-500/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-violet-300">
            shadow only
          </span>
          {WINDOWS.map((w) => (
            <button
              key={w.label}
              onClick={() => setDays(w.value)}
              className={`rounded border px-2 py-0.5 text-[11px] ${
                days === w.value
                  ? "border-sky-500/50 bg-sky-500/15 text-sky-200"
                  : "border-slate-700 text-slate-400 hover:text-slate-200"
              }`}
            >
              {w.label}
            </button>
          ))}
          <button onClick={reload} className="text-[11px] text-slate-400 hover:text-slate-200">
            ↻
          </button>
        </div>
      }
    >
      <p className="mb-3 text-xs text-slate-400">
        A second, distinct income sweep the engine runs nightly on cash that
        can't fund a new full CFM position — short-duration puts, furthest-OTM
        inside a delta band, sized from leftover cash only. It never places an
        order or touches the real book; this is a record of what it found.
      </p>

      {loading && <Spinner />}
      {error && <ErrorState error={error} onRetry={reload} />}

      {data && !data.enabled && (
        <p className="mb-3 rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200">
          Disabled — CFM_DRY_POWDER_SCAN=0 in this environment. The nightly
          sweep is not running; the history below (if any) predates that.
        </p>
      )}

      {data && (
        <>
          <div className="mb-4 grid grid-cols-2 gap-4 rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-3 sm:grid-cols-4">
            <Stat label="Candidates scanned" value={data.candidates.toLocaleString()} sub={`over ${data.days} day(s)`} />
            <Stat label="Shadow trades" value={data.shadow_trades_total.toLocaleString()} />
            <Stat
              label="Assignment rate"
              value={data.assignment_rate == null ? "—" : `${data.assignment_rate}%`}
              sub={data.assigned + data.expired ? `${data.assigned} assigned / ${data.expired} expired` : "no resolved trades yet"}
              tone="text-amber-300"
            />
            <Stat
              label="Avg. annualized yield"
              value={data.avg_annualized_yield_pct == null ? "—" : `${data.avg_annualized_yield_pct}%`}
              tone="text-emerald-300"
            />
          </div>

          {!data.candidates ? (
            <p className="text-xs text-slate-500">
              No scans recorded yet. The nightly sweep appends one file per
              day; this fills in over the following days.
            </p>
          ) : (
            <div className="space-y-6">
              <section>
                <h4 className="mb-2 text-xs font-semibold text-slate-300">
                  Open shadow positions
                </h4>
                <TradeTable trades={data.open_trades} resolved={false} />
              </section>

              <section>
                <h4 className="mb-2 text-xs font-semibold text-slate-300">
                  Resolved trades
                </h4>
                <TradeTable trades={data.resolved_trades} resolved />
                <p className="pt-2 text-[11px] text-slate-500">
                  An assignment's re-gate check reads TODAY's live gate stack,
                  not the gate as it stood on the historical date — read it as
                  "would this still look like an entry today", not a backtest.
                </p>
              </section>
            </div>
          )}
        </>
      )}
    </Card>
  );
}
