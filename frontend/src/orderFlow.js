// Shared order-submission flow: one moving toast tracks an order from submit to
// its terminal state. The paper/logged path commits immediately and reports
// status "filled". A live order comes back "working" with an order_id — we then
// poll the fill for up to 3 seconds and, if it still hasn't filled, auto-cancel
// it. Because Schwab cancels asynchronously, the backend confirms the order
// actually went terminal before we claim it cancelled; an unconfirmed cancel
// ("pending_cancel"/still working) is surfaced as such so the operator knows the
// order may still be live. (The /api/order-status + /api/order-cancel endpoints
// are wired when live order placement is enabled; until then the backend returns
// "filled".)

const ACTION_VERB = {
  open_position_atomic: "Open position",
  buy_leap: "Buy LEAP",
  sell_short: "Sell short",
  close_short: "Close short",
  close_leap: "Close LEAP",
  roll_short: "Roll short",
};

const FILL_TIMEOUT_MS = 3000;
const POLL_MS = 400;
// Confirming an UNKNOWN (lost-response / accepted-no-id) order by client_order_ref.
// A handful of quick reads to catch the common "the ack was just slow" case; if it's
// still unconfirmed we leave a persistent, truthful message rather than poll forever.
const CONFIRM_ATTEMPTS = 5;
const CONFIRM_MS = 2000;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The order's broker outcome isn't confirmed yet (no response, a timeout, or a 2xx
// ack with no id). It may be LIVE at Schwab — so we NEVER say "failed". Poll the
// truthful by-ref status a few times; resolve on a real outcome, else leave a
// persistent "confirming — check your broker" message the operator can act on.
async function confirmByRef(api, toast, id, label, ref) {
  for (let i = 0; i < CONFIRM_ATTEMPTS; i++) {
    await sleep(CONFIRM_MS);
    let st;
    try {
      st = await api.submissionStatus(ref);
    } catch {
      continue; // transient — keep confirming
    }
    if (st.status === "filled") {
      toast.update(id, `${label} filled & logged.`, { type: "success" });
      return st;
    }
    if (st.status === "rejected") {
      toast.update(id, `${label} rejected by Schwab: ${st.reason || "no reason given"}.`,
        { type: "error", duration: 10000 });
      return st;
    }
    if (st.status === "canceled") {
      toast.update(id, `${label} canceled.`, { type: "error", duration: 8000 });
      return st;
    }
    if (st.status === "working") {
      toast.update(id,
        `${label} confirmed working at Schwab${st.order_id ? ` (order ${st.order_id})` : ""}.`,
        { type: "success" });
      return st;
    }
  }
  // Still UNKNOWN — do NOT claim failure. Tell the truth and stop.
  toast.update(id,
    `${label} — the broker hasn't confirmed this order yet. It may be working at Schwab; ` +
      `use "Check status" or confirm in your broker before placing another.`,
    { type: "pending", duration: 0 });
  return { status: "unknown", client_order_ref: ref };
}

export async function submitOrder(api, toast, payload) {
  const label = `${ACTION_VERB[payload.action] || payload.action} ${payload.ticker || ""}`.trim();
  const id = toast.show(`Submitting ${label}…`, { type: "pending", duration: 0 });
  const ref = payload.client_order_ref;

  let res;
  try {
    res = await api.execute(payload);
  } catch (e) {
    // A refuse-to-construct (400) is a real, pre-submission validation stop — show it.
    // But a LOST response for a ref-keyed order is NOT a failure: the order may be live
    // at Schwab. Switch to confirming-by-ref instead of lying "failed" (D2/D3).
    if (ref && !(e.status >= 400 && e.status < 500 && !e.timeout)) {
      toast.update(id, `${label} — confirming with broker…`, { type: "pending", duration: 0 });
      return confirmByRef(api, toast, id, label, ref);
    }
    toast.update(id, `${label} failed: ${e.message}`, { type: "error", duration: 8000 });
    throw e;
  }

  // Explicit broker rejection — show Schwab's verbatim reason (never a generic "failed").
  if (res.status === "rejected") {
    toast.update(id, `${label} rejected by Schwab: ${res.reason || "no reason given"}.`,
      { type: "error", duration: 10000 });
    return res;
  }
  // Broker outcome not yet confirmed (UNKNOWN) — confirm by ref, never "failed".
  if (res.status === "unknown") {
    toast.update(id, `${label} — confirming with broker…`, { type: "pending", duration: 0 });
    return confirmByRef(api, toast, id, label, ref || res.client_order_ref);
  }

  // Immediate fill (paper/logged path, or a live order that filled on placement).
  if (res.status !== "working") {
    const paper = res.mode === "logged";
    toast.update(id, `${label} filled & logged${paper ? " (paper)" : ""}.`, { type: "success" });
    return res;
  }

  // Live working order — confirm the fill within the window or cancel it.
  const orderId = res.order_id;
  toast.update(id, `${label} working — confirming fill…`, { type: "pending", duration: 0 });
  const deadline = Date.now() + FILL_TIMEOUT_MS;
  while (Date.now() < deadline) {
    await sleep(POLL_MS);
    let st;
    try {
      st = await api.orderStatus(orderId);
    } catch {
      continue; // transient — keep polling until the deadline
    }
    if (st.status === "filled") {
      toast.update(id, `${label} filled & logged.`, { type: "success" });
      return st;
    }
    if (st.status === "canceled" || st.status === "rejected") {
      toast.update(id, `${label} ${st.status}.`, { type: "error", duration: 8000 });
      return st;
    }
  }

  // Still unfilled after the window — cancel the resting order. The cancel is
  // only "done" once the backend confirms it against the broker; if it fails the
  // order is STILL WORKING at Schwab, so say so plainly rather than claim it was
  // cancelled — otherwise the operator's next order collides with this one.
  let cancelled;
  try {
    cancelled = await api.cancelOrder(orderId);
  } catch (e) {
    toast.update(
      id,
      `${label} didn't fill within 3s and could NOT be cancelled (${e.message}). ` +
        `The order may still be working — cancel it in your broker before placing another.`,
      { type: "error", duration: 0 },
    );
    return { ...res, status: "working" };
  }
  // A fill can slip in between the last poll and the cancel — the backend commits
  // it and reports "filled" rather than cancelling a filled order.
  if (cancelled.status === "filled") {
    toast.update(id, `${label} filled & logged.`, { type: "success" });
    return cancelled;
  }
  // Only claim it was cancelled once the backend confirmed a terminal state at
  // Schwab. Schwab's cancel is async — the request can be accepted while the
  // order stays working (and can still fill), reported here as "pending_cancel".
  // Don't tell the operator it's gone when the broker hasn't confirmed it.
  if (cancelled.status !== "canceled" && cancelled.status !== "rejected") {
    toast.update(
      id,
      `${label} didn't fill within 3s and the cancel is NOT confirmed — the order ` +
        `may still be working. Check it in your broker before placing another.`,
      { type: "error", duration: 0 },
    );
    return { ...res, status: "working" };
  }
  toast.update(id, `${label} didn't fill within 3s — order ${cancelled.status}.`, { type: "error", duration: 8000 });
  return { ...res, status: cancelled.status };
}
