import React from "react";
import { api } from "../api.js";
import { Card } from "./ui.jsx";
import LiveTradingSwitch from "./LiveTradingSwitch.jsx";
import { AlertSettings } from "./AlertsPanel.jsx";
import TrustScoreboard from "./TrustScoreboard.jsx";
import DataHealth from "./DataHealth.jsx";
import AccountsPanel from "./AccountsPanel.jsx";

// Settings, in the order they matter on a trading day: the two switches that
// change what the app DOES (posture, live trading) are always in view; the
// admin surfaces (accounts, notification plumbing, engine diagnostics, data
// health) sit behind one-line section headers so the page reads as four
// choices, not a console. Each section remembers whether you left it open.

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

// A collapsible section. `defaultOpen` seeds the first visit; after that the
// operator's last choice wins (kept per section in localStorage — a per-device
// convenience, so every read is guarded).
function Section({ id, title, summary, defaultOpen = false, children }) {
  const key = `cfm.settings.${id}`;
  const [open, setOpen] = React.useState(() => {
    try {
      const v = localStorage.getItem(key);
      return v == null ? defaultOpen : v === "1";
    } catch { return defaultOpen; }
  });
  const toggle = () => {
    const next = !open;
    setOpen(next);
    try { localStorage.setItem(key, next ? "1" : "0"); } catch { /* ignore */ }
  };
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40">
      <button onClick={toggle}
              className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-slate-900/70">
        <span className={`text-xs text-slate-500 transition ${open ? "rotate-90" : ""}`}>▶</span>
        <span className="text-sm font-semibold text-slate-200">{title}</span>
        {summary && <span className="ml-auto truncate text-xs text-slate-500">{summary}</span>}
      </button>
      {open && <div className="grid gap-4 border-t border-slate-800 p-4">{children}</div>}
    </div>
  );
}

export default function SettingsTab({ demo, modeBusy, onToggleDemo, posture, postureBusy,
                                     onTogglePosture, accountRegistry, accountId,
                                     onSelectAccount, onAccountsChanged }) {
  const [summary, setSummary] = React.useState(null);
  React.useEffect(() => {
    api.accountsSummary(true).then(setSummary).catch(() => setSummary(null));
  }, [accountRegistry, accountId]);

  const accounts = accountRegistry?.accounts || [];
  const activeLabel = accounts.find((a) => a.id === accountId)?.label || accountId || "";
  const accountSummary = accounts.length > 1
    ? `${accounts.length} accounts · viewing ${activeLabel}`
    : activeLabel ? `1 account · ${activeLabel}` : undefined;

  return (
    <div className="grid gap-4">
      <Card title="Trading">
        <div className="divide-y divide-slate-800">
          <ToggleRow
            title="Strike posture"
            desc="Aggressive sells weekly calls closer to the stock (more juice, less room). Conservative sells further out (less juice, more protection)."
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
            desc="Demo shows a seeded practice book; Live reads your real book. This only picks the data — sending orders to Schwab is the Live trading switch below. Switching reloads the app."
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

      <Section id="accounts" title="Accounts & Schwab connection" summary={accountSummary}
               defaultOpen={accounts.length > 1}>
        <AccountsPanel registry={accountRegistry} summary={summary} activeId={accountId}
                       onSelect={onSelectAccount} onChanged={onAccountsChanged} />
      </Section>

      <Section id="notifications" title="Notifications"
               summary="push · email · which alerts fire">
        <AlertSettings />
      </Section>

      <Section id="diagnostics" title="Diagnostics"
               summary="engine trust scoreboard · data sources · universe">
        <p className="text-xs text-slate-500">
          Read-only instruments for checking on the machine. Nothing here changes what the
          engine recommends or what the gates allow.
        </p>
        <TrustScoreboard />
        <DataHealth />
      </Section>
    </div>
  );
}
