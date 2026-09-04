import React from "react";
import { api } from "../api.js";
import PushSetup from "./PushSetup.jsx";
import { Card, Loading, Pill, useApi } from "./ui.jsx";
import { useToast } from "./Toast.jsx";

// Alerts, read the way an inbox reads: what's NEW is on top, what you've already
// seen is folded away (still true, still refreshing its numbers, not shouting),
// and what has cleared is history. "Seen" is the only verb — the engine owns
// when a condition resolves, the operator only says "I've read this".

const CHANNEL_LABELS = { email: "Email", ntfy: "Push (ntfy)", webpush: "Push (this app)" };

const SEVERITY_TONE = {
  CRITICAL: "border-rose-500/40 bg-rose-500/10",
  HIGH: "border-amber-500/40 bg-amber-500/10",
  MEDIUM: "border-sky-500/40 bg-sky-500/5",
  LOW: "border-slate-600/40 bg-slate-500/5",
};
const SEVERITY_PILL = { CRITICAL: "red", HIGH: "yellow", MEDIUM: "unknown", LOW: "unknown" };

function actFromUrl(url) {
  // "/?action=roll&ticker=NVDA&reason=75%-rule" -> dispatch the in-app intent.
  try {
    const q = new URLSearchParams((url.split("?")[1] || ""));
    const action = q.get("action");
    const ticker = q.get("ticker");
    if (action && ticker) {
      window.dispatchEvent(new CustomEvent("cfm-action",
        { detail: { action, ticker, reason: q.get("reason") || undefined } }));
      return true;
    }
    // A /?tab=… link (payout ready, scan transition) — jump to that tab.
    const tab = q.get("tab");
    if (tab) {
      window.dispatchEvent(new CustomEvent("cfm-navigate", { detail: { tab } }));
      return true;
    }
  } catch { /* malformed link — ignore */ }
  return false;
}

const when = (iso) => (iso || "").slice(0, 16).replace("T", " ");

