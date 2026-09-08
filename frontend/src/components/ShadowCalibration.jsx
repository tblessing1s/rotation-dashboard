import React from "react";
import { api } from "../api.js";
import { Card, Spinner, ErrorState, Stat, useApi } from "./ui.jsx";

// Shadow-metric calibration — the graduation dataset for the SCORE composite,
// the Level-4 structure metrics and the income shadow floors
// (scan_rejection_log.summary()). None of these carry blocking authority
// today; this view exists so that fact can eventually change on evidence
// instead of staying frozen forever for lack of anywhere to look.
//
// READ-ONLY OBSERVABILITY. Nothing here grants authority to a shadow metric —
// graduating one to the gate is a separate, reviewed code change.

const asPct = (r) => (r == null ? null : `${r.toFixed(1)}%`);

function parseBucket(key) {
  const [score, of] = key.split("/").map(Number);
  return { score, of };
}

function StructureTable({ structure }) {
  const entries = Object.entries(structure?.by_score || {});
  if (!entries.length) {
    return (
      <p className="text-[11px] text-slate-500">
        No structure_score reads recorded yet in this window.
      </p>
    );
  }
  // Newest/most-complete buckets first: full 4-of-4 reads before partials,
  // then by score descending within a bucket — highest scores read first.
  entries.sort(([a], [b]) => {
    const pa = parseBucket(a), pb = parseBucket(b);
    return pb.of - pa.of || pb.score - pa.score;
  });
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[480px] text-sm">
        <thead>
          <tr className="text-left text-[10px] uppercase tracking-wide text-slate-500">
            <th className="py-1 pr-3">Score</th>
            <th className="py-1 pr-3 text-right">N</th>
            <th className="py-1 pr-3 text-right">Eligible rate</th>
            <th className="py-1">Verdict breakdown</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([bucket, verdicts]) => {
            const total = Object.values(verdicts).reduce((s, n) => s + n, 0);
            const eligible = verdicts.ELIGIBLE || 0;
            const { of } = parseBucket(bucket);
            return (
              <tr key={bucket} className="border-t border-slate-800 text-slate-200">
                <td className="py-1.5 pr-3 font-mono">
                  {bucket}
                  {of < 4 && (
                    <span
                      title="Partial structure read — fewer than 4 sub-metrics were computable for these rows."
                      className="ml-1 text-[10px] text-amber-400"
                    >
                      partial
                    </span>
                  )}
                </td>
                <td className="py-1.5 pr-3 text-right font-mono text-slate-400">{total}</td>
                <td className="py-1.5 pr-3 text-right font-mono font-semibold text-emerald-300">
                  {asPct((eligible / total) * 100)}
                </td>
                <td className="py-1.5 font-mono text-[11px] text-slate-500">
                  {Object.entries(verdicts)
                    .sort(([, x], [, y]) => y - x)
                    .map(([v, n]) => `${v}:${n}`)
                    .join("  ")}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="pt-2 text-[11px] text-slate-500">
        If higher structure_score buckets show a visibly higher eligible rate
        (or, joined against Travis's manual compelling/not-compelling labels,
        better forward outcomes among ELIGIBLE names), that is the evidence a
        future graduation decision would rest on. Nothing here computes that
        join automatically yet.
        {structure?.partial_reads > 0 &&
          ` ${structure.partial_reads} record(s) in this window are partial reads (fewer than 4 of the 4 sub-metrics).`}
        {structure?.phase_suppressed_volume_cautions > 0 &&
          ` The consolidation-phase flag suppressed a thin-volume CAUTION ${structure.phase_suppressed_volume_cautions} time(s).`}
      </p>
    </div>
  );
}

function ShadowFloorTable({ floor, reasons }) {
  const profiles = Object.entries(floor || {});
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <div>
        <div className="mb-2 text-[10px] uppercase tracking-wide text-slate-500">
          Pass rate, had the floor any authority
        </div>
        {!profiles.length ? (
          <p className="text-[11px] text-slate-500">No shadow-floor reads recorded yet.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-wide text-slate-500">
                <th className="py-1 pr-3">Profile</th>
                <th className="py-1 pr-3 text-right">Pass</th>
                <th className="py-1 pr-3 text-right">Fail</th>
                <th className="py-1 text-right">Pass rate</th>
              </tr>
            </thead>
            <tbody>
              {profiles.map(([profile, agg]) => (
                <tr key={profile} className="border-t border-slate-800 text-slate-200">
                  <td className="py-1.5 pr-3 font-mono">{profile}</td>
                  <td className="py-1.5 pr-3 text-right font-mono text-emerald-300">{agg.pass}</td>
                  <td className="py-1.5 pr-3 text-right font-mono text-rose-300">{agg.fail}</td>
                  <td className="py-1.5 text-right font-mono font-semibold text-slate-100">
                    {asPct(agg.pass_rate)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div>
        <div className="mb-2 text-[10px] uppercase tracking-wide text-slate-500">
          Why the floor would have failed
        </div>
        {!Object.keys(reasons || {}).length ? (
          <p className="text-[11px] text-slate-500">No floor-fail reasons recorded yet.</p>
        ) : (
          <ul className="space-y-1 text-[11px] text-slate-400">
            {Object.entries(reasons).map(([reason, n]) => (
              <li key={reason} className="flex items-center justify-between gap-3">
                <span className="truncate font-mono">{reason}</span>
                <span className="font-mono text-slate-300">{n}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function BindingBars({ counts, total }) {
  const entries = Object.entries(counts || {});
  if (!entries.length) {
    return <p className="text-[11px] text-slate-500">No rejections recorded — every read row was ELIGIBLE.</p>;
  }
  const max = Math.max(...entries.map(([, n]) => n));
  return (
    <div className="space-y-1.5">
      {entries.slice(0, 12).map(([reason, n]) => (
        <div key={reason} className="flex items-center gap-2">
          <span className="w-48 shrink-0 truncate text-right font-mono text-[11px] text-slate-400" title={reason}>
            {reason}
          </span>
          <span className="h-3 rounded-sm bg-rose-500/50" style={{ width: `${Math.max(2, (n / max) * 100)}%` }} />
          <span className="font-mono text-[11px] text-slate-500">
            {n}
            {total ? ` (${asPct((n / total) * 100)})` : ""}
          </span>
        </div>
      ))}
      {entries.length > 12 && (
        <p className="pt-1 text-[11px] text-slate-500">+{entries.length - 12} more binding constraint(s), not shown.</p>
      )}
    </div>
  );
}

const WINDOWS = [
  { label: "All", value: null },
  { label: "30", value: 30 },
  { label: "90", value: 90 },
  { label: "180", value: 180 },
];

export default function ShadowCalibration() {
  const [window_, setWindow] = React.useState(null);
  const { data, error, loading, reload } = useApi(
    () => api.scanRejectionStats(window_), [window_], null);

  return (
    <Card
      title="Shadow-metric calibration"
      right={
        <div className="flex items-center gap-2">
          <span className="rounded bg-violet-500/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-violet-300">
            no authority
          </span>
          <span className="text-[10px] uppercase tracking-wide text-slate-500">Records/symbol</span>
          {WINDOWS.map((w) => (
            <button
              key={w.label}
              onClick={() => setWindow(w.value)}
              className={`rounded border px-2 py-0.5 text-[11px] ${
                window_ === w.value
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
        The empirical dataset behind the SCORE composite, the Level-4 structure
        metrics and the income shadow floors —{" "}
        <span className="font-semibold text-violet-300">scan_triggers.shadow_floor</span>{" "}
        and <span className="font-semibold text-violet-300">chart_structure</span>{" "}
        have zero blocking authority today; this is the evidence a future
        graduation decision would rest on, not a recommendation to change
        anything now.
      </p>

      {loading && <Spinner />}
      {error && <ErrorState error={error} onRetry={reload} />}

      {data && (
        <>
          <div className="mb-4 grid grid-cols-2 gap-4 rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-3 sm:grid-cols-3">
            <Stat label="Records" value={data.records.toLocaleString()} />
            <Stat label="Symbols" value={data.symbols.toLocaleString()} />
            <Stat
              label="Eligible rate"
              value={data.eligible_rate == null ? "—" : `${data.eligible_rate}%`}
              tone="text-emerald-300"
            />
          </div>

          {!data.records ? (
            <p className="text-xs text-slate-500">
              No scan rejection records yet. The nightly full-universe sweep
              appends one record per symbol per run; this fills in over the
              following days.
            </p>
          ) : (
            <div className="space-y-6">
              <section>
                <h4 className="mb-2 text-xs font-semibold text-slate-300">
                  Level-4 structure score vs. verdict
                </h4>
                <StructureTable structure={data.structure} />
              </section>

              <section>
                <h4 className="mb-2 text-xs font-semibold text-slate-300">
                  Income shadow floor
                </h4>
                <ShadowFloorTable floor={data.shadow_floor} reasons={data.shadow_floor_reasons} />
              </section>

              <section>
                <h4 className="mb-2 text-xs font-semibold text-slate-300">
                  Binding constraints (why a candidate was rejected)
                </h4>
                <BindingBars counts={data.binding_counts} total={data.records} />
              </section>
            </div>
          )}
        </>
      )}
    </Card>
  );
}
