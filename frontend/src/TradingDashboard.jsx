import React, { useState, useEffect, useMemo, useCallback, useRef } from "react";

/* ============================================================================
   TRAVIS — INSTITUTIONAL ROTATION DASHBOARD  (local app build)
   4-level decision system: Macro -> Institutional -> Money Flow -> Technical.
   Data + indicator math come from the local Python backend (no CORS, cached).
   Manual inputs and positions persist to the backend's state.json.
   ============================================================================ */

const API = ""; // same origin when served by backend; Vite proxies /api in dev

// In-memory mirror of persisted state. Hydrated from local browser storage
// first, then /api/state, and written back (debounced) whenever it changes.
// Local storage makes positions sticky across fast refreshes and backend deploys.
const STATE_STORAGE_KEY = "rotation-dashboard-state-v1";
const STICKY_POSITION_KEYS = new Set(["positions", "positionTransactions", "positionMarks", "schwabSnapshot"]);
const SCHWAB_STRATEGY = "SCHWAB";

const safeParseJson = (raw, fallback = {}) => {
  try {
    return raw ? JSON.parse(raw) : fallback;
  } catch (e) {
    return fallback;
  }
};

const readLocalState = () => {
  if (typeof window === "undefined") return {};
  return safeParseJson(window.localStorage?.getItem(STATE_STORAGE_KEY), {});
};

const writeLocalState = (state) => {
  if (typeof window === "undefined") return;
  try {
    window.localStorage?.setItem(STATE_STORAGE_KEY, JSON.stringify(state));
  } catch (e) {
    // localStorage can be unavailable in private/restricted browsers; backend save still works.
  }
};

const store = (() => {
  let mem = readLocalState();
  let saveTimer = null;
  const persistLocal = () => writeLocalState(mem);
  const stamp = (k) => {
    mem.__updatedAt = { ...(mem.__updatedAt || {}), [k]: Date.now() };
  };
  const mergeRemote = (obj = {}) => {
    const localUpdatedAt = mem.__updatedAt || {};
    const remoteUpdatedAt = obj.__updatedAt || {};
    const merged = { ...mem };

    Object.entries(obj || {}).forEach(([key, value]) => {
      if (key === "__updatedAt") return;
      const keepLocalSticky = STICKY_POSITION_KEYS.has(key)
        && key in mem
        && (localUpdatedAt[key] || 0) > (remoteUpdatedAt[key] || 0);
      if (!keepLocalSticky) merged[key] = value;
    });

    merged.__updatedAt = { ...remoteUpdatedAt, ...localUpdatedAt };
    mem = merged;
    persistLocal();
  };
  const flush = (useBeacon = false) => {
    clearTimeout(saveTimer);
    saveTimer = null;
    persistLocal();
    const body = JSON.stringify(mem);
    if (useBeacon && typeof navigator !== "undefined" && navigator.sendBeacon) {
      return navigator.sendBeacon(`${API}/api/state`, new Blob([body], { type: "application/json" }));
    }
    return fetch(`${API}/api/state`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body,
      keepalive: true,
    });
  };
  return {
    hydrate: mergeRemote,
    get: (k, fb) => (k in mem ? mem[k] : fb),
    set: (k, v, immediate = false) => {
      mem[k] = v;
      stamp(k);
      persistLocal();
      if (immediate) return flush();
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => { flush().catch(() => {}); }, 600); // debounce disk writes
      return Promise.resolve();
    },
    flush,
    flushBeforeUnload: () => flush(true),
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

// ---- Staleness (Phase 4) ----------------------------------------------------
// Every backend value carries staleness: fresh = covers the last completed
// trading session, yellow = 1 session behind, red = 2+ behind or quarantined.
const STALE_COLOR = { fresh: C.green, yellow: C.yellow, red: C.red, unknown: C.inkFaint, missing: C.red };
const STALE_LABEL = { fresh: "fresh", yellow: "1 day stale", red: "STALE", unknown: "unknown", missing: "missing" };

function StaleDot({ state, asOf, source, showDate = false }) {
  const color = STALE_COLOR[state] || C.inkFaint;
  const title = `${STALE_LABEL[state] || state || "unknown"}${asOf ? ` · as of ${asOf}` : ""}${source ? ` · ${source}` : ""}`;
  return (
    <span title={title} style={{ display: "inline-flex", alignItems: "center", gap: 4, verticalAlign: "middle" }}>
      <span style={{ width: 7, height: 7, borderRadius: "50%", background: color, display: "inline-block", flexShrink: 0 }} />
      {showDate && asOf && (
        <span style={{ font: `400 10px ${C.mono}`, color: state === "fresh" ? C.inkFaint : color }}>
          {String(asOf).slice(0, 10)}
        </span>
      )}
    </span>
  );
}

// Worst staleness across the Level 1 inputs, plus which input is oldest.
function macroFreshness(macroComputed) {
  const order = { fresh: 0, yellow: 1, red: 2, unknown: 2, missing: 2 };
  const expected = ["vix", "breadth", "fed", "growth", "inflation"];
  const fields = macroComputed?.fields || {};
  let worst = "unknown";
  let oldest = null;
  for (const key of expected) {
    const f = fields[key];
    const state = f ? (f.staleness || "unknown") : "missing";
    if (order[state] >= order[worst]) worst = state;
    const asOf = f?.asOf || null;
    if (!f || (oldest == null) || (order[state] > order[oldest.state]) ||
        (order[state] === order[oldest.state] && asOf && oldest.asOf && asOf < oldest.asOf)) {
      oldest = { key, state, asOf };
    }
  }
  const degraded = !macroComputed || macroComputed.degraded || worst === "red" || worst === "missing";
  return { worst, oldest, degraded };
}

const SECTORS = [
  { symbol: "XLK", name: "Technology", group: "growth" },
  { symbol: "XLY", name: "Consumer Discretionary", group: "growth" },
  { symbol: "XLC", name: "Communication Services", group: "growth" },
  { symbol: "XLI", name: "Industrials", group: "cyclical" },
  { symbol: "XLF", name: "Financials", group: "cyclical" },
  { symbol: "XLE", name: "Energy", group: "inflation" },
  { symbol: "XLB", name: "Materials", group: "inflation" },
  { symbol: "XLV", name: "Health Care", group: "defensive" },
  { symbol: "XLP", name: "Consumer Staples", group: "defensive" },
  { symbol: "XLU", name: "Utilities", group: "defensive" },
  { symbol: "XLRE", name: "Real Estate", group: "rates" },
];

const SECTOR_BY_SYMBOL = Object.fromEntries(SECTORS.map((sector) => [sector.symbol, sector]));
const DEFENSIVE_SECTORS = ["XLV", "XLP", "XLU", "XLRE"];
const APP_SECTORS = ["XLK", "XLY", "XLC", "XLI"];
const CFM_CANDIDATE_UNIVERSE = [
  "XLV", "XLP", "XLU", "XLRE",
  "LLY", "UNH", "JNJ", "MRK", "ABBV", "PFE",
  "PG", "COST", "WMT", "PEP", "KO",
  "NEE", "SO", "DUK", "PLD", "AMT",
];
const APP_CANDIDATE_UNIVERSE = [
  "XLK", "XLY", "XLC", "XLI",
  "NVDA", "MSFT", "AAPL", "AVGO", "AMD", "CRM", "NOW",
  "META", "GOOGL", "NFLX", "AMZN", "TSLA",
  "HD", "CAT", "GE", "HON", "DE",
];
const ENTRY_CANDIDATE_UNIVERSE = [...new Set([...CFM_CANDIDATE_UNIVERSE, ...APP_CANDIDATE_UNIVERSE])];
const SECTOR_PROXY_BY_STOCK = {
  AAPL: "XLK", MSFT: "XLK", NVDA: "XLK", AMD: "XLK", AVGO: "XLK", CRM: "XLK", NOW: "XLK",
  META: "XLC", GOOGL: "XLC", GOOG: "XLC", NFLX: "XLC",
  AMZN: "XLY", TSLA: "XLY", HD: "XLY", MCD: "XLY", NKE: "XLY",
  JNJ: "XLV", MRK: "XLV", PFE: "XLV", ABBV: "XLV", LLY: "XLV", UNH: "XLV", ILMN: "XLV",
  PG: "XLP", KO: "XLP", PEP: "XLP", COST: "XLP", WMT: "XLP",
  JPM: "XLF", BAC: "XLF", GS: "XLF", MS: "XLF", BRK: "XLF",
  XOM: "XLE", CVX: "XLE", COP: "XLE", SLB: "XLE",
  CAT: "XLI", GE: "XLI", HON: "XLI", DE: "XLI", BA: "XLI",
  LIN: "XLB", FCX: "XLB", NEM: "XLB",
  NEE: "XLU", SO: "XLU", DUK: "XLU",
  PLD: "XLRE", AMT: "XLRE",
};

const STRATEGY_META = {
  AUTO: { label: "Auto", color: C.blue },
  CFM: { label: "CFM", color: C.blue },
  APP: { label: "APP", color: C.amber },
  WAIT: { label: "No trade", color: C.inkDim },
  MIXED: { label: "Mixed", color: C.yellow },
};

const SECTOR_WATCH_PROFILES = {
  XLK: {
    tag: "GROWTH",
    name: "Technology Leadership",
    color: C.blue,
    setup: "Growth leadership / innovation entry",
    strategy: "growth",
    regimeLabel: "Risk-on growth",
    trigger: "Enter after XLK shows leadership momentum, rising sponsorship, clean money flow, and price holds above MA21.",
    bestWhen: "Best when growth is accelerating, breadth is firm, volatility is contained, and investors are paying up for earnings growth.",
    macroOk: (m) => m.growth === "accelerating" || m.fed === "dovish" || m.breadth >= 60,
    breadthMin: 55, vixMax: 22, rs3mMin: 0, mfiMin: 50, mfiMax: 80, volumeMin: 85, volumeMax: 180, needsRisingObv: true,
    rsLabel: "RS3M leadership positive", obvLabel: "OBV rising confirms sponsorship",
  },
  XLY: {
    tag: "CYCLE",
    name: "Consumer Discretionary",
    color: C.amber,
    setup: "Consumer cycle / risk-on entry",
    strategy: "cyclical growth",
    regimeLabel: "Consumer risk-on",
    trigger: "Enter after XLY confirms improving relative strength, broad risk appetite, constructive money flow, and MA21 support.",
    bestWhen: "Best when growth is improving, credit conditions are not restrictive, volatility is calm, and consumers are not under macro pressure.",
    macroOk: (m) => m.growth === "accelerating" || (m.fed !== "hawkish" && m.breadth >= 60),
    breadthMin: 58, vixMax: 20, rs3mMin: 0, mfiMin: 50, mfiMax: 75, volumeMin: 90, volumeMax: 175, needsRisingObv: true,
    rsLabel: "RS3M leadership positive", obvLabel: "OBV rising confirms risk appetite",
  },
  XLC: {
    tag: "GROWTH",
    name: "Communication Services",
    color: C.blue,
    setup: "Growth communication / platform entry",
    strategy: "growth",
    regimeLabel: "Growth leadership",
    trigger: "Enter after XLC confirms leadership, positive money flow, and a clean hold above MA21.",
    bestWhen: "Best when breadth is healthy, volatility is contained, and mega-cap/platform growth is attracting sponsorship.",
    macroOk: (m) => m.growth === "accelerating" || m.fed === "dovish" || m.breadth >= 60,
    breadthMin: 55, vixMax: 22, rs3mMin: 0, mfiMin: 50, mfiMax: 78, volumeMin: 85, volumeMax: 180, needsRisingObv: true,
    rsLabel: "RS3M leadership positive", obvLabel: "OBV rising or clearly supportive",
  },
  XLI: {
    tag: "CYCLE",
    name: "Industrials",
    color: C.green,
    setup: "Industrial cycle / broad-economy entry",
    strategy: "cyclical",
    regimeLabel: "Cyclical expansion",
    trigger: "Enter after XLI shows improving relative strength, healthy breadth, steady money flow, and MA21 support.",
    bestWhen: "Best when growth is stable-to-improving, breadth is broad, and cyclical leadership is rotating into the tape.",
    macroOk: (m) => m.growth !== "slowing" || m.breadth >= 60 || m.inflation > 3.2,
    breadthMin: 55, vixMax: 24, rs3mMin: -5, mfiMin: 50, mfiMax: 75, volumeMin: 80, volumeMax: 170, needsRisingObv: false,
    rsLabel: "RS3M at least near leadership", obvLabel: "OBV not falling",
  },
  XLF: {
    tag: "CYCLE",
    name: "Financials",
    color: C.green,
    setup: "Financials / credit-cycle entry",
    strategy: "cyclical",
    regimeLabel: "Credit-cycle support",
    trigger: "Enter after XLF confirms relative strength, stable macro risk, constructive money flow, and price above MA21.",
    bestWhen: "Best when credit is healthy, rates are not collapsing, breadth is stable, and financials are gaining sponsorship.",
    macroOk: (m) => m.fed !== "dovish" || m.growth === "accelerating" || m.breadth >= 58,
    breadthMin: 55, vixMax: 24, rs3mMin: -5, mfiMin: 50, mfiMax: 75, volumeMin: 80, volumeMax: 170, needsRisingObv: false,
    rsLabel: "RS3M at least near leadership", obvLabel: "OBV not falling",
  },
  XLE: {
    tag: "INFL",
    name: "Energy",
    color: C.amber,
    setup: "Inflation / commodity pressure entry",
    strategy: "inflation hedge",
    regimeLabel: "Inflation support",
    trigger: "Enter after XLE confirms commodity leadership, strong relative momentum, positive flow, and a controlled MA21 hold.",
    bestWhen: "Best when inflation is hot or sticky, commodity leadership is present, and energy is attracting tactical sponsorship.",
    macroOk: (m) => m.inflation > 3 || m.growth !== "accelerating" || m.fed === "hawkish",
    breadthMin: 50, vixMax: 26, rs3mMin: 0, mfiMin: 52, mfiMax: 82, volumeMin: 90, volumeMax: 200, needsRisingObv: true,
    rsLabel: "RS3M commodity leadership positive", obvLabel: "OBV rising confirms accumulation",
  },
  XLB: {
    tag: "INFL",
    name: "Materials",
    color: C.amber,
    setup: "Materials / inflation-cycle entry",
    strategy: "inflation cyclical",
    regimeLabel: "Commodity-cycle support",
    trigger: "Enter after XLB confirms cyclical or inflation leadership, constructive flow, and price above MA21.",
    bestWhen: "Best when inflation is sticky, global cycle data is firming, and materials are improving versus the market.",
    macroOk: (m) => m.inflation > 3 || m.growth === "accelerating" || m.breadth >= 58,
    breadthMin: 52, vixMax: 25, rs3mMin: -5, mfiMin: 50, mfiMax: 78, volumeMin: 85, volumeMax: 185, needsRisingObv: false,
    rsLabel: "RS3M improving versus market", obvLabel: "OBV not falling",
  },
  XLV: {
    tag: "DEF",
    name: "Health Care Defense",
    color: C.blue,
    setup: "Defensive health care / cashflow entry",
    strategy: "defensive cashflow",
    regimeLabel: "Defensive rotation",
    trigger: "Enter after XLV confirms defensive rotation, steady money flow, controlled volatility, and support above MA21.",
    bestWhen: "Best when growth is slowing, inflation is sticky, volatility is contained, and defensive sectors are gaining sponsorship.",
    macroOk: (m) => m.growth === "slowing" || m.fed === "hawkish" || (m.inflation >= 2 && m.inflation <= 3.2),
    breadthMin: 50, vixMax: 25, rs3mMin: -15, mfiMin: 55, mfiMax: 75, volumeMin: 70, volumeMax: 150, needsRisingObv: false,
    rsLabel: "RS3M defensive rotation range", obvLabel: "OBV green or flat",
  },
  XLP: {
    tag: "DEF",
    name: "Staples Defense",
    color: C.green,
    setup: "Defensive staples / capital-preservation entry",
    strategy: "defensive cashflow",
    regimeLabel: "Defensive rotation",
    trigger: "Enter after XLP confirms defensive sponsorship, resilient relative strength, steady money flow, and MA21 support.",
    bestWhen: "Best when growth is slowing, volatility is rising but not disorderly, and investors are rotating toward stable demand.",
    macroOk: (m) => m.growth === "slowing" || m.fed === "hawkish" || m.vix >= 18,
    breadthMin: 48, vixMax: 28, rs3mMin: -10, mfiMin: 52, mfiMax: 75, volumeMin: 70, volumeMax: 155, needsRisingObv: false,
    rsLabel: "RS3M defensive rotation improving", obvLabel: "OBV green or flat",
  },
  XLU: {
    tag: "DEF",
    name: "Utilities Defense",
    color: C.green,
    setup: "Utilities / defensive yield entry",
    strategy: "defensive yield",
    regimeLabel: "Defensive yield support",
    trigger: "Enter after XLU confirms defensive rotation, stable volatility, constructive flow, and MA21 support.",
    bestWhen: "Best when growth is slowing, volatility is elevated-but-controlled, and defensive yield is being sponsored.",
    macroOk: (m) => m.growth === "slowing" || m.fed !== "hawkish" || m.vix >= 18,
    breadthMin: 48, vixMax: 28, rs3mMin: -10, mfiMin: 52, mfiMax: 75, volumeMin: 70, volumeMax: 155, needsRisingObv: false,
    rsLabel: "RS3M defensive rotation improving", obvLabel: "OBV green or flat",
  },
  XLRE: {
    tag: "RATES",
    name: "Real Estate",
    color: C.amber,
    setup: "Rate-sensitive real estate entry",
    strategy: "rate-sensitive income",
    regimeLabel: "Rate relief support",
    trigger: "Enter after XLRE confirms rate-sensitive sponsorship, improving relative strength, constructive flow, and MA21 support.",
    bestWhen: "Best when the Fed is neutral-to-dovish, volatility is contained, and rate-sensitive groups are rotating higher.",
    macroOk: (m) => m.fed !== "hawkish" || m.growth === "slowing" || m.inflation <= 3,
    breadthMin: 52, vixMax: 24, rs3mMin: -5, mfiMin: 50, mfiMax: 75, volumeMin: 80, volumeMax: 165, needsRisingObv: false,
    rsLabel: "RS3M rate-sensitive rotation improving", obvLabel: "OBV not falling",
  },
};

// ============================================================================
// DATA LAYER — all fetching + indicator math happens in the Python backend.
// These just call the local API. No CORS, cached server-side.
// ============================================================================
async function apiQuotes() {
  const r = await fetch(`${API}/api/quotes`);
  if (!r.ok) throw new Error("quotes failed");
  return r.json();
}

async function apiIndicators(symbols = []) {
  const query = symbols.length ? `?symbols=${encodeURIComponent(symbols.join(","))}` : "";
  const r = await fetch(`${API}/api/indicators${query}`);
  if (!r.ok) throw new Error("indicators failed");
  return r.json();
}

async function apiMacro() {
  const r = await fetch(`${API}/api/macro`);
  if (!r.ok) throw new Error("macro failed");
  return r.json();
}

async function apiLevels(symbol) {
  const r = await fetch(`${API}/api/levels?symbol=${encodeURIComponent(symbol)}`);
  if (!r.ok) throw new Error("levels failed");
  return r.json();
}

async function apiDataIssues() {
  const r = await fetch(`${API}/api/data-issues`);
  if (!r.ok) throw new Error("data issues failed");
  return r.json();
}

// Pull live positions + trade history from the linked Schwab account. Returns
// { configured, accounts, transactions, errors } — transactions arrive in the
// same row shape the positions ledger consumes, so they merge through mergeTransactions.
async function apiAccountSync(days = 365) {
  const r = await fetch(`${API}/api/account/sync`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ days }),
  });
  const data = await r.json().catch(() => ({}));
  return { ...data, ok: r.ok };
}

