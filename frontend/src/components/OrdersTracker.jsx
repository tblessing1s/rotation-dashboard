import React from "react";
import { api } from "../api.js";
import { Card } from "./ui.jsx";

// Live order tracker. Reads orders straight from Schwab (last 24h), so it shows an
// order that is working at TOS even when the app never got its order id (a lost or
// id-less ack) — and Cancel here cancels it at the broker by that id. Statuses are
// the broker's, not the app's guess. A cancel is only reported done once Schwab
// confirms a terminal state.
const POLL_MS = 8000;

const STATUS_STYLE = {
  live: "border-sky-500/40 bg-sky-500/10 text-sky-300",
  FILLED: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  done: "border-slate-700 bg-slate-800/60 text-slate-400",
};

const stripSym = (s) => (s || "").replace(/\s+/g, " ").trim();
const fmtTime = (t) => (t ? String(t).slice(11, 19) : "");

function legText(l) {
  const side = (l.instruction || "").replace(/_/g, " ").toLowerCase();
  return `${side} ${l.quantity ?? ""} ${stripSym(l.symbol)}`.trim();
}

export default function OrdersTracker() {
  const [data, setData] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const [busyId, setBusyId] = React.useState(null);
  const [note, setNote] = React.useState(null);

  const load = React.useCallback(async () => {
    try {
      setData(await api.brokerOrders());
      setErr(null);
    } catch (e) {
      setErr(String(e.message || e));
    }
  }, []);

  React.useEffect(() => {
    load();
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  const cancel = async (orderId) => {
    setBusyId(orderId);
    setNote(null);
    try {
      const res = await api.cancelBrokerOrder(orderId);
      if (res.status === "canceled") setNote({ ok: true, text: `Order ${orderId} canceled at Schwab.` });
      else if (res.status === "filled") setNote({ ok: false, text: res.message || `Order ${orderId} already filled.` });
      else setNote({ ok: false, text: `Cancel sent but Schwab hasn't confirmed it yet (${res.raw_status || res.status}). Check TOS.` });
    } catch (e) {
      setNote({ ok: false, text: `Could not cancel: ${e.message || e}. Cancel it in TOS.` });
    } finally {
      setBusyId(null);
      load();
    }
  };

  if (data?.skipped) return null;   // no live broker on this book — nothing to track
  const orders = data?.orders || [];
  const live = orders.filter((o) => o.live);
  const recent = orders.filter((o) => !o.live).slice(0, 3);
  const unconfirmed = data?.unconfirmed || [];
  if (!err && data && !live.length && !unconfirmed.length && !recent.length) return null;

  return (
    <Card title="Orders">
      <div className="flex items-center justify-between text-xs text-slate-500">
        <span>
          {live.length ? `${live.length} working at Schwab` : "No working orders"}
          {unconfirmed.length ? ` · ${unconfirmed.length} unconfirmed` : ""}
        </span>
        <button onClick={load} className="rounded-full border border-slate-700 px-2 py-0.5 text-slate-300 hover:bg-slate-800">
          Refresh
        </button>
      </div>
      {err && <p className="mt-2 text-xs text-amber-300">Couldn't read orders from Schwab: {err}</p>}
      {note && <p className={`mt-2 text-xs ${note.ok ? "text-emerald-300" : "text-rose-300"}`}>{note.text}</p>}

      {unconfirmed.map((u) => (
        <div key={u.client_order_ref} className="mt-2 rounded-md border border-amber-700/50 bg-amber-500/5 p-2">
          <p className="text-xs font-semibold text-amber-300">
            {u.ticker} {u.action} — sent, not yet confirmed by Schwab
          </p>
          <p className="mt-0.5 text-[11px] text-slate-400">
            If it's working, it appears above once Schwab lists it (this refreshes every few seconds) — you can cancel it there.
            {u.detail ? ` (${u.detail})` : ""}
          </p>
        </div>
      ))}

      {[...live, ...recent].map((o) => (
        <div key={o.order_id} className="mt-2 rounded-md border border-slate-800 bg-slate-900/40 p-2">
          <div className="flex items-center justify-between gap-2">
            <span className="flex items-center gap-2 text-sm text-slate-200">
              <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${STATUS_STYLE[o.live ? "live" : o.status] || STATUS_STYLE.done}`}>
                {o.status.replace(/_/g, " ")}
              </span>
              <span className="font-mono text-[11px] text-slate-500">order {o.order_id}</span>
            </span>
            <span className="text-[11px] text-slate-500">{fmtTime(o.entered_time)}</span>
          </div>
          <p className="mt-1 font-mono text-[11px] text-slate-300">
            {(o.legs || []).map(legText).join("  →  ")}
          </p>
          <p className="mt-0.5 font-mono text-[11px] text-slate-500">
            {o.order_type || ""}{o.price != null ? ` @ $${Number(o.price).toFixed(2)}` : ""}
            {o.filled_quantity ? ` · ${o.filled_quantity}/${o.quantity} filled` : ""}
            {!o.tracked && o.live ? " · not tracked by the app" : ""}
          </p>
          {o.live && (
            <div className="mt-1.5 flex justify-end">
              <button onClick={() => cancel(o.order_id)} disabled={busyId === o.order_id}
                      title="Cancel this order at Schwab (confirmed before it says canceled)"
                      className="rounded-md border border-rose-800/60 px-2 py-0.5 text-[11px] font-semibold text-rose-300 hover:bg-rose-500/10 disabled:opacity-50">
                {busyId === o.order_id ? "Cancelling…" : "Cancel"}
              </button>
            </div>
          )}
        </div>
      ))}
    </Card>
  );
}
