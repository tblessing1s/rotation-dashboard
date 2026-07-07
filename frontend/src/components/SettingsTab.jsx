import React from "react";
import { Card } from "./ui.jsx";
import LiveTradingSwitch from "./LiveTradingSwitch.jsx";
import AlertsPanel from "./AlertsPanel.jsx";
import DataHealth from "./DataHealth.jsx";

// Low-frequency controls and admin surfaces, gathered off the trading tabs:
// data source (demo/live), strike posture, live-trading switch, alert config,
// and the data-health/universe admin console.

function ToggleRow({ title, desc, on, busy, onToggle, onLabel, offLabel, onTone, offTone, trackOn }) {
  return (
    <div className="flex items-center justify-between gap-4 py-3">
      <div className="min-w-0">
        <div className="text-sm font-medium text-slate-200">{title}</div>
        <div className="mt-0.5 text-xs text-slate-500">{desc}</div>
      </div>
      <button
        onClick={onToggle}
        disabled={busy}
        className={`flex shrink-0 items-center gap-2 rounded-full border px-2.5 py-1.5 text-xs font-semibold transition disabled:opacity-50 ${on ? onTone : offTone}`}
      >
        <span className={`relative inline-flex h-3.5 w-6 items-center rounded-full transition ${on ? trackOn : "bg-slate-600"}`}>
          <span className={`inline-block h-2.5 w-2.5 transform rounded-full bg-white transition ${on ? "translate-x-3" : "translate-x-0.5"}`} />
        </span>
        {busy ? "Switching…" : on ? onLabel : offLabel}
      </button>
    </div>
  );
}

export default function SettingsTab({ demo, modeBusy, onToggleDemo, posture, postureBusy, onTogglePosture }) {
  return (
    <div className="grid gap-4">
      <Card title="Trading preferences">
        <div className="divide-y divide-slate-800">
          <ToggleRow
            title="Strike posture"
            desc="Aggressive = thinner ATR/ITM% floor on weekly-short strikes (more juice, less protection). Conservative = wider floor."
            on={posture === "aggressive"}
            busy={postureBusy || !posture}
            onToggle={onTogglePosture}
            onLabel="Aggressive"
            offLabel={posture ? "Conservative" : "Posture…"}
            onTone="border-rose-500/50 bg-rose-500/10 text-rose-300 hover:bg-rose-500/20"
            offTone="border-sky-500/50 bg-sky-500/10 text-sky-300 hover:bg-sky-500/20"
            trackOn="bg-rose-500/70"
          />
          <ToggleRow
            title="Data source"
            desc="Demo mode points every tab at a seeded demo store; live mode reads your real state. Switching reloads the app."
            on={demo}
            busy={modeBusy}
            onToggle={onToggleDemo}
            onLabel="Demo data"
            offLabel="Live data"
            onTone="border-amber-500/50 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20"
            offTone="border-slate-700 bg-slate-800/60 text-slate-300 hover:bg-slate-800"
            trackOn="bg-amber-500/70"
          />
        </div>
      </Card>
      <LiveTradingSwitch />
      <AlertsPanel />
      <DataHealth />
    </div>
  );
}
