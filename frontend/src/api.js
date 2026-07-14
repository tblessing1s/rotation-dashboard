// Thin fetch wrapper for the CFM API. Returns parsed JSON or throws.
const BASE = "";

// A full-universe scan (cold cache) can legitimately take a while, but a request
// that never returns must not spin the UI forever — abort it and surface a clear
// timeout the caller (useApi) can retry, instead of an indefinite spinner.
const TIMEOUT_MS = 60000;

async function request(path, opts = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), opts.timeoutMs || TIMEOUT_MS);
  let res;
  try {
    res = await fetch(BASE + path, {
      credentials: "same-origin", // send/receive the session cookie
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      ...opts,
    });
  } catch (e) {
    // AbortError (our timeout) or a network failure — both are transient and
    // worth retrying; give them a message the UI can show and useApi can catch.
    if (e.name === "AbortError") {
      const err = new Error("Request timed out — the server is taking too long.");
      err.timeout = true;
      throw err;
    }
    throw new Error("Network error — couldn't reach the server.");
  } finally {
    clearTimeout(timer);
  }
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
    const err = new Error(data.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.data = data; // e.g. a 409 freeze carries {frozen, ticker, review}
    throw err;
  }
  return data;
}

export const api = {
  // One-call landing payload: regime + positions/capital + theta + kill-switch.
  overview: () => request("/api/overview"),
  regime: () => request("/api/regime"),
  scorecard: (tickers) => request(`/api/scan/scorecard${tickers ? `?tickers=${tickers}` : ""}`),
  scanReady: (tickers) => request(`/api/scan/ready${tickers ? `?tickers=${tickers}` : ""}`),
  // Kick a detached server-side scan (keeps running if the tab is backgrounded)
  // and poll its status. The refresh POST returns immediately.
  scanRefresh: () => request("/api/scan/refresh", { method: "POST" }),
  scanStatus: () => request("/api/scan/status"),
  // Force a live quote + bars pull for specific stale Ready-to-Enter names, so
  // they can clear the STALE_BLOCKS_GO gate on the next scan.
  refreshReadyQuote: (tickers) =>
    request("/api/scan/refresh-quote", { method: "POST", body: JSON.stringify({ tickers }) }),
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
  optionChain: (ticker, strategy = "atr") => request(`/api/option-chain/${ticker}?strategy=${strategy}`),
  execute: (payload) => request("/api/execute", { method: "POST", body: JSON.stringify(payload) }),
  // Live order lifecycle (used when an order comes back "working"; paper fills immediately).
  orderStatus: (orderId) => request(`/api/order-status?order_id=${encodeURIComponent(orderId)}`),
  cancelOrder: (orderId) => request("/api/order-cancel", { method: "POST", body: JSON.stringify({ order_id: orderId }) }),
  // Truthful status by client_order_ref (incident hotfix): resolves an order whose
  // broker outcome isn't confirmed yet — recovers a missing orderId and never lies
  // (UNKNOWN stays "confirming", a rejection carries Schwab's verbatim reason).
  submissionStatus: (ref) => request(`/api/order-submission-status?ref=${encodeURIComponent(ref)}`),
  positions: () => request("/api/positions"),
  burn: (ticker) => request(`/api/burn/${ticker}`),
  thetaLedger: (params = "") => request(`/api/theta-ledger${params}`),
  killSwitch: () => request("/api/kill-switch"),
  // Monthly payout tracker: current-month estimate + past months + finalize/paid.
  payouts: () => request("/api/payouts"),
  finalizePayout: (month, amount, note) =>
    request("/api/payouts/finalize", {
      method: "POST",
      body: JSON.stringify({ month, amount, note }),
    }),
  unfinalizePayout: (month) =>
    request("/api/payouts/unfinalize", { method: "POST", body: JSON.stringify({ month }) }),
  markPayoutPaid: (month, note, amount) =>
    request("/api/payouts/mark-paid", {
      method: "POST",
      body: JSON.stringify({ month, note, amount }),
    }),
  unmarkPayoutPaid: (month) =>
    request("/api/payouts/unmark-paid", { method: "POST", body: JSON.stringify({ month }) }),
  history: () => request("/api/history"),
  portfolioRisk: () => request("/api/portfolio-risk"),
  dataHealth: () => request("/api/data-health"),
  dataBudget: () => request("/api/data-budget"),
  maintenanceRefresh: () => request("/api/maintenance/refresh", { method: "POST" }),
  refreshHot: () => request("/api/refresh/hot", { method: "POST" }),
  // Force-pull a live quote for one name / a whole sector (ETF + constituents),
  // bypassing the daily cache; returns fresh scorecard rows to patch into the Scan.
  refreshTicker: (ticker) =>
    request("/api/refresh/ticker", { method: "POST", body: JSON.stringify({ ticker }) }),
  refreshSector: (sector) =>
    request("/api/refresh/sector", { method: "POST", body: JSON.stringify({ sector }) }),
  alerts: () => request("/api/alerts"),
  runAlerts: (dryRun) =>
    request("/api/alerts/run", {
      method: "POST",
      body: JSON.stringify(dryRun === undefined ? {} : { dry_run: dryRun }),
    }),
  ackAlert: (id) => request("/api/alerts/ack", { method: "POST", body: JSON.stringify({ id }) }),
  // Recommendation trust layer: open recs + the derived trust scoreboard.
  recommendations: () => request("/api/recommendations"),
  runRecommendations: () => request("/api/recommendations/run", { method: "POST", body: JSON.stringify({}) }),
  dismissRecommendation: (recId, reason, note) =>
    request("/api/recommendations/dismiss", {
      method: "POST",
      body: JSON.stringify({ rec_id: recId, reason, ...(note ? { note } : {}) }),
    }),
  // Toggle pre-approval on a PENDING_SETTLE rec: it auto-submits when its settle
  // window opens, but only if its trigger re-validates at that moment.
  preapproveRecommendation: (recId, approve = true) =>
    request("/api/recommendations/preapprove", {
      method: "POST",
      body: JSON.stringify({ rec_id: recId, approve }),
    }),
  trustScoreboard: () => request("/api/trust-scoreboard"),
  reconcile: () => request("/api/reconcile"),
  runReconcile: () => request("/api/reconcile", { method: "POST" }),
  verifyFills: (limit) =>
    request("/api/verify-fills", { method: "POST", body: JSON.stringify(limit ? { limit } : {}) }),
  resolveExpiry: (diffId) =>
    request("/api/reconcile/resolve-expiry", { method: "POST", body: JSON.stringify({ diff_id: diffId }) }),
  acknowledgeDiff: (diffId, ackReason) =>
    request("/api/reconcile/acknowledge", { method: "POST", body: JSON.stringify({ diff_id: diffId, ack_reason: ackReason }) }),
  // Record an already-executed out-of-band roll from captured fills.
  recordManualRoll: (body) =>
    request("/api/reconcile/record-manual-roll", { method: "POST", body: JSON.stringify(body) }),
  // The global reconciliation-freeze verdict + minutes staleness (spec §5).
  freezeStatus: () => request("/api/reconcile/freeze-status"),
  // Execution ingestion from Schwab transactions (spec §4).
  ingestion: () => request("/api/ingestion"),
  runIngestion: () => request("/api/ingestion", { method: "POST" }),
  // Adopt one out-of-band broker trade (a proposal) into state.json.
  adoptBrokerTrade: (proposalId) =>
    request("/api/ingestion/adopt", { method: "POST", body: JSON.stringify({ proposal_id: proposalId }) }),
  // List booked broker_manual adoptions + reverse (undo) one exactly.
  adoptions: () => request("/api/ingestion/adoptions"),
  reverseAdoption: (proposalId) =>
    request("/api/ingestion/reverse", { method: "POST", body: JSON.stringify({ proposal_id: proposalId }) }),
  alertSettings: (patch) =>
    request("/api/alerts/settings", { method: "POST", body: JSON.stringify(patch) }),
  pushVapidKey: () => request("/api/push/vapid-key"),
  pushSubscribe: (subscription) =>
    request("/api/push/subscribe", { method: "POST", body: JSON.stringify({ subscription }) }),
  pushUnsubscribe: (endpoint) =>
    request("/api/push/unsubscribe", { method: "POST", body: JSON.stringify({ endpoint }) }),
  pushTest: () => request("/api/push/test", { method: "POST" }),
  version: () => request("/api/version"),
  config: () => request("/api/config"),
  mode: () => request("/api/mode"),
  setMode: (demo) => request("/api/mode", { method: "POST", body: JSON.stringify({ demo }) }),
  liveTrading: () => request("/api/live-trading"),
  setLiveTrading: (enabled) =>
    request("/api/live-trading", { method: "POST", body: JSON.stringify({ enabled }) }),
  accountStatus: () => request("/api/account/status"),
  schwabAuth: () => request("/auth/schwab"),
  authStatus: () => request("/api/auth/status"),
  login: (password) => request("/api/login", { method: "POST", body: JSON.stringify({ password }) }),
  logout: () => request("/api/logout", { method: "POST" }),
};
