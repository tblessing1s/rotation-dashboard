import React, { useState, useEffect, useMemo, useCallback } from "react";

/* ============================================================================
   TRAVIS — INSTITUTIONAL ROTATION DASHBOARD  (local app build)
   4-level decision system: Macro -> Institutional -> Money Flow -> Technical.
   Data + indicator math come from the local Python backend (no CORS, cached).
   Manual inputs and positions persist to the backend's state.json.
   ============================================================================ */

const API = ""; // same origin when served by backend; Vite proxies /api in dev

// In-memory mirror of persisted state. Hydrated from /api/state on load,
// and written back (debounced) whenever it changes.
const store = (() => {
  let mem = {};
  let saveTimer = null;
  const flush = () => {
    fetch(`${API}/api/state`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(mem),
    }).catch(() => {});
  };
  return {
    hydrate: (obj) => { mem = { ...mem, ...obj }; },
    get: (k, fb) => (k in mem ? mem[k] : fb),
    set: (k, v) => {
      mem[k] = v;
      clearTimeout(saveTimer);
      saveTimer = setTimeout(flush, 600); // debounce disk writes
    },
  };
})();

// ---- Color tokens ----------------------------------------------------------
const C = {
  bg: "#0a0e14",
  panel: "#121821",
  panel2: "#0f141c",
  line: "#1f2935",
  lineSoft: "#19222e",
  ink: "#e6edf3",
  inkDim: "#8b97a7",
  inkFaint: "#5a6573",
  green: "#3fb950",
  greenDim: "#1f6f2e",
  yellow: "#d2a64a",
  red: "#f0506e",
  redDim: "#7a2438",
  blue: "#4493f8",
  amber: "#e3a008",
  mono: "'Roboto Mono', ui-monospace, 'SF Mono', Menlo, monospace",
  sans: "'Inter', -apple-system, system-ui, sans-serif",
};

const SIG = { GREEN: C.green, YELLOW: C.yellow, RED: C.red };

// ============================================================================
// DATA LAYER — all fetching + indicator math happens in the Python backend.
// These just call the local API. No CORS, cached server-side.
// ============================================================================
async function apiQuotes() {
  const r = await fetch(`${API}/api/quotes`);
  if (!r.ok) throw new Error("quotes failed");
  return r.json();
}

async function apiIndicators() {
  const r = await fetch(`${API}/api/indicators`);
  if (!r.ok) throw new Error("indicators failed");
  return r.json();
}

// ============================================================================
// SIGNAL ENGINE — pure functions translating inputs into framework verdicts
// ============================================================================

// Level 1: Macro environment -> GREEN / YELLOW / RED
function macroSignal(m) {
  // Score each metric: +1 growth-favoring (risk-on), -1 defensive (risk-off)
  let riskOn = 0, riskOff = 0;
  const notes = [];

  // VIX
  if (m.vix < 15) { riskOn++; notes.push(["VIX", "risk-on", C.green]); }
  else if (m.vix <= 20) { notes.push(["VIX", "caution", C.yellow]); }
  else { riskOff++; notes.push(["VIX", m.vix > 30 ? "panic" : "elevated", m.vix > 30 ? C.red : C.yellow]); }

  // Breadth
  if (m.breadth >= 60) { riskOn++; notes.push(["Breadth", "strong", C.green]); }
  else if (m.breadth >= 55) { notes.push(["Breadth", "ok", C.yellow]); }
  else if (m.breadth >= 45) { notes.push(["Breadth", "weak", C.yellow]); }
  else { riskOff++; notes.push(["Breadth", "very weak", C.red]); }

  // Fed
  if (m.fed === "dovish") { riskOn++; notes.push(["Fed", "dovish", C.green]); }
  else if (m.fed === "hawkish") { riskOff++; notes.push(["Fed", "hawkish", C.yellow]); }
  else notes.push(["Fed", "holding", C.yellow]);

  // Growth
  if (m.growth === "accelerating") { riskOn++; notes.push(["Growth", "accelerating", C.green]); }
  else if (m.growth === "slowing") { riskOff++; notes.push(["Growth", "slowing", C.yellow]); }
  else notes.push(["Growth", "stable", C.yellow]);

  // Inflation
  if (m.inflation < 2) { riskOn++; notes.push(["Inflation", "low", C.green]); }
  else if (m.inflation <= 3) { notes.push(["Inflation", "neutral", C.yellow]); }
  else { riskOff++; notes.push(["Inflation", "hot", C.red]); }

  let level = "YELLOW";
  if (m.vix > 30 || m.breadth < 45) level = "RED";
  else if (riskOn >= 3 && riskOff === 0 && m.breadth >= 55) level = "GREEN";
  else if (riskOff >= 3) level = "RED";

  return { level, notes, riskOn, riskOff };
}

// CFM entry checklist
function cfmChecklist(m, inst, flow, tech) {
  const macro = macroSignal(m);
  const items = [
    ["Fed holding or hawkish (not dovish)", m.fed !== "dovish"],
    ["Growth slowing (defensive rotation)", m.growth === "slowing"],
    ["Inflation sticky 2–3%", m.inflation >= 2 && m.inflation <= 3.2],
    ["Market breadth > 55%", m.breadth > 55],
    ["VIX < 25", m.vix < 25],
    ["RS3M slightly negative (in rotation)", inst.rs3m < 5 && inst.rs3m > -15],
    ["RS3M_MOM +300 to +1000 (moving in)", inst.rs3mMom >= 300],
    ["Earnings revisions trending up", inst.earnings === "up"],
    ["Valuation cheap vs history", inst.valuation === "cheap"],
    ["Credit healthy", inst.credit !== "tight"],
    ["MoneyFlow 60–75 (accumulation)", flow.mfi >= 60 && flow.mfi <= 75],
    ["OBV green or flat", flow.obv !== "falling"],
    ["Volume normal (healthy consolidation)", flow.volRatio >= 70 && flow.volRatio <= 150],
    ["Price above MA21", tech.priceAboveMA21],
    ["2–3 bounces at support confirmed", tech.bouncesConfirmed],
    ["Support clearly defined", tech.supportDefined],
  ];
  const pass = items.filter((i) => i[1]).length;
  const verdict = pass === items.length ? "ENTER" : "WAIT";
  return { items, pass, total: items.length, verdict, macro };
}

