// Shared order-submission flow: one moving toast tracks an order from submit to
// its terminal state. The paper/logged path commits immediately and reports
// status "filled". A live order comes back "working" with an order_id — we then
// poll the fill for up to 3 seconds and, if it still hasn't filled, auto-cancel
// it and say so. (The /api/order-status + /api/order-cancel endpoints are wired
// when live order placement is enabled; until then the backend returns "filled".)

const ACTION_VERB = {
  buy_leap: "Buy LEAP",
  sell_short: "Sell short",
  close_short: "Close short",
  close_leap: "Close LEAP",
  roll_short: "Roll short",
};

const FILL_TIMEOUT_MS = 3000;
const POLL_MS = 400;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function submitOrder(api, toast, payload) {
  const label = `${ACTION_VERB[payload.action] || payload.action} ${payload.ticker || ""}`.trim();
  const id = toast.show(`Submitting ${label}…`, { type: "pending", duration: 0 });

  let res;
  try {
    res = await api.execute(payload);
  } catch (e) {
    toast.update(id, `${label} failed: ${e.message}`, { type: "error", duration: 8000 });
    throw e;
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

  // Still unfilled after the window — cancel the resting order.
  try {
    await api.cancelOrder(orderId);
  } catch {
    /* report cancellation regardless; a stuck order is surfaced on the next poll */
  }
  toast.update(id, `${label} didn't fill within 3s — order cancelled.`, { type: "error", duration: 8000 });
  return { ...res, status: "canceled" };
}
