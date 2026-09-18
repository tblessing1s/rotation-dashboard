import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { submitOrder } from "./orderFlow.js";

// A minimal toast stub that records every show/update call so assertions can
// read the last message an operator would have seen.
function makeToast() {
  let nextId = 1;
  const calls = [];
  return {
    calls,
    show(message, opts) {
      const id = nextId++;
      calls.push({ id, message, opts });
      return id;
    },
    update(id, message, opts) {
      calls.push({ id, message, opts });
    },
    last() {
      return calls[calls.length - 1];
    },
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("submitOrder — immediate outcomes", () => {
  it("the paper/logged path is reported as RECORDED, never as a fill", async () => {
    const api = { execute: vi.fn().mockResolvedValue({ status: "logged", mode: "logged" }) };
    const toast = makeToast();
    const res = await submitOrder(api, toast, { action: "sell_short", ticker: "ON" });
    expect(res.mode).toBe("logged");
    const last = toast.last();
    expect(last.message).toContain("RECORDED to your ledger");
    expect(last.message).toContain("NO order was sent to Schwab");
    expect(last.opts.type).toBe("warning");
  });

  it("a live order that filled on placement reports filled & logged", async () => {
    const api = { execute: vi.fn().mockResolvedValue({ status: "filled" }) };
    const toast = makeToast();
    await submitOrder(api, toast, { action: "buy_shares", ticker: "ON" });
    expect(toast.last().message).toBe("Buy shares ON filled & logged.");
    expect(toast.last().opts.type).toBe("success");
  });

  it("a broker rejection shows Schwab's verbatim reason", async () => {
    const api = { execute: vi.fn().mockResolvedValue({ status: "rejected", reason: "insufficient buying power" }) };
    const toast = makeToast();
    const res = await submitOrder(api, toast, { action: "sell_short", ticker: "ON" });
    expect(res.status).toBe("rejected");
    expect(toast.last().message).toContain("rejected by Schwab: insufficient buying power");
    expect(toast.last().opts.type).toBe("error");
  });

  it("a pre-submission validation failure (no ref) surfaces as failed, not confirming", async () => {
    const err = Object.assign(new Error("strike out of range"), { status: 400 });
    const api = { execute: vi.fn().mockRejectedValue(err) };
    const toast = makeToast();
    await expect(submitOrder(api, toast, { action: "sell_short", ticker: "ON" })).rejects.toThrow();
    expect(toast.last().message).toContain("failed: strike out of range");
    expect(toast.last().opts.type).toBe("error");
  });
});

describe("submitOrder — working order fill polling", () => {
  it("polls with the documented backoff cadence (700ms, then +400ms steps up to a 2000ms cap) until it fills", async () => {
    const timestamps = [];
    const api = {
      execute: vi.fn().mockResolvedValue({ status: "working", order_id: "O1", fill_wait_ms: 10000 }),
      orderStatus: vi.fn().mockImplementation(() => {
        timestamps.push(Date.now());
        // Fill on the 4th poll so at least three backoff steps are observed.
        if (timestamps.length >= 4) return Promise.resolve({ status: "filled" });
        return Promise.resolve({ status: "working" });
      }),
    };
    const toast = makeToast();
    const start = Date.now();

    const promise = submitOrder(api, toast, { action: "sell_short", ticker: "ON" });
    await vi.runAllTimersAsync();
    const res = await promise;

    expect(res.status).toBe("filled");
    expect(toast.last().message).toBe("Sell covered call ON filled & logged.");

    const deltas = timestamps.map((t, i) => t - (i === 0 ? start : timestamps[i - 1]));
    // POLL_FIRST_MS=700, then steps of +400 capped at 2000: 700, 1100, 1500, ...
    expect(deltas[0]).toBe(700);
    expect(deltas[1]).toBe(1100);
    expect(deltas[2]).toBe(1500);
  });

  it("backs off harder after a transient poll error, then recovers and keeps polling", async () => {
    let call = 0;
    const api = {
      execute: vi.fn().mockResolvedValue({ status: "working", order_id: "O1", fill_wait_ms: 20000 }),
      orderStatus: vi.fn().mockImplementation(() => {
        call += 1;
        if (call === 2) return Promise.reject(new Error("429"));
        if (call === 3) return Promise.resolve({ status: "filled" });
        return Promise.resolve({ status: "working" });
      }),
    };
    const toast = makeToast();
    const promise = submitOrder(api, toast, { action: "close_short", ticker: "ON" });
    await vi.runAllTimersAsync();
    const res = await promise;
    expect(res.status).toBe("filled");
    expect(api.orderStatus).toHaveBeenCalledTimes(3);
  });

  it("cancels automatically when nothing fills before the deadline, and reports the broker-confirmed outcome", async () => {
    const api = {
      execute: vi.fn().mockResolvedValue({ status: "working", order_id: "O1", fill_wait_ms: 3000 }),
      orderStatus: vi.fn().mockResolvedValue({ status: "working" }),
      cancelOrder: vi.fn().mockResolvedValue({ status: "canceled" }),
    };
    const toast = makeToast();
    const promise = submitOrder(api, toast, { action: "sell_short", ticker: "ON" });
    await vi.runAllTimersAsync();
    const res = await promise;

    expect(api.cancelOrder).toHaveBeenCalledWith("O1");
    expect(res.status).toBe("canceled");
    expect(toast.last().message).toContain("didn't fill in 3s");
    expect(toast.last().message).toContain("order canceled");
  });

  it("a fill that slips in right as the deadline cancel goes out is reported as filled, not cancelled", async () => {
    const api = {
      execute: vi.fn().mockResolvedValue({ status: "working", order_id: "O1", fill_wait_ms: 3000 }),
      orderStatus: vi.fn().mockResolvedValue({ status: "working" }),
      cancelOrder: vi.fn().mockResolvedValue({ status: "filled" }),
    };
    const toast = makeToast();
    const promise = submitOrder(api, toast, { action: "sell_short", ticker: "ON" });
    await vi.runAllTimersAsync();
    const res = await promise;
    expect(res.status).toBe("filled");
    expect(toast.last().message).toBe("Sell covered call ON filled & logged.");
  });

  it("an operator-requested cancel skips waiting for the deadline and reports the confirmed cancel", async () => {
    let requestCancel;
    const api = {
      execute: vi.fn().mockResolvedValue({ status: "working", order_id: "O1", fill_wait_ms: 30000 }),
      orderStatus: vi.fn().mockResolvedValue({ status: "working" }),
      cancelOrder: vi.fn().mockResolvedValue({ status: "canceled" }),
    };
    const toast = {
      calls: [],
      show(message, opts) {
        this.calls.push({ message, opts });
        return 1;
      },
      update(id, message, opts) {
        this.calls.push({ message, opts });
        if (opts?.action?.onClick) requestCancel = opts.action.onClick;
      },
      last() {
        return this.calls[this.calls.length - 1];
      },
    };

    const promise = submitOrder(api, toast, { action: "sell_short", ticker: "ON" });
    // Let the first "working — confirming fill…" toast (with the Cancel action) land.
    await vi.advanceTimersByTimeAsync(0);
    expect(typeof requestCancel).toBe("function");
    requestCancel();
    await vi.runAllTimersAsync();
    const res = await promise;

    expect(res.status).toBe("canceled");
    expect(toast.last().message).toContain("cancelled by request");
    // The automatic deadline (30s) never needed to fire — cancelOrder was called once.
    expect(api.cancelOrder).toHaveBeenCalledTimes(1);
  });

  it("an unconfirmed cancel is reported truthfully as still possibly working, never as gone", async () => {
    const api = {
      execute: vi.fn().mockResolvedValue({ status: "working", order_id: "O1", fill_wait_ms: 3000 }),
      orderStatus: vi.fn().mockResolvedValue({ status: "working" }),
      cancelOrder: vi.fn().mockResolvedValue({ status: "working" }),
    };
    const toast = makeToast();
    const promise = submitOrder(api, toast, { action: "sell_short", ticker: "ON" });
    await vi.runAllTimersAsync();
    const res = await promise;
    expect(res.status).toBe("working");
    expect(toast.last().message).toContain("NOT confirmed");
  });
});

describe("submitOrder — unknown/lost-response outcomes", () => {
  it("confirms an UNKNOWN ack by ref, polling every 2s, and reports the eventual fill", async () => {
    let call = 0;
    const api = {
      execute: vi.fn().mockResolvedValue({ status: "unknown", client_order_ref: "ref-1" }),
      submissionStatus: vi.fn().mockImplementation(() => {
        call += 1;
        return Promise.resolve(call < 2 ? { status: "unknown" } : { status: "filled" });
      }),
    };
    const toast = makeToast();
    const promise = submitOrder(api, toast, { action: "sell_short", ticker: "ON", client_order_ref: "ref-1" });
    await vi.runAllTimersAsync();
    const res = await promise;
    expect(res.status).toBe("filled");
    expect(toast.last().message).toBe("Sell covered call ON filled & logged.");
    expect(api.submissionStatus).toHaveBeenCalledWith("ref-1");
  });

  it("never claims failure when confirmation stays unknown — leaves a persistent truthful message", async () => {
    const api = {
      execute: vi.fn().mockResolvedValue({ status: "unknown", client_order_ref: "ref-1" }),
      submissionStatus: vi.fn().mockResolvedValue({ status: "unknown" }),
    };
    const toast = makeToast();
    const promise = submitOrder(api, toast, { action: "sell_short", ticker: "ON", client_order_ref: "ref-1" });
    await vi.runAllTimersAsync();
    const res = await promise;
    expect(res.status).toBe("unknown");
    expect(toast.last().message).not.toContain("failed");
    expect(toast.last().message).toContain("hasn't confirmed this order yet");
    expect(toast.last().opts.duration).toBe(0);
  });

  it("a lost response for a ref-keyed order switches to confirm-by-ref instead of claiming failure", async () => {
    const err = new Error("network timeout");
    const api = {
      execute: vi.fn().mockRejectedValue(err),
      submissionStatus: vi.fn().mockResolvedValue({ status: "filled" }),
    };
    const toast = makeToast();
    const promise = submitOrder(api, toast, { action: "sell_short", ticker: "ON", client_order_ref: "ref-1" });
    await vi.runAllTimersAsync();
    const res = await promise;
    expect(res.status).toBe("filled");
    expect(api.submissionStatus).toHaveBeenCalledWith("ref-1");
  });
});
