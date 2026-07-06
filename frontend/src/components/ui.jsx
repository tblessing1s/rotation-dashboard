import React from "react";

// Shared presentational primitives used across the CFM tabs.

export const STATUS_COLORS = {
  green: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  yellow: "bg-amber-500/15 text-amber-300 border-amber-500/40",
  red: "bg-rose-500/15 text-rose-300 border-rose-500/40",
  ready: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  wait: "bg-amber-500/15 text-amber-300 border-amber-500/40",
  no: "bg-rose-500/15 text-rose-300 border-rose-500/40",
  // Scorecard verdicts share the Kill Switch traffic-light convention.
  go: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  caution: "bg-amber-500/15 text-amber-300 border-amber-500/40",
  avoid: "bg-rose-500/15 text-rose-300 border-rose-500/40",
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
    // min-w-0: when a Card is a flex/grid item, let it shrink to its track so a
    // wide inner table scrolls inside its own overflow-x-auto wrapper instead of
    // stretching the card past the viewport (mobile horizontal-overflow guard).
    <div className={`min-w-0 rounded-xl border border-slate-800 bg-slate-900/60 p-4 ${className}`}>
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
    <div className="min-w-0">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`text-xl font-semibold leading-tight sm:text-2xl ${tone}`}>{value}</div>
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

// Retrying a 401 (auth-required) or a 4xx client error is pointless — the app
// swaps in the login screen on 401, and a bad request won't fix itself. Only a
// timeout, a network drop, or a 5xx server error is worth retrying.
function isTransient(e) {
  if (e.timeout) return true;
  if (e.status && e.status >= 400 && e.status < 500) return false;
  return true;
}

export function useApi(fn, deps = [], interval = null, retries = 2) {
  const [data, setData] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const load = React.useCallback(async () => {
    setError(null);
    setLoading(true);
    // A cold full-universe scan can be slow or blip on a transient provider
    // hiccup; retry a few times with backoff before surfacing the error, so the
    // panel self-heals instead of stranding the operator on a dead message.
    for (let attempt = 0; ; attempt++) {
      try {
        const d = await fn();
        setData(d);
        setLoading(false);
        return;
      } catch (e) {
        if (attempt >= retries || !isTransient(e)) {
          setError(e.message);
          setLoading(false);
          return;
        }
        await new Promise((r) => setTimeout(r, 1000 * 2 ** attempt));
      }
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

// Inline error with a Retry button — for a panel whose fetch failed, so the
// operator can re-try in place instead of reloading the whole dashboard.
export function ErrorState({ error, onRetry, className = "" }) {
  return (
    <div className={`flex flex-col items-start gap-2 py-4 text-sm ${className}`}>
      <p className="text-rose-400">{error || "Something went wrong."}</p>
      {onRetry && (
        <button
          onClick={onRetry}
          className="rounded-md border border-slate-700 px-3 py-1 text-xs text-slate-200 hover:bg-slate-800"
        >
          Retry
        </button>
      )}
    </div>
  );
}
