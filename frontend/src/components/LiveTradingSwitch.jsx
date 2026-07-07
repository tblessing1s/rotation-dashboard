import React from "react";
import { api } from "../api.js";
import { Card, Pill } from "./ui.jsx";

// Settings control for the live-trading toggle. Enabling it means executed
// orders transmit to the real Schwab account. Enabling requires an explicit
// confirmation; turning it off is immediate. Locked when CFM_LIVE_TRADING is set
// in the environment (an ops override that must be cleared at the deploy level).
export default function LiveTradingSwitch() {
  const [st, setSt] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const [confirming, setConfirming] = React.useState(false);

  const load = React.useCallback(async () => {
    try { setSt(await api.liveTrading()); setErr(null); }
    catch (e) { setErr(e.message); }
  }, []);
  React.useEffect(() => { load(); }, [load]);

  async function apply(enabled) {
    setBusy(true); setErr(null);
    try { setSt(await api.setLiveTrading(enabled)); }
    catch (e) { setErr(e.message); }
    finally { setBusy(false); setConfirming(false); }
  }

  if (!st) {
    return <Card title="Live trading"><p className="text-sm text-slate-500">Loading…</p></Card>;
  }

  const { enabled, transmit, env_locked, demo, schwab_configured, schwab } = st;
  const tokenWarn = schwab_configured && schwab && schwab.status !== "ok";

  return (
    <Card
      title="Live trading"
      right={<Pill status={transmit ? "green" : "yellow"}>{transmit ? "LIVE" : "PAPER"}</Pill>}
    >
      <p className="text-sm text-slate-300">
        {transmit
          ? "Executed orders are transmitted to your real Schwab account."
          : enabled
            ? "Live is ON, but you're in Demo data — orders stay paper until you switch to Live data."
            : "Paper mode — trades are logged to your ledger at live prices; no order is sent to Schwab."}
      </p>

      {/* Preconditions the operator should know before/while live is on. */}
      <ul className="mt-2 space-y-1 text-xs">
        <li className={demo ? "text-amber-300" : "text-slate-500"}>
          {demo ? "⚠ Demo data is on — live orders will NOT transmit until you switch to Live data."
                : "✓ Live data mode (not demo)."}
        </li>
        <li className={schwab_configured ? (tokenWarn ? "text-amber-300" : "text-slate-500") : "text-rose-300"}>
          {schwab_configured
            ? (tokenWarn
                ? `⚠ Schwab token ${schwab?.status || ""}${schwab?.daysLeft != null ? ` (${schwab.daysLeft}d left)` : ""} — re-authorize soon or live orders will fail.`
                : "✓ Schwab connected.")
            : "✗ Schwab not connected — live orders will fail until you re-authorize."}
        </li>
      </ul>

      <div className="mt-3 flex items-center gap-3">
        {env_locked ? (
          <span className="rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-2 text-xs text-slate-400">
            Locked on by the <code className="text-slate-300">CFM_LIVE_TRADING</code> environment
            variable — change it at the deploy level.
          </span>
        ) : enabled ? (
          <button
            onClick={() => apply(false)}
            disabled={busy}
            className="rounded-lg border border-slate-700 px-4 py-2 text-sm font-semibold text-slate-200 hover:bg-slate-800 disabled:opacity-40"
          >
            {busy ? "Saving…" : "Disable live trading"}
          </button>
        ) : (
          <button
            onClick={() => setConfirming(true)}
            disabled={busy}
            className="rounded-lg bg-emerald-500/20 px-4 py-2 text-sm font-semibold text-emerald-300 hover:bg-emerald-500/30 disabled:opacity-40"
          >
            Enable live trading
          </button>
        )}
      </div>

      {err && <p className="mt-2 text-xs text-rose-400">{err}</p>}

      {confirming && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 p-4"
             role="dialog" aria-modal="true" onClick={() => setConfirming(false)}>
          <div className="w-full max-w-md rounded-xl border border-emerald-700 bg-slate-900 p-5 shadow-2xl"
               onClick={(e) => e.stopPropagation()}>
            <h2 className="mb-2 text-base font-semibold text-slate-100">Enable live trading?</h2>
            <p className="text-sm text-emerald-200">
              After this, every order you submit transmits a <span className="font-semibold">real order to
              your Schwab account</span> for real money. Each ticket still asks you to confirm before it sends.
            </p>
            {demo && (
              <p className="mt-2 rounded-lg border border-amber-700 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
                You're in Demo data — orders will keep going to paper until you switch to Live data.
              </p>
            )}
            {!schwab_configured && (
              <p className="mt-2 rounded-lg border border-rose-800 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
                Schwab isn't connected — live orders will fail until you re-authorize.
              </p>
            )}
            <div className="mt-4 flex items-center justify-end gap-2">
              <button onClick={() => setConfirming(false)} disabled={busy}
                      className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800 disabled:opacity-40">
                Cancel
              </button>
              <button onClick={() => apply(true)} disabled={busy}
                      className="rounded-lg bg-emerald-500/20 px-4 py-2 text-sm font-semibold text-emerald-300 hover:bg-emerald-500/30 disabled:opacity-40">
                {busy ? "Enabling…" : "Enable live trading"}
              </button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}
