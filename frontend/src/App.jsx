import React from "react";
import { api } from "./api.js";
import Navbar from "./components/Navbar.jsx";
import Login from "./components/Login.jsx";
import SchwabStatus from "./components/SchwabStatus.jsx";
import Scorecard from "./components/Scorecard.jsx";
import ExecuteTab from "./components/ExecuteTab.jsx";
import PositionTracker from "./components/PositionTracker.jsx";
import HistoryTab from "./components/HistoryTab.jsx";
import ReadyToEnter from "./components/ReadyToEnter.jsx";
import ScanProgress from "./components/ScanProgress.jsx";
import Overview from "./components/Overview.jsx";
import SettingsTab from "./components/SettingsTab.jsx";

const TABS = ["Overview", "Scan", "Positions", "History", "Settings"];

export default function App() {
  const [tab, setTab] = React.useState("Overview");
  const [regimeStatus, setRegimeStatus] = React.useState("unknown");
  // The order flow: a non-null value renders the entry-gate + ticket view over
  // the current tab (ticker may be "" for a blank gate). Cleared on tab change.
  const [execute, setExecute] = React.useState(null);
  const [execNonce, setExecNonce] = React.useState(0);
  const [demo, setDemo] = React.useState(false);
  const [modeBusy, setModeBusy] = React.useState(false);
  const [posture, setPosture] = React.useState(null);
  const [postureBusy, setPostureBusy] = React.useState(false);
  // null = still checking, true = signed in (or auth disabled), false = show login.
  const [authed, setAuthed] = React.useState(null);
  const [alertCount, setAlertCount] = React.useState(0);
  // Deep-link intent for the Positions tab: {action:"roll"|"focus", ticker, reason, id}.
  // Set from the ?action=…&ticker=… URL (a tapped push notification) or an
  // in-app "Act" click, so an alert lands you on the prefilled ticket, not a tab.
  const [positionIntent, setPositionIntent] = React.useState(null);
  // The full-universe Scorecard stays UNMOUNTED until opened, so its ~500-ticker
  // sweep isn't fetched on every Scan-tab visit.
  const [scanDetails, setScanDetails] = React.useState(false);
  // Bumped when the detached background scan finishes, so the Scan panels reload
  // with the freshly-warmed data (see ScanProgress).
  const [scanNonce, setScanNonce] = React.useState(0);
  // Build identity shown in the footer (version · commit). Fetched once; the
  // /api/version endpoint is open, so this works before/without a session too.
  const [version, setVersion] = React.useState(null);

  // Navbar bell badge: poll the active-alert count once a minute.
  React.useEffect(() => {
    if (authed !== true) return;
    let stop = false;
    const poll = () =>
      api.alerts().then((a) => !stop && setAlertCount((a.active || []).length)).catch(() => {});
    poll();
    const id = setInterval(poll, 60000);
    return () => { stop = true; clearInterval(id); };
  }, [authed, execNonce]);

  React.useEffect(() => {
    api.version().then(setVersion).catch(() => {});
  }, []);

  React.useEffect(() => {
    api.authStatus()
      .then((s) => setAuthed(!s.required || s.authenticated))
      .catch(() => setAuthed(false));
    const onAuthRequired = () => setAuthed(false);
    window.addEventListener("auth-required", onAuthRequired);
    return () => window.removeEventListener("auth-required", onAuthRequired);
  }, []);

  const goToTab = React.useCallback((t) => {
    setExecute(null); // leaving the order flow — a tab tap always lands on the tab
    setTab(t);
  }, []);

  // Route an alert action (from a tapped push or an in-app "Act" click) to the
  // Positions tab with a prefilled intent. Each call gets a fresh id so the same
  // ticker/action re-triggers the modal.
  const goToAction = React.useCallback((action, ticker, reason) => {
    if (!action || !ticker) return;
    setPositionIntent({ action, ticker, reason, id: Date.now() });
    setExecute(null);
    setTab("Positions");
  }, []);

  // On load: a ?action=…&ticker=… deep link (the push's target URL). Consume it
  // and strip the query so a refresh doesn't replay the action.
  React.useEffect(() => {
    if (authed !== true) return;
    const params = new URLSearchParams(window.location.search);
    const action = params.get("action");
    const ticker = params.get("ticker");
    if (action && ticker) {
      goToAction(action, ticker, params.get("reason") || undefined);
      params.delete("action"); params.delete("ticker"); params.delete("reason");
      const qs = params.toString();
      window.history.replaceState({}, "", window.location.pathname + (qs ? `?${qs}` : ""));
    }
    // In-app "Act" clicks from the Alerts panel dispatch this event.
    const onAction = (e) => goToAction(e.detail?.action, e.detail?.ticker, e.detail?.reason);
    window.addEventListener("cfm-action", onAction);
    return () => window.removeEventListener("cfm-action", onAction);
  }, [authed, goToAction]);

  React.useEffect(() => {
    if (authed !== true) return;
    api.mode().then((m) => setDemo(!!m.demo)).catch(() => {});
  }, [authed]);

  React.useEffect(() => {
    if (authed !== true) return;
    api.strikePosture().then((p) => setPosture(p.posture)).catch(() => {});
  }, [authed, demo]); // re-read on demo/live switch — posture is per-store

  async function logout() {
    try {
      await api.logout();
    } finally {
      setAuthed(false);
    }
  }

  async function toggleDemo() {
    setModeBusy(true);
    try {
      await api.setMode(!demo); // seeds the demo store on first switch-on
      window.location.reload(); // refetch every tab against the newly active source
    } catch {
      setModeBusy(false);
    }
  }

  async function togglePosture() {
    const next = posture === "aggressive" ? "conservative" : "aggressive";
    setPostureBusy(true);
    try {
      const r = await api.setStrikePosture(next);
      setPosture(r.posture);
    } catch {
      // leave the previous posture displayed on failure
    } finally {
      setPostureBusy(false);
    }
  }

  // Open the entry-gate + order-ticket flow (from a scan pick, a position card,
  // or the blank "check a ticker" button on Scan).
  function openTicket(ticker = "") {
    setExecute({ ticker, id: Date.now() });
  }

  if (authed === null) {
    return (
      <div className="flex min-h-full items-center justify-center bg-slate-950 text-sm text-slate-500">
        Loading…
      </div>
    );
  }
  if (!authed) return <Login onSuccess={() => setAuthed(true)} />;

  return (
    <div className="min-h-full bg-slate-950 text-slate-100">
      <Navbar tabs={TABS} active={tab} onChange={goToTab} regimeStatus={regimeStatus}
              onLogout={logout} alertCount={alertCount}
              onAlertsClick={() => goToTab("Settings")} />
      <main className="mx-auto max-w-7xl px-3 py-4 sm:px-4 sm:py-6">
        <SchwabStatus demo={demo} />
        {execute ? (
          <ExecuteTab
            key={execute.id}
            initialTicker={execute.ticker}
            onBack={() => setExecute(null)}
            onExecuted={() => setExecNonce((n) => n + 1)}
          />
        ) : (
          <>
            {tab === "Overview" && (
              <Overview
                onNavigate={goToTab}
                onSelectStock={openTicket}
                onAction={goToAction}
                onRegimeStatus={setRegimeStatus}
              />
            )}
            {tab === "Scan" && (
              <div className="grid gap-4">
                <ScanProgress onComplete={() => setScanNonce((n) => n + 1)} />
                <ReadyToEnter onSelectStock={openTicket} refreshKey={scanNonce} />
                <button
                  onClick={() => openTicket("")}
                  className="rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-2 text-left text-sm text-slate-400 hover:bg-slate-900/70"
                >
                  Check any ticker — entry gate &amp; order ticket →
                </button>
                <button
                  onClick={() => setScanDetails((v) => !v)}
                  className="flex w-full items-center justify-between rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-2 text-sm text-slate-400 hover:bg-slate-900/70"
                >
                  <span>{scanDetails ? "Hide" : "Show"} full universe scorecard</span>
                  <span className="text-xs text-slate-600">
                    {scanDetails ? "▲ collapse" : "▼ loads the full sweep on open"}
                  </span>
                </button>
                {scanDetails && <Scorecard regimeStatus={regimeStatus} refreshKey={scanNonce} />}
              </div>
            )}
            {tab === "Positions" && (
              <PositionTracker key={execNonce} intent={positionIntent}
                               onIntentHandled={() => setPositionIntent(null)}
                               onOpenTicket={openTicket} />
            )}
            {tab === "History" && <HistoryTab key={execNonce} />}
            {tab === "Settings" && (
              <SettingsTab demo={demo} modeBusy={modeBusy} onToggleDemo={toggleDemo}
                           posture={posture} postureBusy={postureBusy}
                           onTogglePosture={togglePosture} />
            )}
          </>
        )}
      </main>
      <footer
        className="mx-auto max-w-7xl px-4 pb-8 pt-4 text-center text-xs text-slate-600"
        style={{ paddingBottom: "calc(2rem + env(safe-area-inset-bottom))" }}
      >
        <div>CFM dashboard · scan → gate → execute → track · state.json is the source of truth</div>
        {version?.version && (
          <div className="mt-1 text-slate-700" title={version.built_at ? `Built ${version.built_at}` : undefined}>
            v{version.display || version.version}
            {version.commit ? ` · ${version.commit}` : ""}
          </div>
        )}
      </footer>
    </div>
  );
}
