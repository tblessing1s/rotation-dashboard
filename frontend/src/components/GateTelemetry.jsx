import React from "react";
import { api } from "../api.js";
import { Card, Spinner, ErrorState, useApi } from "./ui.jsx";

// Gate rejection telemetry — the calibration view.
//
// READ-ONLY OBSERVABILITY. Nothing here grants blocking authority, changes a
// threshold, or touches a gate. It answers one question: for each gate, how
// often was it the ONLY veto-authority failure (the SOLE-BLOCKER RATE). A gate
// with a high block rate but a low sole-blocker rate co-fires with genuinely bad
// setups; a gate with a high sole-blocker rate is the binding constraint on the
// whole system, and the first place to look.
//
// Deliberately NOT on the daily monitoring path and deliberately without a card
// chip on the recommendation surface: this is a diagnostic instrument reached on
// purpose, and it must not subtly pressure an entry decision.

const AUTHORITY_STYLE = {
  veto: "border-rose-500/40 bg-rose-500/15 text-rose-300",
  rank: "border-sky-500/40 bg-sky-500/15 text-sky-300",
  shadow: "border-violet-500/40 bg-violet-500/15 text-violet-300",
};

// `indeterminate` and zero are NOT the same fact and must never be confused at a
// glance — an em-dash on a slate chip reads as "no answer", a 0.0% reads as an
// answer. Every null rate renders through here.
function Indeterminate({ title }) {
  return (
    <span
      title={title || "Not computable — this gate is never evaluated in a scan sweep"}
      className="rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-300"
    >
      indeterminate
    </span>
  );
}

function AuthorityChip({ authority, changed }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span
        className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
          AUTHORITY_STYLE[authority] || AUTHORITY_STYLE.shadow
        }`}
      >
        {authority || "—"}
      </span>
      {changed && (
        <span
          title="This gate's authority changed inside the selected range. Rows retain the authority in force when they were written; the rates below mix both."
          className="text-[10px] font-semibold text-amber-400"
        >
          ⚠ changed
        </span>
      )}
    </span>
  );
}

const asPct = (r) => (r == null ? null : `${(r * 100).toFixed(1)}%`);

// Near-miss is "how far PAST its threshold a failing value sat, as a fraction of
// the threshold". A gate whose rejections cluster at 0.05 reads very differently
// from one clustering at 0.40 — that difference is the point of the column.
function nearMissLabel(nm) {
  if (!nm) return "—";
  if (nm.normalized && nm.median != null) return `+${(nm.median * 100).toFixed(1)}%`;
  if (nm.raw_n > 0 && nm.raw_median != null) return `+${nm.raw_median.toFixed(2)} raw`;
  return "—";
}

function Histogram({ nm }) {
  const buckets = Object.entries(nm?.buckets || {});
  if (!buckets.length) {
    return (
      <p className="text-[11px] text-slate-500">
        {nm?.raw_n > 0
          ? `No normalized distribution: this gate's threshold is zero, so a fractional distance is undefined. Raw median ${nm.raw_median?.toFixed(3)}, p75 ${nm.raw_p75?.toFixed(3)} over ${nm.raw_n} failure(s).`
          : "No numeric near-miss for this gate — it is not a threshold comparison, or it never failed in range."}
      </p>
    );
  }
  const max = Math.max(...buckets.map(([, n]) => n));
  return (
    <div className="space-y-1">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">
        Distance past threshold, over {nm.n} failure(s) · median{" "}
        {asPct(nm.median)} · p75 {asPct(nm.p75)}
      </div>
      {buckets.map(([label, n]) => (
        <div key={label} className="flex items-center gap-2">
          <span className="w-16 shrink-0 text-right font-mono text-[10px] text-slate-400">
            {label}
          </span>
          <span className="h-3 rounded-sm bg-sky-500/50"
                style={{ width: `${Math.max(2, (n / max) * 100)}%` }} />
          <span className="font-mono text-[10px] text-slate-500">{n}</span>
        </div>
      ))}
    </div>
  );
}

function CoBlock({ gate }) {
  const pairs = Object.entries(gate.co_block || {}).filter(([, n]) => n > 0);
  if (!pairs.length) {
    return (
      <p className="text-[11px] text-slate-500">
        No co-failures: whenever this gate failed in range, no other veto gate did.
      </p>
    );
  }
  const failed = gate.failed_n || 0;
  return (
    <div className="space-y-1">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">
        Co-failed with (share of this gate&apos;s {failed} failures)
      </div>
      {pairs.map(([id, n]) => (
        <div key={id} className="flex items-center justify-between gap-3">
          <span className="truncate font-mono text-[11px] text-slate-400">{id}</span>
          <span className="font-mono text-[11px] text-slate-300">
            {n} · {failed ? `${((n / failed) * 100).toFixed(0)}%` : "—"}
          </span>
        </div>
      ))}
      <p className="pt-1 text-[11px] text-slate-500">
        Two gates that fail together nearly always are one gate wearing two hats.
      </p>
    </div>
  );
}

