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
     POST /api/executor/paper/execute {signal}
     GET  /api/executor/paper/trades?date=YYYY-MM-DD

   Paper execution is supported by logging simulated bracket trades only. No
   live Schwab order placement is called from this UI.
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
};

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

  const [autoRefresh, setAutoRefresh] = useState(false);
  const [pollSec, setPollSec] = useState(30);
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

  const scan = useCallback(async (refresh) => {
    setScanning(true); setErrors([]);
    setStatus(refresh ? "Pulling today's 5-minute bars, then scanning…" : "Scanning latest closed candles…");
    const { ok, data } = await postJson("/api/executor/monitor", { config: buildConfig(form), refresh });
    setScanning(false);
    if (!ok || data.ok === false) {
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
    setStatus(`Scanned ${data.monitors?.length || 0} ticker(s) · ${live} live · ${data.signals?.length || 0} active signal(s).`);
  }, [form, ingestSignals, refreshLog, refreshPaperTrades]);

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
    ackAlert(sig);
  }, [ackAlert, refreshPaperTrades]);

  const runPlayback = useCallback(async () => {
    setPbRunning(true); setPbStatus("Replaying session…"); setPbSignals(null);
    const { ok, data } = await postJson("/api/executor/playback", { config: buildConfig(form), date: pbDate, autoBackfill: true });
    setPbRunning(false);
    if (!ok || data.ok === false) { setPbStatus((data.errors || [data.error || "Playback failed."]).join(" ")); return; }
    setPbSignals(data.signals || []);
    setPbStatus(`${data.count} signal(s) would have fired on ${pbDate}.`);
  }, [form, pbDate]);

  const activeSignalTickers = useMemo(() => new Set(liveSignals.map((s) => s.ticker)), [liveSignals]);
  const activeAlert = alertQueue[0] || null;

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <style>{`
        @keyframes execBlink { 0%,100% { box-shadow: 0 0 0 1px ${C.red}; } 50% { box-shadow: 0 0 14px 1px ${C.red}; } }
        @keyframes execFade { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
      `}</style>

      <SectionHeader title="Intraday Executor" subtitle="Live setup detection on 5-minute candles — alerts when price breaks yesterday's level on a volume spike." />

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
        </Grid>

        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginTop: 16, paddingTop: 14, borderTop: `1px solid ${C.lineSoft}` }}>
          <Button primary disabled={scanning} onClick={() => scan(true)}>{scanning ? "Scanning…" : "Refresh + Scan"}</Button>
          <Button disabled={scanning} onClick={() => scan(false)} title="Re-evaluate the latest stored candles without pulling new data.">Scan stored</Button>
          <Toggle checked={autoRefresh} onChange={setAutoRefresh} label="Auto-refresh" />
          {autoRefresh && (
            <select value={pollSec} onChange={(e) => setPollSec(Number(e.target.value))} style={selectStyle}>
              {POLL_CHOICES.map((p) => <option key={p.value} value={p.value}>every {p.label}</option>)}
            </select>
          )}
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
          {monitors.map((m) => (
            <MonitorCard key={m.ticker} m={m} hasSignal={activeSignalTickers.has(m.ticker)} />
          ))}
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

      <PaperTradesPanel trades={paperTrades} />

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
      </Card>

      {activeAlert && (
        <AlertModal
          sig={activeAlert}
          queued={alertQueue.length}
          executing={executingKey === signalKey(activeAlert)}
          onExecute={() => executePaper(activeAlert)}
          onAck={() => ackAlert(activeAlert)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-ticker monitor card: chart + live stats
// ---------------------------------------------------------------------------
function MonitorCard({ m, hasSignal }) {
  const label = STATE_LABEL[m.state] || STATE_LABEL["no-data"];
  return (
    <div style={{
      background: C.panel, border: `1px solid ${hasSignal ? C.red : C.line}`, borderRadius: 10, padding: 14,
      animation: hasSignal ? "execBlink 1.1s ease-in-out infinite" : "none",
    }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
        <strong style={{ font: `700 15px ${C.sans}`, color: C.ink }}>{m.ticker}</strong>
        {m.last_close != null && <span style={{ font: `600 14px ${C.mono}`, color: C.ink }}>{m.last_close.toFixed(2)}</span>}
        <span style={{ flex: 1 }} />
        <span style={{ font: `600 10px ${C.sans}`, color: label.color, textTransform: "uppercase", letterSpacing: 0.4 }}>
          {hasSignal ? "⚡ Setup!" : label.text}
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
function AlertModal({ sig, queued, executing, onExecute, onAck }) {
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
        <div style={{ padding: "12px 18px", borderTop: `1px solid ${C.line}`, display: "flex", gap: 10 }}>
          <Button primary disabled={executing} onClick={onExecute}>{executing ? "Logging…" : "Execute Paper"}</Button>
          <Button onClick={onAck}>Skip</Button>
          <span style={{ flex: 1 }} />
          <span style={{ font: `400 10px ${C.sans}`, color: C.inkFaint, alignSelf: "center", maxWidth: 130, textAlign: "right" }}>Paper only — no live Schwab order.</span>
        </div>
      </div>
    </div>
  );
}

function PaperTradesPanel({ trades }) {
  const open = (trades || []).filter((t) => t.outcome === "OPEN");
  return (
    <Card>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <strong style={{ font: `600 13px ${C.sans}`, color: C.ink }}>Paper trades</strong>
        <span style={{ font: `400 12px ${C.mono}`, color: C.inkFaint }}>{open.length} open · {(trades || []).length} total today</span>
        <span style={{ flex: 1 }} />
        <span style={{ font: `600 10px ${C.sans}`, color: C.yellow, textTransform: "uppercase", letterSpacing: 0.3 }}>Simulated only</span>
      </div>
      {!trades?.length ? (
        <div style={{ font: `400 12px ${C.sans}`, color: C.inkFaint }}>
          Confirm a setup with <b>Execute Paper</b> to log a simulated bracket trade here. Live Schwab execution is intentionally disabled.
        </div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", font: `400 12px ${C.mono}` }}>
            <thead>
              <tr>
                {["Status", "Ticker", "Dir", "Entry", "Stop", "Target", "Size", "Signal", "Order"].map((h) => <th key={h} style={thStyle}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {trades.map((t) => (
                <tr key={t.id} style={{ borderTop: `1px solid ${C.lineSoft}` }}>
                  <td style={{ ...tdStyle, color: t.outcome === "OPEN" ? C.green : C.inkDim }}>{t.outcome}</td>
                  <td style={{ ...tdStyle, color: C.ink }}>{t.ticker}</td>
                  <td style={{ ...tdStyle, color: t.direction === "LONG" ? C.green : C.red }}>{t.direction}</td>
                  <td style={tdStyle}>{fmt(t.entry_price)}</td>
                  <td style={tdStyle}>{fmt(t.stop_price)}</td>
                  <td style={tdStyle}>{fmt(t.target_price)}</td>
                  <td style={tdStyle}>{t.position_size}</td>
                  <td style={tdStyle}>{t.date} {t.entry_time} CT</td>
                  <td style={tdStyle}>{t.order_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
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
