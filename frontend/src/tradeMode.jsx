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