// APP entry checklist
function appChecklist(m, inst, flow, tech) {
  const macro = macroSignal(m);
  const items = [
    ["Fed dovish or neutral (not hawkish)", m.fed !== "hawkish"],
    ["Growth accelerating", m.growth === "accelerating"],
    ["Inflation falling / not hot", m.inflation <= 3],
    ["Market breadth > 60%", m.breadth > 60],
    ["VIX < 20", m.vix < 20],
    ["RS3M negative (laggard rotating in)", inst.rs3m < 0],
    ["RS3M_MOM +500+ and accelerating", inst.rs3mMom >= 500],
    ["Earnings revisions trending up fast", inst.earnings === "up"],
    ["Valuation reasonable (not expensive)", inst.valuation !== "expensive"],
    ["Credit easy", inst.credit === "easy"],
    ["MoneyFlow 50–70 (money in, not overbought)", flow.mfi >= 50 && flow.mfi <= 70],
    ["OBV green and rising", flow.obv === "rising"],
    ["Volume 120%+ (real breakout)", flow.volRatio >= 120],
    ["RSI 50–70 (strong, not overbought)", flow.rsi >= 50 && flow.rsi <= 70],
    ["Breakout above resistance", tech.breakoutConfirmed],
    ["Support clear below entry", tech.supportDefined],
  ];
  const pass = items.filter((i) => i[1]).length;
  const verdict = pass === items.length ? "ENTER" : "WAIT";
  return { items, pass, total: items.length, verdict, macro };
}

// Exit triggers — any TRUE means act within 1–2 days
function exitTriggers(inst, flow, m, tech) {
  return [
    ["RS3M negative and staying negative", inst.rs3m < 0 && inst.rs3mTrend === "down"],
    ["RS3M_MOM turned negative", inst.rs3mMom < 0],
    ["MoneyFlow below 40", flow.mfi < 40],
    ["OBV red and declining", flow.obv === "falling"],
    ["Price closed below MA21 on high volume", !tech.priceAboveMA21 && flow.volRatio > 120],
    ["Breadth below 45%", m.breadth < 45],
    ["VIX spiked above 30", m.vix > 30],
  ];
}

// ============================================================================
// SMALL UI PRIMITIVES
// ============================================================================
function Panel({ title, eyebrow, children, accent, right }) {
  return (
    <section style={{
      background: C.panel, border: `1px solid ${C.line}`, borderRadius: 10,
      overflow: "hidden",
    }}>
      <header style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "12px 16px", borderBottom: `1px solid ${C.lineSoft}`,
        borderLeft: accent ? `3px solid ${accent}` : "none",
      }}>
        <div>
          {eyebrow && <div style={{ font: `600 9px/1 ${C.mono}`, letterSpacing: 2, color: C.inkFaint, textTransform: "uppercase", marginBottom: 5 }}>{eyebrow}</div>}
          <h2 style={{ margin: 0, font: `600 14px/1 ${C.sans}`, color: C.ink, letterSpacing: -0.2 }}>{title}</h2>
        </div>
        {right}
      </header>
      <div style={{ padding: 16 }}>{children}</div>
    </section>
  );
}

function Field({ label, children, hint }) {
  return (
    <label style={{ display: "block", marginBottom: 12 }}>
      <div style={{ font: `500 11px/1.2 ${C.sans}`, color: C.inkDim, marginBottom: 6, display: "flex", justifyContent: "space-between" }}>
        <span>{label}</span>
        {hint && <span style={{ color: C.inkFaint, font: `400 10px ${C.mono}` }}>{hint}</span>}
      </div>
      {children}
    </label>
  );
}

const inputStyle = {
  width: "100%", boxSizing: "border-box", background: C.panel2,
  border: `1px solid ${C.line}`, borderRadius: 6, color: C.ink,
  font: `500 13px ${C.mono}`, padding: "9px 10px", outline: "none",
};

function NumIn({ value, onChange, step = "0.01" }) {
  return <input type="number" step={step} value={value}
    onChange={(e) => onChange(e.target.value === "" ? "" : parseFloat(e.target.value))}
    style={inputStyle} />;
}

function Sel({ value, onChange, options }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}
      style={{ ...inputStyle, appearance: "none", cursor: "pointer" }}>
      {options.map((o) => <option key={o[0]} value={o[0]} style={{ background: C.panel }}>{o[1]}</option>)}
    </select>
  );
}

function Toggle({ value, onChange }) {
  return (
    <button onClick={() => onChange(!value)} style={{
      width: 46, height: 26, borderRadius: 13, border: "none", cursor: "pointer",
      background: value ? C.greenDim : C.line, position: "relative", transition: "background .15s",
    }}>
      <span style={{
        position: "absolute", top: 3, left: value ? 23 : 3, width: 20, height: 20,
        borderRadius: "50%", background: value ? C.green : C.inkFaint, transition: "left .15s",
      }} />
    </button>
  );
}

function CheckRow({ label, ok }) {
  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 10, padding: "6px 0", borderBottom: `1px solid ${C.lineSoft}` }}>
      <span style={{
        flexShrink: 0, width: 16, height: 16, borderRadius: 4, marginTop: 1,
        background: ok ? C.greenDim : "transparent", border: `1.5px solid ${ok ? C.green : C.redDim}`,
        display: "grid", placeItems: "center", font: `700 11px ${C.mono}`, color: ok ? C.green : C.red,
      }}>{ok ? "✓" : "·"}</span>
      <span style={{ font: `400 12px/1.35 ${C.sans}`, color: ok ? C.ink : C.inkDim }}>{label}</span>
    </div>
  );
}

// ============================================================================
// MAIN APP
// ============================================================================
const TABS = ["Command", "Checklists", "Positions", "Indicators"];

