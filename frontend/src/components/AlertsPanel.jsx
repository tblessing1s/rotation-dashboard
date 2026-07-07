import React from "react";
import { api } from "../api.js";
import PushSetup from "./PushSetup.jsx";
import { Card, Loading, Pill, useApi } from "./ui.jsx";

const CHANNEL_LABELS = { email: "Email", ntfy: "Push (ntfy)", webpush: "Push (this app)" };

const SEVERITY_TONE = {
  CRITICAL: "border-rose-500/40 bg-rose-500/10",
  HIGH: "border-amber-500/40 bg-amber-500/10",
  MEDIUM: "border-sky-500/40 bg-sky-500/5",
};
const SEVERITY_PILL = { CRITICAL: "red", HIGH: "yellow", MEDIUM: "unknown" };

function actFromUrl(url) {
  // "/?action=roll&ticker=NVDA&reason=75%-rule" -> dispatch the in-app intent.
  try {
    const q = new URLSearchParams((url.split("?")[1] || ""));
    const action = q.get("action");
    const ticker = q.get("ticker");
    if (action && ticker) {
      window.dispatchEvent(new CustomEvent("cfm-action",
        { detail: { action, ticker, reason: q.get("reason") || undefined } }));
    }
  } catch { /* malformed link — ignore */ }
}

function AlertRow({ alert, onAck }) {
  const tone = SEVERITY_TONE[alert.severity] || "border-slate-800";
  const actLabel = alert.action_url?.includes("action=roll") ? "Roll →" : "Open →";
  return (
    <li className={`rounded-lg border px-3 py-2 ${tone} ${alert.acknowledged ? "opacity-60" : ""}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Pill status={SEVERITY_PILL[alert.severity]}>{alert.severity}</Pill>
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">
          {alert.type.replaceAll("_", " ")}
        </span>
        {alert.ticker && <span className="text-sm font-bold text-slate-100">{alert.ticker}</span>}
        <span className="ml-auto text-xs text-slate-500">{(alert.first_seen || "").slice(0, 16).replace("T", " ")}</span>
        {alert.action_url && (
          <button
            onClick={() => actFromUrl(alert.action_url)}
            className="rounded-full border border-emerald-600/50 bg-emerald-500/10 px-2 py-0.5 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20"
          >
            {actLabel}
          </button>
        )}
        {onAck && !alert.acknowledged && (
          <button
            onClick={() => onAck(alert.id)}
            className="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-xs text-slate-300 hover:bg-slate-800"
          >
            Acknowledge
          </button>
        )}
        {alert.acknowledged && <span className="text-xs text-slate-500">acknowledged</span>}
      </div>
      <p className="mt-1 text-sm text-slate-200">{alert.message}</p>
      {alert.action && <p className="mt-0.5 text-sm font-medium text-emerald-300">→ {alert.action}</p>}
      <p className="mt-0.5 text-xs text-slate-600" title={alert.rule}>{alert.rule}</p>
    </li>
  );
}

function Settings({ settings, types, onSaved }) {
  const [busy, setBusy] = React.useState(false);

  async function patch(p) {
    setBusy(true);
    try {
      await api.alertSettings(p);
      onSaved();
    } finally {
      setBusy(false);
    }
  }

  const enabled = settings?.enabled || {};
  const channels = settings?.channels || {};
  return (
    <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
      <div className="mb-2 flex items-center gap-4 text-xs text-slate-400">
        <label className="flex items-center gap-2">
          <input type="checkbox" disabled={busy} checked={!!settings?.dry_run}
                 onChange={(e) => patch({ dry_run: e.target.checked })}
                 className="h-3.5 w-3.5 accent-emerald-500" />
          Dry run (log instead of send)
        </label>
        {["email", "ntfy", "webpush"].map((ch) => (
          <label key={ch} className="flex items-center gap-2">
            <input type="checkbox" disabled={busy} checked={channels[ch] !== false}
                   onChange={(e) => patch({ channels: { [ch]: e.target.checked } })}
                   className="h-3.5 w-3.5 accent-emerald-500" />
            {CHANNEL_LABELS[ch]}
          </label>
        ))}
      </div>
      <PushSetup />
      <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
        {Object.entries(types || {}).map(([type, info]) => (
          <label key={type} className="flex items-center gap-2 text-xs text-slate-300" title={info.rule}>
            <input type="checkbox" disabled={busy} checked={enabled[type] !== false}
                   onChange={(e) => patch({ enabled: { [type]: e.target.checked } })}
                   className="h-3.5 w-3.5 accent-emerald-500" />
            <span className="font-mono">{type}</span>
            <span className="text-slate-600">({info.severity})</span>
          </label>
        ))}
      </div>
      <p className="mt-2 text-xs text-slate-600">
        Channels are configured via environment variables (SMTP_HOST / ALERT_EMAIL_TO for email,
        ALERT_NTFY_TOPIC for ntfy, VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY for this app’s push).
        Unconfigured channels fall back to the server log.
      </p>
    </div>
  );
}

export default function AlertsPanel() {
  // No interval here — App already polls /api/alerts every minute for the navbar
  // bell; this panel loads once and refreshes on Run-now / acknowledge.
  const { data, error, loading, reload } = useApi(api.alerts, [], null);
  const [showSettings, setShowSettings] = React.useState(false);
  const [showHistory, setShowHistory] = React.useState(false);
  const [running, setRunning] = React.useState(false);

  async function runNow() {
    setRunning(true);
    try {
      await api.runAlerts();
      await reload();
    } finally {
      setRunning(false);
    }
  }

  async function ack(id) {
    await api.ackAlert(id);
    await reload();
  }

  if (loading && !data) return <Card title="Alerts"><Loading /></Card>;
  if (error) return <Card title="Alerts"><p className="text-sm text-rose-400">{error}</p></Card>;

  const active = data?.active || [];
  const history = (data?.log || []).filter((a) => a.status !== "active").slice(0, 25);
  const lastRun = data?.last_run;

  return (
    <Card
      title={`Alerts${active.length ? ` — ${active.length} active` : ""}`}
      right={
        <div className="flex items-center gap-2 text-xs">
          {lastRun && (
            <span className="text-slate-500">
              last run {(lastRun.at || "").slice(0, 16).replace("T", " ")}Z{lastRun.dry_run ? " (dry)" : ""}
            </span>
          )}
          <button onClick={runNow} disabled={running}
                  className="rounded-full border border-emerald-600/50 bg-emerald-500/10 px-2.5 py-1 font-semibold text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-50">
            {running ? "Running…" : "Run now"}
          </button>
          <button onClick={() => setShowSettings((s) => !s)}
                  className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 font-semibold text-slate-300 hover:bg-slate-800">
            Settings
          </button>
        </div>
      }
    >
      <ul className="space-y-2">
        {active.map((a) => <AlertRow key={a.fingerprint} alert={a} onAck={ack} />)}
        {active.length === 0 && (
          <li className="text-sm text-slate-500">No active alerts — all conditions clear.</li>
        )}
      </ul>
      {showSettings && <Settings settings={data?.settings} types={data?.types} onSaved={reload} />}
      <button onClick={() => setShowHistory((s) => !s)}
              className="mt-3 text-xs text-slate-500 hover:text-slate-300">
        {showHistory ? "Hide" : "Show"} recent history ({history.length})
      </button>
      {showHistory && (
        <ul className="mt-2 space-y-2 opacity-80">
          {history.map((a) => <AlertRow key={a.id} alert={a} />)}
          {history.length === 0 && <li className="text-xs text-slate-600">No resolved alerts yet.</li>}
        </ul>
      )}
    </Card>
  );
}
