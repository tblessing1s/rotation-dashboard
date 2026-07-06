import React from "react";
import { api } from "./api.js";

// Whether a submitted order will actually be transmitted to Schwab ("live") or
// only captured to the local ledger ("paper"). Mirrors the backend routing in
// executor.execute(): an order goes to the broker only when CFM_LIVE_TRADING is
// enabled AND a Schwab refresh token is present (schwab_api.configured()); every
// other case — flag off, or no token — falls through to the honest logged/paper
// path that updates state.json but sends nothing to Schwab.
//
// We key off schwab.present, NOT status === "ok": an expiring/expired token
// still routes live (the order is attempted, then errors on refresh — it is not
// silently logged), so calling that "paper" would be a dangerous mislabel.
export function resolveTradeMode(cfg) {
  if (!cfg) return null;
  return cfg.live_trading && cfg?.schwab?.present ? "live" : "paper";
}

// Fetch the effective trade mode once. Returns "paper" | "live" | null (still
// resolving). Cheap enough that each order ticket can call it independently.
export function useTradeMode() {
  const [mode, setMode] = React.useState(null);
  React.useEffect(() => {
    let stop = false;
    api.config()
      .then((cfg) => { if (!stop) setMode(resolveTradeMode(cfg)); })
      .catch(() => { if (!stop) setMode(null); });
    return () => { stop = true; };
  }, []);
  return mode;
}

// PAPER vs LIVE indicator for the order tickets. Paper (the default) captures the
// trade to the local ledger but sends NO order to Schwab; live transmits a real
// order. `mode` is "paper" | "live" | null (renders nothing while resolving).
export function TradeModeBadge({ mode, className = "" }) {
  if (!mode) return null;
  const live = mode === "live";
  const cls = live
    ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
    : "border-amber-500/40 bg-amber-500/15 text-amber-300";
  return (
    <span
      title={live
        ? "Live — this order is transmitted to Schwab."
        : "Paper — the trade is logged to the local ledger only. NO order is sent to Schwab. Enable CFM_LIVE_TRADING and connect Schwab to trade live."}
      className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-semibold uppercase tracking-wide ${cls} ${className}`}
    >
      <span aria-hidden="true">{live ? "●" : "◍"}</span>
      {live ? "Live" : "Paper"}
    </span>
  );
}

// Human-readable summary of an order payload for the live-confirm dialog: the
// symbol, quantity, and per-leg limit price(s) about to be transmitted. Mirrors
// executor._limit_price so the confirmed number matches what's actually sent.
export function describeOrder(payload = {}) {
  const money = (n) => (n == null || Number.isNaN(Number(n)) ? "—" : `$${Number(n).toFixed(2)}`);
  const verb = {
    buy_leap: "Buy LEAP (buy to open)",
    sell_short: "Sell weekly (sell to open)",
    close_short: "Close short (buy to close)",
    close_leap: "Close LEAP (sell to close)",
    roll_short: "Roll short (buy-to-close + sell-to-open)",
  }[payload.action] || payload.action;

  const rows = [["Order", verb], ["Ticker", payload.ticker || "—"],
               ["Contracts", String(payload.contracts ?? "—")]];
  if (payload.action === "roll_short") {
    rows.push(["From strike", `${payload.from_strike ?? "—"}C`]);
    rows.push(["To strike", `${payload.to_strike ?? "—"}C · exp ${payload.to_expiration || "—"}`]);
    const net = payload.premium_per_share != null && payload.close_price_per_share != null
      ? payload.premium_per_share - payload.close_price_per_share : null;
    rows.push(["Net limit / share", net == null ? "—" : `${net >= 0 ? "+" : ""}${money(net)}`]);
  } else {
    rows.push(["Strike", payload.strike != null ? `${payload.strike}C` : "—"]);
    if (payload.expiration) rows.push(["Expiration", payload.expiration]);
    const limit = payload.action === "buy_leap" ? (payload.execution_price || 0) / 100
      : payload.action === "close_leap" ? (payload.close_price || 0) / 100
      : payload.action === "sell_short" ? payload.premium_per_share
      : payload.close_price_per_share;
    rows.push(["Limit / share", money(limit)]);
  }
  if (payload.option_symbol) rows.push(["Symbol", payload.option_symbol]);
  return rows;
}

// Live-order confirmation gate: an explicit "yes, transmit" step shown ONLY in
// live mode, so a real Schwab order can't go out on a stray click. Lists the
// symbol/qty/limit from describeOrder.
export function LiveOrderConfirm({ payload, busy = false, onConfirm, onCancel }) {
  const rows = describeOrder(payload || {});
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 p-4"
         role="dialog" aria-modal="true" onClick={onCancel}>
      <div className="w-full max-w-md rounded-xl border border-emerald-700 bg-slate-900 p-5 shadow-2xl"
           onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-center gap-2">
          <TradeModeBadge mode="live" />
          <h2 className="text-base font-semibold text-slate-100">Confirm live order</h2>
        </div>
        <p className="mb-3 text-sm text-emerald-200">
          This transmits a <span className="font-semibold">real order to your Schwab account</span>.
        </p>
        <dl className="mb-4 divide-y divide-slate-800 rounded-lg border border-slate-800 bg-slate-950 text-sm">
          {rows.map(([k, v]) => (
            <div key={k} className="flex items-center justify-between gap-3 px-3 py-1.5">
              <dt className="text-slate-400">{k}</dt>
              <dd className="text-right font-medium tabular-nums text-slate-100">{v}</dd>
            </div>
          ))}
        </dl>
        <div className="flex items-center justify-end gap-2">
          <button onClick={onCancel} disabled={busy}
                  className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800 disabled:opacity-40">
            Cancel
          </button>
          <button onClick={onConfirm} disabled={busy}
                  className="rounded-lg bg-emerald-500/20 px-4 py-2 text-sm font-semibold text-emerald-300 hover:bg-emerald-500/30 disabled:opacity-40">
            {busy ? "Transmitting…" : "Transmit live order"}
          </button>
        </div>
      </div>
    </div>
  );
}
