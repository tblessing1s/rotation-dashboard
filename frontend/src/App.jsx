import React from "react";
import { api } from "./api.js";
import Navbar from "./components/Navbar.jsx";
import Login from "./components/Login.jsx";
import SchwabStatus from "./components/SchwabStatus.jsx";
import RegimeScanner from "./components/RegimeScanner.jsx";
import StockFilter from "./components/StockFilter.jsx";
import Scorecard from "./components/Scorecard.jsx";
import ExecuteTab from "./components/ExecuteTab.jsx";
import ThetaLedger from "./components/ThetaLedger.jsx";
import KillSwitchMonitor from "./components/KillSwitchMonitor.jsx";
import PositionTracker from "./components/PositionTracker.jsx";
import DailyChecklist from "./components/DailyChecklist.jsx";
import AlertsPanel from "./components/AlertsPanel.jsx";
import HistoryTab from "./components/HistoryTab.jsx";
import DataHealth from "./components/DataHealth.jsx";

const TABS = ["Scan", "Execute", "Theta", "Kill Switch", "Positions", "History", "Checklist"];

export default function App() {
  const [tab, setTab] = React.useState("Scan");
  const [regimeStatus, setRegimeStatus] = React.useState("unknown");
  const [selectedTicker, setSelectedTicker] = React.useState("");
  const [execNonce, setExecNonce] = React.useState(0);
  const [demo, setDemo] = React.useState(false);
  const [modeBusy, setModeBusy] = React.useState(false);
  const [posture, setPosture] = React.useState(null);
  const [postureBusy, setPostureBusy] = React.useState(false);
  // null = still checking, true = signed in (or auth disabled), false = show login.
  const [authed, setAuthed] = React.useState(null);
  const [alertCount, setAlertCount] = React.useState(0);

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
    api.authStatus()
      .then((s) => setAuthed(!s.required || s.authenticated))
      .catch(() => setAuthed(false));
    const onAuthRequired = () => setAuthed(false);
    window.addEventListener("auth-required", onAuthRequired);
    return () => window.removeEventListener("auth-required", onAuthRequired);
  }, []);

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

  function selectStock(ticker) {
    setSelectedTicker(ticker);
    setTab("Execute");
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
      <Navbar tabs={TABS} active={tab} onChange={setTab} regimeStatus={regimeStatus}
              demo={demo} modeBusy={modeBusy} onToggleDemo={toggleDemo} onLogout={logout}
              alertCount={alertCount} onAlertsClick={() => setTab("Checklist")}
              posture={posture} postureBusy={postureBusy} onTogglePosture={togglePosture} />
      <main className="mx-auto max-w-7xl px-4 py-6">
        <SchwabStatus demo={demo} />
        {tab === "Scan" && (
          <div className="grid gap-4">
            <RegimeScanner onStatus={setRegimeStatus} />
            <Scorecard regimeStatus={regimeStatus} />
            <StockFilter onSelectStock={selectStock} />
          </div>
        )}
        {tab === "Execute" && (
          <ExecuteTab
            initialTicker={selectedTicker}
            onExecuted={() => setExecNonce((n) => n + 1)}
          />
        )}
        {tab === "Theta" && <ThetaLedger key={execNonce} />}
        {tab === "Kill Switch" && <KillSwitchMonitor />}
        {tab === "Positions" && <PositionTracker key={execNonce} />}
        {tab === "History" && <HistoryTab key={execNonce} />}
        {tab === "Checklist" && (
          <div className="grid gap-4">
            <AlertsPanel />
            <DailyChecklist />
            <DataHealth />
          </div>
        )}
      </main>
      <footer className="mx-auto max-w-7xl px-4 pb-8 pt-4 text-center text-xs text-slate-600">
        CFM dashboard · scan → gate → execute → track · state.json is the source of truth
      </footer>
    </div>
  );
}