function TimeSeries({ points }) {
  if (!points?.length) return null;
  return (
    <div className="space-y-1">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">
        Weekly block / sole-blocker rate
      </div>
      <div className="overflow-x-auto">
        <table className="text-[11px]">
          <thead>
            <tr className="text-slate-500">
              {points.map((p) => (
                <th key={p.week} className="px-2 py-0.5 font-mono font-normal">
                  {p.week.slice(5)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr className="text-slate-300">
              {points.map((p) => (
                <td key={p.week} className="px-2 py-0.5 text-center font-mono">
                  {asPct(p.block_rate) ?? "—"}
                </td>
              ))}
            </tr>
            <tr className="text-amber-300">
              {points.map((p) => (
                <td key={p.week} className="px-2 py-0.5 text-center font-mono">
                  {asPct(p.sole_blocker_rate) ?? "—"}
                </td>
              ))}
            </tr>
            <tr className="text-slate-600">
              {points.map((p) => (
                <td key={p.week} className="px-2 py-0.5 text-center font-mono">
                  n={p.evaluated_n}
                </td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>
      <p className="text-[11px] text-slate-500">
        Distinguishes a gate that has always been tight from one that became tight
        as conditions changed.
      </p>
    </div>
  );
}

function GateRow({ gate, data, expanded, onToggle }) {
  const ind = gate.indeterminate;
  return (
    <>
      <tr
        onClick={onToggle}
        className={`cursor-pointer border-t border-slate-800 hover:bg-slate-900/60 ${
          ind ? "text-slate-500" : "text-slate-200"
        }`}
      >
        <td className="py-2 pr-3">
          <div className="flex items-center gap-2">
            <span className={`text-[10px] transition-transform ${expanded ? "rotate-90" : ""}`}>▸</span>
            <div className="min-w-0">
              <div className="truncate font-mono text-[11px]">{gate.gate_id}</div>
              <div className="truncate text-[11px] text-slate-500">{gate.label}</div>
            </div>
          </div>
        </td>
        <td className="py-2 pr-3">
          <AuthorityChip authority={gate.authority} changed={gate.authority_changed_in_range} />
        </td>
        <td className="py-2 pr-3 text-right font-mono">
          {ind ? <Indeterminate title={gate.indeterminate_reason} /> : asPct(gate.block_rate)}
        </td>
        <td className="py-2 pr-3 text-right font-mono">
          {gate.sole_blocker_rate == null ? (
            ind ? (
              <Indeterminate title={gate.indeterminate_reason} />
            ) : (
              <span
                title="Sole-blocker rate is defined only for veto-authority gates. A shadow metric flagging is not a block."
                className="text-slate-600"
              >
                n/a
              </span>
            )
          ) : (
            <span className="font-semibold text-amber-300">{asPct(gate.sole_blocker_rate)}</span>
          )}
        </td>
        <td className="py-2 pr-3 text-right font-mono">
          {ind ? "—" : nearMissLabel(gate.near_miss)}
        </td>
        <td className="py-2 text-right font-mono text-slate-400">
          {ind ? "0" : gate.evaluated_n}
        </td>
      </tr>
      {expanded && (
        <tr className="border-t border-slate-900 bg-slate-950/60">
          <td colSpan={6} className="px-6 py-3">
            {ind ? (
              <p className="text-[11px] text-slate-400">
                <span className="font-semibold text-amber-300">Not computable.</span>{" "}
                {gate.indeterminate_reason === "absent_from_scan_path"
                  ? "This gate is only appended for a SHARES entry in the executor; the bulk scan path never evaluates it."
                  : gate.indeterminate_reason === "inactive_shares_mode"
                  ? "This veto is declared but cannot fire: both its tiers are LEAP-denominated and shares carry no LEAP burn. A 0% block rate here would be an artefact, not evidence."
                  : "Level 5 is an account overlay run only over candidates that already cleared Levels 1–4, so it is never evaluated for a rejected candidate. Its sole-blocker rate is not merely unknown — it is undefined."}{" "}
                No value is imputed and the gate is not dropped from this table.
              </p>
            ) : (
              <div className="grid gap-4 md:grid-cols-3">
                <Histogram nm={gate.near_miss} />
                <CoBlock gate={gate} />
                <TimeSeries points={data.time_series?.[gate.gate_id]} />
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

function RulesetPicker({ present, active, onChange }) {
  const names = Object.keys(present || {});
  if (!names.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-[10px] uppercase tracking-wide text-slate-500">Ruleset</span>
      {names.map((n) => (
        <button
          key={n}
          onClick={() => onChange(n)}
          className={`rounded-full border px-3 py-1 text-xs ${
            active === n
              ? "border-sky-500/50 bg-sky-500/15 text-sky-200"
              : "border-slate-700 bg-slate-900/60 text-slate-400 hover:text-slate-200"
          }`}
        >
          {n} <span className="font-mono text-[10px] text-slate-500">({present[n]})</span>
        </button>
      ))}
      <span className="text-[11px] text-slate-500">
        Data is never pooled across rulesets.
      </span>
    </div>
  );
}

export default function GateTelemetry() {
  const [days, setDays] = React.useState(90);
  const [ruleset, setRuleset] = React.useState(null);
  const [open, setOpen] = React.useState({});
  const { data, error, loading, reload } = useApi(
    () => api.gateTelemetry({ days, ruleset }), [days, ruleset], null);

  const toggle = (id) => setOpen((o) => ({ ...o, [id]: !o[id] }));

  return (
    <Card
      title="Gate rejection telemetry"
      right={
        <div className="flex items-center gap-2">
          <span className="rounded bg-violet-500/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-violet-300">
            no authority
          </span>
          {[30, 90, 180].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`rounded border px-2 py-0.5 text-[11px] ${
                days === d
                  ? "border-sky-500/50 bg-sky-500/15 text-sky-200"
                  : "border-slate-700 text-slate-400 hover:text-slate-200"
              }`}
            >
              {d}d
            </button>
          ))}
          <button onClick={reload} className="text-[11px] text-slate-400 hover:text-slate-200">
            ↻
          </button>
        </div>
      }
    >
      <p className="mb-3 text-xs text-slate-400">
        For each gate: how often it blocked, and how often it was the{" "}
        <span className="font-semibold text-amber-300">only</span> veto-authority
        failure. A high block rate with a low sole-blocker rate means the gate
        co-fires with genuinely bad setups. A high sole-blocker rate means the gate
        is the binding constraint on the whole system. Read-only — nothing here
        changes a threshold or a verdict.
      </p>

      {loading && <Spinner />}
      {error && <ErrorState error={error} onRetry={reload} />}

      {data && (
        <>
          {/* The denominator, up top and unmissable: every rate below is
              meaningless without it, and a range covering three scans must look
              visibly untrustworthy. */}
          <div className="mb-3 flex flex-wrap items-baseline gap-x-6 gap-y-1 rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-3">
            <div>
              <div className="text-[10px] uppercase tracking-wide text-slate-500">
                Candidates evaluated
              </div>
              <div
                className={`text-2xl font-semibold leading-tight ${
                  data.low_confidence ? "text-amber-300" : "text-slate-100"
                }`}
              >
                {data.evaluated_n.toLocaleString()}
              </div>
            </div>
            <div className="text-[11px] text-slate-500">
              {data.runs} scan run(s) over {data.days} day(s)
              {data.first_day ? ` · ${data.first_day} → ${data.last_day}` : ""}
              <br />
              range {data.start} → {data.end} · admitted{" "}
              {data.admitted_n ?? 0}
              {data.admitted_rate != null ? ` (${asPct(data.admitted_rate)})` : ""}
            </div>
          </div>

          {data.low_confidence && data.evaluated_n > 0 && (
            <p className="mb-3 rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200">
              Low confidence: {data.evaluated_n.toLocaleString()} evaluated
              candidates is below the {data.min_evaluated_n.toLocaleString()}
              -candidate floor. These rates are not yet a calibration basis.
            </p>
          )}

          <div className="mb-3">
            <RulesetPicker
              present={data.rulesets_present}
              active={data.gate_ruleset}
              onChange={setRuleset}
            />
          </div>

          {data.note && (
            <p className="mb-3 rounded border border-sky-500/40 bg-sky-500/10 px-3 py-2 text-[11px] text-sky-200">
              {data.note}
            </p>
          )}

          {!data.gates.length && !data.note && (
            <p className="text-xs text-slate-500">
              No gate evaluations recorded for this period. The nightly scan sweep
              appends one record per candidate per run; history begins accruing
              from the first sweep after this feature shipped and is retained for{" "}
              {data.retention_days} days. Absence of history is a fact, not a gap —
              nothing is backfilled.
            </p>
          )}

          {!!data.gates.length && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] text-sm">
                <thead>
                  <tr className="text-left text-[10px] uppercase tracking-wide text-slate-500">
                    <th className="py-1 pr-3">Gate</th>
                    <th className="py-1 pr-3">Authority</th>
                    <th className="py-1 pr-3 text-right">Block rate</th>
                    <th className="py-1 pr-3 text-right">Sole-blocker</th>
                    <th className="py-1 pr-3 text-right">Near-miss med.</th>
                    <th className="py-1 text-right">Evaluated N</th>
                  </tr>
                </thead>
                <tbody>
                  {data.gates.map((g) => (
                    <GateRow
                      key={g.gate_id}
                      gate={g}
                      data={data}
                      expanded={!!open[g.gate_id]}
                      onToggle={() => toggle(g.gate_id)}
                    />
                  ))}
                </tbody>
              </table>
              <p className="pt-3 text-[11px] text-slate-500">
                Sorted by sole-blocker rate, descending. Click a row for its
                near-miss histogram, co-failure pairs and weekly trend. This view
                is a diagnostic instrument — it is not a recommendation to loosen
                anything.
              </p>
            </div>
          )}
        </>
      )}
    </Card>
  );
}
