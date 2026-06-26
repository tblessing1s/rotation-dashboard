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
  execute: (payload) => request("/api/execute", { method: "POST", body: JSON.stringify(payload) }),
  positions: () => request("/api/positions"),
  thetaLedger: (params = "") => request(`/api/theta-ledger${params}`),
  killSwitch: () => request("/api/kill-switch"),
  dailyChecklist: () => request("/api/daily-checklist"),
  config: () => request("/api/config"),
  state: () => request("/api/state"),
  saveState: (payload) => request("/api/state", { method: "POST", body: JSON.stringify(payload) }),
  schwabAuth: () => request("/auth/schwab"),
};
