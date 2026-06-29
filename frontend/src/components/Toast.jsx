import React from "react";
import { Spinner } from "./ui.jsx";

// Lightweight toast system: a provider holds the active toasts, useToast() exposes
// show/update/dismiss, and <Toaster/> renders the stack (top-right). A toast can be
// updated in place (e.g. "Submitting…" → "Filled & logged") so an order's lifecycle
// is one moving notification rather than a pile of separate ones.

const ToastCtx = React.createContext(null);

export function useToast() {
  const ctx = React.useContext(ToastCtx);
  if (!ctx) throw new Error("useToast must be used within <ToastProvider>");
  return ctx;
}

const TONE = {
  success: "border-emerald-500/40 bg-emerald-500/15 text-emerald-200",
  error: "border-rose-500/40 bg-rose-500/15 text-rose-200",
  pending: "border-sky-500/40 bg-sky-500/15 text-sky-200",
  info: "border-slate-600/40 bg-slate-700/30 text-slate-200",
};
const ICON = { success: "✓", error: "✕", info: "•" };

let _seq = 0;

export function ToastProvider({ children }) {
  const [toasts, setToasts] = React.useState([]);
  const timers = React.useRef({});

  const dismiss = React.useCallback((id) => {
    setToasts((ts) => ts.filter((t) => t.id !== id));
    const tm = timers.current[id];
    if (tm) { clearTimeout(tm); delete timers.current[id]; }
  }, []);

  // duration <= 0 keeps the toast sticky (used while an order is in flight).
  const arm = React.useCallback((id, duration) => {
    if (timers.current[id]) clearTimeout(timers.current[id]);
    if (duration && duration > 0) timers.current[id] = setTimeout(() => dismiss(id), duration);
  }, [dismiss]);

  const show = React.useCallback((message, { type = "info", duration = 4000 } = {}) => {
    const id = ++_seq;
    setToasts((ts) => [...ts, { id, message, type }]);
    arm(id, duration);
    return id;
  }, [arm]);

  const update = React.useCallback((id, message, { type, duration = 4000 } = {}) => {
    setToasts((ts) => ts.map((t) => (t.id === id ? { ...t, message, ...(type ? { type } : {}) } : t)));
    arm(id, duration);
  }, [arm]);

  React.useEffect(() => {
    const t = timers.current;
    return () => Object.values(t).forEach(clearTimeout);
  }, []);

  const value = React.useMemo(() => ({ show, update, dismiss }), [show, update, dismiss]);

  return (
    <ToastCtx.Provider value={value}>
      {children}
      <Toaster toasts={toasts} onDismiss={dismiss} />
    </ToastCtx.Provider>
  );
}

function Toaster({ toasts, onDismiss }) {
  return (
    <div className="pointer-events-none fixed right-4 top-4 z-[100] flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          role="status"
          className={`pointer-events-auto flex items-start gap-2 rounded-lg border px-3 py-2 text-sm shadow-lg backdrop-blur ${TONE[t.type] || TONE.info}`}
        >
          <span className="mt-0.5 shrink-0">
            {t.type === "pending" ? <Spinner size="h-3.5 w-3.5" /> : <span aria-hidden>{ICON[t.type] || ICON.info}</span>}
          </span>
          <span className="flex-1 leading-snug">{t.message}</span>
          <button
            onClick={() => onDismiss(t.id)}
            className="-mr-1 shrink-0 rounded px-1 text-slate-400 hover:text-slate-200"
            aria-label="Dismiss"
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}
