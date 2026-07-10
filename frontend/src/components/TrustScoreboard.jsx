import React from "react";
import { api } from "../api.js";
import { Card, Loading, ErrorState, fmt, useApi } from "./ui.jsx";
import { useToast } from "./Toast.jsx";

// The recommendation-engine trust scoreboard (Settings): coverage / precision /
// fidelity / graduation per action type, plus the loud lists (coverage misses,
// fidelity failures) and emission timeliness. Everything rendered here is a
// derived, display-only readout — no automation exists, and nothing on this
// panel (or anywhere else) places an order from a recommendation.

const ACTION_ORDER = ["ENTER", "ROLL_OUT", "ROLL_DOWN", "DEFEND", "EXIT"];

const rate = (r) => (r == null ? "—" : `${Math.round(r * 100)}%`);
const ts = (s) => (s ? `${String(s).slice(0, 16).replace("T", " ")}Z` : "—");

// An action type with no resolutions and no graded fidelity yet has nothing to
// say — used to hide ROLL_DOWN until it exists.
function isEmpty(m) {
  const cov = m?.coverage || {};
  const prec = m?.precision || {};
  const fid = m?.fidelity || {};
  return (cov.total_manual_actions || 0) === 0
    && (prec.executed_matched || 0) === 0
    && (prec.overridden || 0) === 0
    && (fid.graded || 0) === 0;
}

