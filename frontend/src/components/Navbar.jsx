import React from "react";
import { Light } from "./ui.jsx";

export default function Navbar({ tabs, active, onChange, regimeStatus }) {
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
        <div className="flex items-center gap-2 text-xs text-slate-400">
          <Light status={regimeStatus} />
          <span className="hidden sm:inline">Regime</span>
        </div>
      </div>
    </nav>
  );
}
