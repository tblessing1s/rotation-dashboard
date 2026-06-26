import React from "react";
import Navbar from "./components/Navbar.jsx";
import RegimeScanner from "./components/RegimeScanner.jsx";
import StockFilter from "./components/StockFilter.jsx";
import ExecuteTab from "./components/ExecuteTab.jsx";
import ThetaLedger from "./components/ThetaLedger.jsx";
import KillSwitchMonitor from "./components/KillSwitchMonitor.jsx";
import PositionTracker from "./components/PositionTracker.jsx";
import DailyChecklist from "./components/DailyChecklist.jsx";

const TABS = ["Scan", "Execute", "Theta", "Kill Switch", "Positions", "Checklist"];

export default function App() {
  const [tab, setTab] = React.useState("Scan");
  const [regimeStatus, setRegimeStatus] = React.useState("unknown");
  const [selectedTicker, setSelectedTicker] = React.useState("");
  const [execNonce, setExecNonce] = React.useState(0);

  function selectStock(ticker) {
    setSelectedTicker(ticker);
    setTab("Execute");
  }

  return (
    <div className="min-h-full bg-slate-950 text-slate-100">
      <Navbar tabs={TABS} active={tab} onChange={setTab} regimeStatus={regimeStatus} />
      <main className="mx-auto max-w-7xl px-4 py-6">
        {tab === "Scan" && (
          <div className="grid gap-4">
            <RegimeScanner onStatus={setRegimeStatus} />
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
        {tab === "Checklist" && <DailyChecklist />}
      </main>
      <footer className="mx-auto max-w-7xl px-4 pb-8 pt-4 text-center text-xs text-slate-600">
        CFM dashboard · scan → gate → execute → track · state.json is the source of truth
      </footer>
    </div>
  );
}