function AlertRow({ alert, onAck, onAct, compact = false }) {
  const tone = SEVERITY_TONE[alert.severity] || "border-slate-800";
  const actLabel = alert.action_url?.includes("action=roll") ? "Roll →"
    : alert.action_url?.includes("tab=") ? "Open →" : "Go →";
  return (
    <li className={`rounded-lg border px-3 py-2 ${tone} ${compact ? "opacity-70" : ""}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Pill status={SEVERITY_PILL[alert.severity]}>{alert.severity}</Pill>
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">
          {alert.type.replaceAll("_", " ")}
        </span>
        {alert.ticker && <span className="text-sm font-bold text-slate-100">{alert.ticker}</span>}
        <span className="ml-auto text-xs text-slate-500" title={`first seen ${alert.first_seen || ""}`}>
          {when(alert.first_seen)}
        </span>
        {alert.action_url && onAct && (
          <button
            onClick={() => onAct(alert)}
            className="rounded-full border border-emerald-600/50 bg-emerald-500/10 px-2 py-0.5 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20"
          >
            {actLabel}
          </button>
        )}
        {onAck && !alert.acknowledged && (
          <button
            onClick={() => onAck(alert.id)}
            title="Mark as seen — drops it off the bell; the condition still resolves on its own"
            className="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-xs text-slate-300 hover:bg-slate-800"
          >
            Seen
          </button>
        )}
      </div>
      <p className="mt-1 text-sm text-slate-200">{alert.message}</p>
      {alert.action && !compact && (
        <p className="mt-0.5 text-sm font-medium text-emerald-300">→ {alert.action}</p>
      )}
      {!compact && <p className="mt-0.5 text-xs text-slate-600" title={alert.rule}>{alert.rule}</p>}
    </li>
  );
}

// Notification configuration — lives on the Settings tab, NOT in the alerts
// list, so reading alerts never means wading through checkboxes.
export function AlertSettings() {
  const { data, error, loading, reload } = useApi(api.alerts, [], null);
  const toast = useToast();
  const [busy, setBusy] = React.useState(false);
  const [testing, setTesting] = React.useState(false);
  const [lastTest, setLastTest] = React.useState(null);
  const [showTypes, setShowTypes] = React.useState(false);

  // "Would I actually get paged?" — one SAMPLE position alert through the real
  // dispatch path (channels, toggles, dry run as saved), reported honestly.
  async function sendSample() {
    setTesting(true);
    try {
      const res = await api.testAlert();
      setLastTest(res);
      toast.show(res.verdict, { type: res.ok ? "success" : "error", duration: 9000 });
    } catch (e) {
      toast.show(e.message || "Sample alert failed.", { type: "error", duration: 7000 });
    } finally {
      setTesting(false);
    }
  }

  async function patch(p) {
    setBusy(true);
    try {
      await api.alertSettings(p);
      await reload();
    } finally {
      setBusy(false);
    }
  }

  if (loading && !data) return <Loading />;
  if (error) return <p className="text-sm text-rose-400">{error}</p>;

  const settings = data?.settings;
  const types = data?.types || {};
  const enabled = settings?.enabled || {};
  const channels = settings?.channels || {};
  const disabledTypes = Object.keys(types).filter((t) => enabled[t] === false).length;

  return (
    <div className="space-y-3">
      <div>
        <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-400">Where alerts go</div>
        <div className="flex flex-wrap items-center gap-4 text-xs text-slate-300">
          {["webpush", "ntfy", "email"].map((ch) => (
            <label key={ch} className="flex items-center gap-2">
              <input type="checkbox" disabled={busy} checked={channels[ch] !== false}
                     onChange={(e) => patch({ channels: { [ch]: e.target.checked } })}
                     className="h-3.5 w-3.5 accent-emerald-500" />
              {CHANNEL_LABELS[ch]}
            </label>
          ))}
          <label className="flex items-center gap-2 text-slate-400" title="Log alerts instead of sending them">
            <input type="checkbox" disabled={busy} checked={!!settings?.dry_run}
                   onChange={(e) => patch({ dry_run: e.target.checked })}
                   className="h-3.5 w-3.5 accent-emerald-500" />
            Dry run
          </label>
        </div>
      </div>
      <PushSetup />
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={sendSample} disabled={testing}
                className="rounded-full border border-emerald-600/50 bg-emerald-500/10 px-2.5 py-1 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-50">
          {testing ? "Sending…" : "Send a test alert"}
        </button>
        <span className="text-xs text-slate-500">
          One sample position alert through the real channels. Tap it — it should open that position's roll ticket.
        </span>
      </div>
      {lastTest && (
        <p className={`text-xs ${lastTest.ok ? "text-emerald-300" : "text-amber-300"}`}>
          {lastTest.verdict}
          {lastTest.alert?.ticker && <span className="text-slate-500"> (sample: {lastTest.alert.ticker})</span>}
        </p>
      )}
      <button onClick={() => setShowTypes((s) => !s)}
              className="text-xs text-slate-500 hover:text-slate-300">
        {showTypes ? "Hide" : "Choose"} which alert types fire
        {disabledTypes > 0 && <span className="text-amber-300"> · {disabledTypes} off</span>}
      </button>
      {showTypes && (
        <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
          {Object.entries(types).map(([type, info]) => (
            <label key={type} className="flex items-center gap-2 text-xs text-slate-300" title={info.rule}>
              <input type="checkbox" disabled={busy} checked={enabled[type] !== false}
                     onChange={(e) => patch({ enabled: { [type]: e.target.checked } })}
                     className="h-3.5 w-3.5 accent-emerald-500" />
              <span>{type.replaceAll("_", " ")}</span>
              <span className="text-slate-600">({info.severity})</span>
            </label>
          ))}
          <p className="col-span-full mt-1 text-xs text-slate-600">
            Channels are configured by environment variables (SMTP_HOST / ALERT_EMAIL_TO,
            ALERT_NTFY_TOPIC, VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY). Unconfigured channels
            fall back to the server log.
          </p>
        </div>
      )}
    </div>
  );
}

export default function AlertsPanel({ onChanged, onNavigate }) {
  // No interval here — App already polls /api/alerts every minute for the navbar
  // bell; this panel loads once and refreshes on Run-now / seen.
  const { data, error, loading, reload } = useApi(api.alerts, [], null);
  const [showSeen, setShowSeen] = React.useState(false);
  const [showHistory, setShowHistory] = React.useState(false);
  const [running, setRunning] = React.useState(false);

  const refresh = React.useCallback(async () => {
    await reload();
    onChanged?.();
  }, [reload, onChanged]);

  async function runNow() {
    setRunning(true);
    try {
      await api.runAlerts();
      await refresh();
    } finally {
      setRunning(false);
    }
  }

  async function ack(id) {
    await api.ackAlert(id);
    await refresh();
  }

  async function ackAll() {
    await api.ackAllAlerts();
    await refresh();
  }

  // Acting on an alert means you've read it: mark it seen on the way out, and
  // let the host close the drawer so the action lands on the page it belongs to.
  async function act(alert) {
    if (actFromUrl(alert.action_url)) {
      if (!alert.acknowledged) api.ackAlert(alert.id).then(() => onChanged?.()).catch(() => {});
      onNavigate?.();
    }
  }

  if (loading && !data) return <Card title="Alerts"><Loading /></Card>;
  if (error) return <Card title="Alerts"><p className="text-sm text-rose-400">{error}</p></Card>;

  const active = data?.active || [];
  const unseen = active.filter((a) => !a.acknowledged);
  const seen = active.filter((a) => a.acknowledged);
  const history = (data?.log || []).filter((a) => a.status !== "active").slice(0, 25);
  const lastRun = data?.last_run;

  return (
    <Card
      title={unseen.length ? `Alerts — ${unseen.length} new` : "Alerts"}
      right={
        <div className="flex items-center gap-2 text-xs">
          {lastRun && (
            <span className="hidden text-slate-500 sm:inline" title={lastRun.dry_run ? "dry run" : undefined}>
              checked {when(lastRun.at)}Z
            </span>
          )}
          {unseen.length > 1 && (
            <button onClick={ackAll}
                    className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 font-semibold text-slate-300 hover:bg-slate-800">
              Mark all seen
            </button>
          )}
          <button onClick={runNow} disabled={running}
                  className="rounded-full border border-emerald-600/50 bg-emerald-500/10 px-2.5 py-1 font-semibold text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-50">
            {running ? "Checking…" : "Check now"}
          </button>
        </div>
      }
    >
      <ul className="space-y-2">
        {unseen.map((a) => <AlertRow key={a.fingerprint} alert={a} onAck={ack} onAct={act} />)}
        {unseen.length === 0 && (
          <li className="text-sm text-slate-500">
            {seen.length
              ? "Nothing new — everything below has been seen and will clear on its own."
              : "No active alerts — all conditions clear."}
          </li>
        )}
      </ul>
      {seen.length > 0 && (
        <>
          <button onClick={() => setShowSeen((s) => !s)}
                  className="mt-3 text-xs text-slate-500 hover:text-slate-300">
            {showSeen ? "Hide" : "Show"} seen · still true ({seen.length})
          </button>
          {showSeen && (
            <ul className="mt-2 space-y-2">
              {seen.map((a) => <AlertRow key={a.fingerprint} alert={a} onAct={act} compact />)}
            </ul>
          )}
        </>
      )}
      <button onClick={() => setShowHistory((s) => !s)}
              className="ml-3 mt-3 text-xs text-slate-500 hover:text-slate-300">
        {showHistory ? "Hide" : "Show"} cleared ({history.length})
      </button>
      {showHistory && (
        <ul className="mt-2 space-y-2 opacity-70">
          {history.map((a) => <AlertRow key={a.id} alert={a} compact />)}
          {history.length === 0 && <li className="text-xs text-slate-600">No resolved alerts yet.</li>}
        </ul>
      )}
    </Card>
  );
}