// Manual overrides persist server-side with source="manual" and always beat
// ingested values. value=null clears the override (back to auto).
async function apiSetOverride(key, value) {
  return fetch(`${API}/api/overrides`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope: "macro", key, value }),
  });
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

function sectorFocus(m) {
  const sig = macroSignal(m);
  const primary = [];
  const secondary = [];
  const avoid = [];
  const rationale = [];
  let regime = "Mixed / confirmation required";
  let favoredStrategy = "Selective";
  let entryPermission = sig.level === "RED" ? "Protect capital" : "Selective entries only";

  if (m.vix > 30 || m.breadth < 45) {
    regime = "Risk-off / capital protection";
    favoredStrategy = "Defense / cash";
    primary.push("XLV", "XLP", "XLU");
    avoid.push("XLK", "XLY", "XLC", "XLI");
    rationale.push("VIX/breadth conditions are hostile; prioritize exits and only monitor defensives.");
  } else if (m.growth === "slowing" || m.fed === "hawkish") {
    regime = "Defensive rotation";
    favoredStrategy = "CFM";
    primary.push("XLV", "XLP", "XLU");
    secondary.push("XLRE");
    avoid.push("XLY", "XLK");
    entryPermission = "CFM sectors only after rotation confirms";
    rationale.push("Growth is slowing and/or Fed is restrictive, so defensive sectors get first attention.");
  } else if (m.inflation > 3.2) {
    regime = "Inflation / commodity pressure";
    favoredStrategy = "CFM / tactical";
    primary.push("XLE", "XLB", "XLI");
    secondary.push("XLF");
    avoid.push("XLY", "XLRE");
    entryPermission = "Tactical entries after flow confirms";
    rationale.push("Hot inflation favors commodity/cyclical sectors over rate-sensitive groups.");
  } else if (m.fed === "dovish" || (m.growth === "accelerating" && m.breadth >= 55)) {
    regime = "Risk-on growth";
    favoredStrategy = "APP";
    primary.push("XLK", "XLY", "XLC", "XLI");
    secondary.push("XLF");
    avoid.push("XLU", "XLP");
    entryPermission = "APP setups allowed with breakout confirmation";
    rationale.push("Growth/breadth/Fed backdrop supports appreciation sectors first.");
  } else {
    primary.push("XLI", "XLF", "XLV");
    secondary.push("XLK", "XLP");
    entryPermission = "Wait for RS3M_MOM leadership";
    rationale.push("Macro is mixed; only sectors with clear institutional confirmation should matter.");
  }

  return { ...sig, regime, favoredStrategy, entryPermission, primary, secondary, avoid, rationale };
}

function bucketsFromCalc(calc = {}) {
  return {
    inst: { rs3m: calc.rs3m ?? 0, rs3mMom: calc.rs3mMom ?? 0, rs3mTrend: calc.rs3mTrend || "flat" },
    flow: { mfi: calc.mfi ?? 0, rsi: calc.rsi ?? 0, obv: calc.obv || "flat", volRatio: calc.volRatio ?? 0, volAccel: calc.volAccel ?? 0 },
    tech: { priceAboveMA21: !!calc.priceAboveMA21, bouncesConfirmed: false, supportDefined: true, breakoutConfirmed: false },
  };
}

function sectorRotationStatus(calc, focusTier, macro) {
  if (!calc) return { score: 0, total: 7, status: "No data", action: "Refresh / enter manually", color: C.inkFaint, exits: [] };
  const { inst, flow, tech } = bucketsFromCalc(calc);
  const checks = [
    inst.rs3mMom > 0,
    inst.rs3mTrend === "up",
    inst.rs3m > -15,
    flow.mfi >= 50,
    flow.obv !== "falling",
    flow.volRatio >= 70,
    tech.priceAboveMA21,
  ];
  const score = checks.filter(Boolean).length;
  const exits = exitTriggers(inst, flow, macro, tech).filter((t) => t[1]);
  let status = "Ignore";
  let action = focusTier === "Ignored" ? "De-emphasize until Level 1 changes" : "Watch";
  let color = C.inkDim;

  if (exits.length >= 2) { status = "Exiting"; action = "Reduce / avoid new entries"; color = C.red; }
  else if (score >= 6 && focusTier !== "Ignored") { status = "Confirmed"; action = "Entry allowed after support/breakout"; color = C.green; }
  else if (inst.rs3mMom > 0 && inst.rs3mTrend === "up" && focusTier !== "Ignored") { status = "Rotating in"; action = "Track for confirmation"; color = C.blue; }
  else if (score >= 6) { status = "Exceptional"; action = "Monitor despite macro mismatch"; color = C.amber; }
  else if (score >= 4 && focusTier !== "Ignored") { status = "Watch"; action = "Needs stronger flow/technical proof"; color = C.yellow; }

  return { score, total: checks.length, status, action, color, exits };
}

function positionGuidance(position, computed, macro, focus) {
  const symbol = (position.symbol || "").toUpperCase();
  const calc = computed?.[symbol];
  const focusTier = focus.primary.includes(symbol) ? "Primary" : focus.secondary.includes(symbol) ? "Secondary" : focus.avoid.includes(symbol) ? "Ignored" : "Neutral";
  if (!calc) return { action: "Manual", color: C.inkDim, note: "No live indicator set for this symbol yet." };
  const rotation = sectorRotationStatus(calc, focusTier, macro);
  const isCFM = position.strategy === "CFM";
  const isAPP = position.strategy === "APP";

  if (rotation.exits.length >= 2) return { action: "Exit / reduce", color: C.red, note: rotation.exits.map((e) => e[0]).join(" · ") };
  if (focusTier === "Ignored" && isAPP) return { action: "Tighten", color: C.amber, note: "Position sector is not favored by Level 1; require leadership to stay." };
  if (focusTier === "Ignored" && isCFM) return { action: "Review", color: C.amber, note: "CFM sector no longer fits the current macro focus." };
  if (rotation.status === "Confirmed") return { action: isAPP ? "Hold winner" : "Hold", color: C.green, note: "Institutional rotation, flow, and MA21 remain supportive." };
  if (rotation.status === "Rotating in") return { action: "Hold / monitor", color: C.blue, note: "Rotation is improving but still needs daily confirmation." };
  return { action: "Tighten", color: C.yellow, note: "Not enough confirmation; watch RS3M_MOM, OBV, and MA21." };
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
const TABS = ["Command", "Rotation", "Entry Watch", "Positions", "Indicators"];
const DEFAULT_ENTRY_WATCH_SYMBOLS = [];

// Hydration gate: load persisted state from the backend before the dashboard
// mounts, so saved positions and manual inputs initialize correctly.
export default function App() {
  const [ready, setReady] = useState(false);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    const persistBeforeUnload = () => { store.flushBeforeUnload(); };
    window.addEventListener("pagehide", persistBeforeUnload);
    window.addEventListener("beforeunload", persistBeforeUnload);

    fetch(`${API}/api/state`)
      .then((r) => r.json())
      .then((data) => { store.hydrate(data || {}); setReady(true); })
      .catch(() => { setOffline(true); setReady(true); });

    return () => {
      window.removeEventListener("pagehide", persistBeforeUnload);
      window.removeEventListener("beforeunload", persistBeforeUnload);
    };
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

  // ---- State: macro (Level 1). Auto-fills from /api/macro when available. ----
  const [macro, setMacro] = useState(store.get("macro", {
    vix: 21.51, breadth: 47, fed: "hawkish", growth: "slowing", inflation: 3.0,
  }));
  const [macroComputed, setMacroComputed] = useState(store.get("macroComputed", null));
  const [macroStatus, setMacroStatus] = useState("idle");

  // ---- State: institutional (Level 2) per instrument ----
  const [instXLV, setInstXLV] = useState(store.get("instXLV", {
    rs3m: -8.91, rs3mMom: 884, rs3mTrend: "up", earnings: "up", valuation: "cheap", credit: "easy",
  }));
  const [instAAPL, setInstAAPL] = useState(store.get("instAAPL", {
    rs3m: 0, rs3mMom: 0, rs3mTrend: "flat", earnings: "flat", valuation: "reasonable", credit: "neutral",
  }));

  // ---- State: money flow (Level 3) per instrument ----
  const [flowXLV, setFlowXLV] = useState(store.get("flowXLV", {
    mfi: 70.66, rsi: 58, obv: "rising", volRatio: 95, volAccel: 100,
  }));
  const [flowAAPL, setFlowAAPL] = useState(store.get("flowAAPL", {
    mfi: 50, rsi: 50, obv: "flat", volRatio: 100, volAccel: 100,
  }));

  // ---- State: technical (Level 4) per instrument ----
  const [techXLV, setTechXLV] = useState(store.get("techXLV", {
    priceAboveMA21: true, bouncesConfirmed: false, supportDefined: true, breakoutConfirmed: false,
  }));
  const [techAAPL, setTechAAPL] = useState(store.get("techAAPL", {
    priceAboveMA21: false, bouncesConfirmed: false, supportDefined: false, breakoutConfirmed: false,
  }));

  // ---- State: data issues (quarantine, provider auth, last ingest run) ----
  const [dataIssues, setDataIssues] = useState(null);

  // ---- State: positions ----
  const [positions, setPositions] = useState(store.get("positions", []));

  // ---- State: entry watch list ----
  const [entryWatchSymbols, setEntryWatchSymbols] = useState(store.get("entryWatchSymbols", DEFAULT_ENTRY_WATCH_SYMBOLS));

  // ---- State: auto-computed indicators per symbol + TOS sector overrides ----
  const [computed, setComputed] = useState(store.get("computed", {}));
  const [sectorOverrides, setSectorOverrides] = useState(store.get("sectorOverrides", {}));
  const [calcStatus, setCalcStatus] = useState("idle");

  const normalizedEntryWatchItems = useMemo(() => normalizeWatchItems(entryWatchSymbols || []), [entryWatchSymbols]);

  const updateEntryWatchSymbols = useCallback((items) => {
    const normalized = normalizeWatchItems(items);
    setEntryWatchSymbols(normalized);
    store.set("entryWatchSymbols", normalized, true).catch(() => {});
  }, []);

  useEffect(() => {
    if (JSON.stringify(entryWatchSymbols || []) !== JSON.stringify(normalizedEntryWatchItems)) {
      updateEntryWatchSymbols(normalizedEntryWatchItems);
    }
  }, [entryWatchSymbols, normalizedEntryWatchItems, updateEntryWatchSymbols]);

  // Persist on change
  useEffect(() => { store.set("macro", macro); }, [macro]);
  useEffect(() => { store.set("instXLV", instXLV); store.set("instAAPL", instAAPL); }, [instXLV, instAAPL]);
  useEffect(() => { store.set("flowXLV", flowXLV); store.set("flowAAPL", flowAAPL); }, [flowXLV, flowAAPL]);
  useEffect(() => { store.set("techXLV", techXLV); store.set("techAAPL", techAAPL); }, [techXLV, techAAPL]);
  useEffect(() => { store.set("positions", positions); }, [positions]);
  useEffect(() => { store.set("entryWatchSymbols", normalizedEntryWatchItems); }, [normalizedEntryWatchItems]);
  useEffect(() => { store.set("sectorOverrides", sectorOverrides); }, [sectorOverrides]);

  // ---- Pull quotes + backend-computed indicators ----
  const refreshQuotes = useCallback(async () => {
    setFetchStatus("loading");
    setCalcStatus("loading");
    setMacroStatus("loading");

    // Quotes (backend returns keys XLV, AAPL, ^VIX, SPY — read from the datastore)
    try {
      const raw = await apiQuotes();
      const out = {
        XLV: raw.XLV || { symbol: "XLV", error: true },
        AAPL: raw.AAPL || { symbol: "AAPL", error: true },
        VIX: raw["^VIX"] || { symbol: "VIX", error: true },
        SPY: raw.SPY || { symbol: "SPY", error: true },
      };
      setQuotes(out);
      store.set("quotes", out);
      const ts = new Date().toLocaleTimeString();
      setLastFetch(ts); store.set("lastFetch", ts);
      setFetchStatus(Object.values(out).some((q) => q.error) ? "partial" : "ok");
    } catch (e) {
      setFetchStatus("partial");
    }

    // Data issues (quarantine, provider auth problems, last ingest run)
    try {
      setDataIssues(await apiDataIssues());
    } catch (e) {
      /* panel simply shows nothing new */
    }

    // Level 1 macro snapshot (best-effort server-side calculations)
    try {
      const snap = await apiMacro();
      const vals = snap?.values || {};
      setMacro((m) => ({ ...m, ...vals }));
      setMacroComputed(snap);
      store.set("macroComputed", snap);
      setMacroStatus(Object.keys(vals).length ? (Object.keys(snap?.errors || {}).length ? "partial" : "ok") : "fail");
    } catch (e) {
      setMacroStatus("fail");
    }

    // Indicators (already computed server-side)
    try {
      const watchIndicatorSymbols = normalizedEntryWatchItems.flatMap((item) => [item.symbol, item.sectorProxy]).filter(Boolean);
      const comp = await apiIndicators([...SECTORS.map((s) => s.symbol), ...ENTRY_CANDIDATE_UNIVERSE, ...watchIndicatorSymbols]);
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
  }, [normalizedEntryWatchItems]);

  useEffect(() => { refreshQuotes(); /* once on mount and watch-list changes */ }, [refreshQuotes]);

  // ---- Macro manual overrides --------------------------------------------
  // Editing a Level 1 field stores a server-side override (source="manual",
  // timestamped) that wins over ingested values on every refresh. Accepting
  // the computed value clears the override.
  const overrideTimers = useRef({});
  const setMacroField = useCallback((key, value) => {
    setMacro((m) => ({ ...m, [key]: value }));
    clearTimeout(overrideTimers.current[key]);
    overrideTimers.current[key] = setTimeout(() => { apiSetOverride(key, value).catch(() => {}); }, 800);
  }, []);
  const acceptAutoMacro = useCallback(async (key) => {
    clearTimeout(overrideTimers.current[key]);
    try {
      await apiSetOverride(key, null);
      const snap = await apiMacro();
      setMacroComputed(snap); store.set("macroComputed", snap);
      if (snap?.values?.[key] != null) setMacro((m) => ({ ...m, [key]: snap.values[key] }));
    } catch (e) { /* keep current value; next refresh reconciles */ }
  }, []);

  // ---- Derived signals ----
  const sig = useMemo(() => macroSignal(macro), [macro]);
  const focus = useMemo(() => sectorFocus(macro), [macro]);
  const sectorRows = useMemo(() => SECTORS.map((sector) => {
    const tier = focus.primary.includes(sector.symbol) ? "Primary" : focus.secondary.includes(sector.symbol) ? "Secondary" : focus.avoid.includes(sector.symbol) ? "Ignored" : "Neutral";
    const calc = computed?.[sector.symbol] || null;
    return { ...sector, tier, calc, rotation: sectorRotationStatus(calc, tier, macro) };
  }).sort((a, b) => {
    const tierRank = { Primary: 0, Secondary: 1, Neutral: 2, Ignored: 3 };
    return (tierRank[a.tier] - tierRank[b.tier]) || (b.rotation.score - a.rotation.score);
  }), [computed, focus, macro]);
  const positionGuide = useMemo(() => Object.fromEntries(positions.map((p) => [p.id, positionGuidance(p, computed, macro, focus)])), [positions, computed, macro, focus]);
  const cfm = useMemo(() => cfmChecklist(macro, instXLV, flowXLV, techXLV), [macro, instXLV, flowXLV, techXLV]);
  const app = useMemo(() => appChecklist(macro, instAAPL, flowAAPL, techAAPL), [macro, instAAPL, flowAAPL, techAAPL]);
  const exitsXLV = useMemo(() => exitTriggers(instXLV, flowXLV, macro, techXLV), [instXLV, flowXLV, macro, techXLV]);
  const exitsAAPL = useMemo(() => exitTriggers(instAAPL, flowAAPL, macro, techAAPL), [instAAPL, flowAAPL, macro, techAAPL]);

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
        <Header sig={sig} lastFetch={lastFetch} status={fetchStatus} onRefresh={refreshQuotes} quotes={quotes} macroComputed={macroComputed} />

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
          <CommandView sig={sig} focus={focus} sectorRows={sectorRows} positionGuide={positionGuide} cfm={cfm} app={app} macro={macro} setMacro={setMacro}
            quotes={quotes} exitsXLV={exitsXLV} exitsAAPL={exitsAAPL}
            deployed={deployed} reserve={reserve} capital={capital} openPL={openPL} positions={positions} />
        )}
        {tab === "Rotation" && (
          <RotationView focus={focus} rows={sectorRows} />
        )}
        {tab === "Entry Watch" && (
          <EntryWatchView app={app} macro={macro} focus={focus} computed={computed} calcStatus={calcStatus} entryWatchSymbols={normalizedEntryWatchItems} setEntryWatchSymbols={updateEntryWatchSymbols} />
        )}
        {tab === "Positions" && (
          <PositionsView
            positions={positions}
            setPositions={setPositions}
            guidance={positionGuide}
            capital={capital}
            reserve={reserve}
            deployed={deployed}
            openPL={openPL}
          />
        )}
        {tab === "Indicators" && (
          <IndicatorsView
            macro={macro} setMacroField={setMacroField} acceptAutoMacro={acceptAutoMacro}
            macroComputed={macroComputed} macroStatus={macroStatus}
            computed={computed} calcStatus={calcStatus} onRefresh={refreshQuotes}
            dataIssues={dataIssues}
            instXLV={instXLV} setInstXLV={setInstXLV} flowXLV={flowXLV} setFlowXLV={setFlowXLV} techXLV={techXLV} setTechXLV={setTechXLV}
            instAAPL={instAAPL} setInstAAPL={setInstAAPL} flowAAPL={flowAAPL} setFlowAAPL={setFlowAAPL} techAAPL={techAAPL} setTechAAPL={setTechAAPL}
          />
        )}
      </div>
    </div>
  );
}

