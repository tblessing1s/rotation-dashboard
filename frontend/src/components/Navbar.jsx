import React from "react";
import { Light } from "./ui.jsx";

function DemoToggle({ demo, busy, onToggle }) {
  return (
    <button
      onClick={onToggle}
      disabled={busy}
      title={demo ? "Showing demo data — click for live data" : "Showing live data — click for demo data"}
      className={`flex items-center gap-2 rounded-full border px-2.5 py-1 text-xs font-semibold transition disabled:opacity-50 ${
        demo
          ? "border-amber-500/50 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20"
          : "border-slate-700 bg-slate-800/60 text-slate-300 hover:bg-slate-800"
      }`}
    >
      <span className={`relative inline-flex h-3.5 w-6 items-center rounded-full transition ${demo ? "bg-amber-500/70" : "bg-slate-600"}`}>
        <span className={`inline-block h-2.5 w-2.5 transform rounded-full bg-white transition ${demo ? "translate-x-3" : "translate-x-0.5"}`} />
      </span>
      {busy ? "Switching…" : demo ? "Demo data" : "Live data"}
    </button>
  );
}

function PostureToggle({ posture, busy, onToggle }) {
  const aggressive = posture === "aggressive";
  return (
    <button
      onClick={onToggle}
      disabled={busy || !posture}
      title={
        aggressive
          ? "Aggressive weekly-short strikes (thinner ATR/ITM% floor, more juice, less protection) — click for Conservative"
          : "Conservative weekly-short strikes (wider ATR/ITM% floor, more protection, less juice) — click for Aggressive"
      }
      className={`flex items-center gap-2 rounded-full border px-2.5 py-1 text-xs font-semibold transition disabled:opacity-50 ${
        aggressive
          ? "border-rose-500/50 bg-rose-500/10 text-rose-300 hover:bg-rose-500/20"
          : "border-sky-500/50 bg-sky-500/10 text-sky-300 hover:bg-sky-500/20"
      }`}
    >
      <span className={`relative inline-flex h-3.5 w-6 items-center rounded-full transition ${aggressive ? "bg-rose-500/70" : "bg-sky-500/70"}`}>
        <span className={`inline-block h-2.5 w-2.5 transform rounded-full bg-white transition ${aggressive ? "translate-x-3" : "translate-x-0.5"}`} />
      </span>
      {busy ? "Switching…" : !posture ? "Posture…" : aggressive ? "Aggressive" : "Conservative"}
    </button>
  );
}

function AlertBell({ count, onClick }) {
  const hot = count > 0;
  return (
    <button
      onClick={onClick}
      title={hot ? `${count} active alert(s) — open the Alerts panel` : "No active alerts"}
      className={`relative flex items-center rounded-full border px-2.5 py-1 text-xs font-semibold transition ${
        hot
          ? "border-rose-500/50 bg-rose-500/10 text-rose-300 hover:bg-rose-500/20"
          : "border-slate-700 bg-slate-800/60 text-slate-400 hover:bg-slate-800"
      }`}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-4 w-4">
        <path strokeLinecap="round" strokeLinejoin="round"
              d="M15 17h5l-1.4-1.4a2 2 0 0 1-.6-1.4V11a6 6 0 1 0-12 0v3.2a2 2 0 0 1-.6 1.4L4 17h5m6 0v1a3 3 0 1 1-6 0v-1m6 0H9" />
      </svg>
      {hot && <span className="ml-1">{count}</span>}
    </button>
  );
}

export default function Navbar({ tabs, active, onChange, regimeStatus, demo, modeBusy, onToggleDemo, onLogout,
                                alertCount = 0, onAlertsClick, posture, postureBusy, onTogglePosture }) {
  return (
    <nav className="sticky top-0 z-10 border-b border-slate-800 bg-slate-950/90 backdrop-blur">
      <div className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-lg font-bold tracking-tight text-emerald-400">CFM</span>
          <span className="hidden text-xs text-slate-500 sm:inline">Cash Flow Machine</span>
        </div>
        <div className="flex flex-1 flex-wrap gap-1">
          {tabs.map((t) => (
            <button
              key={t}
              onClick={() => onChange(t)}
              className={`rounded-lg px-3 py-1.5 text-sm font-medium transition ${
                active === t
                  ? "bg-emerald-500/20 text-emerald-300"
                  : "text-slate-400 hover:bg-slate-800 hover:text-slate-200"
              }`}
            >
              {t}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-3 text-xs text-slate-400">
          {onAlertsClick && <AlertBell count={alertCount} onClick={onAlertsClick} />}
          {onTogglePosture && <PostureToggle posture={posture} busy={postureBusy} onToggle={onTogglePosture} />}
          <DemoToggle demo={demo} busy={modeBusy} onToggle={onToggleDemo} />
          <div className="flex items-center gap-2">
            <Light status={regimeStatus} />
            <span className="hidden sm:inline">Regime</span>
          </div>
          {onLogout && (
            <button
              onClick={onLogout}
              title="Sign out"
              className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs font-semibold text-slate-300 transition hover:bg-slate-800 hover:text-slate-100"
            >
              Sign out
            </button>
          )}
        </div>
      </div>
    </nav>
  );
}
