import React, { useState, useEffect, useMemo, useCallback, useRef } from "react";
import { C } from "./theme.js";

/* ============================================================================
   INTRADAY SETUP EXECUTOR — Phase 2 (live monitor + alerts).
   Polls the Flask detection API and renders a live monitor for each watched
   stock: a 5-minute candle chart with yesterday's High/Low marked, a volume
   histogram, and the current volume ratio / distance to each level. When a
   setup triggers on a closed candle it raises a blinking alert, a modal with
   the entry / stop / target / position size, and (opt-in) a desktop
   notification. A playback panel replays a historical session so alerts can be
   validated against paper-trading data.

     GET  /api/executor/config
     POST /api/executor/monitor   {config, refresh, date?}
     POST /api/executor/playback  {config, date, autoBackfill}
     GET  /api/executor/signals?date=YYYY-MM-DD
     POST /api/executor/paper/execute  {signal}
     POST /api/executor/schwab/preview {signal}
     GET  /api/executor/paper/trades?date=YYYY-MM-DD

   Two execution paths, neither of which fills with money:
     • Execute Paper      — logs a simulated bracket trade in-app.
     • Preview on Schwab  — dry-runs the bracket against Schwab's previewOrder
                            endpoint (validates buying power / pricing / fees on
                            the real account, but NOTHING is placed). Schwab has
                            no paper-trading API, so this is the safe equivalent.
   ============================================================================ */

const API = "";
const DEFAULT_TICKERS = ["CRWV", "HIMS", "CVNA", "HOOD", "TOST"];
const SETUP_TYPES = [
  { value: "support_resistance_break", label: "S/R breakout — close beyond yesterday's level" },
  { value: "support_resistance_bounce", label: "S/R bounce — fade yesterday's level" },
];
const POLL_CHOICES = [
  { value: 15, label: "15s" },
  { value: 30, label: "30s" },
  { value: 60, label: "60s" },
];

// Execution mode binds the data source + execution adapter on the backend.
// Default is blank — the trader must consciously pick a mode before running an
// engine session, so PAPER/LIVE are never entered by accident.
const MODE_CHOICES = [
  { value: "", label: "— Select mode —" },
  { value: "REPLAY", label: "REPLAY — offline historical playback" },
  { value: "PAPER", label: "PAPER — live data, simulated fills" },
  { value: "LIVE", label: "LIVE — guarded (no orders placed)" },
];
const MODE_META = {
  "": { label: "No mode", color: "#8a93a6", hint: "Pick a mode to run an engine session." },
  REPLAY: { label: "REPLAY", color: "#3b82f6", hint: "Offline replay of stored candles — reproduces backtest results." },
  PAPER: { label: "PAPER", color: "#22c55e", hint: "Real-time Schwab data, simulated execution. Nothing is placed." },
  LIVE: { label: "LIVE", color: "#ef4444", hint: "Guarded scaffold — live order placement is intentionally disabled." },
};

const today = () => new Date().toISOString().slice(0, 10);

const DEFAULT_FORM = {
  tickers: DEFAULT_TICKERS.join(", "),
  setupType: "support_resistance_break",
  proximityPct: 0,
  volumeMultiplier: 2,
  volAvgLength: 50,
  riskReward: 2,
  atrMultiplier: 2,
  atrPeriod: 14,
  fixedRisk: 20,
  startTime: "08:30",
  endTime: "10:00",
  // Engine (PAPER/REPLAY) knobs — surfaced so paper assumptions are explicit.
  entrySlip: 0.02,
  stopSlip: 0.02,
  exitGranularity: "tick",
  gapRule: true,
};

const GRANULARITY_OPTIONS = [
  { value: "tick", label: "Tick (live quote)" },
  { value: "1min", label: "1-minute bars" },
];

// Form state -> the monitor-config JSON the backend validates (mirrors the
// executor's DEFAULT_MONITOR_CONFIG so live detection matches the backtester).
function buildConfig(f) {
  return {
    tickers: String(f.tickers || "").split(",").map((t) => t.trim().toUpperCase()).filter(Boolean),
    setup_conditions: { type: f.setupType, use_yesterday_levels: true, proximity_pct: Number(f.proximityPct) },
    entry_rules: { volume_multiplier: Number(f.volumeMultiplier), vol_avg_length: Number(f.volAvgLength) || 50, entry_timing: "candle_close" },
    risk_reward: Number(f.riskReward),
    stop_logic: "atr_beyond_level",
    stop_params: { atr_multiplier: Number(f.atrMultiplier), atr_period: Number(f.atrPeriod) || 14, atr_timeframe: "intraday" },
    time_window: { start_time: f.startTime, end_time: f.endTime },
    fixed_risk_per_trade: Number(f.fixedRisk),
    // Engine adapter knobs (ignored by the detection-only monitor endpoint).
    entry_slippage: { type: "cents", value: Number(f.entrySlip) || 0 },
    stop_slippage: { type: "cents", value: Number(f.stopSlip) || 0 },
    exit_resolution_granularity: f.exitGranularity || "tick",
    gap_rule: f.gapRule !== false,
  };
}

