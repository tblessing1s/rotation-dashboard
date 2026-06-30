// Thin fetch wrapper for the CFM API. Returns parsed JSON or throws.
const BASE = "";

async function request(path, opts = {}) {
  const res = await fetch(BASE + path, {
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
  if (!res.ok || data.error) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

export const api = {
  regime: () => request("/api/regime"),
  sectors: () => request("/api/sectors"),
  stockFilter: (sector) => request(`/api/stock-filter${sector ? `?sector=${sector}` : ""}`),
  entryGate: (ticker) => request(`/api/entry-gate?ticker=${ticker}`),
  rollSuggestion: (ticker) => request(`/api/roll-suggestion?ticker=${ticker}`),
  rollOptions: (ticker) => request(`/api/roll-options?ticker=${ticker}`),
  coverage: (ticker) => request(`/api/coverage?ticker=${ticker}`),
  earnings: (ticker, refresh = false) => request(`/api/earnings?ticker=${ticker}${refresh ? "&refresh=1" : ""}`),
  optionChain: (ticker, strategy = "atr") => request(`/api/option-chain/${ticker}?strategy=${strategy}`),
  execute: (payload) => request("/api/execute", { method: "POST", body: JSON.stringify(payload) }),
  positions: () => request("/api/positions"),
  thetaLedger: (params = "") => request(`/api/theta-ledger${params}`),
  killSwitch: () => request("/api/kill-switch"),
  dailyChecklist: () => request("/api/daily-checklist"),
  config: () => request("/api/config"),
  mode: () => request("/api/mode"),
  setMode: (demo) => request("/api/mode", { method: "POST", body: JSON.stringify({ demo }) }),
  state: () => request("/api/state"),
  saveState: (payload) => request("/api/state", { method: "POST", body: JSON.stringify(payload) }),
  accountStatus: () => request("/api/account/status"),
  schwabAuth: () => request("/auth/schwab"),
};