// ============================================================================
// HEADER — the signature: a live macro verdict beacon + ticker strip
// ============================================================================
function Header({ sig, lastFetch, status, onRefresh, quotes, macroComputed }) {
  const fresh = macroFreshness(macroComputed);
  // A permission system must never show a confident verdict on stale inputs:
  // when any regime input is red-stale or missing, the beacon goes grey.
  const degraded = fresh.degraded;
  const color = degraded ? C.inkFaint : SIG[sig.level];
  const verdictMap = { GREEN: "RISK-ON", YELLOW: "MIXED", RED: "RISK-OFF" };
  const actionMap = {
    GREEN: "Conditions favor deployment",
    YELLOW: "Hold — wait for confirmation",
    RED: "Defensive — do not force entries",
  };
  const oldestNote = fresh.oldest
    ? `oldest input: ${fresh.oldest.key}${fresh.oldest.asOf ? ` (as of ${String(fresh.oldest.asOf).slice(0, 10)})` : " (no data)"}`
    : "no ingested macro data";
  return (
    <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "stretch" }}>
      {/* Beacon */}
      <div style={{
        flex: "1 1 320px", background: C.panel, border: `1px solid ${degraded ? C.red : C.line}`,
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
          <div style={{ font: `700 30px/1 ${C.sans}`, color, letterSpacing: -0.8 }}>
            {degraded ? "DEGRADED DATA" : verdictMap[sig.level]}
          </div>
          <div style={{ font: `400 12px/1.3 ${C.sans}`, color: degraded ? C.red : C.inkDim, marginTop: 6 }}>
            {degraded
              ? `Regime inputs are stale or missing — do not trust this gate. ${oldestNote}`
              : actionMap[sig.level]}
          </div>
          {!degraded && (
            <div style={{ font: `400 10px/1.3 ${C.mono}`, color: fresh.worst === "yellow" ? C.yellow : C.inkFaint, marginTop: 5 }}>
              {fresh.worst === "yellow" ? `⚠ data 1 trading day behind — ${oldestNote}` : oldestNote}
            </div>
          )}
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
          {["XLV", "AAPL", "VIX", "SPY"].map((s) => {
            const q = quotes[s];
            const chg = q && !q.error && q.open != null ? q.close - q.open : null;
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
                <div style={{ marginTop: 5 }}>
                  {q && !q.error
                    ? <StaleDot state={q.staleness || "unknown"} asOf={q.date} source={q.source} showDate />
                    : <StaleDot state="missing" asOf={null} showDate={false} />}
                  {q && q.error && <span style={{ font: `400 10px ${C.mono}`, color: C.red, marginLeft: 4 }}>no data</span>}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// COMMAND VIEW — everything at a glance
// ============================================================================
function CommandView({ sig, focus, sectorRows, positionGuide, cfm, app, macro, setMacro, quotes, exitsXLV, exitsAAPL, deployed, reserve, capital, openPL, positions }) {
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

      <SectorFocusPanel focus={focus} />
      <SectorRotationTable rows={sectorRows.filter((r) => r.tier !== "Ignored").slice(0, 6)} compact />
      {positions.length > 0 && <PositionGuidancePanel positions={positions} guidance={positionGuide} />}

      {/* Two strategy cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 16 }}>
        <StrategyCard
          tag="CFM" name="Cashflow Machine" instrument="XLV" color={C.blue}
          verdict={cfm.verdict} pass={cfm.pass} total={cfm.total}
          target="1–2% weekly" note={macroFavorsCFM ? "Macro supports defensive rotation" : "Macro not ideal for CFM"} />
        <StrategyCard
          tag="APP" name="Appreciation" instrument="AAPL" color={C.amber}
          verdict={app.verdict} pass={app.pass} total={app.total}
          target="30–50% / trade" note={macro.breadth > 60 ? "Breadth supports growth" : "Breadth below 60% — wait"} />
      </div>

      {/* Exit watch */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 16 }}>
        <ExitWatch title="XLV exit triggers" triggers={exitsXLV} />
        <ExitWatch title="AAPL exit triggers" triggers={exitsAAPL} />
      </div>

      {/* Capital bar */}
      <Panel title="Capital allocation" eyebrow="Portfolio · non-negotiable rules">
        <CapitalBar capital={capital} deployed={deployed} reserve={reserve} openPL={openPL} positions={positions} />
      </Panel>
    </div>
  );
}

function SectorFocusPanel({ focus }) {
  return (
    <Panel title="Level 1 sector focus" eyebrow={focus.regime} accent={SIG[focus.level]}
      right={<span style={{ font: `700 12px ${C.mono}`, color: focus.favoredStrategy === "APP" ? C.amber : focus.favoredStrategy === "CFM" ? C.blue : C.ink }}>{focus.favoredStrategy}</span>}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px,1fr))", gap: 12 }}>
        <FocusList title="Primary focus" symbols={focus.primary} color={C.green} />
        <FocusList title="Secondary" symbols={focus.secondary} color={C.yellow} />
        <FocusList title="De-emphasize" symbols={focus.avoid} color={C.inkFaint} />
      </div>
      <div style={{ marginTop: 12, font: `400 12px/1.45 ${C.sans}`, color: C.inkDim }}>
        <b style={{ color: C.ink }}>Entry permission:</b> {focus.entryPermission}. {focus.rationale[0]}
      </div>
    </Panel>
  );
}

function FocusList({ title, symbols, color }) {
  return (
    <div style={{ background: C.panel2, border: `1px solid ${C.lineSoft}`, borderRadius: 8, padding: "12px 13px" }}>
      <div style={{ font: `600 10px ${C.mono}`, color: C.inkFaint, letterSpacing: 1, marginBottom: 8 }}>{title}</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {symbols.length ? symbols.map((sym) => <span key={sym} style={{ font: `700 12px ${C.mono}`, color, background: C.bg, border: `1px solid ${C.line}`, borderRadius: 5, padding: "5px 8px" }}>{sym}</span>) : <span style={{ color: C.inkFaint }}>—</span>}
      </div>
    </div>
  );
}

function SectorRotationTable({ rows, compact = false }) {
  return (
    <Panel title={compact ? "Focused institutional rotation" : "Sector rotation monitor"} eyebrow="Level 2/3/4 · sectors ranked by Level 1 focus">
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", minWidth: compact ? 860 : 980 }}>
          <thead>
            <tr style={{ font: `600 10px ${C.mono}`, color: C.inkFaint, letterSpacing: 1, textTransform: "uppercase" }}>
              {["Focus", "Sector", "RS3M", "MOM", "Vol%", "VolAccel", "RSI", "MA21", "Score", "Status", "Action"].map((h) =>
                <th key={h} style={{ textAlign: "left", padding: "8px 10px", borderBottom: `1px solid ${C.line}` }}>{h}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const c = r.calc || {};
              return (
                <tr key={r.symbol} style={{ font: `400 12px ${C.sans}` }}>
                  <td style={td}><span style={{ color: r.tier === "Primary" ? C.green : r.tier === "Secondary" ? C.yellow : r.tier === "Ignored" ? C.inkFaint : C.inkDim, font: `700 10px ${C.mono}` }}>{r.tier}</span></td>
                  <td style={td}>
                    <b>{r.symbol}</b>{" "}
                    <StaleDot state={c.staleness || (r.calc ? "unknown" : "missing")} asOf={c.asOf} source={c.source} />
                    <div style={{ color: C.inkFaint, fontSize: 10 }}>{r.name}</div>
                  </td>
                  <td style={{ ...td, color: (c.rs3m ?? 0) >= 0 ? C.green : C.red }}>{c.rs3m != null ? c.rs3m.toFixed(2) : "—"}</td>
                  <td style={{ ...td, color: (c.rs3mMom ?? 0) >= 0 ? C.green : C.red }}>{c.rs3mMom != null ? c.rs3mMom.toFixed(0) : "—"}</td>
                  <td style={td}>{c.volRatio != null ? c.volRatio.toFixed(0) : "—"}</td>
                  <td style={td}>{c.volAccel != null ? c.volAccel.toFixed(0) : "—"}</td>
                  <td style={td}>{c.rsi != null ? c.rsi.toFixed(1) : "—"}</td>
                  <td style={{ ...td, color: c.priceAboveMA21 ? C.green : C.red }}>{c.priceAboveMA21 == null ? "—" : c.priceAboveMA21 ? "Above" : "Below"}</td>
                  <td style={td}>{r.rotation.score}/{r.rotation.total}</td>
                  <td style={{ ...td, color: r.rotation.color, fontWeight: 700 }}>{r.rotation.status}</td>
                  <td style={{ ...td, color: C.inkDim }}>{r.rotation.action}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function PositionGuidancePanel({ positions, guidance }) {
  return (
    <Panel title="Position management" eyebrow="Hold / tighten / reduce by strategy">
      <div style={{ display: "grid", gap: 8 }}>
        {positions.slice(0, 4).map((p) => {
          const g = guidance[p.id] || {};
          return (
            <div key={p.id} style={{ display: "grid", gridTemplateColumns: "80px 100px 130px 1fr", gap: 10, alignItems: "center", background: C.panel2, border: `1px solid ${C.lineSoft}`, borderRadius: 8, padding: "10px 12px" }}>
              <span style={{ font: `700 11px ${C.mono}`, color: p.strategy === "CFM" ? C.blue : C.amber }}>{p.strategy}</span>
              <span style={{ font: `700 12px ${C.mono}`, color: C.ink }}>{p.symbol}</span>
              <span style={{ font: `700 12px ${C.mono}`, color: g.color || C.ink }}>{g.action || "Monitor"}</span>
              <span style={{ font: `400 12px ${C.sans}`, color: C.inkDim }}>{g.note || "No guidance yet."}</span>
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

function RotationView({ focus, rows }) {
  return (
    <div style={{ display: "grid", gap: 16 }}>
      <SectorFocusPanel focus={focus} />
      <SectorRotationTable rows={rows} />
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
// ENTRY WATCH VIEW — actionable watch list for when to enter
// ============================================================================
function readinessFromChecklist(data) {
  const ratio = data.total ? data.pass / data.total : 0;
  if (data.verdict === "ENTER") return { label: "Entry ready", color: C.green, tone: C.greenDim };
  if (ratio >= 0.8) return { label: "Close watch", color: C.amber, tone: `${C.amber}22` };
  if (ratio >= 0.6) return { label: "Building", color: C.blue, tone: `${C.blue}22` };
  return { label: "Early / wait", color: C.inkDim, tone: C.panel2 };
}

function splitChecklistItems(items) {
  const passed = items.filter(([, ok]) => ok).map(([label]) => label);
  const missing = items.filter(([, ok]) => !ok).map(([label]) => label);
  return { passed, missing };
}

function normalizeWatchSymbol(value) {
  return String(value || "").trim().toUpperCase().replace(/[^A-Z0-9.^-]/g, "");
}

function inferSectorProxy(symbol) {
  const clean = normalizeWatchSymbol(symbol);
  if (SECTOR_BY_SYMBOL[clean]) return clean;
  return SECTOR_PROXY_BY_STOCK[clean] || "";
}

function normalizeStrategyMode(value) {
  const mode = String(value || "AUTO").toUpperCase();
  return ["AUTO", "CFM", "APP"].includes(mode) ? mode : "AUTO";
}

function normalizeWatchItem(item) {
  if (typeof item === "string") {
    const symbol = normalizeWatchSymbol(item);
    if (!symbol) return null;
    return { symbol, strategyMode: "AUTO", sectorProxy: inferSectorProxy(symbol) };
  }
  const symbol = normalizeWatchSymbol(item?.symbol);
  if (!symbol) return null;
  const sectorProxy = normalizeWatchSymbol(item?.sectorProxy) || inferSectorProxy(symbol);
  return { symbol, strategyMode: normalizeStrategyMode(item?.strategyMode), sectorProxy };
}

function normalizeWatchItems(items = []) {
  const bySymbol = new Map();
  for (const item of items) {
    const normalized = normalizeWatchItem(item);
    if (normalized) bySymbol.set(normalized.symbol, normalized);
  }
  return Array.from(bySymbol.values());
}

function missingIndicatorChecklist(symbol, calcStatus, setup, bestWhen) {
  const loading = calcStatus === "loading";
  return {
    items: [[loading ? "Looking up indicator data" : "Indicator data found", false]],
    pass: 0,
    total: 1,
    verdict: "WAIT",
    trigger: loading
      ? `Looking up ${symbol} now. The checklist will evaluate as soon as indicators load.`
      : `No indicator data was found for ${symbol}. Check the ticker symbol or refresh indicators before making an entry decision.`,
    setup,
    bestWhen,
  };
}

function focusTierForSymbol(symbol, focus) {
  if (focus.primary.includes(symbol)) return "Primary";
  if (focus.secondary.includes(symbol)) return "Secondary";
  if (focus.avoid.includes(symbol)) return "Ignored";
  return "Neutral";
}

function sectorWatchChecklist(symbol, calc, macro, focus, calcStatus) {
  const profile = SECTOR_WATCH_PROFILES[symbol];
  if (!calc) return missingIndicatorChecklist(symbol, calcStatus, profile.setup, profile.bestWhen);

  const { inst, flow, tech } = bucketsFromCalc(calc);
  const focusTier = focusTierForSymbol(symbol, focus);
  const flowInRange = flow.mfi >= profile.mfiMin && flow.mfi <= profile.mfiMax;
  const volumeParticipating = flow.volRatio >= profile.volumeMin;
  const volumeControlled = flow.volRatio <= profile.volumeMax;
  const obvOk = profile.needsRisingObv ? flow.obv === "rising" : flow.obv !== "falling";
  const sectorIsFavored = focusTier === "Primary" || focusTier === "Secondary";
  const items = [
    ["Macro is not risk-off", macroSignal(macro).level !== "RED"],
    [`${profile.regimeLabel} macro backdrop`, profile.macroOk(macro)],
    [`Breadth supports ${profile.strategy} entries`, macro.breadth >= profile.breadthMin],
    [`VIX below ${profile.vixMax}`, macro.vix < profile.vixMax],
    ["Sector is primary or secondary focus", sectorIsFavored],
    ["Sector is not in avoided list", focusTier !== "Ignored"],
    [profile.rsLabel, inst.rs3m >= profile.rs3mMin],
    ["RS3M momentum positive", inst.rs3mMom > 0],
    ["RS3M trend rising", inst.rs3mTrend === "up"],
    [`MoneyFlow ${profile.mfiMin}–${profile.mfiMax}`, flowInRange],
    [profile.obvLabel, obvOk],
    [`Volume at least ${profile.volumeMin}% of normal`, volumeParticipating],
    ["Volume not a chase spike", volumeControlled],
    ["Price above MA21", tech.priceAboveMA21],
  ];
  const pass = items.filter(([, ok]) => ok).length;
  return {
    items,
    pass,
    total: items.length,
    verdict: pass === items.length ? "ENTER" : "WAIT",
    trigger: profile.trigger,
    setup: profile.setup,
    bestWhen: profile.bestWhen,
  };
}

function watchResult(items, trigger, setup, bestWhen, extras = {}) {
  const pass = items.filter(([, ok]) => ok).length;
  const total = items.length;
  return {
    items,
    pass,
    total,
    verdict: pass === total ? "ENTER" : "WAIT",
    score: total ? pass / total : 0,
    trigger,
    setup,
    bestWhen,
    ...extras,
  };
}

function sectorProxyLabel(sectorProxy) {
  const sector = SECTOR_BY_SYMBOL[sectorProxy];
  return sector ? `${sectorProxy} ${sector.name}` : "selected sector";
}

function sectorProxyRotationOk(sectorCalc, macro, focus, sectorProxy, preferredSectors) {
  if (!sectorProxy) return false;
  const tier = focusTierForSymbol(sectorProxy, focus);
  if (tier === "Ignored") return false;
  if (tier === "Primary" || tier === "Secondary") return true;
  if (preferredSectors.includes(sectorProxy)) return true;
  if (!sectorCalc) return false;
  return sectorRotationStatus(sectorCalc, tier, macro).score >= 5;
}

function cfmStockWatchChecklist(symbol, calc, sectorCalc, macro, focus, calcStatus, sectorProxy = "") {
  if (!calc) {
    return missingIndicatorChecklist(
      symbol,
      calcStatus,
      "Auto CFM candidate",
      "Best when macro favors defensive cashflow, the sector proxy confirms rotation, and the stock is holding support with controlled accumulation."
    );
  }
  const { inst, flow, tech } = bucketsFromCalc(calc);
  const macroLevel = macroSignal(macro).level;
  const proxy = sectorProxy || inferSectorProxy(symbol);
  const proxyTier = proxy ? focusTierForSymbol(proxy, focus) : "Neutral";
  const macroFavorsCFM = focus.favoredStrategy.includes("CFM") || focus.favoredStrategy.includes("Defense") || macro.growth === "slowing" || macro.fed === "hawkish";
  const proxyIsDefensive = DEFENSIVE_SECTORS.includes(proxy);
  const proxyOk = sectorProxyRotationOk(sectorCalc, macro, focus, proxy, DEFENSIVE_SECTORS);
  const items = [
    ["Macro is not risk-off", macroLevel !== "RED"],
    ["Macro favors CFM / defensive cashflow", macroFavorsCFM],
    ["Breadth stable enough for defensive entries", macro.breadth >= 45],
    ["VIX below panic level 30", macro.vix < 30],
    [proxy ? `${sectorProxyLabel(proxy)} is not avoided` : "Sector proxy selected", proxy ? proxyTier !== "Ignored" : false],
    [proxy ? `${sectorProxyLabel(proxy)} fits CFM or confirms rotation` : "Sector proxy confirms rotation", proxyOk || proxyIsDefensive],
    ["RS3M no worse than deep laggard", inst.rs3m > -25],
    ["RS3M momentum improving", inst.rs3mMom > 0 || inst.rs3mTrend === "up"],
    ["MoneyFlow 55–75 accumulation", flow.mfi >= 55 && flow.mfi <= 75],
    ["OBV not falling", flow.obv !== "falling"],
    ["Volume healthy 70–160%", flow.volRatio >= 70 && flow.volRatio <= 160],
    ["RSI constructive 45–70", flow.rsi >= 45 && flow.rsi <= 70],
    ["Price above MA21 / support reclaim", tech.priceAboveMA21],
  ];
  return watchResult(
    items,
    `${symbol} is a CFM candidate when macro favors cashflow/defense, ${proxy || "its sector proxy"} confirms, flow is controlled, and price holds above MA21/support.`,
    "Auto-detected CFM / cashflow support entry",
    "Best when the market rewards defensive cashflow and the stock shows accumulation without chase-volume risk.",
    { strategy: "CFM", confidence: Math.round((items.filter(([, ok]) => ok).length / items.length) * 100), sectorProxy: proxy }
  );
}

function appStockWatchChecklist(symbol, calc, sectorCalc, macro, focus, calcStatus, sectorProxy = "") {
  if (!calc) {
    return missingIndicatorChecklist(
      symbol,
      calcStatus,
      "Auto APP candidate",
      "Best when macro favors appreciation, the sector proxy leads, and the stock confirms with relative strength, volume expansion, and breakout behavior."
    );
  }
  const { inst, flow, tech } = bucketsFromCalc(calc);
  const macroLevel = macroSignal(macro).level;
  const proxy = sectorProxy || inferSectorProxy(symbol);
  const proxyTier = proxy ? focusTierForSymbol(proxy, focus) : "Neutral";
  const macroFavorsAPP = focus.favoredStrategy === "APP" || macro.fed === "dovish" || (macro.growth === "accelerating" && macro.breadth >= 55);
  const proxyIsApp = APP_SECTORS.includes(proxy);
  const proxyOk = sectorProxyRotationOk(sectorCalc, macro, focus, proxy, APP_SECTORS);
  const volumeBreakout = flow.volRatio >= 110 || flow.volAccel >= 110;
  const items = [
    ["Macro is not risk-off", macroLevel !== "RED"],
    ["Macro favors APP / risk-on growth", macroFavorsAPP],
    ["Breadth strong enough for APP entries", macro.breadth >= 55],
    ["VIX below 22", macro.vix < 22],
    [proxy ? `${sectorProxyLabel(proxy)} is not avoided` : "Sector proxy selected", proxy ? proxyTier !== "Ignored" : false],
    [proxy ? `${sectorProxyLabel(proxy)} fits APP or confirms rotation` : "Sector proxy confirms rotation", proxyOk || proxyIsApp],
    ["RS3M leadership positive", inst.rs3m > 0],
    ["RS3M momentum positive", inst.rs3mMom > 0],
    ["RS3M trend rising", inst.rs3mTrend === "up"],
    ["MoneyFlow 50–75, not exhausted", flow.mfi >= 50 && flow.mfi <= 75],
    ["OBV rising", flow.obv === "rising"],
    ["Volume expansion / breakout participation", volumeBreakout],
    ["RSI strength 50–70", flow.rsi >= 50 && flow.rsi <= 70],
    ["Price above MA21", tech.priceAboveMA21],
  ];
  return watchResult(
    items,
    `${symbol} is an APP candidate when macro supports growth, ${proxy || "its sector proxy"} leads, RS/OBV improve, and volume confirms a breakout attempt.`,
    "Auto-detected APP / appreciation breakout entry",
    "Best when risk-on leadership is active and the stock shows relative strength plus expanding participation.",
    { strategy: "APP", confidence: Math.round((items.filter(([, ok]) => ok).length / items.length) * 100), sectorProxy: proxy }
  );
}

function autoStrategyWatchChecklist(symbol, calc, sectorCalc, macro, focus, calcStatus, strategyMode = "AUTO", sectorProxy = "") {
  const cfm = cfmStockWatchChecklist(symbol, calc, sectorCalc, macro, focus, calcStatus, sectorProxy);
  const app = appStockWatchChecklist(symbol, calc, sectorCalc, macro, focus, calcStatus, sectorProxy);
  if (strategyMode === "CFM") return { ...cfm, strategy: "CFM", alternate: app, autoReason: "Manual CFM mode" };
  if (strategyMode === "APP") return { ...app, strategy: "APP", alternate: cfm, autoReason: "Manual APP mode" };
  if (!calc) return { ...cfm, strategy: "WAIT", alternate: app, autoReason: "Waiting for indicator data" };

  const delta = Math.abs(cfm.score - app.score);
  const selected = cfm.score >= app.score ? cfm : app;
  const alternate = selected === cfm ? app : cfm;
  const strategy = selected.score < 0.55 ? "WAIT" : delta < 0.08 ? "MIXED" : selected.strategy;
  const autoReason = strategy === "MIXED"
    ? `CFM ${Math.round(cfm.score * 100)}% vs APP ${Math.round(app.score * 100)}% — both need review.`
    : strategy === "WAIT"
      ? `Neither setup is strong yet: CFM ${Math.round(cfm.score * 100)}%, APP ${Math.round(app.score * 100)}%.`
      : `${strategy} is the better current fit: CFM ${Math.round(cfm.score * 100)}%, APP ${Math.round(app.score * 100)}%.`;
  return {
    ...selected,
    strategy,
    alternate,
    autoReason,
    confidence: Math.round(selected.score * 100),
    verdict: selected.verdict === "ENTER" && ["CFM", "APP"].includes(strategy) ? "ENTER" : "WAIT",
  };
}

function strategyCandidateRanking(symbols, strategy, computed, macro, focus, calcStatus) {
  const checklistFn = strategy === "CFM" ? cfmStockWatchChecklist : appStockWatchChecklist;
  return symbols.map((symbol) => {
    const proxy = inferSectorProxy(symbol);
    const data = checklistFn(symbol, computed?.[symbol], computed?.[proxy], macro, focus, calcStatus, proxy);
    const score = data.total ? data.pass / data.total : 0;
    const readiness = readinessFromChecklist(data);
    const { missing } = splitChecklistItems(data.items || []);
    return {
      symbol,
      strategy,
      proxy,
      data,
      score,
      readiness,
      nextBlocker: missing[0] || "Entry checklist complete",
    };
  }).sort((a, b) => (b.score - a.score) || (b.data.pass - a.data.pass) || a.symbol.localeCompare(b.symbol));
}

function CandidateLeaderboard({ title, strategy, candidates, watchedSymbols, onAdd }) {
  const color = STRATEGY_META[strategy]?.color || C.blue;
  const visible = candidates.slice(0, 6);
  return (
    <Panel title={title} eyebrow={`${strategy} ranked candidates`} accent={color}
      right={<span style={{ font: `800 11px ${C.mono}`, color }}>{visible.length ? `${Math.round(visible[0].score * 100)}% top` : "No data"}</span>}>
      <div style={{ display: "grid", gap: 8 }}>
        {visible.map((candidate, idx) => {
          const watched = watchedSymbols.has(candidate.symbol);
          return (
            <div key={candidate.symbol} style={{ display: "grid", gridTemplateColumns: "28px 1fr auto", gap: 9, alignItems: "center", padding: "9px 0", borderBottom: idx === visible.length - 1 ? "none" : `1px solid ${C.lineSoft}` }}>
              <div style={{ font: `800 12px ${C.mono}`, color: C.inkFaint }}>#{idx + 1}</div>
              <div>
                <div style={{ display: "flex", gap: 7, alignItems: "center", flexWrap: "wrap" }}>
                  <span style={{ font: `800 13px ${C.mono}`, color: C.ink }}>{candidate.symbol}</span>
                  <span style={{ font: `700 10px ${C.mono}`, color, border: `1px solid ${color}`, borderRadius: 999, padding: "2px 6px" }}>{candidate.data.pass}/{candidate.data.total}</span>
                  {candidate.proxy && <span style={{ font: `600 10px ${C.mono}`, color: C.inkFaint }}>Proxy {candidate.proxy}</span>}
                  <span style={{ font: `700 10px ${C.mono}`, color: candidate.readiness.color }}>{candidate.readiness.label}</span>
                </div>
                <div style={{ font: `400 11px/1.35 ${C.sans}`, color: C.inkDim, marginTop: 4 }}>{candidate.nextBlocker}</div>
              </div>
              <button onClick={() => onAdd(candidate)} disabled={watched} style={{ background: watched ? C.panel2 : color, color: watched ? C.inkFaint : "white", border: `1px solid ${watched ? C.line : color}`, borderRadius: 6, padding: "6px 9px", font: `700 11px ${C.sans}`, cursor: watched ? "default" : "pointer" }}>
                {watched ? "Watched" : "Watch"}
              </button>
            </div>
          );
        })}
        {!visible.length && <div style={{ font: `400 12px ${C.sans}`, color: C.inkDim }}>No candidate data yet. Refresh indicators to rank this strategy.</div>}
      </div>
    </Panel>
  );
}

function genericWatchChecklist(symbol, calc, macro, focus, calcStatus) {
  if (!calc) {
    return missingIndicatorChecklist(
      symbol,
      calcStatus,
      "Custom ticker monitor",
      "Add tickers you want monitored against the current macro and rotation framework."
    );
  }
  const { inst, flow, tech } = bucketsFromCalc(calc);
  const focusTier = focusTierForSymbol(symbol, focus);
  const items = [
    ["Macro is not risk-off", macroSignal(macro).level !== "RED"],
    ["Ticker is not in avoided sector list", focusTier !== "Ignored"],
    ["RS3M leadership positive", inst.rs3m > 0],
    ["RS3M momentum positive", inst.rs3mMom > 0],
    ["RS3M trend rising", inst.rs3mTrend === "up"],
    ["MoneyFlow 50–75", flow.mfi >= 50 && flow.mfi <= 75],
    ["OBV not falling", flow.obv !== "falling"],
    ["Volume at least 70% of normal", flow.volRatio >= 70],
    ["Volume not a chase spike", flow.volRatio <= 175],
    ["Price above MA21", tech.priceAboveMA21],
  ];
  const pass = items.filter(([, ok]) => ok).length;
  return {
    items,
    pass,
    total: items.length,
    verdict: pass === items.length ? "ENTER" : "WAIT",
    trigger: `${symbol} needs relative-strength leadership, constructive flow, controlled volume, and price above MA21 before entry.`,
    setup: "Standard ticker monitor",
    bestWhen: "Best when its indicators confirm the macro regime and money is rotating into the name.",
  };
}

function EntryWatchView({ app, macro, focus, computed, calcStatus, entryWatchSymbols, setEntryWatchSymbols }) {
  const [draftSymbol, setDraftSymbol] = useState("");
  const [draftStrategyMode, setDraftStrategyMode] = useState("AUTO");
  const [draftSectorProxy, setDraftSectorProxy] = useState("");
  const normalizedWatch = normalizeWatchItems(entryWatchSymbols || []);

  const candidates = normalizedWatch.map((watch) => {
    const { symbol, strategyMode, sectorProxy } = watch;
    const sectorProfile = SECTOR_WATCH_PROFILES[symbol];
    const effectiveProxy = sectorProxy || inferSectorProxy(symbol);
    if (sectorProfile && strategyMode === "AUTO") {
      const forcedMode = DEFENSIVE_SECTORS.includes(symbol) ? "CFM" : APP_SECTORS.includes(symbol) ? "APP" : "AUTO";
      const autoData = autoStrategyWatchChecklist(symbol, computed?.[symbol], computed?.[symbol], macro, focus, calcStatus, forcedMode, symbol);
      const data = { ...sectorWatchChecklist(symbol, computed?.[symbol], macro, focus, calcStatus), ...autoData };
      return {
        tag: data.strategy || sectorProfile.tag,
        name: sectorProfile.name,
        symbol,
        strategyMode,
        sectorProxy: symbol,
        color: STRATEGY_META[data.strategy]?.color || sectorProfile.color,
        data,
        setup: data.setup,
        trigger: data.trigger,
        bestWhen: data.bestWhen,
      };
    }

    const data = autoStrategyWatchChecklist(symbol, computed?.[symbol], computed?.[effectiveProxy], macro, focus, calcStatus, strategyMode, effectiveProxy);
    return {
      tag: data.strategy || strategyMode,
      name: STRATEGY_META[data.strategy]?.label || "Auto strategy",
      symbol,
      strategyMode,
      sectorProxy: effectiveProxy,
      color: STRATEGY_META[data.strategy]?.color || C.green,
      data,
      setup: data.setup,
      trigger: data.trigger,
      bestWhen: data.bestWhen,
    };
  }).sort((a, b) => (b.data.pass / b.data.total) - (a.data.pass / a.data.total));

  const watchedSymbolSet = new Set(normalizedWatch.map((item) => item.symbol));
  const cfmTopCandidates = strategyCandidateRanking(CFM_CANDIDATE_UNIVERSE, "CFM", computed, macro, focus, calcStatus);
  const appTopCandidates = strategyCandidateRanking(APP_CANDIDATE_UNIVERSE, "APP", computed, macro, focus, calcStatus);

  const addRankedCandidate = (candidate) => {
    setEntryWatchSymbols([
      ...normalizedWatch.filter((item) => item.symbol !== candidate.symbol),
      { symbol: candidate.symbol, strategyMode: candidate.strategy, sectorProxy: candidate.proxy },
    ]);
  };

  const addSymbol = (event) => {
    event.preventDefault();
    const symbol = normalizeWatchSymbol(draftSymbol);
    if (!symbol) return;
    const sectorProxy = normalizeWatchSymbol(draftSectorProxy) || inferSectorProxy(symbol);
    setEntryWatchSymbols([...normalizedWatch.filter((item) => item.symbol !== symbol), { symbol, strategyMode: draftStrategyMode, sectorProxy }]);
    setDraftSymbol("");
    setDraftStrategyMode("AUTO");
    setDraftSectorProxy("");
  };

  const removeSymbol = (symbol) => {
    setEntryWatchSymbols(normalizedWatch.filter((item) => item.symbol !== symbol));
  };

  const updateWatchItem = (symbol, patch) => {
    setEntryWatchSymbols(normalizedWatch.map((item) => (item.symbol === symbol ? normalizeWatchItem({ ...item, ...patch }) : item)));
  };

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Panel title="Entry watch list" eyebrow="Purpose · tell me what is close enough to watch" accent={SIG[focus.level]}
        right={<span style={{ font: `700 12px ${C.mono}`, color: SIG[focus.level] }}>{focus.level}</span>}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10 }}>
          <Stat label="Macro permission" value={focus.entryPermission} color={SIG[focus.level]} />
          <Stat label="Favored strategy" value={focus.favoredStrategy} color={focus.favoredStrategy === "APP" ? C.amber : focus.favoredStrategy === "CFM" ? C.blue : C.ink} />
          <Stat label="Tickers monitored" value={normalizedWatch.length} color={C.blue} />
        </div>
        <div style={{ marginTop: 12, font: `400 12px/1.45 ${C.sans}`, color: C.inkDim }}>
          Add tickers and let Auto score both CFM and APP. The app selects the better fit for the current macro, sector proxy, rotation, flow, volume, and MA21 setup; use Force CFM/APP only when you have a specific thesis.
        </div>
        <form onSubmit={addSymbol} style={{ display: "grid", gridTemplateColumns: "minmax(150px, 1fr) 150px 170px auto", gap: 8, marginTop: 14 }}>
          <input value={draftSymbol} onChange={(e) => setDraftSymbol(e.target.value)} placeholder="Add ticker (ex: MSFT)" style={{ ...inputStyle, textTransform: "uppercase" }} />
          <select value={draftStrategyMode} onChange={(e) => setDraftStrategyMode(e.target.value)} style={inputStyle}>
            <option value="AUTO">Auto strategy</option>
            <option value="CFM">Force CFM</option>
            <option value="APP">Force APP</option>
          </select>
          <select value={draftSectorProxy} onChange={(e) => setDraftSectorProxy(e.target.value)} style={inputStyle}>
            <option value="">Auto sector proxy</option>
            {SECTORS.map((sector) => <option key={sector.symbol} value={sector.symbol}>{sector.symbol} · {sector.name}</option>)}
          </select>
          <button type="submit" style={{ background: C.blue, color: "white", border: "none", borderRadius: 6, padding: "9px 14px", font: `700 12px ${C.sans}`, cursor: "pointer" }}>Add ticker</button>
        </form>
      </Panel>

      <Panel title="How accurate is this?" eyebrow="Scoring transparency" accent={C.yellow}>
        <div style={{ display: "grid", gap: 8, font: `400 12px/1.45 ${C.sans}`, color: C.inkDim }}>
          <div>These rankings are rule-based watch-list screens, not predictions. They use the same live indicator inputs as the rest of the dashboard: macro regime, sector proxy fit, RS3M/RS3M_MOM, OBV, MFI, RSI, volume participation, and MA21 status.</div>
          <div>Use the score to decide what deserves attention next; still confirm chart structure, news/earnings risk, liquidity, and actual execution levels before entering.</div>
        </div>
      </Panel>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))", gap: 16 }}>
        <CandidateLeaderboard title="Top CFM candidates" strategy="CFM" candidates={cfmTopCandidates} watchedSymbols={watchedSymbolSet} onAdd={addRankedCandidate} />
        <CandidateLeaderboard title="Top APP candidates" strategy="APP" candidates={appTopCandidates} watchedSymbols={watchedSymbolSet} onAdd={addRankedCandidate} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: 16 }}>
        {candidates.length ? candidates.map((candidate) => <EntryWatchCard key={candidate.symbol} {...candidate} onRemove={removeSymbol} onUpdate={updateWatchItem} />) : (
          <Panel title="No tickers monitored" eyebrow="Entry watch" accent={C.inkFaint}>
            <div style={{ font: `400 12px ${C.sans}`, color: C.inkDim }}>Add a ticker above to start monitoring entry readiness.</div>
          </Panel>
        )}
      </div>
    </div>
  );
}

function EntryWatchCard({ tag, name, symbol, color, data, setup, trigger, bestWhen, strategyMode, sectorProxy, onRemove, onUpdate }) {
  const go = data.verdict === "ENTER";
  const readiness = readinessFromChecklist(data);
  const { passed, missing } = splitChecklistItems(data.items);
  const nextBlockers = missing.slice(0, 4);
  const confirmations = passed.slice(0, 4);

  const [levels, setLevels] = useState(null);
  const [levelsState, setLevelsState] = useState("idle"); // idle | loading | done | empty | error
  const loadLevels = async () => {
    setLevelsState("loading");
    try {
      const result = await apiLevels(symbol);
      if (result.error) { setLevels(null); setLevelsState("empty"); }
      else { setLevels(result); setLevelsState("done"); }
    } catch {
      setLevels(null);
      setLevelsState("error");
    }
  };

  return (
    <Panel title={`${symbol} · ${name}`} eyebrow={`${tag} watch candidate`} accent={color}
      right={<div style={{ display: "flex", gap: 8, alignItems: "center" }}><span style={{ font: `700 12px ${C.mono}`, padding: "5px 10px", borderRadius: 6, background: readiness.tone, color: readiness.color }}>{readiness.label}</span><button onClick={() => onRemove(symbol)} title={`Remove ${symbol}`} style={{ background: C.panel2, border: `1px solid ${C.line}`, borderRadius: 6, color: C.inkDim, cursor: "pointer", padding: "4px 8px", font: `700 12px ${C.mono}` }}>×</button></div>}>
      <div style={{ display: "grid", gap: 14 }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: 12, alignItems: "center" }}>
          <div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 6 }}>
              <span style={{ font: `800 11px ${C.mono}`, color, border: `1px solid ${color}`, borderRadius: 999, padding: "3px 8px" }}>{STRATEGY_META[data.strategy]?.label || tag}</span>
              {data.confidence != null && <span style={{ font: `700 11px ${C.mono}`, color: C.inkDim }}>Confidence {data.confidence}%</span>}
              {sectorProxy && <span style={{ font: `600 11px ${C.mono}`, color: C.inkFaint }}>Proxy {sectorProxy}</span>}
            </div>
            <div style={{ font: `600 13px ${C.sans}`, color: C.ink }}>{setup}</div>
            <div style={{ font: `400 12px/1.4 ${C.sans}`, color: C.inkDim, marginTop: 4 }}>{bestWhen}</div>
            {data.autoReason && <div style={{ font: `600 11px/1.35 ${C.sans}`, color: C.inkDim, marginTop: 8 }}>{data.autoReason}</div>}
          </div>
          <div style={{ textAlign: "right" }}>
            <div style={{ font: `800 24px/1 ${C.mono}`, color: go ? C.green : color }}>{data.pass}/{data.total}</div>
            <div style={{ font: `600 10px ${C.mono}`, color: C.inkFaint, marginTop: 3 }}>READY</div>
          </div>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(145px, 1fr))", gap: 8 }}>
          <label style={{ display: "grid", gap: 4 }}>
            <span style={{ font: `700 10px ${C.mono}`, color: C.inkFaint, letterSpacing: 1 }}>STRATEGY MODE</span>
            <select value={strategyMode || "AUTO"} onChange={(e) => onUpdate(symbol, { strategyMode: e.target.value })} style={inputStyle}>
              <option value="AUTO">Auto detect</option>
              <option value="CFM">Force CFM</option>
              <option value="APP">Force APP</option>
            </select>
          </label>
          <label style={{ display: "grid", gap: 4 }}>
            <span style={{ font: `700 10px ${C.mono}`, color: C.inkFaint, letterSpacing: 1 }}>SECTOR PROXY</span>
            <select value={sectorProxy || ""} onChange={(e) => onUpdate(symbol, { sectorProxy: e.target.value })} style={inputStyle}>
              <option value="">Auto / none</option>
              {SECTORS.map((sector) => <option key={sector.symbol} value={sector.symbol}>{sector.symbol} · {sector.name}</option>)}
            </select>
          </label>
        </div>

        <div style={{ height: 7, background: C.panel2, borderRadius: 4, overflow: "hidden" }}>
          <div style={{ width: `${(data.pass / data.total) * 100}%`, height: "100%", background: go ? C.green : color, transition: "width .3s" }} />
        </div>

        <div style={{ background: go ? `${C.greenDim}55` : C.panel2, border: `1px solid ${go ? C.greenDim : C.lineSoft}`, borderRadius: 8, padding: "11px 12px" }}>
          <div style={{ font: `700 10px ${C.mono}`, color: go ? C.green : C.inkFaint, letterSpacing: 1, marginBottom: 5 }}>{go ? "ENTRY TRIGGER ACTIVE" : "ENTRY TRIGGER TO WATCH"}</div>
          <div style={{ font: `500 12px/1.45 ${C.sans}`, color: C.ink }}>{go ? "All conditions are met. Validate price/action live before placing the trade." : trigger}</div>
        </div>

        <div style={{ borderTop: `1px solid ${C.lineSoft}`, paddingTop: 10 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: levelsState === "idle" ? 0 : 8 }}>
            <span style={{ font: `700 10px ${C.mono}`, color: C.inkFaint, letterSpacing: 1 }}>SUPPORT / RESISTANCE</span>
            <button onClick={loadLevels} disabled={levelsState === "loading"} style={{ background: C.panel2, border: `1px solid ${C.line}`, borderRadius: 6, color: C.inkDim, cursor: levelsState === "loading" ? "default" : "pointer", padding: "4px 10px", font: `700 11px ${C.mono}`, opacity: levelsState === "loading" ? 0.6 : 1 }}>
              {levelsState === "loading" ? "Analyzing…" : levels ? "Refresh levels" : "Analyze levels"}
            </button>
          </div>
          {levelsState === "empty" && <div style={{ font: `400 12px ${C.sans}`, color: C.inkDim }}>No bar data yet for {symbol} — try again after the next ingest.</div>}
          {levelsState === "error" && <div style={{ font: `400 12px ${C.sans}`, color: C.red }}>Couldn’t compute levels. Try again.</div>}
          {levels && (levels.support?.length || levels.resistance?.length
            ? <LevelsBlock levels={levels} />
            : <div style={{ font: `400 12px ${C.sans}`, color: C.inkDim }}>No clear levels detected in recent history.</div>)}
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
          <WatchList title="Blocking entry" items={nextBlockers} empty="No blockers — entry checklist complete." color={go ? C.green : C.red} />
          <WatchList title="Already confirmed" items={confirmations} empty="No confirmations yet." color={C.green} />
        </div>

        {data.alternate && (
          <div style={{ font: `600 11px/1.4 ${C.sans}`, color: C.inkFaint, borderTop: `1px solid ${C.lineSoft}`, paddingTop: 10 }}>
            Alternate score: {data.alternate.strategy} {Math.round((data.alternate.score || 0) * 100)}% ({data.alternate.pass}/{data.alternate.total})
          </div>
        )}

        <details style={{ borderTop: `1px solid ${C.lineSoft}`, paddingTop: 10 }}>
          <summary style={{ cursor: "pointer", font: `700 11px ${C.mono}`, color: C.inkDim, letterSpacing: 1 }}>FULL RULE CHECK</summary>
          <div style={{ marginTop: 8 }}>
            {data.items.map((i) => <CheckRow key={i[0]} label={i[0]} ok={i[1]} />)}
          </div>
        </details>
      </div>
    </Panel>
  );
}

function LevelsBlock({ levels }) {
  const price = Number(levels.price) || 0;
  const dots = (s) => "●".repeat(Math.max(1, Math.min(3, Math.round((Number(s) || 0) / 2))));

  const LevelRow = ({ tag, zone, color }) => (
    <div style={{ display: "grid", gridTemplateColumns: "28px 1fr auto 34px", gap: 8, alignItems: "center", padding: "3px 0", font: `600 12px ${C.mono}` }}>
      <span style={{ color, font: `800 11px ${C.mono}` }}>{tag}</span>
      <span style={{ color: C.ink }}>${zone.center.toFixed(2)} <span style={{ color: C.inkFaint, font: `500 11px ${C.mono}` }}>({zone.low.toFixed(2)}–{zone.high.toFixed(2)})</span></span>
      <span style={{ color: zone.distancePct >= 0 ? C.red : C.green, textAlign: "right" }}>{zone.distancePct >= 0 ? "+" : ""}{zone.distancePct.toFixed(1)}%</span>
      <span style={{ color: C.inkFaint, textAlign: "right" }} title={`${zone.touches} swing touches · strength ${zone.strength}`}>{dots(zone.strength)}</span>
    </div>
  );

  const resistance = (levels.resistance || []);
  const support = (levels.support || []);

  return (
    <div style={{ display: "grid", gap: 1 }}>
      {/* Resistance: render farthest at top, nearest just above the price line */}
      {resistance.slice().reverse().map((z, i, arr) => (
        <LevelRow key={`r${i}`} tag={`R${arr.length - i}`} zone={z} color={C.red} />
      ))}
      <div style={{ display: "flex", justifyContent: "center", gap: 8, alignItems: "center", padding: "5px 0", borderTop: `1px dashed ${C.lineSoft}`, borderBottom: `1px dashed ${C.lineSoft}`, margin: "3px 0", font: `700 12px ${C.mono}`, color: C.blue }}>
        <span>● now ${price.toFixed(2)}</span>
        {levels.asOf && <span style={{ color: C.inkFaint, font: `500 10px ${C.mono}` }}>as of {levels.asOf}</span>}
      </div>
      {support.map((z, i) => (
        <LevelRow key={`s${i}`} tag={`S${i + 1}`} zone={z} color={C.green} />
      ))}
      {(levels.breakoutTrigger != null || levels.stop != null) && (
        <div style={{ font: `500 11px/1.5 ${C.sans}`, color: C.inkDim, marginTop: 7 }}>
          {levels.breakoutTrigger != null && <>Break trigger ≈ <b style={{ color: C.ink }}>${levels.breakoutTrigger.toFixed(2)}</b></>}
          {levels.breakoutTrigger != null && levels.stop != null && " · "}
          {levels.stop != null && <>Stop ≈ <b style={{ color: C.ink }}>${levels.stop.toFixed(2)}</b></>}
        </div>
      )}
    </div>
  );
}

function WatchList({ title, items, empty, color }) {
  return (
    <div>
      <div style={{ font: `700 10px ${C.mono}`, color: C.inkFaint, letterSpacing: 1, marginBottom: 7 }}>{title}</div>
      <div style={{ display: "grid", gap: 6 }}>
        {items.length ? items.map((item) => (
          <div key={item} style={{ display: "flex", gap: 7, alignItems: "flex-start", font: `400 12px/1.35 ${C.sans}`, color: C.inkDim }}>
            <span style={{ color, font: `700 11px ${C.mono}`, marginTop: 1 }}>•</span>
            <span>{item}</span>
          </div>
        )) : <div style={{ font: `400 12px ${C.sans}`, color }}>{empty}</div>}
      </div>
    </div>
  );
}


function cleanCurrency(value) {
  if (value === undefined || value === null) return 0;
  const raw = String(value).trim();
  if (!raw) return 0;
  const negative = /^\(.*\)$/.test(raw);
  const parsed = parseFloat(raw.replace(/[()$,]/g, ""));
  if (Number.isNaN(parsed)) return 0;
  return negative ? -Math.abs(parsed) : parsed;
}

function money(value) {
  const num = Number(value) || 0;
  const sign = num > 0 ? "+" : num < 0 ? "-" : "";
  return `${sign}$${Math.abs(num).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}


function normalizeOptionExpiry(expiry = "") {
  const parts = String(expiry).match(/^(\d{1,2})\/(\d{1,2})\/(\d{2}|\d{4})$/);
  if (!parts) return String(expiry || "");
  const year = parts[3].length === 2 ? `20${parts[3]}` : parts[3];
  return `${parts[1].padStart(2, "0")}/${parts[2].padStart(2, "0")}/${year}`;
}

function inferOptionSymbolFromDescription(symbol = "", description = "") {
  const base = String(symbol || "").trim().toUpperCase();
  const text = String(description || "").toUpperCase();
  if (!base || instrumentMetaFromSymbol(base).category === "Option") return base;
  const type = text.match(/\b(CALL|PUT)\b/);
  const expiry = text.match(/\bEXP(?:IR(?:ES|ATION)?)?\s*(\d{1,2}\/\d{1,2}\/\d{2,4})\b/);
  const strike = text.match(/(?:\$|STRIKE\s*)([0-9]+(?:\.[0-9]+)?)/);
  if (!type || !expiry || !strike) return base;
  return `${base} ${normalizeOptionExpiry(expiry[1])} ${Number(strike[1]).toFixed(2)} ${type[1][0]}`;
}

function isSchwabTransaction(row) {
  return String(row?.source || "").toLowerCase() === "schwab"
    || String(row?.strategy || "").toUpperCase() === SCHWAB_STRATEGY;
}

function schwabTransactionsOnly(rows = []) {
  return (rows || []).filter(isSchwabTransaction);
}

function transactionKey(row, fallback = "") {
  return [
    row.date || "", row.symbol || "", row.positionId || "", row.leg || "", row.action || "",
    row.qty || 0, row.price || 0, row.amount || 0, row.note || "", fallback,
  ].map((v) => String(v).trim().toUpperCase()).join("|");
}

function mergeTransactions(existing, incoming) {
  const existingSchwabRows = schwabTransactionsOnly(existing);
  const incomingSchwabRows = schwabTransactionsOnly(incoming);
  const seen = new Set(existingSchwabRows.map((row) => transactionKey(row)));
  const additions = [];
  incomingSchwabRows.forEach((row) => {
    const key = transactionKey(row);
    if (seen.has(key)) return;
    seen.add(key);
    additions.push({ ...row, id: row.id || `${Date.now()}-${additions.length}` });
  });
  return { rows: [...existingSchwabRows, ...additions], added: additions.length, skipped: incomingSchwabRows.length - additions.length, removedCsv: existing.length - existingSchwabRows.length };
}

function signedCostBasisDelta(value, side) {
  const amount = cleanCurrency(value);
  if (!amount) return 0;
  return side === "credit" ? -Math.abs(amount) : Math.abs(amount);
}

function inferTransactionLeg(action, legHint, qty) {
  const haystack = `${action} ${legHint}`;
  if (/SHORT|\bSTO\b|SELL TO OPEN|SOLD TO OPEN|\bBTC\b|BUY TO CLOSE|COVER/.test(haystack)) return "short";
  if (/LONG|\bBTO\b|BUY TO OPEN|BOUGHT TO OPEN|\bSTC\b|SELL TO CLOSE|SOLD TO CLOSE/.test(haystack)) return "long";
  if (qty < 0) return "short";
  return "long";
}

function inferTransactionAmount(action, legHint, qty, price) {
  const gross = Math.abs(qty * price);
  const haystack = `${action} ${legHint}`;
  if (!gross) return 0;
  if (/BUY|BOT|BOUGHT|DEBIT|COVER|\bBTC\b|BUY TO CLOSE|\bBTO\b|BUY TO OPEN/.test(haystack)) return -gross;
  if (/SELL|SOLD|CREDIT|SHORT|\bSTO\b|SELL TO OPEN|\bSTC\b|SELL TO CLOSE/.test(haystack)) return gross;
  return qty < 0 ? gross : -gross;
}

function signedTransactionQty(row) {
  const qty = Math.abs(cleanCurrency(row.qty));
  const haystack = `${row.action || ""} ${row.leg || ""} ${row.note || ""}`.toUpperCase();
  if (!qty) return 0;
  if (row.leg === "short") {
    if (/BUY TO CLOSE|BOUGHT TO CLOSE|\bBTC\b|COVER|CLOSE/.test(haystack)) return qty;
    return -qty;
  }
  if (/SELL TO CLOSE|SOLD TO CLOSE|\bSTC\b|\bSELL\b|\bSOLD\b|CLOSE/.test(haystack)) return -qty;
  return qty;
}

function inferOpenClose(action, legHint, leg = "") {
  const haystack = `${action || ""} ${legHint || ""}`.toUpperCase();
  if (/BUY TO CLOSE|BOUGHT TO CLOSE|SELL TO CLOSE|SOLD TO CLOSE|\bBTC\b|\bSTC\b|COVER|CLOSE/.test(haystack)) return "close";
  if (/BUY TO OPEN|BOUGHT TO OPEN|SELL TO OPEN|SOLD TO OPEN|\bBTO\b|\bSTO\b|OPEN/.test(haystack)) return "open";
  if (leg === "short") {
    if (/\bBUY\b|\bBOT\b|\bBOUGHT\b/.test(haystack)) return "close";
    if (/\bSELL\b|\bSOLD\b/.test(haystack)) return "open";
  } else {
    if (/\bSELL\b|\bSOLD\b/.test(haystack)) return "close";
    if (/\bBUY\b|\bBOT\b|\bBOUGHT\b/.test(haystack)) return "open";
  }
  return "activity";
}


function instrumentMetaFromSymbol(symbol = "") {
  const raw = String(symbol || "").trim();
  const normalized = raw.replace(/\s+/g, " ").toUpperCase();
  const optionMatch = normalized.match(/^([A-Z][A-Z0-9.]{0,5})\s+(\d{1,2}\/\d{1,2}\/\d{2,4})\s+([0-9]+(?:\.[0-9]+)?)\s*([CP])\b/);
  if (optionMatch) {
    return {
      underlying: optionMatch[1],
      category: "Option",
      label: normalized,
      symbol: normalized,
    };
  }
  const compactOptionMatch = normalized.match(/^([A-Z][A-Z0-9.]{0,5})\s*(\d{6})[CP]\d{8}$/);
  if (compactOptionMatch) {
    return {
      underlying: compactOptionMatch[1],
      category: "Option",
      label: normalized,
      symbol: normalized,
    };
  }
  const underlying = normalized.split(/[\s:-]+/)[0] || "—";
  return {
    underlying,
    category: "Stock",
    label: underlying,
    symbol: normalized || underlying,
  };
}

function getInstrumentMeta(item = {}) {
  const meta = instrumentMetaFromSymbol(item.symbol);
  return {
    ...meta,
    category: item.instrumentType || meta.category,
    label: item.instrumentLabel || meta.label,
    underlying: item.underlying || meta.underlying,
  };
}

function isLegLabel(value) {
  return /^(LONG|SHORT)$/i.test(String(value || "").trim());
}

function isSchwabRow(row = {}) {
  return isSchwabTransaction(row);
}

function positionKey(row) {
  const meta = getInstrumentMeta(row);
  const root = meta.category === "Option" ? `${meta.underlying} · ${meta.label}` : meta.underlying;
  if (isSchwabRow(row)) return `${root} · SCHWAB`;
  return row.positionId ? `${root} · ${row.positionId}` : `${root} · ${row.leg || "position"}`;
}

function schwabOpenHoldings(accounts = []) {
  const holdings = new Map();
  (accounts || []).forEach((acct) => {
    (acct?.positions || []).forEach((position) => {
      const qty = cleanCurrency(position.netQty ?? position.longQty ?? position.shortQty);
      const symbol = String(position.symbol || "").trim().toUpperCase();
      if (!symbol || Math.abs(qty) <= 0.000001) return;
      const marketValue = cleanCurrency(position.marketValue);
      const existing = holdings.get(symbol) || { qty: 0, marketValue: 0 };
      holdings.set(symbol, {
        qty: existing.qty + qty,
        marketValue: existing.marketValue + marketValue,
      });
    });
  });
  return holdings;
}

function schwabHoldingForPosition(group, holdings) {
  const candidates = (group.instrumentType === "Option"
    ? [group.rawSymbol, group.instrumentLabel]
    : [group.rawSymbol, group.symbol, group.underlying])
    .map((value) => String(value || "").trim().toUpperCase())
    .filter(Boolean);
  for (const symbol of candidates) {
    if (holdings.has(symbol)) return holdings.get(symbol);
  }
  return null;
}

function summarizeTransactions(rows, currentMarks = {}, schwabSnapshot = []) {
  const groups = new Map();
  const openSchwabHoldings = schwabOpenHoldings(schwabSnapshot);
  const openSchwabSymbols = new Set(openSchwabHoldings.keys());
  rows.forEach((row, idx) => {
    const key = positionKey(row);
    if (!groups.has(key)) {
      groups.set(key, {
        key,
        id: key,
        symbol: getInstrumentMeta(row).underlying || row.symbol || "—",
        underlying: getInstrumentMeta(row).underlying || row.symbol || "—",
        instrumentType: getInstrumentMeta(row).category,
        instrumentLabel: getInstrumentMeta(row).label,
        rawSymbol: row.symbol || "",
        positionId: row.positionId || "",
        strategy: row.strategy || SCHWAB_STRATEGY,
        sourceStrategies: new Set(),
        firstDate: row.date || "",
        lastDate: row.date || "",
        rows: [],
        long: { cash: 0, count: 0, netQty: 0, openCount: 0, closeCount: 0 },
        short: { cash: 0, count: 0, netQty: 0, openCount: 0, closeCount: 0 },
      });
    }
    const group = groups.get(key);
    const bucket = row.leg === "short" ? "short" : "long";
    const flowType = row.flowType && row.flowType !== "activity" ? row.flowType : inferOpenClose(row.action, row.leg, row.leg);
    const typed = { ...row, flowType, signedQty: signedTransactionQty(row), order: idx };
    group.rows.push(typed);
    group.sourceStrategies.add(String(row.strategy || SCHWAB_STRATEGY).toUpperCase());
    group.strategy = group.strategy === SCHWAB_STRATEGY && row.strategy ? row.strategy : group.strategy;
    group.firstDate = [group.firstDate, row.date].filter(Boolean).sort()[0] || group.firstDate;
    group.lastDate = [group.lastDate, row.date].filter(Boolean).sort().slice(-1)[0] || group.lastDate;
    group[bucket].cash += row.amount;
    group[bucket].count += 1;
    group[bucket].netQty += typed.signedQty;
    if (typed.flowType === "open") group[bucket].openCount += 1;
    if (typed.flowType === "close") group[bucket].closeCount += 1;
  });

  const positions = [...groups.values()].map((group) => {
    const marks = currentMarks[group.key] || {};
    const longCurrent = cleanCurrency(marks.long);
    const shortClose = cleanCurrency(marks.short);
    const markCurrent = cleanCurrency(marks.current);
    const hasCurrentMark = String(marks.current ?? "").trim() !== "";
    const cash = group.long.cash + group.short.cash;
    const netQty = group.long.netQty + group.short.netQty;
    const nettedClosed = Math.abs(group.long.netQty) < 0.000001 && Math.abs(group.short.netQty) < 0.000001 && group.rows.some((row) => row.flowType === "close");
    const schwabOnly = group.sourceStrategies.size === 1 && group.sourceStrategies.has("SCHWAB");
    const liveHolding = schwabHoldingForPosition(group, openSchwabHoldings);
    const hasSchwabCurrent = liveHolding?.marketValue != null;
    const schwabCurrent = hasSchwabCurrent ? liveHolding.marketValue : 0;
    const missingFromLiveSchwabBook = schwabOnly && openSchwabSymbols.size > 0 && !liveHolding;
    const isClosed = nettedClosed || missingFromLiveSchwabBook;
    const manualLegCurrent = longCurrent - shortClose;
    const netCurrent = hasCurrentMark ? markCurrent : hasSchwabCurrent ? schwabCurrent : manualLegCurrent;
    const currentSource = hasCurrentMark ? "manual" : hasSchwabCurrent ? "schwab" : "manual-legs";
    const estimated = cash + (isClosed ? 0 : netCurrent);
    return {
      ...group,
      sourceStrategies: [...group.sourceStrategies],
      isClosed,
      cash,
      netQty,
      longCurrent,
      shortClose,
      markCurrent,
      hasCurrentMark,
      schwabCurrent,
      hasSchwabCurrent,
      currentSource,
      current: isClosed ? 0 : netCurrent,
      estimated,
      rowCount: group.rows.length,
      opened: group.firstDate,
      closed: isClosed ? group.lastDate : "",
    };
  }).sort((a, b) => (a.isClosed === b.isClosed ? String(b.lastDate).localeCompare(String(a.lastDate)) : a.isClosed ? 1 : -1));

  const totals = positions.reduce((acc, position) => {
    const bucket = position.isClosed ? acc.closed : acc.open;
    bucket.count += 1;
    bucket.cash += position.cash;
    bucket.current += position.current;
    bucket.estimated += position.estimated;
    acc.long.cash += position.long.cash;
    acc.long.count += position.long.count;
    acc.short.cash += position.short.cash;
    acc.short.count += position.short.count;
    return acc;
  }, {
    open: { count: 0, cash: 0, current: 0, estimated: 0 },
    closed: { count: 0, cash: 0, current: 0, estimated: 0 },
    long: { cash: 0, count: 0 },
    short: { cash: 0, count: 0 },
  });
  totals.total = {
    count: positions.length,
    cash: totals.open.cash + totals.closed.cash,
    current: totals.open.current,
    estimated: totals.open.estimated + totals.closed.estimated,
  };
  return { positions, openPositions: positions.filter((p) => !p.isClosed), closedPositions: positions.filter((p) => p.isClosed), totals };
}


function makePositionBucket(label) {
  return {
    label,
    positions: [],
    cash: 0,
    estimated: 0,
    current: 0,
    opened: "",
    closed: "",
  };
}

function addPositionToBucket(bucket, position) {
  bucket.positions.push(position);
  bucket.cash += position.cash;
  bucket.estimated += position.estimated;
  bucket.current += position.current;
  bucket.opened = [bucket.opened, position.opened].filter(Boolean).sort()[0] || bucket.opened;
  bucket.closed = [bucket.closed, position.closed].filter(Boolean).sort().slice(-1)[0] || bucket.closed;
}

function groupPositionsByUnderlying(list) {
  const grouped = new Map();
  list.forEach((position) => {
    const meta = getInstrumentMeta(position);
    const symbol = meta.underlying || "—";
    const category = meta.category === "Option" ? "Option" : "Stock";
    if (!grouped.has(symbol)) {
      grouped.set(symbol, {
        symbol,
        positions: [],
        categories: new Map(),
        cash: 0,
        estimated: 0,
        current: 0,
        opened: "",
        closed: "",
      });
    }
    const group = grouped.get(symbol);
    if (!group.categories.has(category)) group.categories.set(category, makePositionBucket(category));
    addPositionToBucket(group, position);
    addPositionToBucket(group.categories.get(category), position);
  });
  return [...grouped.values()]
    .map((group) => ({
      ...group,
      categories: [...group.categories.values()].sort((a, b) => {
        if (a.label === b.label) return 0;
        if (a.label === "Stock") return -1;
        if (b.label === "Stock") return 1;
        return a.label.localeCompare(b.label);
      }),
    }))
    .sort((a, b) => a.symbol.localeCompare(b.symbol));
}

function toDashboardPosition(position) {
  const basis = Math.abs(position.cash);
  return {
    id: position.key,
    strategy: position.strategy || SCHWAB_STRATEGY,
    symbol: position.underlying || position.symbol,
    desc: position.positionId || position.instrumentLabel || `${position.rowCount} Schwab fills`,
    cost: basis.toFixed(2),
    current: (basis + position.estimated).toFixed(2),
    opened: position.opened,
  };
}

// ============================================================================
// POSITIONS VIEW
// ============================================================================
function SchwabAccountPanel({ snapshot }) {
  if (!snapshot || !snapshot.length) return null;
  return (
    <Panel title="Schwab account snapshot" eyebrow="Live holdings · last sync">
      <div style={{ display: "grid", gap: 14 }}>
        {snapshot.map((acct, idx) => (
          <div key={acct.account || idx} style={{ border: `1px solid ${C.line}`, borderRadius: 9, overflow: "hidden", background: C.panel2 }}>
            <div style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 10, padding: "11px 13px", borderBottom: `1px solid ${C.line}` }}>
              <span style={{ font: `800 13px ${C.mono}`, color: C.ink }}>{acct.account}{acct.type ? <span style={{ marginLeft: 8, font: `500 11px ${C.sans}`, color: C.inkDim }}>{acct.type}</span> : null}</span>
              <span style={{ display: "flex", gap: 16, font: `700 11px ${C.mono}` }}>
                {acct.liquidationValue != null && <span style={{ color: C.ink }}>Value {money(acct.liquidationValue)}</span>}
                {acct.cashBalance != null && <span style={{ color: C.inkDim }}>Cash {money(acct.cashBalance)}</span>}
              </span>
            </div>
            {acct.positions && acct.positions.length ? (
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 520 }}>
                  <thead><tr style={{ font: `600 10px ${C.mono}`, color: C.inkFaint, letterSpacing: 1, textTransform: "uppercase" }}>
                    {["Symbol", "Net qty", "Avg price", "Market value", "Open P/L"].map((h) => <th key={h} style={{ textAlign: "left", padding: "8px 10px", borderBottom: `1px solid ${C.line}` }}>{h}</th>)}
                  </tr></thead>
                  <tbody>
                    {acct.positions.map((p) => (
                      <tr key={p.symbol} style={{ font: `400 12px ${C.sans}` }}>
                        <td style={td}><b style={{ font: `700 12px ${C.mono}` }}>{p.symbol}</b></td>
                        <td style={td}>{p.netQty}</td>
                        <td style={td}>{p.averagePrice != null ? `$${p.averagePrice.toLocaleString()}` : "—"}</td>
                        <td style={{ ...td, font: `600 12px ${C.mono}` }}>{p.marketValue != null ? money(p.marketValue) : "—"}</td>
                        <td style={{ ...td, font: `700 12px ${C.mono}`, color: (p.openPL || 0) >= 0 ? C.green : C.red }}>{p.openPL != null ? money(p.openPL) : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div style={{ font: `400 12px ${C.sans}`, color: C.inkDim, padding: "10px 13px" }}>No open positions in this account.</div>
            )}
          </div>
        ))}
        <div style={{ font: `400 11px/1.45 ${C.sans}`, color: C.inkFaint }}>
          Read-only snapshot from your last sync. The Open/Closed tables below are built only from Schwab-synced fills, so they keep open/close history and estimated P&L the snapshot alone can't show.
        </div>
      </div>
    </Panel>
  );
}

function PositionsView({ positions, setPositions, guidance = {}, capital, reserve, deployed, openPL }) {
  const [transactions, setTransactions] = useState(() => schwabTransactionsOnly(store.get("positionTransactions", [])));
  const [positionMarks, setPositionMarks] = useState(store.get("positionMarks", {}));
  const [expandedPositions, setExpandedPositions] = useState({});
  const [collapsedSymbols, setCollapsedSymbols] = useState({});
  const [collapsedCategories, setCollapsedCategories] = useState({});
  const [importMessage, setImportMessage] = useState("");
  const [saveStatus, setSaveStatus] = useState("");
  const [schwabSnapshot, setSchwabSnapshot] = useState(store.get("schwabSnapshot", []));
  const [schwabStatus, setSchwabStatus] = useState("");
  const autoSyncStarted = useRef(false);

  useEffect(() => { store.set("positionTransactions", schwabTransactionsOnly(transactions)); }, [transactions]);
  useEffect(() => { store.set("positionMarks", positionMarks); }, [positionMarks]);

  const transactionSummary = useMemo(() => summarizeTransactions(transactions, positionMarks, schwabSnapshot), [transactions, positionMarks, schwabSnapshot]);

  useEffect(() => {
    setPositions(transactionSummary.openPositions.map(toDashboardPosition));
  }, [setPositions, transactionSummary.openPositions]);

  const updateMark = (key, field, value) => {
    setPositionMarks({ ...positionMarks, [key]: { ...(positionMarks[key] || {}), [field]: value } });
  };

  const savePositionState = async () => {
    setSaveStatus("saving");
    try {
      await Promise.all([
        store.set("positionTransactions", transactions, true),
        store.set("positionMarks", positionMarks, true),
        store.set("positions", transactionSummary.openPositions.map(toDashboardPosition), true),
      ]);
      await store.flush();
      setSaveStatus("saved");
    } catch (e) {
      setSaveStatus("error");
    }
  };

  const syncFromSchwab = async ({ auto = false } = {}) => {
    setSchwabStatus(auto ? "refreshing" : "syncing");
    try {
      const res = await apiAccountSync(365);
      if (!res.configured) {
        setSchwabStatus("error");
        setImportMessage(res.error || "Schwab account sync is unavailable — credentials not set.");
        return;
      }
      const incoming = res.transactions || [];
      setTransactions((existing) => {
        const merged = mergeTransactions(existing, incoming);
        const errCount = Object.keys(res.errors || {}).length;
        const verb = auto ? "Refreshed" : "Synced";
        setImportMessage(
          `${verb} current values and ${merged.added} new transaction${merged.added === 1 ? "" : "s"} from Schwab`
          + `${merged.skipped ? ` (${merged.skipped} already present)` : ""}`
          + `${merged.removedCsv ? ` — removed ${merged.removedCsv} CSV-imported row${merged.removedCsv === 1 ? "" : "s"}` : ""}`
          + `${errCount ? ` — ${errCount} source${errCount === 1 ? "" : "s"} errored, see below` : ""}`
          + `. Save to persist before leaving.`
        );
        return merged.rows;
      });
      const accounts = res.accounts || [];
      setSchwabSnapshot(accounts);
      store.set("schwabSnapshot", accounts, true).catch(() => {});
      setSchwabStatus(Object.keys(res.errors || {}).length ? "partial" : "done");
    } catch (e) {
      setSchwabStatus("error");
      setImportMessage(`Schwab sync failed: ${e.message}`);
    }
  };

  useEffect(() => {
    if (autoSyncStarted.current) return;
    autoSyncStarted.current = true;
    syncFromSchwab({ auto: true });
  }, []);

  const renderPositionRows = (list, closed = false) => list.map((p) => {
    const guide = guidance[p.key] || {};
    const expanded = !!expandedPositions[p.key];
    return (
      <React.Fragment key={p.key}>
        <tr style={{ font: `400 12px ${C.sans}` }}>
          <td style={td}><button onClick={() => setExpandedPositions({ ...expandedPositions, [p.key]: !expanded })} style={{ background: "none", border: "none", color: C.blue, cursor: "pointer", font: `700 13px ${C.mono}` }} title="Expand open/close fills">{expanded ? "−" : "+"}</button></td>
          <td style={td}><span style={{ font: `700 11px ${C.mono}`, color: p.strategy === "CFM" ? C.blue : C.amber }}>{p.strategy}</span></td>
          <td style={td}>
            <div style={{ display: "grid", gap: 3 }}>
              <span>{p.instrumentLabel || p.positionId || <span style={{ color: C.inkFaint }}>Ungrouped</span>}</span>
              {p.positionId && <span style={{ font: `500 10px ${C.sans}`, color: C.inkFaint }}>{p.positionId}</span>}
            </div>
          </td>
          <td style={td}>{p.long.netQty || "—"}</td>
          <td style={td}>{p.short.netQty || "—"}</td>
          <td style={{ ...td, color: p.cash >= 0 ? C.green : C.red, font: `600 12px ${C.mono}` }}>{money(p.cash)}</td>
          {!closed && <td style={td}>
            <input type="number" value={positionMarks[p.key]?.current || ""} onChange={(e) => updateMark(p.key, "current", e.target.value)} placeholder={p.hasSchwabCurrent ? money(p.schwabCurrent) : "Current net"} style={{ ...inputStyle, padding: "5px 7px", width: 120, font: `500 12px ${C.mono}` }} />
            <div style={{ font: `500 9px ${C.sans}`, color: C.inkFaint, marginTop: 4 }}>{p.hasSchwabCurrent && !p.hasCurrentMark ? "Auto from Schwab MV" : "Long MV − short close"}</div>
          </td>}
          {!closed && <td style={td}>
            <div style={{ display: "flex", gap: 6 }}>
              <input type="number" value={positionMarks[p.key]?.long || ""} onChange={(e) => updateMark(p.key, "long", e.target.value)} placeholder="Long MV" style={{ ...inputStyle, padding: "5px 7px", width: 82, font: `500 12px ${C.mono}` }} />
              <input type="number" value={positionMarks[p.key]?.short || ""} onChange={(e) => updateMark(p.key, "short", e.target.value)} placeholder="Short close" style={{ ...inputStyle, padding: "5px 7px", width: 92, font: `500 12px ${C.mono}` }} />
            </div>
            <div style={{ font: `500 9px ${C.sans}`, color: C.inkFaint, marginTop: 4 }}>Optional legs if no current value</div>
          </td>}
          {!closed && <td style={{ ...td, color: p.current >= 0 ? C.green : C.red, font: `700 12px ${C.mono}` }}>
            {money(p.current)}
            {p.currentSource === "schwab" && <div style={{ font: `500 9px ${C.sans}`, color: C.inkFaint, marginTop: 4 }}>Schwab MV</div>}
          </td>}
          <td style={{ ...td, color: p.estimated >= 0 ? C.green : C.red, font: `700 12px ${C.mono}` }}>{money(p.estimated)}</td>
          {!closed && <td style={{ ...td, color: guide.color || C.inkDim, font: `700 11px ${C.mono}` }} title={guide.note || ""}>{guide.action || "Monitor"}</td>}
          <td style={{ ...td, font: `400 11px ${C.mono}`, color: C.inkDim }}>{p.opened || "—"}</td>
          <td style={{ ...td, font: `400 11px ${C.mono}`, color: C.inkDim }}>{p.closed || "—"}</td>
        </tr>
        {expanded && <tr><td colSpan={closed ? 9 : 13} style={{ ...td, background: C.panel2, padding: 0 }}><PositionTransactionLog rows={p.rows} /></td></tr>}
      </React.Fragment>
    );
  });

  const renderPositionTable = (list, closed = false) => (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", minWidth: closed ? 760 : 1120 }}>
        <thead><tr style={{ font: `600 10px ${C.mono}`, color: C.inkFaint, letterSpacing: 1, textTransform: "uppercase" }}>
          {(closed
            ? ["", "Strat", "Position", "Long qty", "Short qty", "Cash flow", "P/L", "Opened", "Closed"]
            : ["", "Strat", "Position", "Long qty", "Short qty", "Cash flow", "Current value", "Optional leg marks", "Net current", "Est. P/L", "Guidance", "Opened", "Closed"]
          ).map((h) => <th key={h} style={{ textAlign: "left", padding: "8px 10px", borderBottom: `1px solid ${C.line}` }}>{h}</th>)}
        </tr></thead>
        <tbody>{renderPositionRows(list, closed)}</tbody>
      </table>
    </div>
  );

  const toggleSymbol = (status, symbol) => {
    const key = `${status}:${symbol}`;
    setCollapsedSymbols((prev) => ({ ...prev, [key]: !(prev[key] ?? true) }));
  };

  const toggleCategory = (status, symbol, category) => {
    const key = `${status}:${symbol}:${category}`;
    setCollapsedCategories((prev) => ({ ...prev, [key]: !(prev[key] ?? true) }));
  };

  const renderCategoryGroup = (status, symbol, category, closed = false) => {
    const key = `${status}:${symbol}:${category.label}`;
    const collapsed = collapsedCategories[key] ?? true;
    return (
      <div key={key} style={{ borderTop: `1px solid ${C.line}` }}>
        <button onClick={() => toggleCategory(status, symbol, category.label)} style={{ width: "100%", background: C.panel, border: 0, color: C.ink, cursor: "pointer", padding: "9px 13px 9px 36px", display: "grid", gridTemplateColumns: "auto 1fr auto", gap: 10, alignItems: "center", textAlign: "left" }}>
          <span style={{ color: C.blue, font: `800 12px ${C.mono}` }}>{collapsed ? "+" : "−"}</span>
          <span>
            <span style={{ font: `800 12px ${C.sans}`, color: C.ink }}>{category.label}</span>
            <span style={{ marginLeft: 10, font: `500 10px ${C.sans}`, color: C.inkDim }}>{category.positions.length} position{category.positions.length === 1 ? "" : "s"} · {category.opened || "—"}{closed && category.closed ? ` → ${category.closed}` : ""}</span>
          </span>
          <span style={{ display: "flex", gap: 14, font: `700 10px ${C.mono}` }}>
            <span style={{ color: category.cash >= 0 ? C.green : C.red }}>Cash {money(category.cash)}</span>
            <span style={{ color: category.estimated >= 0 ? C.green : C.red }}>{closed ? "P/L" : "Est."} {money(category.estimated)}</span>
          </span>
        </button>
        {!collapsed && renderPositionTable(category.positions, closed)}
      </div>
    );
  };

  const renderSymbolGroups = (list, status, closed = false) => (
    <div style={{ display: "grid", gap: 10 }}>
      {groupPositionsByUnderlying(list).map((group) => {
        const key = `${status}:${group.symbol}`;
        const collapsed = collapsedSymbols[key] ?? true;
        return (
          <div key={key} style={{ border: `1px solid ${C.line}`, borderRadius: 9, overflow: "hidden", background: C.panel2 }}>
            <button onClick={() => toggleSymbol(status, group.symbol)} style={{ width: "100%", background: "transparent", border: 0, color: C.ink, cursor: "pointer", padding: "11px 13px", display: "grid", gridTemplateColumns: "auto 1fr auto", gap: 12, alignItems: "center", textAlign: "left" }}>
              <span style={{ color: C.blue, font: `800 13px ${C.mono}` }}>{collapsed ? "+" : "−"}</span>
              <span>
                <span style={{ font: `800 14px ${C.mono}`, color: C.ink }}>{group.symbol}</span>
                <span style={{ marginLeft: 10, font: `500 11px ${C.sans}`, color: C.inkDim }}>{group.positions.length} position{group.positions.length === 1 ? "" : "s"} · {group.opened || "—"}{closed && group.closed ? ` → ${group.closed}` : ""}</span>
              </span>
              <span style={{ display: "flex", gap: 14, font: `700 11px ${C.mono}` }}>
                <span style={{ color: group.cash >= 0 ? C.green : C.red }}>Cash {money(group.cash)}</span>
                <span style={{ color: group.estimated >= 0 ? C.green : C.red }}>{closed ? "P/L" : "Est."} {money(group.estimated)}</span>
              </span>
            </button>
            {!collapsed && group.categories.map((category) => renderCategoryGroup(status, group.symbol, category, closed))}
          </div>
        );
      })}
    </div>
  );

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <SchwabAccountPanel snapshot={schwabSnapshot} />

      <Panel title="Sync transactions" eyebrow="Schwab-only ledger"
        right={<span style={{ font: `700 12px ${C.mono}`, color: transactionSummary.totals.total.estimated >= 0 ? C.green : C.red }}>Total {money(transactionSummary.totals.total.estimated)}</span>}>
        <div style={{ display: "grid", gap: 14 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px,1fr))", gap: 12, alignItems: "end" }}>
            <Field label="Sync from Schwab" hint="pulls the last year of fills">
              <button onClick={syncFromSchwab} disabled={schwabStatus === "syncing" || schwabStatus === "refreshing"} style={{ width: "100%", background: schwabStatus === "syncing" || schwabStatus === "refreshing" ? C.line : C.green, border: `1px solid ${schwabStatus === "syncing" || schwabStatus === "refreshing" ? C.line : C.green}`, borderRadius: 6, color: "white", font: `700 13px ${C.sans}`, padding: "9px 16px", cursor: schwabStatus === "syncing" || schwabStatus === "refreshing" ? "default" : "pointer", height: 38 }}>
                {schwabStatus === "syncing" ? "Syncing…" : schwabStatus === "refreshing" ? "Refreshing…" : "↻ Sync from Schwab"}
              </button>
            </Field>
            <button onClick={() => { setTransactions([]); setImportMessage("Schwab transaction history cleared. Save to persist this change."); }} style={{ background: C.line, border: `1px solid ${C.line}`, borderRadius: 6, color: C.ink, font: `600 13px ${C.sans}`, padding: "10px 16px", cursor: "pointer", height: 38 }}>Clear Schwab history</button>
            <button onClick={savePositionState} style={{ background: C.blue, border: `1px solid ${C.blue}`, borderRadius: 6, color: "white", font: `700 13px ${C.sans}`, padding: "10px 16px", cursor: "pointer", height: 38 }}>Save positions</button>
          </div>
          <div style={{ font: `400 11px/1.45 ${C.sans}`, color: C.inkDim }}>
            <b>Sync from Schwab</b> pulls trade fills straight from your linked account (needs the app's "Accounts and Trading" product enabled); it keeps only Schwab-sourced rows, refreshes live market values for open positions, removes legacy CSV-imported rows, and skips duplicate Schwab fills. Type an open position's current net value in the Current value column to update Net current and Estimated P/L; optional long/short leg marks are still available when you prefer to split the mark.
          </div>
          {(importMessage || saveStatus) && (
            <div style={{ font: `500 12px ${C.sans}`, color: saveStatus === "error" || schwabStatus === "error" ? C.red : schwabStatus === "partial" ? C.amber : saveStatus === "saved" || schwabStatus === "done" || importMessage.startsWith("Synced") || importMessage.startsWith("Refreshed") ? C.green : C.amber }}>
              {importMessage}{saveStatus === "saving" ? " Saving…" : saveStatus === "saved" ? " Saved to disk." : saveStatus === "error" ? " Save failed — backend state endpoint unavailable." : ""}
            </div>
          )}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px,1fr))", gap: 10 }}>
            <PLCard title="Open positions" count={transactionSummary.totals.open.count} cash={transactionSummary.totals.open.cash} current={transactionSummary.totals.open.current} estimated={transactionSummary.totals.open.estimated} currentLabel="Net current" />
            <PLCard title="Closed positions" count={transactionSummary.totals.closed.count} cash={transactionSummary.totals.closed.cash} current={0} estimated={transactionSummary.totals.closed.estimated} currentLabel="Current value" />
            <PLCard title="Entire book" count={transactionSummary.totals.total.count} cash={transactionSummary.totals.total.cash} current={transactionSummary.totals.total.current} estimated={transactionSummary.totals.total.estimated} currentLabel="Net current" />
          </div>
        </div>
      </Panel>

      <Panel title="Open positions" eyebrow="Schwab-built · current logs"
        right={<span style={{ font: `500 12px ${C.mono}`, color: transactionSummary.totals.open.estimated >= 0 ? C.green : C.red }}>Open est. {money(transactionSummary.totals.open.estimated)}</span>}>
        {transactionSummary.openPositions.length === 0 ? (
          <div style={{ font: `400 13px ${C.sans}`, color: C.inkDim, padding: "10px 0" }}>No open positions from Schwab-synced transactions.</div>
        ) : renderSymbolGroups(transactionSummary.openPositions, "open")}
      </Panel>

      <Panel title="Closed positions" eyebrow="Archive · expandable open/close detail"
        right={<span style={{ font: `500 12px ${C.mono}`, color: transactionSummary.totals.closed.estimated >= 0 ? C.green : C.red }}>Closed P/L {money(transactionSummary.totals.closed.estimated)}</span>}>
        {transactionSummary.closedPositions.length === 0 ? (
          <div style={{ font: `400 13px ${C.sans}`, color: C.inkDim, padding: "10px 0" }}>Closed positions will appear here automatically once every long and short leg in a Schwab-synced position nets to zero.</div>
        ) : renderSymbolGroups(transactionSummary.closedPositions, "closed", true)}
      </Panel>
    </div>
  );
}

function PositionTransactionLog({ rows }) {
  const sorted = [...rows].sort((a, b) => String(a.date).localeCompare(String(b.date)) || a.order - b.order);
  const sections = [
    { title: "Open fills", rows: sorted.filter((row) => row.flowType === "open"), color: C.green },
    { title: "Close fills", rows: sorted.filter((row) => row.flowType === "close"), color: C.red },
    { title: "Other activity", rows: sorted.filter((row) => !["open", "close"].includes(row.flowType)), color: C.inkDim },
  ].filter((section) => section.rows.length);

  const renderRows = (sectionRows) => (
    <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 620 }}>
      <thead><tr style={{ font: `600 10px ${C.mono}`, color: C.inkFaint, letterSpacing: 1, textTransform: "uppercase" }}>
        {["Date", "Leg", "Action", "Qty", "Net qty", "Price", "Cash flow", "Note"].map((h) => <th key={h} style={{ textAlign: "left", padding: "7px 9px", borderBottom: `1px solid ${C.line}` }}>{h}</th>)}
      </tr></thead>
      <tbody>{sectionRows.map((row) => (
        <tr key={row.id} style={{ font: `400 12px ${C.sans}` }}>
          <td style={td}>{row.date || "—"}</td>
          <td style={td}><span style={{ color: row.leg === "short" ? C.amber : C.blue, font: `700 11px ${C.mono}` }}>{row.leg}</span></td>
          <td style={td}>{row.action}</td>
          <td style={td}>{row.qty || "—"}</td>
          <td style={td}>{row.signedQty || "—"}</td>
          <td style={td}>{row.price ? `$${row.price.toLocaleString()}` : "—"}</td>
          <td style={{ ...td, color: row.amount >= 0 ? C.green : C.red, font: `600 12px ${C.mono}` }}>{money(row.amount)}</td>
          <td style={{ ...td, color: C.inkDim }}>{row.note || "—"}</td>
        </tr>
      ))}</tbody>
    </table>
  );

  return (
    <div style={{ padding: "12px", display: "grid", gap: 10 }}>
      <div style={{ font: `700 11px ${C.sans}`, color: C.ink }}>Open / close transaction detail</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 10 }}>
        {sections.map((section) => (
          <div key={section.title} style={{ border: `1px solid ${C.line}`, borderRadius: 8, overflow: "hidden", background: C.panel }}>
            <div style={{ padding: "8px 10px", borderBottom: `1px solid ${C.line}`, display: "flex", justifyContent: "space-between", gap: 10 }}>
              <span style={{ font: `800 11px ${C.sans}`, color: section.color }}>{section.title}</span>
              <span style={{ font: `600 10px ${C.mono}`, color: C.inkFaint }}>{section.rows.length} fill{section.rows.length === 1 ? "" : "s"}</span>
            </div>
            <div style={{ overflowX: "auto" }}>{renderRows(section.rows)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function PLCard({ title, count, cash, current, estimated, currentLabel }) {
  return (
    <div style={{ background: C.panel2, border: `1px solid ${C.line}`, borderRadius: 8, padding: "12px 14px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 10, marginBottom: 8 }}>
        <div style={{ font: `700 12px ${C.sans}`, color: C.ink }}>{title}</div>
        <div style={{ font: `500 10px ${C.mono}`, color: C.inkFaint }}>{count} rows</div>
      </div>
      <div style={{ display: "grid", gap: 5, font: `500 11px ${C.mono}` }}>
        <div style={{ display: "flex", justifyContent: "space-between", color: C.inkDim }}><span>Closed cash flow</span><span style={{ color: cash >= 0 ? C.green : C.red }}>{money(cash)}</span></div>
        <div style={{ display: "flex", justifyContent: "space-between", color: C.inkDim }}><span>{currentLabel}</span><span style={{ color: current >= 0 ? C.green : C.red }}>{money(current)}</span></div>
        <div style={{ height: 1, background: C.lineSoft, margin: "3px 0" }} />
        <div style={{ display: "flex", justifyContent: "space-between", color: C.ink }}><span>Estimated P/L</span><span style={{ color: estimated >= 0 ? C.green : C.red, fontWeight: 700 }}>{money(estimated)}</span></div>
      </div>
    </div>
  );
}
const td = { padding: "10px", borderBottom: `1px solid ${C.lineSoft}`, color: C.ink };

// ============================================================================
// INDICATORS VIEW — manual inputs for thinkorswim studies + macro
// ============================================================================
function IndicatorsView(props) {
  const { macro, setMacroField, acceptAutoMacro, macroComputed, macroStatus, computed, calcStatus, onRefresh, dataIssues } = props;
  const cx = computed?.XLV, ci = computed?.AAPL;
  const fed = macroComputed?.fields?.fed;
  const fieldMeta = (key) => macroComputed?.fields?.[key] || null;
  const overrideBadge = (key) => {
    const f = fieldMeta(key);
    if (!f?.override) return null;
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 6, marginLeft: 6 }}>
        <span style={{ font: `600 9px ${C.mono}`, color: C.amber, border: `1px solid ${C.amber}55`, borderRadius: 4, padding: "1px 5px" }}
          title={`Manual override since ${f.asOf || "?"} — beats ingested data`}>
          MANUAL {String(f.asOf || "").slice(0, 10)}
        </span>
        <button onClick={() => acceptAutoMacro(key)} title="Clear override, back to computed value" style={{
          background: "none", border: `1px solid ${C.line}`, borderRadius: 4, cursor: "pointer",
          font: `500 9px ${C.mono}`, color: C.inkDim, padding: "1px 5px",
        }}>auto ↻</button>
      </span>
    );
  };
  const staleFor = (key) => {
    const f = fieldMeta(key);
    return f ? <StaleDot state={f.staleness || "unknown"} asOf={f.asOf} source={f.source} showDate /> : <StaleDot state="missing" />;
  };
  const fedConditions = fed
    ? [...(fed.hawkishConditions || []), ...(fed.dovishConditions || [])].slice(0, 3).join(" · ")
    : "";
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
            Computed during scheduled ingestion from stored daily bars (Schwab primary, Yahoo fallback) and FRED history: Level 1 macro, conditions-based Fed policy, RS3M, RS3M_MOM, volume ratio, volume acceleration, RSI, OBV trend, MFI, and MA21.
            Each shows next to your manual field — tap <b style={{ color: C.blue }}>use</b> to apply. Formulas: FORMULAS.md.
            {calcStatus === "fail" && " No ingested data yet — run ingestion or keep entering manually."}
          </div>
        </div>
        <button onClick={onRefresh} style={{
          background: C.blue, border: "none", borderRadius: 6, color: "#fff",
          font: `600 12px ${C.sans}`, padding: "9px 14px", cursor: "pointer", whiteSpace: "nowrap",
        }}>{calcStatus === "loading" ? "Fetching…" : "↻ Recalculate"}</button>
      </div>

      {(cx || ci) && (
        <div style={{ font: `400 10px ${C.mono}`, color: C.amber, padding: "0 2px" }}>
          Schwab-aligned auto values use 63-bar RS3M, Wilder RSI, SimpleMovingAvg(21), and stored daily OHLCV (see FORMULAS.md). If a custom thinkorswim study diverges, type the TOS value into the field; it is stored as a manual override. As of: {cx?.asOf || ci?.asOf || "—"}.
        </div>
      )}

      <DataIssuesPanel issues={dataIssues} />

      <Panel title="Macro inputs" eyebrow={`Level 1 · ${macroStatus === "ok" ? "auto-filled" : macroStatus === "partial" ? "partial auto-fill" : macroStatus === "loading" ? "fetching macro" : "manual fallback"}`}>
        <div style={{ font: `400 11px/1.45 ${C.sans}`, color: C.inkDim, marginBottom: 12 }}>
          Auto-fill uses ingested ^VIX bars, FRED Fed funds/CPI/GDP/unemployment data, and ETF breadth above 50-day MA. Fed policy is scored from current inflation, growth, labor, and rate conditions.
          Editing a field stores a <b style={{ color: C.amber }}>manual override</b> that beats ingested values until you tap <b>auto ↻</b>.
          {macroComputed?.asOf && <span style={{ color: C.inkFaint }}> Updated {macroComputed.asOf}</span>}
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px,1fr))", gap: 14 }}>
          <Field label={<span>VIX {staleFor("vix")}{overrideBadge("vix")}</span>} hint={fieldMeta("vix")?.override ? "manual" : fieldMeta("vix")?.asOf || "auto"}>
            <NumIn value={macro.vix} onChange={(v) => setMacroField("vix", v)} />
            <div style={{ marginTop: 5 }}><CalcChip value={fieldMeta("vix")?.override ? null : fieldMeta("vix")?.value} onApply={() => setMacroField("vix", fieldMeta("vix").value)} /></div>
          </Field>
          <Field label={<span>Breadth % {staleFor("breadth")}{overrideBadge("breadth")}</span>} hint={macroComputed?.fields?.breadth?.above != null ? `${macroComputed.fields.breadth.above}/${macroComputed.fields.breadth.total} above MA50` : ">55 CFM / >60 APP"}>
            <NumIn step="1" value={macro.breadth} onChange={(v) => setMacroField("breadth", v)} />
            <div style={{ marginTop: 5 }}><CalcChip value={fieldMeta("breadth")?.override ? null : fieldMeta("breadth")?.value} fmt={(v) => v.toFixed(0)} onApply={() => setMacroField("breadth", fieldMeta("breadth").value)} /></div>
          </Field>
          <Field label={<span>Fed policy {staleFor("fed")}{overrideBadge("fed")}</span>} hint={fed && fed.score != null ? `score ${fed.score} · ${fed.rate}% funds · CPI ${fed.cpiYoY}% · U-3 ${fed.unemployment}%` : "FRED DFF/CPI/GDP/UNRATE"}>
            <Sel value={macro.fed} onChange={(v) => setMacroField("fed", v)} options={[["dovish", "Dovish"], ["holding", "Holding"], ["hawkish", "Hawkish"]]} />
            <div style={{ marginTop: 5 }}><CalcChip value={fed?.override ? null : fed?.value} onApply={() => setMacroField("fed", fed.value)} /></div>
            {fedConditions && (
              <div style={{ font: `400 10px/1.35 ${C.sans}`, color: C.inkFaint, marginTop: 6 }}>
                Current conditions: {fedConditions}
              </div>
            )}
          </Field>
          <Field label={<span>Growth {staleFor("growth")}{overrideBadge("growth")}</span>} hint={macroComputed?.fields?.growth?.qoqAnnualized != null ? `${macroComputed.fields.growth.qoqAnnualized}% GDP` : "FRED GDP"}>
            <Sel value={macro.growth} onChange={(v) => setMacroField("growth", v)} options={[["accelerating", "Accelerating"], ["stable", "Stable"], ["slowing", "Slowing"]]} />
            <div style={{ marginTop: 5 }}><CalcChip value={fieldMeta("growth")?.override ? null : fieldMeta("growth")?.value} onApply={() => setMacroField("growth", fieldMeta("growth").value)} /></div>
          </Field>
          <Field label={<span>Inflation % {staleFor("inflation")}{overrideBadge("inflation")}</span>} hint={fieldMeta("inflation")?.override ? "manual" : fieldMeta("inflation")?.asOf || "FRED CPI YoY"}>
            <NumIn step="0.1" value={macro.inflation} onChange={(v) => setMacroField("inflation", v)} />
            <div style={{ marginTop: 5 }}><CalcChip value={fieldMeta("inflation")?.override ? null : fieldMeta("inflation")?.value} fmt={(v) => v.toFixed(1)} onApply={() => setMacroField("inflation", fieldMeta("inflation").value)} /></div>
          </Field>
        </div>
        {macroComputed?.errors && Object.keys(macroComputed.errors).length > 0 && (
          <div style={{ font: `400 10px ${C.mono}`, color: C.amber, marginTop: 10 }}>
            Partial macro data: {Object.entries(macroComputed.errors).map(([k, v]) => `${k}: ${v}`).join(" · ")}
          </div>
        )}
      </Panel>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(340px,1fr))", gap: 16 }}>
        <InstrumentInputs label="XLV — CFM candidate" color={C.blue} calc={cx}
          inst={props.instXLV} setInst={props.setInstXLV} flow={props.flowXLV} setFlow={props.setFlowXLV} tech={props.techXLV} setTech={props.setTechXLV} />
        <InstrumentInputs label="AAPL — APP candidate" color={C.amber} calc={ci}
          inst={props.instAAPL} setInst={props.setInstAAPL} flow={props.flowAAPL} setFlow={props.setFlowAAPL} tech={props.techAAPL} setTech={props.setTechAAPL} />
      </div>
    </div>
  );
}

// Small data-issues panel: quarantined rows, provider auth problems, last run.
function DataIssuesPanel({ issues }) {
  const quarantine = issues?.quarantine || [];
  const auth = issues?.schwabAuthError;
  const token = issues?.schwabToken;
  const run = issues?.lastRun;
  // Token nearing expiry (or aged out) is a warning even before a fetch fails.
  const tokenWarn = !auth && token?.present && (token.status === "warning" || token.status === "expired");
  const ok = quarantine.length === 0 && !auth && !tokenWarn;
  const runLine = run
    ? `Last ingest: ${run.started_at || "—"} · ${run.status || "—"} (${run.trigger || "?"})`
    : "No ingestion run recorded yet.";
  const reauthBtn = (
    <a href="/auth/schwab" style={{
      background: C.blue, borderRadius: 5, color: "#fff", textDecoration: "none",
      font: `600 11px ${C.sans}`, padding: "5px 10px", whiteSpace: "nowrap",
    }}>Re-authorize Schwab →</a>
  );
  return (
    <Panel title="Data issues" eyebrow="validation · quarantine · providers"
      accent={ok ? C.greenDim : C.red}
      right={<span style={{ font: `700 11px ${C.mono}`, color: ok ? C.green : C.red }}>{ok ? "CLEAN" : `${quarantine.length + (auth ? 1 : 0) + (tokenWarn ? 1 : 0)} OPEN`}</span>}>
      <div style={{ font: `400 11px ${C.mono}`, color: C.inkFaint, marginBottom: 8 }}>{runLine}</div>
      {auth && (
        <div style={{ display: "flex", gap: 8, alignItems: "center", background: C.redDim + "33", border: `1px solid ${C.redDim}`, borderRadius: 6, padding: "8px 10px", marginBottom: 6 }}>
          <span style={{ color: C.red }}>⚠</span>
          <span style={{ font: `500 12px ${C.sans}`, color: C.ink, flex: 1 }}>
            Schwab auth failed ({auth.at}) — refresh token likely expired; ingestion is falling back to Yahoo.
          </span>
          {reauthBtn}
        </div>
      )}
      {tokenWarn && (
        <div style={{ display: "flex", gap: 8, alignItems: "center", background: C.amber + "22", border: `1px solid ${C.amber}66`, borderRadius: 6, padding: "8px 10px", marginBottom: 6 }}>
          <span style={{ color: C.amber }}>⚠</span>
          <span style={{ font: `500 12px ${C.sans}`, color: C.ink, flex: 1 }}>
            {token.status === "expired"
              ? "Schwab refresh token has expired — ingestion is falling back to Yahoo until you re-authorize."
              : `Schwab refresh token expires in ${Math.max(0, Math.floor(token.daysLeft ?? 0) )}d ${Math.max(0, Math.round((((token.daysLeft ?? 0) % 1) * 24)))}h — re-authorize before it lapses.`}
          </span>
          {reauthBtn}
        </div>
      )}
      {quarantine.length > 0 ? (
        <div style={{ display: "grid", gap: 4 }}>
          {quarantine.slice(0, 8).map((q) => (
            <div key={q.id} style={{ display: "flex", gap: 8, alignItems: "baseline", padding: "5px 0", borderBottom: `1px solid ${C.lineSoft}` }}>
              <span style={{ font: `700 11px ${C.mono}`, color: C.amber, minWidth: 80 }}>{q.kind}{q.symbol ? ` ${q.symbol}` : ""}</span>
              <span style={{ font: `400 11px ${C.sans}`, color: C.inkDim, flex: 1 }}>{q.reason}</span>
              <span style={{ font: `400 10px ${C.mono}`, color: C.inkFaint }}>{String(q.created_at || "").slice(0, 10)}</span>
            </div>
          ))}
          {quarantine.length > 8 && (
            <div style={{ font: `400 10px ${C.mono}`, color: C.inkFaint }}>…and {quarantine.length - 8} more</div>
          )}
        </div>
      ) : !auth && (
        <div style={{ font: `400 12px ${C.sans}`, color: C.inkDim }}>No quarantined data. The last good value is always served; bad rows never overwrite it.</div>
      )}
    </Panel>
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
        <Field label="Volume ratio %" hint="today / prior 20d avg">
          <NumIn step="1" value={flow.volRatio} onChange={(v) => setFlow({ ...flow, volRatio: v })} />
          <div style={{ marginTop: 5 }}><CalcChip value={c.volRatio} fmt={(v) => v.toFixed(0)} onApply={() => setFlow({ ...flow, volRatio: +c.volRatio.toFixed(0) })} /></div>
        </Field>
        <Field label="Volume accel %" hint="today / 5d avg">
          <NumIn step="1" value={flow.volAccel ?? 0} onChange={(v) => setFlow({ ...flow, volAccel: v })} />
          <div style={{ marginTop: 5 }}><CalcChip value={c.volAccel} fmt={(v) => v.toFixed(0)} onApply={() => setFlow({ ...flow, volAccel: +c.volAccel.toFixed(0) })} /></div>
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
