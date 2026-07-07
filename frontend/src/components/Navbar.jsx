import React from "react";
import { Light } from "./ui.jsx";

function AlertBell({ count, onClick }) {
  const hot = count > 0;
  return (
    <button
      onClick={onClick}
      title={hot ? `${count} active alert(s) — open the Alerts panel` : "No active alerts"}
      className={`relative flex items-center rounded-full border px-2.5 py-1.5 text-xs font-semibold transition ${
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

function TabButton({ label, active, onClick }) {
  return (
    <button
      onClick={onClick}
      className={`shrink-0 whitespace-nowrap rounded-lg px-3 py-2 text-sm font-medium transition sm:py-1.5 ${
        active
          ? "bg-emerald-500/20 text-emerald-300"
          : "text-slate-400 hover:bg-slate-800 hover:text-slate-200"
      }`}
    >
      {label}
    </button>
  );
}

// Chrome kept deliberately thin: tabs, the regime light, the alert bell, and
// sign-out. Low-frequency controls (demo data, strike posture, live trading)
// live on the Settings tab.
export default function Navbar({ tabs, active, onChange, regimeStatus, onLogout,
                                alertCount = 0, onAlertsClick }) {
  return (
    <nav
      className="sticky top-0 z-20 border-b border-slate-800 bg-slate-950/90 backdrop-blur"
      style={{ paddingTop: "env(safe-area-inset-top)" }}
    >
      <div className="mx-auto max-w-7xl px-3 sm:px-4">
        {/* Top row: brand · (desktop tabs) · controls */}
        <div className="flex items-center gap-3 py-2.5">
          <div className="flex items-center gap-2">
            <span className="text-lg font-bold tracking-tight text-emerald-400">CFM</span>
            <span className="hidden text-xs text-slate-500 sm:inline">Cash Flow Machine</span>
          </div>

          {/* Desktop: tabs inline */}
          <div className="hidden flex-1 flex-wrap gap-1 sm:flex">
            {tabs.map((t) => (
              <TabButton key={t} label={t} active={active === t} onClick={() => onChange(t)} />
            ))}
          </div>

          {/* Right-side controls */}
          <div className="ml-auto flex items-center gap-2 sm:gap-3">
            <div className="flex items-center gap-2 text-xs text-slate-400" title="Market regime">
              <Light status={regimeStatus} />
              <span className="hidden sm:inline">Regime</span>
            </div>
            {onAlertsClick && <AlertBell count={alertCount} onClick={onAlertsClick} />}
            {onLogout && (
              <button
                onClick={onLogout}
                title="Sign out"
                className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1.5 text-xs font-semibold text-slate-300 transition hover:bg-slate-800 hover:text-slate-100"
              >
                Sign out
              </button>
            )}
          </div>
        </div>

        {/* Mobile: horizontally scrollable tab strip */}
        <div className="-mx-3 flex gap-1 overflow-x-auto px-3 pb-2 no-scrollbar sm:hidden">
          {tabs.map((t) => (
            <TabButton key={t} label={t} active={active === t} onClick={() => onChange(t)} />
          ))}
        </div>
      </div>
    </nav>
  );
}