// Hydration gate: load persisted state from the backend before the dashboard
// mounts, so saved positions and manual inputs initialize correctly.
export default function App() {
  const [ready, setReady] = useState(false);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    fetch(`${API}/api/state`)
      .then((r) => r.json())
      .then((data) => { store.hydrate(data || {}); setReady(true); })
      .catch(() => { setOffline(true); setReady(true); });
  }, []);

  if (!ready) {
    return (
      <div style={{ background: C.bg, minHeight: "100vh", display: "grid", placeItems: "center", color: C.inkDim, font: `500 14px ${C.sans}` }}>
        Loading saved state…
      </div>
    );
  }
  return <TradingDashboard backendOffline={offline} />;
}

function TradingDashboard({ backendOffline }) {
  const [tab, setTab] = useState("Command");

  // ---- State: live quotes ----
  const [quotes, setQuotes] = useState(store.get("quotes", {}));
  const [fetchStatus, setFetchStatus] = useState("idle");
  const [lastFetch, setLastFetch] = useState(store.get("lastFetch", null));

  // ---- State: macro (Level 1). VIX auto-fills from quote when available ----
  const [macro, setMacro] = useState(store.get("macro", {
    vix: 21.51, breadth: 47, fed: "hawkish", growth: "slowing", inflation: 3.0,
  }));

  // ---- State: institutional (Level 2) per instrument ----
  const [instXLV, setInstXLV] = useState(store.get("instXLV", {
    rs3m: -8.91, rs3mMom: 884, rs3mTrend: "up", earnings: "up", valuation: "cheap", credit: "easy",
  }));
  const [instILMN, setInstILMN] = useState(store.get("instILMN", {
    rs3m: 16.88, rs3mMom: 1128, rs3mTrend: "up", earnings: "up", valuation: "reasonable", credit: "easy",
  }));

  // ---- State: money flow (Level 3) per instrument ----
  const [flowXLV, setFlowXLV] = useState(store.get("flowXLV", {
    mfi: 70.66, rsi: 58, obv: "rising", volRatio: 95,
  }));
  const [flowILMN, setFlowILMN] = useState(store.get("flowILMN", {
    mfi: 71.95, rsi: 64, obv: "rising", volRatio: 110,
  }));

  // ---- State: technical (Level 4) per instrument ----
  const [techXLV, setTechXLV] = useState(store.get("techXLV", {
    priceAboveMA21: true, bouncesConfirmed: false, supportDefined: true, breakoutConfirmed: false,
  }));
  const [techILMN, setTechILMN] = useState(store.get("techILMN", {
    priceAboveMA21: true, bouncesConfirmed: false, supportDefined: true, breakoutConfirmed: false,
  }));

  // ---- State: positions ----
  const [positions, setPositions] = useState(store.get("positions", []));

  // Persist on change
  useEffect(() => { store.set("macro", macro); }, [macro]);
  useEffect(() => { store.set("instXLV", instXLV); store.set("instILMN", instILMN); }, [instXLV, instILMN]);
  useEffect(() => { store.set("flowXLV", flowXLV); store.set("flowILMN", flowILMN); }, [flowXLV, flowILMN]);
  useEffect(() => { store.set("techXLV", techXLV); store.set("techILMN", techILMN); }, [techXLV, techILMN]);
  useEffect(() => { store.set("positions", positions); }, [positions]);

  // ---- State: auto-computed indicators per symbol ----
  const [computed, setComputed] = useState(store.get("computed", {}));
  const [calcStatus, setCalcStatus] = useState("idle");

  // ---- Pull quotes + backend-computed indicators ----
  const refreshQuotes = useCallback(async () => {
    setFetchStatus("loading");
    setCalcStatus("loading");

    // Quotes (backend returns keys XLV, ILMN, ^VIX, SPY)
    try {
      const raw = await apiQuotes();
      const out = {
        XLV: raw.XLV || { symbol: "XLV", error: true },
        ILMN: raw.ILMN || { symbol: "ILMN", error: true },
        VIX: raw["^VIX"] || { symbol: "VIX", error: true },
        SPY: raw.SPY || { symbol: "SPY", error: true },
      };
      setQuotes(out);
      store.set("quotes", out);
      const ts = new Date().toLocaleTimeString();
      setLastFetch(ts); store.set("lastFetch", ts);
      if (out.VIX && !out.VIX.error) setMacro((m) => ({ ...m, vix: out.VIX.close }));
      setFetchStatus(Object.values(out).some((q) => q.error) ? "partial" : "ok");
    } catch (e) {
      setFetchStatus("partial");
    }

    // Indicators (already computed server-side)
    try {
      const comp = await apiIndicators();
      const clean = {};
      for (const k of Object.keys(comp)) {
        clean[k] = comp[k] && !comp[k].error ? comp[k] : null;
      }
      setComputed(clean);
      store.set("computed", clean);
      setCalcStatus(Object.values(clean).some(Boolean) ? "ok" : "fail");
    } catch (e) {
      setCalcStatus("fail");
    }
  }, []);

  useEffect(() => { refreshQuotes(); /* once on mount */ }, [refreshQuotes]);

  // ---- Derived signals ----
  const sig = useMemo(() => macroSignal(macro), [macro]);
  const cfm = useMemo(() => cfmChecklist(macro, instXLV, flowXLV, techXLV), [macro, instXLV, flowXLV, techXLV]);
  const app = useMemo(() => appChecklist(macro, instILMN, flowILMN, techILMN), [macro, instILMN, flowILMN, techILMN]);
  const exitsXLV = useMemo(() => exitTriggers(instXLV, flowXLV, macro, techXLV), [instXLV, flowXLV, macro, techXLV]);
  const exitsILMN = useMemo(() => exitTriggers(instILMN, flowILMN, macro, techILMN), [instILMN, flowILMN, macro, techILMN]);

  // ---- Portfolio math ----
  const capital = 35000, reserve = 13000;
  const deployed = positions.reduce((s, p) => s + (parseFloat(p.cost) || 0), 0);
  const openPL = positions.reduce((s, p) => s + ((parseFloat(p.current) || 0) - (parseFloat(p.cost) || 0)), 0);

  return (
    <div style={{ background: C.bg, minHeight: "100vh", color: C.ink, font: `400 14px ${C.sans}`, padding: "20px 16px 60px" }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Roboto+Mono:wght@400;500;600;700&display=swap');
        * { -webkit-tap-highlight-color: transparent; }
        ::-webkit-scrollbar { height: 8px; width: 8px; }
        ::-webkit-scrollbar-thumb { background: ${C.line}; border-radius: 4px; }
        @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
        button:focus-visible, select:focus-visible, input:focus-visible { outline: 2px solid ${C.blue}; outline-offset: 1px; }
      `}</style>

      <div style={{ maxWidth: 1180, margin: "0 auto" }}>
        <Header sig={sig} lastFetch={lastFetch} status={fetchStatus} onRefresh={refreshQuotes} quotes={quotes} />

        {/* Tabs */}
        <nav style={{ display: "flex", gap: 4, margin: "20px 0 18px", borderBottom: `1px solid ${C.line}` }}>
          {TABS.map((t) => (
            <button key={t} onClick={() => setTab(t)} style={{
              background: "none", border: "none", cursor: "pointer", padding: "10px 14px",
              font: `600 13px ${C.sans}`, color: tab === t ? C.ink : C.inkFaint,
              borderBottom: `2px solid ${tab === t ? C.blue : "transparent"}`, marginBottom: -1,
            }}>{t}</button>
          ))}
        </nav>

        {tab === "Command" && (
          <CommandView sig={sig} cfm={cfm} app={app} macro={macro} setMacro={setMacro}
            quotes={quotes} exitsXLV={exitsXLV} exitsILMN={exitsILMN}
            deployed={deployed} reserve={reserve} capital={capital} openPL={openPL} positions={positions} />
        )}
        {tab === "Checklists" && (
          <ChecklistView cfm={cfm} app={app} />
        )}
        {tab === "Positions" && (
          <PositionsView positions={positions} setPositions={setPositions}
            quotes={quotes} capital={capital} reserve={reserve} deployed={deployed} openPL={openPL} />
        )}
        {tab === "Indicators" && (
          <IndicatorsView
            macro={macro} setMacro={setMacro}
            computed={computed} calcStatus={calcStatus} onRefresh={refreshQuotes}
            instXLV={instXLV} setInstXLV={setInstXLV} flowXLV={flowXLV} setFlowXLV={setFlowXLV} techXLV={techXLV} setTechXLV={setTechXLV}
            instILMN={instILMN} setInstILMN={setInstILMN} flowILMN={flowILMN} setFlowILMN={setFlowILMN} techILMN={techILMN} setTechILMN={setTechILMN}
          />
        )}
      </div>
    </div>
  );
}

// ============================================================================
// HEADER — the signature: a live macro verdict beacon + ticker strip
// ============================================================================
function Header({ sig, lastFetch, status, onRefresh, quotes }) {
  const color = SIG[sig.level];
  const verdictMap = { GREEN: "RISK-ON", YELLOW: "MIXED", RED: "RISK-OFF" };
  const actionMap = {
    GREEN: "Conditions favor deployment",
    YELLOW: "Hold — wait for confirmation",
    RED: "Defensive — do not force entries",
  };
  return (
    <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "stretch" }}>
      {/* Beacon */}
      <div style={{
        flex: "1 1 320px", background: C.panel, border: `1px solid ${C.line}`,
        borderRadius: 12, padding: "18px 20px", display: "flex", alignItems: "center", gap: 18,
        position: "relative", overflow: "hidden",
      }}>
        <div style={{ position: "absolute", inset: 0, background: `radial-gradient(120px 80px at 40px 50%, ${color}22, transparent)` }} />
        <div style={{
          width: 54, height: 54, borderRadius: "50%", flexShrink: 0,
          background: color, boxShadow: `0 0 0 6px ${color}22, 0 0 24px ${color}66`, position: "relative",
        }} />
        <div style={{ position: "relative" }}>
          <div style={{ font: `600 10px/1 ${C.mono}`, letterSpacing: 2.5, color: C.inkFaint, marginBottom: 7 }}>LEVEL 1 · MACRO ENVIRONMENT</div>
          <div style={{ font: `700 30px/1 ${C.sans}`, color, letterSpacing: -0.8 }}>{verdictMap[sig.level]}</div>
          <div style={{ font: `400 12px/1.3 ${C.sans}`, color: C.inkDim, marginTop: 6 }}>{actionMap[sig.level]}</div>
        </div>
      </div>

      {/* Ticker strip + refresh */}
      <div style={{
        flex: "2 1 480px", background: C.panel, border: `1px solid ${C.line}`,
        borderRadius: 12, padding: "14px 16px", display: "flex", flexDirection: "column", justifyContent: "space-between", gap: 10,
      }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ font: `600 10px/1 ${C.mono}`, letterSpacing: 2, color: C.inkFaint }}>LIVE QUOTES</span>
          <button onClick={onRefresh} style={{
            background: C.panel2, border: `1px solid ${C.line}`, borderRadius: 6, cursor: "pointer",
            color: status === "loading" ? C.amber : C.inkDim, font: `500 11px ${C.mono}`, padding: "5px 10px",
          }}>
            {status === "loading" ? "fetching…" : `↻ refresh${lastFetch ? "  " + lastFetch : ""}`}
          </button>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10 }}>
          {["XLV", "ILMN", "VIX", "SPY"].map((s) => {
            const q = quotes[s];
            const chg = q && !q.error ? q.close - q.open : null;
            const up = chg >= 0;
            return (
              <div key={s} style={{ background: C.panel2, border: `1px solid ${C.lineSoft}`, borderRadius: 8, padding: "10px 12px" }}>
                <div style={{ font: `600 11px/1 ${C.mono}`, color: C.inkDim, marginBottom: 6 }}>{s}</div>
                <div style={{ font: `600 17px/1 ${C.mono}`, color: C.ink }}>
                  {q && !q.error ? q.close.toFixed(2) : "—"}
                </div>
                {chg != null && (
                  <div style={{ font: `500 11px/1 ${C.mono}`, color: up ? C.green : C.red, marginTop: 4 }}>
                    {up ? "▲" : "▼"} {Math.abs(chg).toFixed(2)}
                  </div>
                )}
              </div>
            );
          })}
        </div>
        {status === "partial" && <div style={{ font: `400 10px ${C.mono}`, color: C.amber }}>Some quotes unavailable — enter manually in Indicators.</div>}
      </div>
    </div>
  );
}

// ============================================================================
// COMMAND VIEW — everything at a glance
// ============================================================================
function CommandView({ sig, cfm, app, macro, setMacro, quotes, exitsXLV, exitsILMN, deployed, reserve, capital, openPL, positions }) {
  const macroFavorsCFM = macro.growth === "slowing";
  return (
    <div style={{ display: "grid", gap: 16 }}>
      {/* Macro metric chips */}
      <Panel title="Macro readout" eyebrow="Level 1 · daily check" accent={SIG[sig.level]}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: 10 }}>
          {sig.notes.map(([k, v, c]) => (
            <div key={k} style={{ background: C.panel2, border: `1px solid ${C.lineSoft}`, borderRadius: 8, padding: "11px 13px" }}>
              <div style={{ font: `500 11px/1 ${C.sans}`, color: C.inkFaint, marginBottom: 6 }}>{k}</div>
              <div style={{ font: `600 14px/1 ${C.sans}`, color: c, textTransform: "capitalize" }}>{v}</div>
            </div>
          ))}
        </div>
      </Panel>

      {/* Two strategy cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 16 }}>
        <StrategyCard
          tag="CFM" name="Cashflow Machine" instrument="XLV" color={C.blue}
          verdict={cfm.verdict} pass={cfm.pass} total={cfm.total}
          target="1–2% weekly" note={macroFavorsCFM ? "Macro supports defensive rotation" : "Macro not ideal for CFM"} />
        <StrategyCard
          tag="APP" name="Appreciation" instrument="ILMN" color={C.amber}
          verdict={app.verdict} pass={app.pass} total={app.total}
          target="30–50% / trade" note={macro.breadth > 60 ? "Breadth supports growth" : "Breadth below 60% — wait"} />
      </div>

      {/* Exit watch */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 16 }}>
        <ExitWatch title="XLV exit triggers" triggers={exitsXLV} />
        <ExitWatch title="ILMN exit triggers" triggers={exitsILMN} />
      </div>

      {/* Capital bar */}
      <Panel title="Capital allocation" eyebrow="Portfolio · non-negotiable rules">
        <CapitalBar capital={capital} deployed={deployed} reserve={reserve} openPL={openPL} positions={positions} />
      </Panel>
    </div>
  );
}

function StrategyCard({ tag, name, instrument, color, verdict, pass, total, target, note }) {
  const go = verdict === "ENTER";
  return (
    <div style={{ background: C.panel, border: `1px solid ${C.line}`, borderRadius: 10, padding: 18, borderTop: `3px solid ${color}` }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <span style={{ font: `700 12px ${C.mono}`, color, letterSpacing: 1 }}>{tag}</span>
          <div style={{ font: `600 17px/1.1 ${C.sans}`, color: C.ink, marginTop: 4 }}>{name}</div>
          <div style={{ font: `400 12px ${C.sans}`, color: C.inkDim, marginTop: 3 }}>{instrument} · {target}</div>
        </div>
        <div style={{
          font: `700 13px ${C.mono}`, padding: "6px 12px", borderRadius: 6,
          background: go ? C.greenDim : C.redDim, color: go ? C.green : C.red, letterSpacing: 1,
        }}>{verdict}</div>
      </div>
      <div style={{ marginTop: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", font: `500 11px ${C.mono}`, color: C.inkDim, marginBottom: 6 }}>
          <span>Entry checklist</span><span>{pass}/{total}</span>
        </div>
        <div style={{ height: 6, background: C.panel2, borderRadius: 3, overflow: "hidden" }}>
          <div style={{ width: `${(pass / total) * 100}%`, height: "100%", background: go ? C.green : color, transition: "width .3s" }} />
        </div>
        <div style={{ font: `400 11px ${C.sans}`, color: C.inkFaint, marginTop: 10 }}>{note}</div>
      </div>
    </div>
  );
}

function ExitWatch({ title, triggers }) {
  const fired = triggers.filter((t) => t[1]);
  const alarm = fired.length > 0;
  return (
    <Panel title={title} eyebrow="Exit rules · act in 1–2 days" accent={alarm ? C.red : C.greenDim}
      right={<span style={{ font: `700 12px ${C.mono}`, color: alarm ? C.red : C.green, padding: "4px 10px", borderRadius: 5, background: alarm ? C.redDim : C.greenDim }}>{alarm ? `${fired.length} FIRED` : "CLEAR"}</span>}>
      {alarm ? (
        <div style={{ display: "grid", gap: 6 }}>
          {fired.map((t) => (
            <div key={t[0]} style={{ display: "flex", gap: 8, alignItems: "center", background: C.redDim + "33", border: `1px solid ${C.redDim}`, borderRadius: 6, padding: "8px 10px" }}>
              <span style={{ color: C.red }}>⚠</span>
              <span style={{ font: `500 12px ${C.sans}`, color: C.ink }}>{t[0]}</span>
            </div>
          ))}
        </div>
      ) : (
        <div style={{ font: `400 12px ${C.sans}`, color: C.inkDim }}>No exit triggers active. Hold and monitor daily.</div>
      )}
    </Panel>
  );
}

function CapitalBar({ capital, deployed, reserve, openPL, positions }) {
  const free = capital - deployed - reserve;
  const segs = [
    ["Deployed", deployed, C.blue],
    ["Reserve (locked)", reserve, C.greenDim],
    ["Free", Math.max(0, free), C.line],
  ];
  const reserveOk = (capital - deployed) >= reserve;
  return (
    <div>
      <div style={{ display: "flex", height: 30, borderRadius: 6, overflow: "hidden", border: `1px solid ${C.line}` }}>
        {segs.map(([l, v, c]) => v > 0 && (
          <div key={l} title={`${l}: $${v.toLocaleString()}`} style={{ width: `${(v / capital) * 100}%`, background: c }} />
        ))}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px,1fr))", gap: 12, marginTop: 14 }}>
        <Stat label="Total capital" value={`$${capital.toLocaleString()}`} />
        <Stat label="Deployed" value={`$${deployed.toLocaleString()}`} color={C.blue} />
        <Stat label="Cash reserve req." value={`$${reserve.toLocaleString()}`} color={reserveOk ? C.green : C.red} sub={reserveOk ? "intact" : "BREACHED"} />
        <Stat label="Open P&L" value={`${openPL >= 0 ? "+" : ""}$${openPL.toLocaleString()}`} color={openPL >= 0 ? C.green : C.red} />
        <Stat label="Open positions" value={`${positions.length}`} sub="max 1–2 CFM + 1–2 APP" />
      </div>
    </div>
  );
}

function Stat({ label, value, color = C.ink, sub }) {
  return (
    <div>
      <div style={{ font: `500 11px ${C.sans}`, color: C.inkFaint, marginBottom: 5 }}>{label}</div>
      <div style={{ font: `600 18px ${C.mono}`, color }}>{value}</div>
      {sub && <div style={{ font: `400 10px ${C.mono}`, color: C.inkFaint, marginTop: 3 }}>{sub}</div>}
    </div>
  );
}

// ============================================================================
// CHECKLIST VIEW — full entry checklists
// ============================================================================
function ChecklistView({ cfm, app }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))", gap: 16 }}>
      <FullChecklist tag="CFM" name="Cashflow Machine — XLV" color={C.blue} data={cfm} />
      <FullChecklist tag="APP" name="Appreciation — ILMN" color={C.amber} data={app} />
    </div>
  );
}

function FullChecklist({ tag, name, color, data }) {
  const go = data.verdict === "ENTER";
  return (
    <Panel title={name} eyebrow={`${tag} entry · all must pass`} accent={color}
      right={<span style={{ font: `700 13px ${C.mono}`, padding: "6px 12px", borderRadius: 6, background: go ? C.greenDim : C.redDim, color: go ? C.green : C.red }}>{data.verdict} · {data.pass}/{data.total}</span>}>
      <div>
        {data.items.map((i) => <CheckRow key={i[0]} label={i[0]} ok={i[1]} />)}
      </div>
      {!go && (
        <div style={{ marginTop: 12, font: `400 12px/1.4 ${C.sans}`, color: C.inkDim, background: C.panel2, padding: "10px 12px", borderRadius: 6 }}>
          {data.total - data.pass} condition{data.total - data.pass > 1 ? "s" : ""} unmet. Per your rules: never force entry — wait for the full setup.
        </div>
      )}
    </Panel>
  );
}

// ============================================================================
// POSITIONS VIEW
// ============================================================================
function PositionsView({ positions, setPositions, capital, reserve, deployed, openPL }) {
  const blank = { id: Date.now(), strategy: "CFM", symbol: "XLV", desc: "", cost: "", current: "", opened: new Date().toISOString().slice(0, 10) };
  const [draft, setDraft] = useState(blank);

  const add = () => {
    if (!draft.cost) return;
    setPositions([...positions, { ...draft, id: Date.now() }]);
    setDraft({ ...blank, id: Date.now() });
  };
  const update = (id, field, val) => setPositions(positions.map((p) => p.id === id ? { ...p, [field]: val } : p));
  const remove = (id) => setPositions(positions.filter((p) => p.id !== id));

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Panel title="Open positions" eyebrow="Tracking · synthetics & options"
        right={<span style={{ font: `500 12px ${C.mono}`, color: openPL >= 0 ? C.green : C.red }}>P&L {openPL >= 0 ? "+" : ""}${openPL.toLocaleString()}</span>}>
        {positions.length === 0 ? (
          <div style={{ font: `400 13px ${C.sans}`, color: C.inkDim, padding: "10px 0" }}>No open positions. Add one below to start tracking cost basis and P&L.</div>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 640 }}>
              <thead>
                <tr style={{ font: `600 10px ${C.mono}`, color: C.inkFaint, letterSpacing: 1, textTransform: "uppercase" }}>
                  {["Strat", "Symbol", "Description", "Cost basis", "Current value", "P&L", "Opened", ""].map((h) =>
                    <th key={h} style={{ textAlign: "left", padding: "8px 10px", borderBottom: `1px solid ${C.line}` }}>{h}</th>)}
                </tr>
              </thead>
              <tbody>
                {positions.map((p) => {
                  const pl = (parseFloat(p.current) || 0) - (parseFloat(p.cost) || 0);
                  const pct = p.cost ? (pl / parseFloat(p.cost)) * 100 : 0;
                  return (
                    <tr key={p.id} style={{ font: `400 12px ${C.sans}` }}>
                      <td style={td}><span style={{ font: `700 11px ${C.mono}`, color: p.strategy === "CFM" ? C.blue : C.amber }}>{p.strategy}</span></td>
                      <td style={td}>{p.symbol}</td>
                      <td style={td}>{p.desc || "—"}</td>
                      <td style={td}>${(parseFloat(p.cost) || 0).toLocaleString()}</td>
                      <td style={td}>
                        <input value={p.current} onChange={(e) => update(p.id, "current", e.target.value)}
                          placeholder="—" style={{ ...inputStyle, padding: "5px 7px", width: 90, font: `500 12px ${C.mono}` }} />
                      </td>
                      <td style={{ ...td, color: pl >= 0 ? C.green : C.red, font: `600 12px ${C.mono}` }}>
                        {pl >= 0 ? "+" : ""}${pl.toLocaleString()} <span style={{ color: C.inkFaint, fontSize: 10 }}>({pct.toFixed(0)}%)</span>
                      </td>
                      <td style={{ ...td, font: `400 11px ${C.mono}`, color: C.inkDim }}>{p.opened}</td>
                      <td style={td}><button onClick={() => remove(p.id)} style={{ background: "none", border: "none", color: C.red, cursor: "pointer", font: `400 16px ${C.sans}` }}>×</button></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel title="Add position" eyebrow="New entry">
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px,1fr))", gap: 12, alignItems: "end" }}>
          <Field label="Strategy"><Sel value={draft.strategy} onChange={(v) => setDraft({ ...draft, strategy: v })} options={[["CFM", "CFM"], ["APP", "APP"]]} /></Field>
          <Field label="Symbol"><input value={draft.symbol} onChange={(e) => setDraft({ ...draft, symbol: e.target.value.toUpperCase() })} style={inputStyle} /></Field>
          <Field label="Description"><input value={draft.desc} onChange={(e) => setDraft({ ...draft, desc: e.target.value })} placeholder="e.g. Jan 150C long" style={inputStyle} /></Field>
          <Field label="Cost basis ($)"><input type="number" value={draft.cost} onChange={(e) => setDraft({ ...draft, cost: e.target.value })} style={inputStyle} /></Field>
          <Field label="Current value ($)"><input type="number" value={draft.current} onChange={(e) => setDraft({ ...draft, current: e.target.value })} style={inputStyle} /></Field>
          <Field label="Opened"><input type="date" value={draft.opened} onChange={(e) => setDraft({ ...draft, opened: e.target.value })} style={inputStyle} /></Field>
          <button onClick={add} style={{ background: C.blue, border: "none", borderRadius: 6, color: "#fff", font: `600 13px ${C.sans}`, padding: "10px 16px", cursor: "pointer", height: 38 }}>Add position</button>
        </div>
      </Panel>
    </div>
  );
}
const td = { padding: "10px", borderBottom: `1px solid ${C.lineSoft}`, color: C.ink };

// ============================================================================
// INDICATORS VIEW — manual inputs for thinkorswim studies + macro
// ============================================================================
function IndicatorsView(props) {
  const { macro, setMacro, computed, calcStatus, onRefresh } = props;
  const cx = computed?.XLV, ci = computed?.ILMN;
  return (
    <div style={{ display: "grid", gap: 16 }}>
      {/* Auto-calc status banner */}
      <div style={{
        background: C.panel, border: `1px solid ${C.line}`, borderRadius: 10, padding: "14px 16px",
        display: "flex", alignItems: "center", justifyContent: "space-between", gap: 14, flexWrap: "wrap",
        borderLeft: `3px solid ${calcStatus === "ok" ? C.green : calcStatus === "loading" ? C.amber : C.redDim}`,
      }}>
        <div>
          <div style={{ font: `600 13px ${C.sans}`, color: C.ink, marginBottom: 4 }}>
            Auto-calc {calcStatus === "ok" ? "ready" : calcStatus === "loading" ? "computing…" : calcStatus === "fail" ? "unavailable" : "idle"}
          </div>
          <div style={{ font: `400 11px/1.4 ${C.sans}`, color: C.inkDim, maxWidth: 620 }}>
            Computed from daily Stooq history: RSI, OBV trend, volume ratio, MFI, RS3M, RS3M_MOM, MA21.
            Each shows next to your manual field — tap <b style={{ color: C.blue }}>use</b> to apply.
            {calcStatus === "fail" && " History blocked (often CORS in preview) — keep entering manually."}
          </div>
        </div>
        <button onClick={onRefresh} style={{
          background: C.blue, border: "none", borderRadius: 6, color: "#fff",
          font: `600 12px ${C.sans}`, padding: "9px 14px", cursor: "pointer", whiteSpace: "nowrap",
        }}>{calcStatus === "loading" ? "Fetching…" : "↻ Recalculate"}</button>
      </div>

      {(cx || ci) && (
        <div style={{ font: `400 10px ${C.mono}`, color: C.amber, padding: "0 2px" }}>
          ⚠ Scale note: computed RS3M / RS3M_MOM use a generic % formula and won't match thinkorswim's EMA-tuned values numerically — trust the direction, recalibrate thresholds to taste. As of: {cx?.asOf || ci?.asOf || "—"}.
        </div>
      )}

      <Panel title="Macro inputs" eyebrow="Level 1 · VIX auto-fills from live quote">
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px,1fr))", gap: 14 }}>
          <Field label="VIX" hint="auto"><NumIn value={macro.vix} onChange={(v) => setMacro({ ...macro, vix: v })} /></Field>
          <Field label="Breadth %" hint=">55 CFM / >60 APP"><NumIn step="1" value={macro.breadth} onChange={(v) => setMacro({ ...macro, breadth: v })} /></Field>
          <Field label="Fed policy"><Sel value={macro.fed} onChange={(v) => setMacro({ ...macro, fed: v })} options={[["dovish", "Dovish"], ["holding", "Holding"], ["hawkish", "Hawkish"]]} /></Field>
          <Field label="Growth"><Sel value={macro.growth} onChange={(v) => setMacro({ ...macro, growth: v })} options={[["accelerating", "Accelerating"], ["stable", "Stable"], ["slowing", "Slowing"]]} /></Field>
          <Field label="Inflation %"><NumIn step="0.1" value={macro.inflation} onChange={(v) => setMacro({ ...macro, inflation: v })} /></Field>
        </div>
      </Panel>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(340px,1fr))", gap: 16 }}>
        <InstrumentInputs label="XLV — CFM candidate" color={C.blue} calc={cx}
          inst={props.instXLV} setInst={props.setInstXLV} flow={props.flowXLV} setFlow={props.setFlowXLV} tech={props.techXLV} setTech={props.setTechXLV} />
        <InstrumentInputs label="ILMN — APP candidate" color={C.amber} calc={ci}
          inst={props.instILMN} setInst={props.setInstILMN} flow={props.flowILMN} setFlow={props.setFlowILMN} tech={props.techILMN} setTech={props.setTechILMN} />
      </div>
    </div>
  );
}

// Small computed-value chip with one-click apply
function CalcChip({ value, fmt = (v) => (typeof v === "number" ? v.toFixed(1) : v), onApply }) {
  if (value == null) return null;
  return (
    <button onClick={onApply} title="Apply computed value" style={{
      background: C.panel2, border: `1px solid ${C.blue}55`, borderRadius: 5, cursor: "pointer",
      font: `500 10px ${C.mono}`, color: C.blue, padding: "3px 7px", display: "inline-flex",
      alignItems: "center", gap: 5,
    }}>
      <span style={{ color: C.inkDim }}>calc</span>
      <b style={{ color: C.ink }}>{fmt(value)}</b>
      <span style={{ color: C.blue }}>use ↵</span>
    </button>
  );
}

function InstrumentInputs({ label, color, calc, inst, setInst, flow, setFlow, tech, setTech }) {
  const c = calc || {};
  return (
    <Panel title={label} eyebrow="auto-calc + manual studies" accent={color}>
      <div style={{ font: `600 10px ${C.mono}`, color: C.inkFaint, letterSpacing: 1, marginBottom: 10 }}>LEVEL 2 · INSTITUTIONAL</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Field label="RS3M" hint="vs SPY">
          <NumIn value={inst.rs3m} onChange={(v) => setInst({ ...inst, rs3m: v })} />
          <div style={{ marginTop: 5 }}><CalcChip value={c.rs3m} fmt={(v) => v.toFixed(2)} onApply={() => setInst({ ...inst, rs3m: +c.rs3m.toFixed(2) })} /></div>
        </Field>
        <Field label="RS3M_MOM">
          <NumIn step="1" value={inst.rs3mMom} onChange={(v) => setInst({ ...inst, rs3mMom: v })} />
          <div style={{ marginTop: 5 }}><CalcChip value={c.rs3mMom} fmt={(v) => v.toFixed(2)} onApply={() => setInst({ ...inst, rs3mMom: +c.rs3mMom.toFixed(2) })} /></div>
        </Field>
        <Field label="RS3M trend">
          <Sel value={inst.rs3mTrend} onChange={(v) => setInst({ ...inst, rs3mTrend: v })} options={[["up", "Up"], ["flat", "Flat"], ["down", "Down"]]} />
          <div style={{ marginTop: 5 }}><CalcChip value={c.rs3mTrend} onApply={() => setInst({ ...inst, rs3mTrend: c.rs3mTrend })} /></div>
        </Field>
        <Field label="Earnings rev."><Sel value={inst.earnings} onChange={(v) => setInst({ ...inst, earnings: v })} options={[["up", "Up"], ["flat", "Flat"], ["down", "Down"]]} /></Field>
        <Field label="Valuation"><Sel value={inst.valuation} onChange={(v) => setInst({ ...inst, valuation: v })} options={[["cheap", "Cheap"], ["reasonable", "Reasonable"], ["expensive", "Expensive"]]} /></Field>
        <Field label="Credit"><Sel value={inst.credit} onChange={(v) => setInst({ ...inst, credit: v })} options={[["easy", "Easy"], ["neutral", "Neutral"], ["tight", "Tight"]]} /></Field>
      </div>

      <div style={{ font: `600 10px ${C.mono}`, color: C.inkFaint, letterSpacing: 1, margin: "14px 0 10px" }}>LEVEL 3 · MONEY FLOW</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Field label="MoneyFlow (MFI)">
          <NumIn step="0.1" value={flow.mfi} onChange={(v) => setFlow({ ...flow, mfi: v })} />
          <div style={{ marginTop: 5 }}><CalcChip value={c.mfi} onApply={() => setFlow({ ...flow, mfi: +c.mfi.toFixed(1) })} /></div>
        </Field>
        <Field label="RSI">
          <NumIn step="1" value={flow.rsi} onChange={(v) => setFlow({ ...flow, rsi: v })} />
          <div style={{ marginTop: 5 }}><CalcChip value={c.rsi} onApply={() => setFlow({ ...flow, rsi: +c.rsi.toFixed(1) })} /></div>
        </Field>
        <Field label="OBV">
          <Sel value={flow.obv} onChange={(v) => setFlow({ ...flow, obv: v })} options={[["rising", "Rising"], ["flat", "Flat"], ["falling", "Falling"]]} />
          <div style={{ marginTop: 5 }}><CalcChip value={c.obv} onApply={() => setFlow({ ...flow, obv: c.obv })} /></div>
        </Field>
        <Field label="Volume ratio %">
          <NumIn step="1" value={flow.volRatio} onChange={(v) => setFlow({ ...flow, volRatio: v })} />
          <div style={{ marginTop: 5 }}><CalcChip value={c.volRatio} fmt={(v) => v.toFixed(0)} onApply={() => setFlow({ ...flow, volRatio: +c.volRatio.toFixed(0) })} /></div>
        </Field>
      </div>

      <div style={{ font: `600 10px ${C.mono}`, color: C.inkFaint, letterSpacing: 1, margin: "14px 0 10px" }}>LEVEL 4 · TECHNICAL</div>
      {calc && calc.price != null && (
        <div style={{ font: `400 11px ${C.mono}`, color: C.inkDim, marginBottom: 8 }}>
          Last {calc.price.toFixed(2)} · MA21 {calc.ma21.toFixed(2)} ·{" "}
          <span style={{ color: calc.priceAboveMA21 ? C.green : C.red }}>
            price {calc.priceAboveMA21 ? "above" : "below"} MA21
          </span>
        </div>
      )}
      <div style={{ display: "grid", gap: 4 }}>
        {[
          ["Price above MA21", "priceAboveMA21", true],
          ["2–3 bounces at support confirmed", "bouncesConfirmed", false],
          ["Support clearly defined", "supportDefined", false],
          ["Breakout above resistance (APP)", "breakoutConfirmed", false],
        ].map(([lbl, key, auto]) => (
          <div key={key} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 0" }}>
            <span style={{ font: `400 12px ${C.sans}`, color: C.inkDim }}>
              {lbl}{auto && calc && <span style={{ color: C.blue, font: `500 10px ${C.mono}`, marginLeft: 6 }}>auto: {calc.priceAboveMA21 ? "yes" : "no"}</span>}
            </span>
            <Toggle value={tech[key]} onChange={(v) => setTech({ ...tech, [key]: v })} />
          </div>
        ))}
      </div>
    </Panel>
  );
}
