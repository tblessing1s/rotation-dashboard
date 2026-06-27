import React from "react";

// Shared presentational primitives used across the CFM tabs.

export const STATUS_COLORS = {
  green: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  yellow: "bg-amber-500/15 text-amber-300 border-amber-500/40",
  red: "bg-rose-500/15 text-rose-300 border-rose-500/40",
  ready: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  wait: "bg-amber-500/15 text-amber-300 border-amber-500/40",
  no: "bg-rose-500/15 text-rose-300 border-rose-500/40",
  unknown: "bg-slate-700/30 text-slate-300 border-slate-600/40",
};

export function Pill({ status, children }) {
  const cls = STATUS_COLORS[status] || STATUS_COLORS.unknown;
  return (
    <span className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold uppercase tracking-wide ${cls}`}>
      {children || status}
    </span>
  );
}

export function Light({ status, size = "h-3 w-3" }) {
  const c = { green: "bg-emerald-400", yellow: "bg-amber-400", red: "bg-rose-400" }[status] || "bg-slate-500";
  return <span className={`inline-block rounded-full ${size} ${c} shadow`} />;
}

export function Card({ title, right, children, className = "" }) {
  return (
    <div className={`rounded-xl border border-slate-800 bg-slate-900/60 p-4 ${className}`}>
      {(title || right) && (
        <div className="mb-3 flex items-center justify-between">
          {title && <h3 className="text-sm font-semibold text-slate-200">{title}</h3>}
          {right}
        </div>
      )}
      {children}
    </div>
  );
}

export function Stat({ label, value, sub, tone = "text-slate-100" }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`text-2xl font-semibold ${tone}`}>{value}</div>
      {sub && <div className="text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

export function Spinner({ size = "h-4 w-4", className = "" }) {
  return (
    <span
      role="status"
      aria-label="loading"
      className={`inline-block animate-spin rounded-full border-2 border-slate-600 border-t-slate-200 ${size} ${className}`}
    />
  );
}

// Centered spinner + label for a section that's waiting on data.
export function Loading({ label = "Loading…", className = "" }) {
  return (
    <div className={`flex items-center justify-center gap-2 py-6 text-sm text-slate-400 ${className}`}>
      <Spinner />
      <span>{label}</span>
    </div>
  );
}

export function Meter({ pct, tone = "bg-emerald-500" }) {
  const w = Math.max(0, Math.min(100, pct || 0));
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-slate-800">
      <div className={`h-full ${tone}`} style={{ width: `${w}%` }} />
    </div>
  );
}

export function fmt(n, digits = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function money(n) {
  if (n === null || n === undefined) return "—";
  return "$" + Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
}

export function pct(n) {
  if (n === null || n === undefined) return "—";
  return `${n > 0 ? "+" : ""}${fmt(n, 1)}%`;
}

export function useApi(fn, deps = [], interval = null) {
  const [data, setData] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const load = React.useCallback(async () => {
    try {
      setError(null);
      const d = await fn();
      setData(d);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  React.useEffect(() => {
    load();
    if (interval) {
      const id = setInterval(load, interval);
      return () => clearInterval(id);
    }
  }, [load, interval]);
  return { data, error, loading, reload: load };
}
