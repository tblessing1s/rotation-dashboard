import React from "react";
import { api } from "../api.js";
import { Pill } from "./ui.jsx";

// Schwab refresh tokens expire every 7 days and can only be renewed by a fresh
// browser login (Schwab allows no programmatic refresh). This banner shows the
// token's health and provides the one-click re-authorize flow.

const TONE = {
  ok: { status: "green", label: "Schwab connected" },
  warning: { status: "yellow", label: "Schwab token expiring" },
  expired: { status: "red", label: "Schwab token expired" },
  missing: { status: "red", label: "Schwab not connected" },
  unknown: { status: "yellow", label: "Schwab token (age unknown)" },
};

function bannerParam() {
  const p = new URLSearchParams(window.location.search);
  return { schwab: p.get("schwab"), msg: p.get("msg") };
}

export default function SchwabStatus({ demo = false }) {
  const [tok, setTok] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const [justResult] = React.useState(bannerParam());

  const load = React.useCallback(async () => {
    try {
      setTok(await api.accountStatus());
    } catch (e) {
      setErr(e.message);
    }
  }, []);

  React.useEffect(() => {
    load();
    // Clean the ?schwab= flag from the URL after reading it once.
    if (justResult.schwab) {
      const url = new URL(window.location.href);
      url.searchParams.delete("schwab");
      url.searchParams.delete("msg");
      window.history.replaceState({}, "", url.toString());
    }
  }, [load, justResult.schwab]);

  async function reauthorize() {
    setBusy(true);
    setErr(null);
    try {
      const { authorize_url } = await api.schwabAuth();
      window.location.href = authorize_url; // full-page redirect into Schwab's consent flow
    } catch (e) {
      setErr(e.message);
      setBusy(false);
    }
  }

  // In demo mode the Schwab token is irrelevant — show a clear demo banner
  // instead of nagging about a connection the demo data doesn't use.
  if (demo) {
    return (
      <div className="mb-4 rounded-xl border border-amber-700 bg-amber-500/5 p-4">
        <div className="flex flex-wrap items-center gap-3">
          <Pill status="yellow">Demo data</Pill>
          <div className="text-sm text-slate-300">
            Showing a synthetic price feed and a sample CFM book — no live providers or orders.
            Flip the navbar switch to <span className="font-semibold text-slate-100">Live data</span> to use Schwab/Alpha&nbsp;Vantage.
          </div>
        </div>
      </div>
    );
  }

  const status = tok?.status || "missing";
  const tone = TONE[status] || TONE.missing;
  const healthy = status === "ok";

  // When healthy and no post-redirect message, stay out of the way (small chip).
  if (healthy && justResult.schwab !== "connected") {
    return (
      <div className="mb-3 flex items-center justify-end gap-2 text-xs text-slate-500">
        <Pill status="green">Schwab connected</Pill>
        {tok?.daysLeft != null && <span>{tok.daysLeft.toFixed(1)}d left</span>}
        <button onClick={reauthorize} disabled={busy} className="underline hover:text-slate-300">
          re-authorize
        </button>
      </div>
    );
  }

  return (
    <div className={`mb-4 rounded-xl border p-4 ${healthy ? "border-emerald-800 bg-emerald-500/5" : "border-amber-700 bg-amber-500/5"}`}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Pill status={tone.status}>{tone.label}</Pill>
          <div className="text-sm text-slate-300">
            {justResult.schwab === "connected" && "Schwab authorized — token refreshed."}
            {justResult.schwab === "error" && `Authorization failed: ${justResult.msg || "unknown error"}`}
            {!justResult.schwab && status === "expired" && "The 7-day refresh token expired. Re-authorize to restore live data."}
            {!justResult.schwab && status === "warning" && `Token expires soon${tok?.daysLeft != null ? ` (${tok.daysLeft.toFixed(1)} days left)` : ""}. Re-authorize to avoid an outage.`}
            {!justResult.schwab && status === "missing" && "No Schwab token. Connect to enable live market data and execution."}
            {!justResult.schwab && status === "unknown" && "Token age is unknown (env-provided). Re-authorize to start tracking expiry."}
          </div>
        </div>
        <button
          onClick={reauthorize}
          disabled={busy}
          className="rounded-lg bg-amber-500/20 px-4 py-2 text-sm font-semibold text-amber-200 hover:bg-amber-500/30 disabled:opacity-40"
        >
          {busy ? "Redirecting…" : "Re-authorize Schwab"}
        </button>
      </div>
      {err && <div className="mt-2 text-xs text-rose-400">{err}</div>}
    </div>
  );
}