async function postJson(path, body) {
  const r = await fetch(`${API}${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, data };
}

const signalKey = (s) => `${s.date}|${s.ticker}|${s.candle_time}`;
const tradeSignalKey = (t) => `${t.date}|${t.ticker}|${t.entry_time}`;

// Test-orders mode: synthesize a signal from a card's price data so the execute /
// preview buttons appear on every watchlist card without waiting for a real setup
// to trigger — lets the order flow be exercised end-to-end. Off by default and
// gated behind the "Test orders" switch. Params are synthetic (0.3% risk off the
// last close / yesterday's level) and the signal is flagged __test so the UI
// labels it; real detected signals are unaffected.
function buildTestSignal(m, form) {
  const entry = m?.last_close ?? m?.y_high ?? m?.y_low;
  if (entry == null) return null;
  const rr = Number(form?.riskReward) || 2;
  const fixedRisk = Number(form?.fixedRisk) || 20;
  const risk = Math.max(0.01, Math.round(entry * 0.003 * 100) / 100);
  const round2 = (v) => Math.round(v * 100) / 100;
  return {
    date: m.date || today(),
    ticker: m.ticker,
    candle_time: m.last_candle_time || "TEST",
    direction: "Long",
    level_type: "TEST",
    level: round2(m.y_high ?? entry),
    entry_price: round2(entry),
    stop_price: round2(entry - risk),
    target_price: round2(entry + rr * risk),
    risk,
    reward: round2(rr * risk),
    risk_reward_ratio: rr,
    position_size: Math.max(1, Math.floor(fixedRisk / risk)),
    volume_ratio: m.volume_ratio ?? null,
    __test: true,
  };
}
const STATE_LABEL = {
  monitoring: { text: "Monitoring", color: C.green },
  waiting: { text: "Waiting for window", color: C.yellow },
  "no-levels": { text: "No yesterday levels", color: C.amber },
  "no-data": { text: "No data", color: C.inkFaint },
};

export default function ExecutorView({ store }) {
  const [form, setForm] = useState(() => ({ ...DEFAULT_FORM, ...(store?.get("executorForm", {}) || {}) }));
  const [monitors, setMonitors] = useState([]);
  const [liveSignals, setLiveSignals] = useState([]);   // signals from the latest scan
  const [logSignals, setLogSignals] = useState([]);     // full persisted log for the day
  const [scanAt, setScanAt] = useState(null);
  const [status, setStatus] = useState("");
  const [errors, setErrors] = useState([]);
  const [scanning, setScanning] = useState(false);
  const [paperTrades, setPaperTrades] = useState([]);
  const [executingKey, setExecutingKey] = useState(null);
  const [previewingKey, setPreviewingKey] = useState(null);
  const [preview, setPreview] = useState(null);   // last Schwab dry-run result
  const [dismissedCardSignals, setDismissedCardSignals] = useState(() => new Set());

  // Execution mode (blank by default — see MODE_CHOICES).
  const [mode, setMode] = useState(() => store?.get("executorMode", "") || "");
  const changeMode = useCallback((v) => { setMode(v); store?.set("executorMode", v); }, [store]);
  const modeRef = useRef(mode);
  modeRef.current = mode;

  const [autoRefresh, setAutoRefresh] = useState(false);
  const [pollSec, setPollSec] = useState(30);
  // Test-orders switch: show execute/preview on every card for flow testing.
  const [testOrders, setTestOrders] = useState(() => Boolean(store?.get("executorTestOrders", false)));
  const toggleTestOrders = useCallback((v) => {
    setTestOrders(v);
    store?.set("executorTestOrders", v);
  }, [store]);
  const [notify, setNotify] = useState(typeof Notification !== "undefined" && Notification.permission === "granted");

  // Alerts the trader has already seen/dismissed (sticky across refreshes).
  const [acked, setAcked] = useState(() => new Set(store?.get("executorAcked", []) || []));
  const [alertQueue, setAlertQueue] = useState([]);     // signals awaiting acknowledgement
  const ackedRef = useRef(acked);
  ackedRef.current = acked;

  // Playback (validate alerts against a historical session)
  const [pbDate, setPbDate] = useState(today());
  const [pbRunning, setPbRunning] = useState(false);
  const [pbSignals, setPbSignals] = useState(null);
  const [pbTrades, setPbTrades] = useState(null);     // REPLAY engine trades (with outcomes)
  const [pbSummary, setPbSummary] = useState(null);
  const [pbStatus, setPbStatus] = useState("");

  const set = useCallback((k, v) => setForm((prev) => {
    const next = { ...prev, [k]: v };
    store?.set("executorForm", next);
    return next;
  }), [store]);

  const persistAcked = useCallback((nextSet) => {
    setAcked(nextSet);
    store?.set("executorAcked", Array.from(nextSet));
  }, [store]);

  const fireDesktopNotification = useCallback((sig) => {
    if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
    try {
      new Notification(`${sig.ticker} ${sig.direction} setup`, {
        body: `Entry ${sig.entry_price} · Stop ${sig.stop_price} · Target ${sig.target_price} · ${sig.position_size} sh`,
        tag: signalKey(sig),   // dedupe re-fires of the same candle
      });
    } catch (e) { /* notifications can throw in some embedded contexts */ }
  }, []);

  // Route freshly detected signals into the alert queue + desktop notifications,
  // skipping any the trader already acknowledged.
  const ingestSignals = useCallback((signals) => {
    const fresh = (signals || []).filter((s) => !ackedRef.current.has(signalKey(s)));
    if (!fresh.length) return;
    setAlertQueue((q) => {
      const have = new Set(q.map(signalKey));
      const add = fresh.filter((s) => !have.has(signalKey(s)));
      add.forEach(fireDesktopNotification);
      return [...q, ...add];
    });
  }, [fireDesktopNotification]);

  const refreshLog = useCallback(async (date) => {
    try {
      const r = await fetch(`${API}/api/executor/signals?date=${encodeURIComponent(date)}`);
      const data = await r.json().catch(() => ({}));
      if (data.ok) setLogSignals(data.signals || []);
    } catch (e) { /* log is best-effort */ }
  }, []);

  const refreshPaperTrades = useCallback(async (date) => {
    try {
      const r = await fetch(`${API}/api/executor/paper/trades?date=${encodeURIComponent(date)}`);
      const data = await r.json().catch(() => ({}));
      if (data.ok) setPaperTrades(data.trades || []);
    } catch (e) { /* paper trade log is best-effort */ }
  }, []);

  // PAPER mode: open newly detected setups at the live price and resolve open
  // virtual trades against the live feed. Detection/sizing are shared with REPLAY
  // (StrategyCore); only the bound data source + adapter differ. Nothing is placed.
  const runPaperSession = useCallback(async (refresh) => {
    const { ok, data } = await postJson("/api/executor/paper/session", { config: buildConfig(form), refresh });
    if (!ok || data.ok === false) {
      setErrors((data.errors) || [data.error || "Paper session failed."]);
      return null;
    }
    await refreshPaperTrades(data.date || today());
    return data;
  }, [form, refreshPaperTrades]);

  const scan = useCallback(async (refresh) => {
    setScanning(true); setErrors([]);
    setStatus(refresh ? "Pulling today's 5-minute bars, then scanning…" : "Scanning latest closed candles…");
    const { ok, data } = await postJson("/api/executor/monitor", { config: buildConfig(form), refresh });
    if (!ok || data.ok === false) {
      setScanning(false);
      setErrors(data.errors || [data.error || "Scan failed."]);
      setStatus("");
      return;
    }
    setMonitors(data.monitors || []);
    setLiveSignals(data.signals || []);
    setScanAt(new Date());
    ingestSignals(data.signals);
    refreshLog(data.date || today());
    refreshPaperTrades(data.date || today());
    const live = (data.monitors || []).filter((m) => m.state === "monitoring").length;
    let note = `Scanned ${data.monitors?.length || 0} ticker(s) · ${live} live · ${data.signals?.length || 0} active signal(s).`;

    // In PAPER mode the same poll also drives the real-time virtual execution.
    if (modeRef.current === "PAPER") {
      const paper = await runPaperSession(refresh);
      if (paper) note += ` · Paper: ${paper.opened?.length || 0} opened, ${paper.resolved?.length || 0} resolved.`;
    }
    setScanning(false);
    setStatus(note);
  }, [form, ingestSignals, refreshLog, refreshPaperTrades, runPaperSession]);

  // Auto-refresh polling.
  useEffect(() => {
    if (!autoRefresh) return undefined;
    const id = setInterval(() => { scan(true); }, Math.max(10, pollSec) * 1000);
    return () => clearInterval(id);
  }, [autoRefresh, pollSec, scan]);

  const enableNotifications = useCallback(async () => {
    if (typeof Notification === "undefined") { setStatus("Desktop notifications aren't supported in this browser."); return; }
    const perm = await Notification.requestPermission();
    setNotify(perm === "granted");
    if (perm !== "granted") setStatus("Notifications blocked — enable them in your browser to get desktop alerts.");
  }, []);

  const ackAlert = useCallback((sig) => {
    const next = new Set(ackedRef.current); next.add(signalKey(sig)); persistAcked(next);
    setAlertQueue((q) => q.filter((s) => signalKey(s) !== signalKey(sig)));
  }, [persistAcked]);

  const dismissCardSignal = useCallback((sig) => {
    setDismissedCardSignals((prev) => {
      const next = new Set(prev);
      next.add(signalKey(sig));
      return next;
    });
  }, []);

  const executePaper = useCallback(async (sig) => {
    const key = signalKey(sig);
    setExecutingKey(key);
    setErrors([]);
    const { ok, data } = await postJson("/api/executor/paper/execute", { signal: sig });
    setExecutingKey(null);
    if (!ok || data.ok === false) {
      setErrors(data.errors || [data.error || "Paper execution failed."]);
      return;
    }
    setStatus(`Paper trade logged for ${data.trade?.ticker || sig.ticker} (${data.trade?.order_id || "simulated order"}).`);
    await refreshPaperTrades(sig.date || today());
    dismissCardSignal(sig);
    ackAlert(sig);
  }, [ackAlert, dismissCardSignal, refreshPaperTrades]);

  // Dry-run the bracket against Schwab's previewOrder endpoint. This validates
  // the order against the real account (buying power / pricing / fees) but never
  // fills — Schwab has no paper-trading API, so this is the safe equivalent.
  const previewSchwab = useCallback(async (sig) => {
    const key = signalKey(sig);
    setPreviewingKey(key);
    setErrors([]);
    const { ok, data } = await postJson("/api/executor/schwab/preview", { signal: sig });
    setPreviewingKey(null);
    if (!ok || data.ok === false) {
      setErrors(data.errors || [data.error || "Schwab preview failed."]);
      return;
    }
    setPreview({ signal: sig, ...data });
    const p = data.preview || {};
    setStatus(`Schwab preview for ${sig.ticker}: ${p.status || "OK"}${p.account || data.account ? ` on ${data.account}` : ""} — nothing was placed.`);
  }, []);

  const runPlayback = useCallback(async () => {
    setPbRunning(true); setPbStatus("Replaying session…");
    setPbSignals(null); setPbTrades(null); setPbSummary(null);
    // In REPLAY mode, run the full engine so exits resolve to outcomes (the
    // backtest-equivalent path); otherwise just list the signals that would fire.
    const replayMode = modeRef.current === "REPLAY";
    const path = replayMode ? "/api/executor/replay" : "/api/executor/playback";
    const { ok, data } = await postJson(path, { config: buildConfig(form), date: pbDate, autoBackfill: true });
    setPbRunning(false);
    if (!ok || data.ok === false) { setPbStatus((data.errors || [data.error || "Playback failed."]).join(" ")); return; }
    if (replayMode) {
      setPbTrades(data.trades || []); setPbSummary(data.summary || null);
      setPbStatus(`${data.count} trade(s) resolved on ${pbDate}.`);
    } else {
      setPbSignals(data.signals || []);
      setPbStatus(`${data.count} signal(s) would have fired on ${pbDate}.`);
    }
  }, [form, pbDate]);

  const executedSignalKeys = useMemo(() => new Set((paperTrades || []).map(tradeSignalKey)), [paperTrades]);
  const cardSignalsByTicker = useMemo(() => {
    const byTicker = new Map();

    // Prefer active signals from the latest scan so the card still blinks while
    // the setup is live. Fall back to today's signal log so traders can execute
    // from the watchlist card even after the latest scan no longer reports the
    // signal as active.
    liveSignals.forEach((s) => {
      if (!executedSignalKeys.has(signalKey(s)) && !acked.has(signalKey(s))) byTicker.set(s.ticker, s);
    });

    [...logSignals]
      .filter((s) => !executedSignalKeys.has(signalKey(s)) && !dismissedCardSignals.has(signalKey(s)))
      .sort((a, b) => `${b.date || ""} ${b.candle_time || ""}`.localeCompare(`${a.date || ""} ${a.candle_time || ""}`))
      .forEach((s) => {
        if (!byTicker.has(s.ticker)) byTicker.set(s.ticker, s);
      });

    return byTicker;
  }, [acked, dismissedCardSignals, executedSignalKeys, liveSignals, logSignals]);
  const activeSignalTickers = useMemo(() => new Set(liveSignals.map((s) => s.ticker)), [liveSignals]);
  const activeAlert = alertQueue[0] || null;
  // Latest price per ticker (from the monitor scan) for live unrealized P&L.
  const priceByTicker = useMemo(
    () => Object.fromEntries((monitors || []).filter((m) => m.last_close != null).map((m) => [m.ticker, m.last_close])),
    [monitors]);
  const tickerList = useMemo(
    () => String(form.tickers || "").split(",").map((t) => t.trim().toUpperCase()).filter(Boolean),
    [form.tickers]);

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <style>{`
        @keyframes execBlink { 0%,100% { box-shadow: 0 0 0 1px ${C.red}; } 50% { box-shadow: 0 0 14px 1px ${C.red}; } }
        @keyframes execFade { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
      `}</style>

      <div style={{ display: "flex", alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
        <SectionHeader title="Intraday Executor" subtitle="Live setup detection on 5-minute candles — alerts when price breaks yesterday's level on a volume spike." />
        <span style={{ flex: 1 }} />
        <SessionClock startTime={form.startTime} endTime={form.endTime} />
        <ModeBadge mode={mode} />
      </div>

      {mode === "LIVE" && (
        <div style={{ padding: 12, border: `1px solid ${C.red}`, borderRadius: 10, background: "#1a0e14" }}>
          <div style={{ font: `600 12px ${C.sans}`, color: C.red }}>🔒 LIVE mode is guarded</div>
          <div style={{ marginTop: 4, font: `400 12px ${C.sans}`, color: C.inkDim }}>
            Live order placement is intentionally disabled. Detection runs, but no real Schwab order is transmitted —
            use <b>Execute Paper</b> or <b>Preview on Schwab</b> (dry-run) instead.
          </div>
        </div>
      )}

      {/* ---- Controls ---- */}
      <Card>
        <Grid>
          <Field label="Watchlist (comma-separated)">
            <Input value={form.tickers} onChange={(e) => set("tickers", e.target.value)} placeholder="CRWV, HIMS, CVNA, HOOD, TOST" />
          </Field>
          <Field label="Setup type"><Select value={form.setupType} onChange={(e) => set("setupType", e.target.value)} options={SETUP_TYPES} /></Field>
          <Field label="Volume spike (×average)" hint="Candle volume must exceed N× the 50-bar volume average.">
            <Input type="number" step="0.1" value={form.volumeMultiplier} onChange={(e) => set("volumeMultiplier", e.target.value)} />
          </Field>
          <Field label="Risk : reward" hint="Target = entry + R×risk."><Input type="number" step="0.5" value={form.riskReward} onChange={(e) => set("riskReward", e.target.value)} /></Field>
          <Field label="Stop = ATR × N beyond level"><Input type="number" step="0.5" value={form.atrMultiplier} onChange={(e) => set("atrMultiplier", e.target.value)} /></Field>
          <Field label="Fixed risk per trade ($)" hint="Sized into share count from the stop distance."><Input type="number" step="1" value={form.fixedRisk} onChange={(e) => set("fixedRisk", e.target.value)} /></Field>
          <Field label="Window start (CT)"><Input type="time" value={form.startTime} onChange={(e) => set("startTime", e.target.value)} /></Field>
          <Field label="Window end (CT)"><Input type="time" value={form.endTime} onChange={(e) => set("endTime", e.target.value)} /></Field>
          <Field label="Entry slippage ($/sh)" hint="Adverse haircut applied to the live entry fill (PAPER).">
            <Input type="number" step="0.01" value={form.entrySlip} onChange={(e) => set("entrySlip", e.target.value)} />
          </Field>
          <Field label="Stop slippage ($/sh)" hint="Pessimistic extra on stop fills (PAPER).">
            <Input type="number" step="0.01" value={form.stopSlip} onChange={(e) => set("stopSlip", e.target.value)} />
          </Field>
          <Field label="Exit resolution" hint="How PAPER resolves stop-vs-target.">
            <Select value={form.exitGranularity} onChange={(e) => set("exitGranularity", e.target.value)} options={GRANULARITY_OPTIONS} />
          </Field>
          <Field label="Gap rule" hint="Don't trade a gapped-open day until price re-enters yesterday's range.">
            <div style={{ paddingTop: 6 }}><Toggle checked={form.gapRule} onChange={(v) => set("gapRule", v)} label={form.gapRule ? "On" : "Off"} /></div>
          </Field>
        </Grid>

        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginTop: 16, paddingTop: 14, borderTop: `1px solid ${C.lineSoft}` }}>
          <label style={{ display: "flex", alignItems: "center", gap: 7 }}>
            <span style={{ font: `600 11px ${C.sans}`, color: C.inkDim, textTransform: "uppercase", letterSpacing: 0.3 }}>Mode</span>
            <select value={mode} onChange={(e) => changeMode(e.target.value)} style={{ ...selectStyle, borderColor: MODE_META[mode]?.color || C.line }}>
              {MODE_CHOICES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
            </select>
          </label>
          <Button primary disabled={scanning} onClick={() => scan(true)}>{scanning ? "Scanning…" : "Refresh + Scan"}</Button>
          <Button disabled={scanning} onClick={() => scan(false)} title="Re-evaluate the latest stored candles without pulling new data.">Scan stored</Button>
          <Toggle checked={autoRefresh} onChange={setAutoRefresh} label="Auto-refresh" />
          {autoRefresh && (
            <select value={pollSec} onChange={(e) => setPollSec(Number(e.target.value))} style={selectStyle}>
              {POLL_CHOICES.map((p) => <option key={p.value} value={p.value}>every {p.label}</option>)}
            </select>
          )}
          <Toggle checked={testOrders} onChange={toggleTestOrders} label="Test orders" />
          {testOrders && <span style={{ font: `500 11px ${C.mono}`, color: C.yellow }}>⚙ Execute/preview on every card (synthetic params)</span>}
          <span style={{ flex: 1 }} />
          {notify
            ? <span style={{ font: `500 12px ${C.mono}`, color: C.green }}>🔔 Desktop alerts on</span>
            : <Button onClick={enableNotifications}>Enable desktop alerts</Button>}
        </div>

        {status && <div style={{ marginTop: 10, font: `400 12px ${C.mono}`, color: C.inkDim }}>{status}{scanAt ? ` · ${scanAt.toLocaleTimeString()}` : ""}</div>}
        {errors.length > 0 && (
          <div style={{ marginTop: 10, padding: 10, border: `1px solid ${C.redDim}`, borderRadius: 8, background: "#1a0e14" }}>
            {errors.map((e, i) => <div key={i} style={{ font: `400 12px ${C.sans}`, color: C.red }}>• {e}</div>)}
          </div>
        )}
      </Card>

      {/* ---- Live monitor grid ---- */}
      {monitors.length > 0 ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 14 }}>
          {monitors.map((m) => {
            const realSig = cardSignalsByTicker.get(m.ticker);
            const testSig = (!realSig && testOrders) ? buildTestSignal(m, form) : null;
            const orderSig = realSig || testSig;
            const orderKey = orderSig ? signalKey(orderSig) : null;
            return (
              <MonitorCard
                key={m.ticker}
                m={m}
                signal={realSig}
                testSignal={testSig}
                signalIsActive={activeSignalTickers.has(m.ticker)}
                executing={executingKey === orderKey}
                previewing={previewingKey === orderKey}
                onExecute={executePaper}
                onPreview={previewSchwab}
                onAck={dismissCardSignal}
              />
            );
          })}
        </div>
      ) : (
        <div style={{ padding: 24, textAlign: "center", font: `400 13px ${C.sans}`, color: C.inkFaint, border: `1px dashed ${C.line}`, borderRadius: 10 }}>
          Set your watchlist above and hit <b>Refresh + Scan</b> to start monitoring. During market hours, enable auto-refresh for live alerts.
        </div>
      )}

      {/* ---- Today's signal log ---- */}
      {logSignals.length > 0 && (
        <Card>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
            <strong style={{ font: `600 13px ${C.sans}`, color: C.ink }}>Today's signals</strong>
            <span style={{ font: `400 12px ${C.mono}`, color: C.inkFaint }}>{logSignals.length}</span>
          </div>
          <SignalTable signals={logSignals} />
        </Card>
      )}

      {preview && <SchwabPreviewPanel preview={preview} onClose={() => setPreview(null)} />}

      <ActiveTradesPanel trades={paperTrades} priceByTicker={priceByTicker} />

      <HistoryPanel tickers={tickerList} nonce={paperTrades.length} />

      {/* ---- Playback (validate alerts on historical data) ---- */}
      <Card>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
          <Field label="Playback date" hint="Replay a past session to see which alerts would have fired (paper-trading validation).">
            <Input type="date" value={pbDate} onChange={(e) => setPbDate(e.target.value)} style={{ width: 160 }} />
          </Field>
          <Button disabled={pbRunning} onClick={runPlayback}>{pbRunning ? "Replaying…" : "Run playback"}</Button>
          {pbStatus && <span style={{ font: `400 12px ${C.mono}`, color: C.inkDim, paddingBottom: 8 }}>{pbStatus}</span>}
        </div>
        {pbSignals && pbSignals.length > 0 && <div style={{ marginTop: 12 }}><SignalTable signals={pbSignals} /></div>}
        {pbSignals && pbSignals.length === 0 && (
          <div style={{ marginTop: 12, font: `400 12px ${C.sans}`, color: C.inkFaint }}>No setups fired on that session for the current config.</div>
        )}
        {pbTrades && <ReplayResults trades={pbTrades} summary={pbSummary} />}
      </Card>

      {activeAlert && (
        <AlertModal
          sig={activeAlert}
          queued={alertQueue.length}
          executing={executingKey === signalKey(activeAlert)}
          previewing={previewingKey === signalKey(activeAlert)}
          onExecute={() => executePaper(activeAlert)}
          onPreview={() => previewSchwab(activeAlert)}
          onAck={() => ackAlert(activeAlert)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-ticker monitor card: chart + live stats
// ---------------------------------------------------------------------------
function MonitorCard({ m, signal, testSignal, signalIsActive, executing, previewing, onExecute, onPreview, onAck }) {
  const hasSignal = Boolean(signal);
  const orderSignal = signal || testSignal;   // test signal lets the flow be exercised without a real setup
  const isTest = !signal && Boolean(testSignal);
  const label = STATE_LABEL[m.state] || STATE_LABEL["no-data"];
  return (
    <div style={{
      background: C.panel, border: `1px solid ${hasSignal ? C.red : C.line}`, borderRadius: 10, padding: 14,
      animation: signalIsActive ? "execBlink 1.1s ease-in-out infinite" : "none",
    }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
        <strong style={{ font: `700 15px ${C.sans}`, color: C.ink }}>{m.ticker}</strong>
        {m.last_close != null && <span style={{ font: `600 14px ${C.mono}`, color: C.ink }}>{m.last_close.toFixed(2)}</span>}
        <span style={{ flex: 1 }} />
        <span style={{ font: `600 10px ${C.sans}`, color: label.color, textTransform: "uppercase", letterSpacing: 0.4 }}>
          {signalIsActive ? "⚡ Setup!" : hasSignal ? "Setup logged" : label.text}
        </span>
      </div>

      <MiniChart candles={m.candles || []} yHigh={m.y_high} yLow={m.y_low} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8, marginTop: 10 }}>
        <Mini label="Y-High" value={fmt(m.y_high)} color={C.red} />
        <Mini label="Y-Low" value={fmt(m.y_low)} color={C.green} />
        <Mini label="RVOL" value={m.volume_ratio == null ? "—" : `${m.volume_ratio}×`} color={m.volume_ratio >= 2 ? C.green : C.inkDim} />
        <Mini label="Last bar (CT)" value={m.last_candle_time || "—"} />
        <Mini label="→ Y-High" value={m.pct_to_high == null ? "—" : `${m.pct_to_high > 0 ? "+" : ""}${m.pct_to_high}%`} />
        <Mini label="→ Y-Low" value={m.pct_to_low == null ? "—" : `${m.pct_to_low > 0 ? "+" : ""}${m.pct_to_low}%`} />
      </div>
      {orderSignal && (
        <div style={{ marginTop: 12, paddingTop: 10, borderTop: `1px solid ${C.lineSoft}` }}>
          {isTest && (
            <div style={{ font: `600 9px ${C.sans}`, color: C.yellow, textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 6 }}>
              ⚙ Test order — synthetic params for flow testing
            </div>
          )}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8, marginBottom: 10 }}>
            <Mini label="Entry" value={fmt(orderSignal.entry_price)} color={C.ink} />
            <Mini label="Stop" value={fmt(orderSignal.stop_price)} color={C.red} />
            <Mini label="Target" value={fmt(orderSignal.target_price)} color={C.green} />
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <Button primary disabled={executing} onClick={() => onExecute(orderSignal)}>{executing ? "Logging…" : "Execute Paper"}</Button>
            <Button disabled={previewing} onClick={() => onPreview(orderSignal)} title="Dry-run the bracket against Schwab's previewOrder endpoint — validates the order without placing it.">{previewing ? "Previewing…" : "Preview on Schwab"}</Button>
            {!isTest && <Button onClick={() => onAck(signal)}>Skip</Button>}
            <span style={{ flex: 1 }} />
            <span style={{ font: `400 10px ${C.sans}`, color: C.inkFaint, textAlign: "right" }}>
              {orderSignal.position_size} sh · paper / dry-run
            </span>
          </div>
        </div>
      )}
      {m.note && <div style={{ marginTop: 8, font: `400 11px ${C.sans}`, color: C.inkFaint }}>{m.note}</div>}
    </div>
  );
}

function Mini({ label, value, color }) {
  return (
    <div>
      <div style={{ font: `600 9px ${C.sans}`, color: C.inkFaint, textTransform: "uppercase", letterSpacing: 0.3 }}>{label}</div>
      <div style={{ font: `600 13px ${C.mono}`, color: color || C.inkDim }}>{value}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Inline SVG candle + volume chart (no external chart dependency)
// ---------------------------------------------------------------------------
function MiniChart({ candles, yHigh, yLow }) {
  const W = 300, H = 150, PRICE_H = 112, VOL_H = 30, GAP = 8;
  if (!candles.length) {
    return (
      <div style={{ height: H, marginTop: 10, display: "grid", placeItems: "center", background: C.panel2, border: `1px solid ${C.lineSoft}`, borderRadius: 8 }}>
        <span style={{ font: `400 11px ${C.mono}`, color: C.inkFaint }}>waiting for candles…</span>
      </div>
    );
  }
  const highs = candles.map((c) => c.high), lows = candles.map((c) => c.low);
  let max = Math.max(...highs, yHigh ?? -Infinity), min = Math.min(...lows, yLow ?? Infinity);
  const pad = (max - min) * 0.06 || 1;
  max += pad; min -= pad;
  const maxVol = Math.max(...candles.map((c) => c.volume), 1);
  const span = max - min || 1;
  const y = (p) => ((max - p) / span) * PRICE_H;
  const n = candles.length;
  const slot = W / n;
  const bodyW = Math.max(2, slot * 0.6);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} style={{ marginTop: 10, background: C.panel2, border: `1px solid ${C.lineSoft}`, borderRadius: 8 }} preserveAspectRatio="none">
      {/* yesterday's levels */}
      {yHigh != null && <line x1="0" x2={W} y1={y(yHigh)} y2={y(yHigh)} stroke={C.red} strokeWidth="1" strokeDasharray="4 3" opacity="0.8" />}
      {yLow != null && <line x1="0" x2={W} y1={y(yLow)} y2={y(yLow)} stroke={C.green} strokeWidth="1" strokeDasharray="4 3" opacity="0.8" />}
      {/* candles */}
      {candles.map((c, i) => {
        const cx = i * slot + slot / 2;
        const up = c.close >= c.open;
        const col = up ? C.green : C.red;
        const bodyTop = y(Math.max(c.open, c.close));
        const bodyBot = y(Math.min(c.open, c.close));
        const volTop = PRICE_H + GAP + (VOL_H - (c.volume / maxVol) * VOL_H);
        return (
          <g key={i}>
            <line x1={cx} x2={cx} y1={y(c.high)} y2={y(c.low)} stroke={col} strokeWidth="1" />
            <rect x={cx - bodyW / 2} y={bodyTop} width={bodyW} height={Math.max(1, bodyBot - bodyTop)} fill={col} />
            <rect x={cx - bodyW / 2} y={volTop} width={bodyW} height={PRICE_H + GAP + VOL_H - volTop} fill={col} opacity="0.5" />
          </g>
        );
      })}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Setup alert modal
// ---------------------------------------------------------------------------
function AlertModal({ sig, queued, executing, previewing, onExecute, onPreview, onAck }) {
  const dirColor = sig.direction === "Long" ? C.green : C.red;
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(3,6,10,0.72)", display: "grid", placeItems: "center", zIndex: 1000 }}>
      <div style={{ width: 360, background: C.panel, border: `1px solid ${C.red}`, borderRadius: 12, padding: 0, animation: "execFade 160ms ease-out", boxShadow: "0 18px 60px rgba(0,0,0,0.55)" }}>
        <div style={{ padding: "14px 18px", borderBottom: `1px solid ${C.line}`, display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ font: `700 15px ${C.sans}`, color: C.red }}>🚨 Setup triggered</span>
          <span style={{ flex: 1 }} />
          {queued > 1 && <span style={{ font: `500 11px ${C.mono}`, color: C.inkFaint }}>+{queued - 1} queued</span>}
        </div>
        <div style={{ padding: 18 }}>
          <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 12 }}>
            <strong style={{ font: `700 22px ${C.sans}`, color: C.ink }}>{sig.ticker}</strong>
            <span style={{ font: `700 13px ${C.sans}`, color: dirColor, textTransform: "uppercase" }}>{sig.direction}</span>
            <span style={{ flex: 1 }} />
            <span style={{ font: `400 12px ${C.mono}`, color: C.inkFaint }}>{sig.candle_time} CT</span>
          </div>
          <Row label="Entry" value={fmt(sig.entry_price)} />
          <Row label="Stop" value={`${fmt(sig.stop_price)}  (${sig.direction === "Long" ? "-" : "+"}${fmt(sig.risk)})`} color={C.red} />
          <Row label="Target" value={`${fmt(sig.target_price)}  (${sig.direction === "Long" ? "+" : "-"}${fmt(sig.reward)})`} color={C.green} />
          <Row label="Position" value={`${sig.position_size} shares`} />
          <Row label="Risk : reward" value={sig.risk_reward_ratio == null ? "—" : `${sig.risk_reward_ratio} : 1`} />
          <Row label="Volume ratio" value={sig.volume_ratio == null ? "—" : `${sig.volume_ratio}× ${sig.volume_ratio >= 2 ? "✓" : ""}`} color={sig.volume_ratio >= 2 ? C.green : C.inkDim} />
          <Row label="Level" value={`${sig.level_type} ${fmt(sig.level)}`} />
        </div>
        <div style={{ padding: "12px 18px", borderTop: `1px solid ${C.line}`, display: "flex", gap: 10, flexWrap: "wrap" }}>
          <Button primary disabled={executing} onClick={onExecute}>{executing ? "Logging…" : "Execute Paper"}</Button>
          <Button disabled={previewing} onClick={onPreview} title="Dry-run the bracket against Schwab's previewOrder endpoint — validates the order without placing it.">{previewing ? "Previewing…" : "Preview on Schwab"}</Button>
          <Button onClick={onAck}>Skip</Button>
          <span style={{ flex: 1 }} />
          <span style={{ font: `400 10px ${C.sans}`, color: C.inkFaint, alignSelf: "center", maxWidth: 150, textAlign: "right" }}>Paper logs in-app; Schwab preview is a dry-run — neither fills.</span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Schwab preview (dry-run) result — proves the bracket would be accepted,
// without placing it. Schwab has no paper account API, so this is the safe path.
// ---------------------------------------------------------------------------
function SchwabPreviewPanel({ preview, onClose }) {
  const { signal = {}, account, preview: p = {} } = preview || {};
  const statusColor = p.status === "REJECTED" ? C.red : p.status === "WARNING" ? C.yellow : C.green;
  return (
    <Card>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <strong style={{ font: `600 13px ${C.sans}`, color: C.ink }}>Schwab order preview</strong>
        <span style={{ font: `600 11px ${C.sans}`, color: statusColor, textTransform: "uppercase", letterSpacing: 0.4 }}>{p.status || "OK"}</span>
        <span style={{ font: `400 12px ${C.mono}`, color: C.inkFaint }}>
          {signal.ticker} {signal.direction} · {signal.position_size} sh{account ? ` · ${account}` : ""}
        </span>
        <span style={{ flex: 1 }} />
        <span style={{ font: `600 10px ${C.sans}`, color: C.yellow, textTransform: "uppercase", letterSpacing: 0.3 }}>Dry-run · not placed</span>
        <Button onClick={onClose}>Dismiss</Button>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 10 }}>
        <Mini label="Entry" value={fmt(signal.entry_price)} color={C.ink} />
        <Mini label="Stop" value={fmt(signal.stop_price)} color={C.red} />
        <Mini label="Target" value={fmt(signal.target_price)} color={C.green} />
        <Mini label="Order value" value={p.orderValue == null ? "—" : `$${Number(p.orderValue).toFixed(2)}`} />
        <Mini label="Est. fees" value={p.estimatedCost == null ? "—" : `$${Number(p.estimatedCost).toFixed(2)}`} />
      </div>

      {p.rejects?.length > 0 && (
        <div style={{ marginTop: 12, padding: 10, border: `1px solid ${C.redDim}`, borderRadius: 8, background: "#1a0e14" }}>
          {p.rejects.map((r, i) => <div key={i} style={{ font: `400 12px ${C.sans}`, color: C.red }}>✕ {r}</div>)}
        </div>
      )}
      {p.alerts?.length > 0 && (
        <div style={{ marginTop: 10 }}>
          {p.alerts.map((a, i) => <div key={i} style={{ font: `400 11px ${C.sans}`, color: C.yellow }}>⚠ {a}</div>)}
        </div>
      )}

      <div style={{ marginTop: 12, font: `400 11px ${C.sans}`, color: C.inkFaint }}>
        Validated against the live Schwab account via <code>previewOrder</code> — no order was placed. Schwab has no paper-trading API; this confirms the bracket would be accepted.
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// US Central session clock + countdown to the window close (10:00 CT default)
// ---------------------------------------------------------------------------
const parseHM = (s) => { const [h, m] = String(s || "").split(":").map(Number); return (h || 0) * 60 + (m || 0); };

function ctParts() {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Chicago", hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(new Date());
  const get = (t) => Number(parts.find((p) => p.type === t)?.value);
  let h = get("hour"); if (h === 24) h = 0;   // some engines emit 24 at midnight
  return { h, m: get("minute"), s: get("second") };
}

function useCtClock() {
  const [, force] = useState(0);
  useEffect(() => { const id = setInterval(() => force((n) => n + 1), 1000); return () => clearInterval(id); }, []);
  return ctParts();
}

const fmtDur = (sec) => {
  const hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60), ss = sec % 60;
  return (hh > 0 ? `${hh}:` : "") + `${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
};

function SessionClock({ startTime, endTime }) {
  const { h, m, s } = useCtClock();
  const nowSec = h * 3600 + m * 60 + s;
  const startSec = parseHM(startTime) * 60, endSec = parseHM(endTime) * 60;
  let status, color, target, prefix;
  if (nowSec < startSec) { status = "Pre-window"; color = C.yellow; target = startSec; prefix = "opens in"; }
  else if (nowSec <= endSec) { status = "In window"; color = C.green; target = endSec; prefix = "closes in"; }
  else { status = "Closed"; color = C.inkFaint; target = null; prefix = null; }
  const remain = target != null ? Math.max(0, target - nowSec) : null;
  const clock = `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return (
    <div title="US Central session clock" style={{ display: "inline-flex", flexDirection: "column", alignItems: "flex-end", gap: 2 }}>
      <div style={{ font: `700 14px ${C.mono}`, color: C.ink }}>{clock} <span style={{ font: `500 10px ${C.sans}`, color: C.inkFaint }}>CT</span></div>
      <div style={{ font: `600 10px ${C.sans}`, color, textTransform: "uppercase", letterSpacing: 0.4 }}>
        {status}{remain != null ? ` · ${prefix} ${fmtDur(remain)}` : ""}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Active virtual trades — open sims with live price, unrealized P&L, time in trade
// ---------------------------------------------------------------------------
const fmtMoney = (v) => (v == null ? "—" : `${v < 0 ? "-" : ""}$${Math.abs(v).toFixed(2)}`);

function ActiveTradesPanel({ trades, priceByTicker }) {
  const { h, m } = useCtClock();
  const nowMin = h * 60 + m;
  const open = (trades || []).filter((t) => t.outcome === "OPEN");
  return (
    <Card>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <strong style={{ font: `600 13px ${C.sans}`, color: C.ink }}>Active virtual trades</strong>
        <span style={{ font: `400 12px ${C.mono}`, color: C.inkFaint }}>{open.length} open</span>
        <span style={{ flex: 1 }} />
        <span style={{ font: `600 10px ${C.sans}`, color: C.yellow, textTransform: "uppercase", letterSpacing: 0.3 }}>Simulated only</span>
      </div>
      {!open.length ? (
        <div style={{ font: `400 12px ${C.sans}`, color: C.inkFaint }}>
          No open sims. In <b>PAPER</b> mode, a detected setup opens a virtual position at the live price; it resolves when price touches the stop or target.
        </div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", font: `400 12px ${C.mono}` }}>
            <thead>
              <tr>{["Ticker", "Dir", "Entry", "Stop", "Target", "Live", "uP&L", "uP&L (R)", "Size", "In trade"].map((x) => <th key={x} style={thStyle}>{x}</th>)}</tr>
            </thead>
            <tbody>
              {open.map((t) => {
                const px = priceByTicker[t.ticker];
                const dir = t.direction === "LONG" ? 1 : -1;
                const perShareRisk = Math.abs(t.entry_price - t.stop_price);
                const upl = px != null ? (px - t.entry_price) * t.position_size * dir : null;
                const uplR = px != null && perShareRisk > 0 ? ((px - t.entry_price) * dir) / perShareRisk : null;
                const tit = nowMin - parseHM(t.entry_time);
                const titLabel = tit < 0 ? "—" : tit >= 60 ? `${Math.floor(tit / 60)}h ${tit % 60}m` : `${tit}m`;
                const pnlColor = upl == null ? C.inkDim : upl >= 0 ? C.green : C.red;
                return (
                  <tr key={t.id} style={{ borderTop: `1px solid ${C.lineSoft}` }}>
                    <td style={{ ...tdStyle, color: C.ink }}>{t.ticker}</td>
                    <td style={{ ...tdStyle, color: t.direction === "LONG" ? C.green : C.red }}>{t.direction}</td>
                    <td style={tdStyle}>{fmt(t.entry_price)}</td>
                    <td style={tdStyle}>{fmt(t.stop_price)}</td>
                    <td style={tdStyle}>{fmt(t.target_price)}</td>
                    <td style={{ ...tdStyle, color: C.ink }}>{fmt(px)}</td>
                    <td style={{ ...tdStyle, color: pnlColor }}>{fmtMoney(upl)}</td>
                    <td style={{ ...tdStyle, color: pnlColor }}>{uplR == null ? "—" : `${uplR >= 0 ? "+" : ""}${uplR.toFixed(2)}R`}</td>
                    <td style={tdStyle}>{t.position_size}</td>
                    <td style={tdStyle}>{titLabel}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div style={{ marginTop: 8, font: `400 10px ${C.sans}`, color: C.inkFaint }}>
            Live price from the latest scan; refresh (or auto-refresh) to update unrealized P&L.
          </div>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Results / history — summary stats, ticker/outcome/date filters, CSV export
// ---------------------------------------------------------------------------
const OUTCOME_FILTERS = [
  { value: "", label: "All outcomes" },
  { value: "OPEN", label: "Open" },
  { value: "WIN", label: "Wins" },
  { value: "LOSS", label: "Losses" },
];

function computeStats(rows) {
  const resolved = rows.filter((t) => t.outcome === "WIN" || t.outcome === "LOSS");
  const wins = resolved.filter((t) => t.outcome === "WIN");
  const losses = resolved.filter((t) => t.outcome === "LOSS");
  const mean = (arr) => (arr.length ? arr.reduce((a, t) => a + (Number(t.r_result) || 0), 0) / arr.length : 0);
  const r3 = (v) => Math.round(v * 1000) / 1000;
  return {
    total: rows.length, open: rows.filter((t) => t.outcome === "OPEN").length, resolved: resolved.length,
    wins: wins.length, losses: losses.length,
    winRate: resolved.length ? Math.round((wins.length / resolved.length) * 1000) / 10 : null,
    avgWinR: wins.length ? r3(mean(wins)) : null, avgLossR: losses.length ? r3(mean(losses)) : null,
    expectancy: resolved.length ? r3(mean(resolved)) : null,
  };
}

function HistoryPanel({ tickers, nonce }) {
  const [fTicker, setFTicker] = useState("");
  const [fOutcome, setFOutcome] = useState("");
  const [fDate, setFDate] = useState("");
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);

  const query = useMemo(() => {
    const p = new URLSearchParams();
    if (fTicker) p.set("ticker", fTicker);
    if (fOutcome) p.set("status", fOutcome);
    if (fDate) p.set("date", fDate);
    p.set("limit", "1000");
    return p.toString();
  }, [fTicker, fOutcome, fDate]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch(`${API}/api/executor/paper/trades?${query}`);
      const data = await r.json().catch(() => ({}));
      if (data.ok) setRows(data.trades || []);
    } catch (e) { /* history is best-effort */ }
    setLoading(false);
  }, [query]);

  useEffect(() => { load(); }, [load, nonce]);

  const stats = useMemo(() => computeStats(rows), [rows]);

  return (
    <Card>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12, flexWrap: "wrap" }}>
        <strong style={{ font: `600 13px ${C.sans}`, color: C.ink }}>Results &amp; history</strong>
        <span style={{ font: `400 12px ${C.mono}`, color: C.inkFaint }}>{rows.length} trade(s){loading ? " · loading…" : ""}</span>
        <span style={{ flex: 1 }} />
        <select value={fTicker} onChange={(e) => setFTicker(e.target.value)} style={selectStyle}>
          <option value="">All tickers</option>
          {tickers.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <select value={fOutcome} onChange={(e) => setFOutcome(e.target.value)} style={selectStyle}>
          {OUTCOME_FILTERS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        <input type="date" value={fDate} onChange={(e) => setFDate(e.target.value)} style={{ ...selectStyle, font: `400 12px ${C.mono}` }} title="Filter by session date (blank = all)" />
        <Button onClick={load}>Refresh</Button>
        <a href={`${API}/api/executor/paper/trades.csv?${query}`} style={{ textDecoration: "none" }}>
          <Button>Export CSV</Button>
        </a>
      </div>

      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: 12 }}>
        <Mini label="Trades" value={stats.total} />
        <Mini label="Open" value={stats.open} color={C.yellow} />
        <Mini label="Wins" value={stats.wins} color={C.green} />
        <Mini label="Losses" value={stats.losses} color={C.red} />
        <Mini label="Win rate" value={stats.winRate == null ? "—" : `${stats.winRate}%`} />
        <Mini label="Avg win" value={stats.avgWinR == null ? "—" : `${stats.avgWinR}R`} color={C.green} />
        <Mini label="Avg loss" value={stats.avgLossR == null ? "—" : `${stats.avgLossR}R`} color={C.red} />
        <Mini label="Expectancy" value={stats.expectancy == null ? "—" : `${stats.expectancy}R`} />
      </div>

      {!rows.length ? (
        <div style={{ font: `400 12px ${C.sans}`, color: C.inkFaint }}>No paper trades match these filters yet.</div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", font: `400 12px ${C.mono}` }}>
            <thead>
              <tr>{["Date", "Time", "Ticker", "Dir", "Entry", "Exit", "Outcome", "R", "Size", "Spread", "Slip"].map((x) => <th key={x} style={thStyle}>{x}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((t) => (
                <tr key={t.id} style={{ borderTop: `1px solid ${C.lineSoft}` }}>
                  <td style={tdStyle}>{t.date}</td>
                  <td style={tdStyle}>{t.entry_time}</td>
                  <td style={{ ...tdStyle, color: C.ink }}>{t.ticker}</td>
                  <td style={{ ...tdStyle, color: t.direction === "LONG" ? C.green : C.red }}>{t.direction}</td>
                  <td style={tdStyle}>{fmt(t.entry_price)}</td>
                  <td style={tdStyle}>{fmt(t.exit_price)}</td>
                  <td style={{ ...tdStyle, color: t.outcome === "WIN" ? C.green : t.outcome === "LOSS" ? C.red : t.outcome === "OPEN" ? C.yellow : C.inkDim }}>{t.outcome}</td>
                  <td style={{ ...tdStyle, color: t.r_result > 0 ? C.green : t.r_result < 0 ? C.red : C.inkDim }}>{t.r_result == null ? "—" : `${t.r_result}R`}</td>
                  <td style={tdStyle}>{t.position_size}</td>
                  <td style={tdStyle}>{t.entry_spread == null ? "—" : fmt(t.entry_spread)}</td>
                  <td style={tdStyle}>{t.slippage == null ? "—" : fmt(t.slippage)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Always-visible MODE badge — so the trader never confuses which mode they're in
// ---------------------------------------------------------------------------
function ModeBadge({ mode }) {
  const meta = MODE_META[mode] || MODE_META[""];
  return (
    <div title={meta.hint} style={{
      display: "inline-flex", alignItems: "center", gap: 7, padding: "6px 12px",
      borderRadius: 999, border: `1px solid ${meta.color}`, background: `${meta.color}1a`,
    }}>
      <span style={{ width: 8, height: 8, borderRadius: 999, background: meta.color }} />
      <span style={{ font: `700 11px ${C.sans}`, color: meta.color, letterSpacing: 0.5, textTransform: "uppercase" }}>
        {meta.label}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// REPLAY engine results — resolved trades (with outcomes) + summary stats
// ---------------------------------------------------------------------------
function ReplayResults({ trades, summary }) {
  return (
    <div style={{ marginTop: 12 }}>
      {summary && (
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: 10 }}>
          <Mini label="Trades" value={summary.total_trades} />
          <Mini label="Wins" value={summary.wins} color={C.green} />
          <Mini label="Losses" value={summary.losses} color={C.red} />
          <Mini label="Win rate" value={summary.win_rate_percent == null ? "—" : `${summary.win_rate_percent}%`} />
          <Mini label="Expectancy" value={summary.expectancy_per_trade == null ? "—" : `${summary.expectancy_per_trade}R`} />
        </div>
      )}
      {trades.length === 0 ? (
        <div style={{ font: `400 12px ${C.sans}`, color: C.inkFaint }}>No trades resolved on that session for the current config.</div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", font: `400 12px ${C.mono}` }}>
            <thead>
              <tr>{["Date", "Time", "Ticker", "Dir", "Entry", "Stop", "Target", "Exit", "Outcome", "R"].map((h) => <th key={h} style={thStyle}>{h}</th>)}</tr>
            </thead>
            <tbody>
              {trades.map((t, i) => (
                <tr key={i} style={{ borderTop: `1px solid ${C.lineSoft}` }}>
                  <td style={tdStyle}>{t.date}</td>
                  <td style={tdStyle}>{t.entry_time}</td>
                  <td style={{ ...tdStyle, color: C.ink }}>{t.ticker}</td>
                  <td style={{ ...tdStyle, color: t.direction === "Long" ? C.green : C.red }}>{t.direction}</td>
                  <td style={tdStyle}>{fmt(t.entry_price)}</td>
                  <td style={tdStyle}>{fmt(t.stop_price)}</td>
                  <td style={tdStyle}>{fmt(t.target_price)}</td>
                  <td style={tdStyle}>{fmt(t.exit_price)}</td>
                  <td style={{ ...tdStyle, color: t.outcome === "Win" ? C.green : t.outcome === "Loss" ? C.red : C.inkDim }}>{t.outcome}</td>
                  <td style={{ ...tdStyle, color: t.r_result > 0 ? C.green : t.r_result < 0 ? C.red : C.inkDim }}>{t.r_result == null ? "—" : `${t.r_result}R`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Row({ label, value, color }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "5px 0", borderBottom: `1px solid ${C.lineSoft}` }}>
      <span style={{ font: `400 12px ${C.sans}`, color: C.inkDim }}>{label}</span>
      <span style={{ font: `600 13px ${C.mono}`, color: color || C.ink }}>{value}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Signal table (today's log + playback results)
// ---------------------------------------------------------------------------
function SignalTable({ signals }) {
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", font: `400 12px ${C.mono}` }}>
        <thead>
          <tr>
            {["Date", "Time (CT)", "Ticker", "Dir", "Level", "Entry", "Stop", "Target", "Risk", "R:R", "Size", "RVOL"].map((h) => (
              <th key={h} style={thStyle}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {signals.map((s, i) => (
            <tr key={i} style={{ borderTop: `1px solid ${C.lineSoft}` }}>
              <td style={tdStyle}>{s.date}</td>
              <td style={tdStyle}>{s.candle_time}</td>
              <td style={{ ...tdStyle, color: C.ink }}>{s.ticker}</td>
              <td style={{ ...tdStyle, color: s.direction === "Long" ? C.green : C.red }}>{s.direction}</td>
              <td style={tdStyle}>{s.level_type} {fmt(s.level)}</td>
              <td style={tdStyle}>{fmt(s.entry_price)}</td>
              <td style={tdStyle}>{fmt(s.stop_price)}</td>
              <td style={tdStyle}>{fmt(s.target_price)}</td>
              <td style={tdStyle}>{fmt(s.risk)}</td>
              <td style={tdStyle}>{s.risk_reward_ratio == null ? "—" : `${s.risk_reward_ratio}:1`}</td>
              <td style={tdStyle}>{s.position_size}</td>
              <td style={{ ...tdStyle, color: s.volume_ratio >= 2 ? C.green : C.inkDim }}>{s.volume_ratio == null ? "—" : `${s.volume_ratio}×`}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---- Small styled primitives (match the dashboard palette) -----------------
const fmt = (v) => (v == null ? "—" : Number(v).toFixed(2));

const selectStyle = {
  background: C.panel2, color: C.ink, border: `1px solid ${C.line}`, borderRadius: 6,
  padding: "7px 9px", font: `400 12px ${C.sans}`, cursor: "pointer",
};
const thStyle = { textAlign: "left", padding: "7px 8px", font: `600 11px ${C.sans}`, color: C.inkFaint, borderBottom: `1px solid ${C.line}`, whiteSpace: "nowrap" };
const tdStyle = { padding: "6px 8px", color: C.inkDim, whiteSpace: "nowrap" };

function SectionHeader({ title, subtitle }) {
  return (
    <div>
      <h2 style={{ margin: 0, font: `700 18px ${C.sans}`, color: C.ink }}>{title}</h2>
      <div style={{ marginTop: 4, font: `400 13px ${C.sans}`, color: C.inkDim }}>{subtitle}</div>
    </div>
  );
}

function Card({ children }) {
  return <div style={{ background: C.panel, border: `1px solid ${C.line}`, borderRadius: 10, padding: 16 }}>{children}</div>;
}

function Grid({ children }) {
  return <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 14 }}>{children}</div>;
}

function Field({ label, hint, children }) {
  return (
    <label style={{ display: "block" }}>
      <div style={{ font: `600 11px ${C.sans}`, color: C.inkDim, marginBottom: 5, textTransform: "uppercase", letterSpacing: 0.3 }}>{label}</div>
      {children}
      {hint && <div style={{ marginTop: 4, font: `400 10px ${C.sans}`, color: C.inkFaint }}>{hint}</div>}
    </label>
  );
}

function Input({ style, ...props }) {
  return <input {...props} style={{
    width: "100%", boxSizing: "border-box", background: C.panel2, color: C.ink,
    border: `1px solid ${C.line}`, borderRadius: 6, padding: "8px 9px", font: `400 13px ${C.mono}`, ...style,
  }} />;
}

function Select({ value, onChange, options }) {
  return (
    <select value={value} onChange={onChange} style={{ ...selectStyle, width: "100%" }}>
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

function Toggle({ checked, onChange, label }) {
  return (
    <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", padding: "4px 0" }}>
      <input type="checkbox" checked={!!checked} onChange={(e) => onChange(e.target.checked)} />
      <span style={{ font: `400 12px ${C.sans}`, color: C.inkDim }}>{label}</span>
    </label>
  );
}

function Button({ children, primary, ...props }) {
  return (
    <button {...props} style={{
      background: primary ? C.blue : C.panel2, color: primary ? "#fff" : C.ink,
      border: `1px solid ${primary ? C.blue : C.line}`, borderRadius: 7, padding: "8px 14px",
      font: `600 12px ${C.sans}`, cursor: props.disabled ? "not-allowed" : "pointer", opacity: props.disabled ? 0.5 : 1,
    }}>{children}</button>
  );
}
