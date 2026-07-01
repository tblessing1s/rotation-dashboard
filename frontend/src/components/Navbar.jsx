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

export default function Navbar({ tabs, active, onChange, regimeStatus, demo, modeBusy, onToggleDemo, onLogout }) {
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
