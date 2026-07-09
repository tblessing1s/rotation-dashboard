import React from "react";
import { api } from "../api.js";
import { Card, Meter, StaleBadge, money, fmt } from "./ui.jsx";

// Coverage → threshold color. healthy = emerald, marginal = amber, flagged/low = rose.
// low_extrinsic (capped display) is paired with the delta/assignment indicators the
// panel already shows — a near-zero denominator must never read as "healthy".
const COV_TONE = {
  healthy: { bar: "bg-emerald-500", text: "text-emerald-300", label: "Healthy" },
  marginal: { bar: "bg-amber-500", text: "text-amber-300", label: "Marginal" },
  flagged: { bar: "bg-rose-500", text: "text-rose-300", label: "Flagged" },
  low_extrinsic: { bar: "bg-slate-500", text: "text-slate-300", label: "Low extrinsic" },
  unknown: { bar: "bg-slate-600", text: "text-slate-400", label: "—" },
};

// One headline metric card, matching the Overview KPI card pattern.
function MetricCard({ label, value, tone = "text-slate-100", sub }) {
  return (
    <div className="min-w-0 rounded-lg border border-slate-800 bg-slate-900/60 p-3">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`mt-1 text-xl font-semibold leading-tight ${tone}`}>{value}</div>
      {sub && <div className="text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

// Weekly juice-vs-burn bars: two bars per week (juice / burn). Realized weeks are
// full-opacity; projected weeks (forward to the planned exit) are lighter. Reuses
// the flex-div bar idiom from HistoryTab (no chart library).
function WeeklyBars({ weekly }) {
  if (!weekly || !weekly.length) {
    return <p className="text-xs text-slate-500">No weekly marks yet — the first lands at the next end-of-week snapshot.</p>;
  }
  const maxVal = Math.max(1, ...weekly.map((w) => Math.max(Math.abs(w.juice || 0), Math.abs(w.burn || 0))));
  return (
    <div>
      <div className="flex items-end gap-1.5" style={{ height: 96 }}>
        {weekly.map((w, i) => {
          const jh = Math.max((Math.abs(w.juice || 0) / maxVal) * 100, 2);
          const bh = Math.max((Math.abs(w.burn || 0) / maxVal) * 100, 2);
          const op = w.projected ? "opacity-40" : "";
          return (
            <div key={i} className="flex flex-1 flex-col items-center gap-0.5">
              <div className="flex w-full items-end justify-center gap-0.5" style={{ height: 80 }}>
                <div className={`w-1/2 rounded-t bg-emerald-500/80 ${op}`} style={{ height: `${jh}%` }}
                     title={`juice ${money(w.juice)}/wk`} />
                <div className={`w-1/2 rounded-t bg-rose-500/70 ${op}`} style={{ height: `${bh}%` }}
                     title={`burn ${money(w.burn)}/wk`} />
              </div>
              <span className="text-[9px] text-slate-600">{w.label}</span>
            </div>
          );
        })}
      </div>
      <div className="mt-1 flex gap-3 text-[10px] text-slate-500">
        <span><span className="inline-block h-2 w-2 rounded-sm bg-emerald-500/80" /> juice</span>
        <span><span className="inline-block h-2 w-2 rounded-sm bg-rose-500/70" /> burn</span>
        <span className="text-slate-600">lighter = projected</span>
      </div>
    </div>
  );
}

// Full theta-burn panel for one open position: three headline cards
// (Juice/wk · Burn/wk with a trend arrow vs last week · Net/wk), a coverage meter
// with threshold coloring, the weekly juice-vs-burn bars, a hold-extension readout,
// and a staleness/divergence badge. Fetched lazily per position (mirrors
// DeltaCoverage) so the Positions tab only pays for it when a card is shown.
export default function BurnPanel({ ticker, health }) {
  const [d, setD] = React.useState(null);
  const [err, setErr] = React.useState(null);
  React.useEffect(() => {
    let live = true;
    api.burn(ticker).then((r) => live && setD(r)).catch((e) => live && setErr(String(e.message || e)));
    return () => { live = false; };
  }, [ticker]);

  if (!health) return null;

  // Headline figures come from leap_health (immediate); the weekly series +
  // divergence + trend come from the lazy fetch.
  const juice = health.trailing_avg_weekly_juice;
  const burn = health.model_burn_per_week;
  const net = health.net_juice_per_week;
  const cov = health.coverage || {};
  const proj = health.burn_projection || {};
  // Take-home over the planned hold: projected juice across the remaining weeks
  // minus the model extrinsic burn to the exit DTE. All three come off the burn
  // projection; null when it isn't priceable (Schwab off / off-hours).
  const weeks = proj.weeks_remaining;
  const juiceTotal = juice != null && weeks != null ? juice * weeks : null;
  const burnTotal = proj.projected_burn_total;
  const takeHome = juiceTotal != null && burnTotal != null ? juiceTotal - burnTotal : null;
  const netTone = net == null ? "text-slate-400" : net >= 0 ? "text-emerald-300" : "text-rose-300";
  const covTone = COV_TONE[cov.status] || COV_TONE.unknown;
  const covPct = cov.ratio != null ? Math.min(100, (cov.ratio / (cov.healthy || 3)) * 100) : 0;

  // Burn trend vs the prior realized week (from the mark series).
  const realized = (d?.weekly || []).filter((w) => !w.projected);
  const lastTwo = realized.slice(-2);
  const trend = lastTwo.length === 2 ? lastTwo[1].burn - lastTwo[0].burn : null;
  const trendArrow = trend == null ? "" : trend > 0.5 ? " ▲" : trend < -0.5 ? " ▼" : "";
  const trendTone = trend > 0.5 ? "text-rose-300" : trend < -0.5 ? "text-emerald-300" : "text-slate-400";

  const div = d?.divergence;
  // Inputs behind the projection are stale if the priced spot/DTE couldn't refresh.
  const stale = proj.priceable === false;

  return (
    <div className="mt-4 border-t border-slate-800 pt-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs uppercase tracking-wide text-slate-500">Theta burn &amp; net juice</span>
        <span className="flex items-center gap-1.5">
          <StaleBadge stale={stale} title="Projection inputs (spot/IV) could not refresh — burn figures may be stale" />
          {div?.warn && (
            <span title={`Realized burn is diverging from projection by ${fmt(div.mean_abs_divergence_pct, 1)}% (trailing), over the ${div.threshold_pct}% warn threshold — a live check on the pricing model.`}
                  className="cursor-help rounded-full border border-amber-500/50 bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold text-amber-300">
              MODEL DRIFT {fmt(div.mean_abs_divergence_pct, 0)}%
            </span>
          )}
          {proj.extended && (
            <span title="Held past the planned exit — projection window slid forward" className="rounded-full border border-slate-600 bg-slate-700/40 px-2 py-0.5 text-[10px] font-semibold text-slate-300">
              PAST PLAN
            </span>
          )}
        </span>
      </div>

      <div className="grid grid-cols-3 gap-2">
        <MetricCard label="Juice / wk" value={juice == null ? "—" : `${money(juice)}`} tone="text-emerald-300" />
        <MetricCard label="Burn / wk" value={burn == null ? "—" : <span>{money(burn)}<span className={trendTone}>{trendArrow}</span></span>}
                    tone="text-rose-300" sub={proj.exit_slippage_est != null ? `incl. ${money(proj.exit_slippage_est)} exit slip` : null} />
        <MetricCard label="Net / wk" value={net == null ? "—" : `${net >= 0 ? "+" : ""}${money(net)}`} tone={netTone} />
      </div>

      {takeHome != null && (
        <div className="mt-3 rounded-lg border border-slate-800 bg-slate-900/40 p-2.5">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[11px] uppercase tracking-wide text-slate-500">
              Take-home over the hold{proj.planned_exit_dte != null ? ` — to ${proj.planned_exit_dte} DTE` : ""}
            </span>
            <span className="text-[11px] text-slate-500">~{fmt(weeks, 0)} wk left</span>
          </div>
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-sm">
            <span className="font-semibold text-emerald-300">+{money(juiceTotal)}</span>
            <span className="text-xs text-slate-500">juice</span>
            <span className="text-slate-600">−</span>
            <span className="font-semibold text-rose-300">{money(burnTotal)}</span>
            <span className="text-xs text-slate-500">burn</span>
            <span className="text-slate-600">=</span>
            <span className={`text-base font-semibold ${takeHome >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
              {takeHome >= 0 ? "+" : "−"}{money(Math.abs(takeHome))}
            </span>
            <span className="text-xs text-slate-500">take-home</span>
          </div>
          <div className="mt-1 text-[10px] text-slate-600">
            Projected: trailing juice × weeks left, net of the extrinsic burned to exit. The
            extrinsic still in the LEAP at exit comes back on the sale — it isn't counted here.
          </div>
        </div>
      )}

      <div className="mt-3">
        <div className="mb-1 flex items-center justify-between text-xs">
          <span className="text-slate-500">Coverage (juice ÷ burn)</span>
          <span className={covTone.text}>
            {cov.ratio == null ? "—" : `${cov.capped ? "≥" : ""}${fmt(cov.ratio, 1)}×`} · {covTone.label}
          </span>
        </div>
        <Meter pct={covPct} tone={covTone.bar} />
        <div className="mt-1 text-[10px] text-slate-600">
          healthy ≥ {fmt(cov.healthy, 1)}× · marginal ≥ {fmt(cov.marginal, 1)}×
          {cov.status === "low_extrinsic" && " · burn ~0 (deep-ITM) — read with delta/assignment risk"}
        </div>
      </div>

      <div className="mt-3">
        <div className="mb-1 text-xs text-slate-500">
          Weekly juice vs burn {d?.planned_exit_dte != null && <span className="text-slate-600">(plan exit {d.planned_exit_dte} DTE)</span>}
        </div>
        {err ? <p className="text-xs text-rose-400">{err}</p> : <WeeklyBars weekly={d?.weekly} />}
      </div>

      {Array.isArray(health.extension_preview) && health.extension_preview.length > 0 && burn != null && (
        <div className="mt-3 rounded-lg border border-slate-800 bg-slate-900/40 p-2 text-xs text-slate-400">
          <span className="text-slate-500">Hold-extension cost: </span>
          {health.extension_preview.map((e, i) => (
            <span key={i}>
              {i > 0 && " · "}+{e.extra_weeks}wk{" "}
              <span className="font-semibold text-slate-200">{money(e.burn_per_week_with_slippage)}/wk</span>
            </span>
          ))}
          <span className="text-slate-600"> (vs {money(burn)}/wk now)</span>
        </div>
      )}
    </div>
  );
}