function GraduationChip({ grad }) {
  const eligible = !!grad?.eligible;
  return (
    <span
      className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
        eligible
          ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
          : "border-slate-600 bg-slate-800/60 text-slate-400"
      }`}
    >
      {eligible ? "graduation eligible" : "not eligible"}
    </span>
  );
}

function MetricBlock({ label, value, sub }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="text-lg font-semibold leading-tight text-slate-100">{value}</div>
      <div className="text-[11px] text-slate-500">{sub}</div>
    </div>
  );
}

function ActionTypeCard({ actionType, m }) {
  const cov = m.coverage || {};
  const prec = m.precision || {};
  const fid = m.fidelity || {};
  const grad = m.graduation || {};
  const breakdown = Object.entries(prec.override_breakdown || {});
  const failing = grad.failing || [];
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm font-semibold text-slate-200">
          {actionType.replaceAll("_", " ")}
        </span>
        <GraduationChip grad={grad} />
      </div>
      <div className="mt-2 grid grid-cols-3 gap-3">
        <MetricBlock
          label="Coverage" value={rate(cov.rate)}
          sub={`${cov.matched ?? 0}/${cov.total_manual_actions ?? 0} manual actions matched`}
        />
        <MetricBlock
          label="Precision" value={rate(prec.rate)}
          sub={`${prec.executed_matched ?? 0} executed · ${prec.overridden ?? 0} overridden`}
        />
        <MetricBlock
          label="Fidelity" value={rate(fid.rate)}
          sub={`${fid.passed ?? 0}/${fid.graded ?? 0} graded orders passed`}
        />
      </div>
      {breakdown.length > 0 && (
        <div className="mt-2 text-[11px] text-slate-500">
          overrides:{" "}
          {breakdown.map(([code, n]) => `${code} ×${n}`).join(" · ")}
        </div>
      )}
      {grad.window_weeks != null && (
        <div className="mt-1 text-[11px] text-slate-600">
          window {grad.window_weeks}w · live matched {grad.live_matched ?? 0} ·
          matched {grad.matched ?? 0} · overridden {grad.overridden ?? 0} ·
          misses {grad.coverage_misses ?? 0} · override rate{" "}
          {grad.override_rate != null ? `${Math.round(grad.override_rate * 100)}%` : "—"}
        </div>
      )}
      {!grad.eligible && failing.length > 0 && (
        <ul className="mt-2 list-disc space-y-0.5 pl-4 text-[11px] text-slate-500">
          {failing.map((f, i) => <li key={i}>{f}</li>)}
        </ul>
      )}
    </div>
  );
}

// A loud (CRITICAL-styled) list: red when populated, a calm green line when not.
function LoudSection({ title, items, renderItem }) {
  if (!items || items.length === 0) {
    return (
      <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-sm text-emerald-300">
        {title}: none.
      </div>
    );
  }
  return (
    <div className="rounded-lg border border-rose-500/50 bg-rose-500/10 p-3">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-rose-300">
        {title} — {items.length}
      </div>
      <ul className="space-y-1.5 text-sm">{items.map(renderItem)}</ul>
    </div>
  );
}

export default function TrustScoreboard() {
  const toast = useToast();
  const { data, error, loading, reload } = useApi(api.trustScoreboard, [], null);
  const [running, setRunning] = React.useState(false);

  async function runNow() {
    setRunning(true);
    try {
      const r = await api.runRecommendations();
      toast.show(
        `Evaluation pass complete — ${r.emitted ?? 0} emitted, ` +
          `${r.positions_evaluated ?? 0} position(s) + ${r.entry_candidates ?? 0} entry candidate(s) evaluated`,
        { type: "success" },
      );
      await reload();
    } catch (e) {
      toast.show(String(e.message || e), { type: "error" });
    } finally {
      setRunning(false);
    }
  }

  if (loading && !data) return <Card title="Trust scoreboard"><Loading /></Card>;
  if (error) return <Card title="Trust scoreboard"><ErrorState error={error} onRetry={reload} /></Card>;

  const board = data?.scoreboard || {};
  const by = board.by_action_type || {};
  const totals = board.totals || {};
  const tl = board.timeliness || {};

  // Coverage misses, gathered across every action type's loud list.
  const misses = [];
  for (const at of Object.keys(by)) {
    for (const mi of by[at]?.coverage?.misses || []) misses.push(mi);
  }
  misses.sort((a, b) => String(b.at || "").localeCompare(String(a.at || "")));

  const failures = data?.fidelity_failures || [];
  const shownTypes = ACTION_ORDER.filter(
    (at) => by[at] && (at !== "ROLL_DOWN" || !isEmpty(by[at])),
  );

  return (
    <Card
      title="Trust scoreboard — recommendation engine"
      right={
        <div className="flex items-center gap-2 text-xs">
          {board.as_of && <span className="text-slate-500">as of {ts(board.as_of)}</span>}
          <button
            onClick={runNow} disabled={running}
            className="rounded-full border border-emerald-600/50 bg-emerald-500/10 px-2.5 py-1 font-semibold text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-50"
          >
            {running ? "Running…" : "Run evaluation pass now"}
          </button>
        </div>
      }
    >
      {/* The one banner that must never be missed: nothing here trades. */}
      <div
        className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm font-medium text-amber-200"
        title={board.automation_note || undefined}
      >
        Display-only — no automation exists. Reconciliation:{" "}
        {board.reconciliation_status || "NOT_YET_IMPLEMENTED"} (blocks all graduation).
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-400">
        {board.since && <span>tracking since <span className="text-slate-300">{ts(board.since)}</span></span>}
        <span>recommendations <span className="font-semibold text-slate-200">{totals.recommendations ?? 0}</span></span>
        <span>all-clear <span className="font-semibold text-slate-200">{totals.all_clear ?? 0}</span></span>
        <span>open <span className="font-semibold text-slate-200">{board.open_recommendations ?? 0}</span>
          {" "}({board.open_actionable ?? 0} actionable)</span>
        <span className={totals.coverage_misses ? "text-rose-300" : ""}>
          coverage misses <span className="font-semibold">{totals.coverage_misses ?? 0}</span>
        </span>
        <span className={totals.fidelity_failures ? "text-rose-300" : ""}>
          fidelity failures <span className="font-semibold">{totals.fidelity_failures ?? 0}</span>
        </span>
      </div>

      {shownTypes.length > 0 ? (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {shownTypes.map((at) => <ActionTypeCard key={at} actionType={at} m={by[at]} />)}
        </div>
      ) : (
        <p className="mt-3 text-sm text-slate-500">
          No scoreboard data yet — run an evaluation pass to start the record.
        </p>
      )}

      {/* The loud lists — the two failure modes that matter most. */}
      <div className="mt-3 space-y-2">
        <LoudSection
          title="Coverage misses (you acted, the engine hadn't committed)"
          items={misses}
          renderItem={(mi, i) => (
            <li key={i} className="text-rose-100">
              <span className="font-semibold">{mi.ticker}</span>
              {" · "}{(mi.action_type || "").replaceAll("_", " ")}
              {" · "}{ts(mi.at)}
              {(mi.execution_ids || []).length > 0 && (
                <span className="text-rose-300/80"> · exec {(mi.execution_ids || []).join(", ")}</span>
              )}
            </li>
          )}
        />
        <LoudSection
          title="Fidelity failures (staged order broke its contract)"
          items={failures}
          renderItem={(f, i) => {
            const failed = Object.entries(f.checks || {})
              .filter(([, c]) => c && c.status === "FAIL");
            return (
              <li key={f.order_id || i} className="text-rose-100">
                <span className="font-mono text-xs">{f.order_id}</span>
                {" · "}<span className="font-semibold">{f.ticker}</span>
                {f.paper ? <span className="text-rose-300/80"> · paper</span> : null}
                {failed.length > 0 && (
                  <span className="text-rose-300/80">
                    {" · "}
                    {failed.map(([name, c]) => `${name}${c.defect ? ` (${c.defect})` : ""}`).join("; ")}
                  </span>
                )}
              </li>
            );
          }}
        />
      </div>

      {/* Timeliness: is the engine leading the operator, or chasing? */}
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-slate-800 pt-3 text-xs text-slate-400">
        <span className="uppercase tracking-wide text-slate-500">timeliness</span>
        <span>avg emission lag <span className="font-semibold text-slate-200">
          {tl.avg_emission_lag_days != null ? `${fmt(tl.avg_emission_lag_days, 2)}d` : "—"}</span></span>
        <span>max <span className="font-semibold text-slate-200">
          {tl.max_emission_lag_days != null ? `${fmt(tl.max_emission_lag_days, 2)}d` : "—"}</span></span>
        <span className={tl.late_after_action_count ? "text-amber-300" : ""}>
          late-after-action <span className="font-semibold">{tl.late_after_action_count ?? 0}</span>
        </span>
      </div>
    </Card>
  );
}
