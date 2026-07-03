// Thin fetch wrapper for the CFM API. Returns parsed JSON or throws.
const BASE = "";

async function request(path, opts = {}) {
  const res = await fetch(BASE + path, {
    credentials: "same-origin", // send/receive the session cookie
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { error: text };
  }
  // The session expired or is missing — let the app swap in the login screen
  // instead of surfacing the error inside whichever tab made the call.
  if (res.status === 401 && data.auth_required && path !== "/api/login") {
    window.dispatchEvent(new CustomEvent("auth-required"));
  }
  if (!res.ok || data.error) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

export const api = {
  regime: () => request("/api/regime"),
  sectors: () => request("/api/sectors"),
  stockFilter: (sector) => request(`/api/stock-filter${sector ? `?sector=${sector}` : ""}`),
  scorecard: (tickers) => request(`/api/scan/scorecard${tickers ? `?tickers=${tickers}` : ""}`),
  scanReady: (tickers) => request(`/api/scan/ready${tickers ? `?tickers=${tickers}` : ""}`),
  entryGate: (ticker) => request(`/api/entry-gate?ticker=${ticker}`),
  accountGate: (ticker, params = {}) => {
    const q = Object.entries(params)
      .filter(([, v]) => v !== null && v !== undefined && v !== "")
      .map(([k, v]) => `&${k}=${encodeURIComponent(v)}`)
      .join("");
    return request(`/api/account-gate?ticker=${ticker}${q}`);
  },
  rollSuggestion: (ticker) => request(`/api/roll-suggestion?ticker=${ticker}`),
  defend: (ticker) => request(`/api/defend?ticker=${ticker}`),
  leapRollEstimate: (ticker) => request(`/api/leap-roll-estimate?ticker=${ticker}`),
  universeHealth: (weeklies = false) => request(`/api/universe-health${weeklies ? "?weeklies=1" : ""}`),
  universe: () => request("/api/universe"),
  universeAdd: (ticker, sector) =>
    request("/api/universe/add", { method: "POST", body: JSON.stringify({ ticker, sector }) }),
  universeRemove: (ticker) =>
    request("/api/universe/remove", { method: "POST", body: JSON.stringify({ ticker }) }),
  universeRemoveBulk: (tickers) =>
    request("/api/universe/remove", { method: "POST", body: JSON.stringify({ tickers }) }),
  universeVet: (symbols) =>
    request("/api/universe/vet", { method: "POST", body: JSON.stringify({ symbols }) }),
  universeSync: () => request("/api/universe/sync", { method: "POST" }),
  strikePosture: () => request("/api/strike-posture"),
  setStrikePosture: (posture) =>
    request("/api/strike-posture", { method: "POST", body: JSON.stringify({ posture }) }),
  rollOptions: (ticker) => request(`/api/roll-options?ticker=${ticker}`),
  coverage: (ticker) => request(`/api/coverage?ticker=${ticker}`),
  earnings: (ticker, refresh = false) => request(`/api/earnings?ticker=${ticker}${refresh ? "&refresh=1" : ""}`),
  optionChain: (ticker, strategy = "atr") => request(`/api/option-chain/${ticker}?strategy=${strategy}`),
  execute: (payload) => request("/api/execute", { method: "POST", body: JSON.stringify(payload) }),
  // Live order lifecycle (used when an order comes back "working"; paper fills immediately).
  orderStatus: (orderId) => request(`/api/order-status?order_id=${encodeURIComponent(orderId)}`),
  cancelOrder: (orderId) => request("/api/order-cancel", { method: "POST", body: JSON.stringify({ order_id: orderId }) }),
  positions: () => request("/api/positions"),
  thetaLedger: (params = "") => request(`/api/theta-ledger${params}`),
  killSwitch: () => request("/api/kill-switch"),
  dailyChecklist: () => request("/api/daily-checklist"),
  history: () => request("/api/history"),
  portfolioRisk: () => request("/api/portfolio-risk"),
  dataHealth: () => request("/api/data-health"),
  maintenanceRefresh: () => request("/api/maintenance/refresh", { method: "POST" }),
  alerts: () => request("/api/alerts"),
  runAlerts: (dryRun) =>
    request("/api/alerts/run", {
      method: "POST",
      body: JSON.stringify(dryRun === undefined ? {} : { dry_run: dryRun }),
    }),
  ackAlert: (id) => request("/api/alerts/ack", { method: "POST", body: JSON.stringify({ id }) }),
  alertSettings: (patch) =>
    request("/api/alerts/settings", { method: "POST", body: JSON.stringify(patch) }),
  config: () => request("/api/config"),
  mode: () => request("/api/mode"),
  setMode: (demo) => request("/api/mode", { method: "POST", body: JSON.stringify({ demo }) }),
  state: () => request("/api/state"),
  saveState: (payload) => request("/api/state", { method: "POST", body: JSON.stringify(payload) }),
  accountStatus: () => request("/api/account/status"),
  schwabAuth: () => request("/auth/schwab"),
  authStatus: () => request("/api/auth/status"),
  login: (password) => request("/api/login", { method: "POST", body: JSON.stringify({ password }) }),
  logout: () => request("/api/logout", { method: "POST" }),
};
