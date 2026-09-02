import React from "react";
import { api, setActiveAccount } from "./api.js";
import { ErrorBoundary } from "./components/ui.jsx";
import Navbar from "./components/Navbar.jsx";
import TickerStrip from "./components/TickerStrip.jsx";
import GateTelemetry from "./components/GateTelemetry.jsx";
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
import PayoutsTab from "./components/PayoutsTab.jsx";

// "Calibration" is a DIAGNOSTIC surface, deliberately its own tab and
// deliberately off the daily monitoring path (Overview / Scan): the gate
// rejection telemetry must be reached on purpose, never encountered while
// deciding an entry, where it could subtly pressure the decision.
const TABS = ["Overview", "Scan", "Positions", "History", "Payouts", "Calibration", "Settings"];

export default function App() {
  const [tab, setTab] = React.useState("Overview");
  const [regimeStatus, setRegimeStatus] = React.useState("unknown");
  // The order flow: a non-null value renders the entry-gate + ticket view over
  // the current tab (ticker may be "" for a blank gate). Cleared on tab change.
  const [execute, setExecute] = React.useState(null);
  const [execNonce, setExecNonce] = React.useState(0);
  const [demo, setDemo] = React.useState(false);
  const [modeBusy, setModeBusy] = React.useState(false);
  // Accounts (books). `accountRegistry` is the list + the server's persisted
  // choice; `accountId` is the book THIS tab reads, sent as a header on every
  // request (see api.js) so two tabs can watch two accounts at once.
  const [accountRegistry, setAccountRegistry] = React.useState(null);
  const [accountId, setAccountId] = React.useState(null);
  const [accountBusy, setAccountBusy] = React.useState(false);
  // Bumped on every account switch: the tab panels are keyed on it, so switching
  // books remounts them and every panel refetches against the new store.
  const [accountNonce, setAccountNonce] = React.useState(0);
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
  // A scan-row deep link (a tapped SCAN_* transition push) focuses one ticker in
  // the Scorecard — {ticker, id}; a fresh id re-triggers focus for the same name.
  const [scanIntent, setScanIntent] = React.useState(null);
  // Bumped when the detached background scan finishes, so the Scan panels reload
  // with the freshly-warmed data (see ScanProgress).
  const [scanNonce, setScanNonce] = React.useState(0);
  // Whether a full-universe sweep is in flight, lifted out of ScanProgress. The
  // panels that read that sweep hold their fetch while it runs instead of racing
  // it — see ReadyToEnter / Scorecard.
  const [scanRunning, setScanRunning] = React.useState(false);
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
  }, [authed, execNonce, accountNonce]);

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
  // ticker/action re-triggers the modal. recId (optional) is the recommendation
  // that staged the action — it travels with the intent into the roll ticket so
  // the resulting execution carries source_rec_id (trust-layer matching).
  const goToAction = React.useCallback((action, ticker, reason, recId) => {
    if (!action || !ticker) return;
    setPositionIntent({ action, ticker, reason, recId, id: Date.now() });
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
      goToAction(action, ticker, params.get("reason") || undefined, params.get("rec_id") || undefined);
      params.delete("action"); params.delete("ticker"); params.delete("reason"); params.delete("rec_id");
      const qs = params.toString();
      window.history.replaceState({}, "", window.location.pathname + (qs ? `?${qs}` : ""));
    }
    // A ?tab=… deep link (e.g. the monthly-payout push targets /?tab=Payouts):
    // land on that tab, then strip the query so a refresh doesn't re-force it.
    const target = params.get("tab");
    if (target && TABS.includes(target)) {
      setTab(target);
      setExecute(null);
      // A scan-transition push targets /?tab=Scan&ticker=X — open the full
      // scorecard and focus that row (expand + scroll), mirroring how a payout
      // push lands on the finalize card, not just the tab.
      if (target === "Scan") {
        const scanTicker = params.get("ticker");
        if (scanTicker) {
          setScanDetails(true);
          setScanIntent({ ticker: scanTicker.toUpperCase(), id: Date.now() });
          params.delete("ticker");
        }
      }
      params.delete("tab");
      const qs = params.toString();
      window.history.replaceState({}, "", window.location.pathname + (qs ? `?${qs}` : ""));
    }
    // In-app "Act" clicks from the Alerts panel (and recommendation-card
    // Execute buttons, which additionally carry rec_id) dispatch this event.
    const onAction = (e) =>
      goToAction(e.detail?.action, e.detail?.ticker, e.detail?.reason, e.detail?.rec_id);
    window.addEventListener("cfm-action", onAction);
    return () => window.removeEventListener("cfm-action", onAction);
  }, [authed, goToAction]);

  React.useEffect(() => {
    if (authed !== true) return;
    api.mode().then((m) => setDemo(!!m.demo)).catch(() => {});
  }, [authed]);

  // Load the account registry once signed in. A ?account=… deep link (an alert
  // push is raised against ONE book) wins over the server's persisted choice, so
  // a tapped notification lands on the account the alert is about.
  const loadAccounts = React.useCallback(async () => {
    const registry = await api.accounts();
    setAccountRegistry(registry);
    return registry;
  }, []);

  React.useEffect(() => {
    if (authed !== true) return;
    loadAccounts()
      .then((registry) => {
        const params = new URLSearchParams(window.location.search);
        const requested = params.get("account");
        const known = (registry.accounts || []).some((a) => a.id === requested);
        const id = known ? requested : registry.active;
        setActiveAccount(id);
        setAccountId(id);
        if (known) {
          params.delete("account");
          const qs = params.toString();
          window.history.replaceState({}, "", window.location.pathname + (qs ? `?${qs}` : ""));
        }
      })
      .catch(() => {}); // an older backend has no accounts endpoint — stay single-book
  }, [authed, loadAccounts]);

  // Switch the book this tab reads. The server's persisted choice moves too, so
  // background alerting/reconciliation reporting and the next session agree with
  // what's on screen.
  const switchAccount = React.useCallback(async (id) => {
    if (!id || id === accountId) return;
    setAccountBusy(true);
    try {
      setActiveAccount(id);          // every subsequent request carries the header
      setAccountId(id);
      await api.setActiveAccountOnServer(id).catch(() => {});
      await loadAccounts().catch(() => {});
      setExecute(null);              // an order ticket belongs to the book it was opened in
      setPositionIntent(null);
      setAccountNonce((n) => n + 1); // remount the tabs -> every panel refetches
    } finally {
      setAccountBusy(false);
    }
  }, [accountId, loadAccounts]);

  React.useEffect(() => {
    if (authed !== true) return;
    api.strikePosture().then((p) => setPosture(p.posture)).catch(() => {});
  }, [authed, demo, accountNonce]); // re-read on demo/live/account switch — posture is per-store

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
              onAlertsClick={() => goToTab("Settings")}
              accounts={accountRegistry?.accounts}
              accountId={accountId} accountBusy={accountBusy}
              onSelectAccount={switchAccount}
              onManageAccounts={() => goToTab("Settings")}>
        {/* Keyed on the account and on fills so it refetches when either changes. */}
        <TickerStrip key={`${accountNonce}:${execNonce}`} />
      </Navbar>
      <main className="mx-auto max-w-7xl px-3 py-4 sm:px-4 sm:py-6">
        <SchwabStatus demo={demo} />
        {/* One boundary around the tab content, keyed on the view: a render throw
            inside a tab (or the order ticket) degrades to a single error card
            instead of tearing down the whole app, and the nav above stays live so
            switching tabs remounts the boundary and clears the error. */}
        <ErrorBoundary key={execute ? `execute:${execute.id}` : `tab:${tab}`}
                       label={execute ? "The order ticket" : tab}>
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
                key={`overview:${accountNonce}`}
                onNavigate={goToTab}
                onSelectStock={openTicket}
                onAction={goToAction}
                onRegimeStatus={setRegimeStatus}
                accountId={accountId}
                accountNonce={accountNonce}
                onSelectAccount={switchAccount}
              />
            )}
            {tab === "Scan" && (
              <div className="grid gap-4">
                <ScanProgress onComplete={() => setScanNonce((n) => n + 1)}
                              onRunningChange={setScanRunning} />
                <ReadyToEnter onSelectStock={openTicket} refreshKey={scanNonce}
                              scanRunning={scanRunning} />
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
                {scanDetails && (
                  <Scorecard regimeStatus={regimeStatus} refreshKey={scanNonce}
                             scanRunning={scanRunning}
                             focusTicker={scanIntent}
                             onFocusHandled={() => setScanIntent(null)} />
                )}
              </div>
            )}
            {tab === "Positions" && (
              <PositionTracker key={`${accountNonce}:${execNonce}`} intent={positionIntent}
                               onIntentHandled={() => setPositionIntent(null)}
                               onOpenTicket={openTicket} />
            )}
            {tab === "History" && <HistoryTab key={`${accountNonce}:${execNonce}`} />}
            {tab === "Payouts" && <PayoutsTab key={`${accountNonce}:${execNonce}`} />}
            {tab === "Calibration" && <GateTelemetry />}
            {tab === "Settings" && (
              <SettingsTab demo={demo} modeBusy={modeBusy} onToggleDemo={toggleDemo}
                           posture={posture} postureBusy={postureBusy}
                           onTogglePosture={togglePosture}
                           accountRegistry={accountRegistry} accountId={accountId}
                           onSelectAccount={switchAccount}
                           onAccountsChanged={loadAccounts} />
            )}
          </>
        )}
        </ErrorBoundary>
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
