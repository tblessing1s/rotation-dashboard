import React from "react";
import { api } from "../api.js";
import { Spinner } from "./ui.jsx";

// Drives the full-universe scan as a DETACHED server-side job instead of letting
// each panel's fetch carry the heavy sweep. On mount it ensures a scan is running
// (a POST that returns immediately), then polls status. Because the work lives on
// the server, it keeps running even if this tab is backgrounded, switched, or the
// app is closed — so a returning client is served warm. When a running scan
// finishes, onComplete() lets the parent refresh the panels with the warm data.
export default function ScanProgress({ onComplete }) {
  const [st, setSt] = React.useState(null);
  const prevRunning = React.useRef(false);

  const poll = React.useCallback(async () => {
    try { setSt(await api.scanStatus()); } catch { /* transient — next poll retries */ }
  }, []);

  const rescan = React.useCallback(async () => {
    try { setSt(await api.scanRefresh()); } catch { /* surfaced by the next poll */ }
  }, []);

  // Mount: check status ONCE (kick a scan if the cache is cold), and re-check
  // only when the tab returns to the foreground. No steady polling here — that's
  // driven by the running state below, so an idle Scan tab doesn't hammer
  // /scan/status forever.
  React.useEffect(() => {
    let alive = true;
    (async () => {
      let s = null;
      try { s = await api.scanStatus(); } catch { /* ignore */ }
      if (!alive) return;
      setSt(s);
      // Cold cache and nothing running yet → start the detached scan.
      if (s && !s.fresh && !s.running) rescan();
    })();
    const onVis = () => { if (!document.hidden) poll(); };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      alive = false;
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [poll, rescan]);

  // Poll ONLY while a scan is actually running — every 2.5s until it finishes,
  // then stop. Starting a scan (Rescan, or one discovered on focus / at mount)
  // flips `running` true and restarts this; when it goes false the interval is
  // torn down. Idle → zero polling.
  React.useEffect(() => {
    if (!st?.running) return undefined;
    const id = setInterval(poll, 2500);
    return () => clearInterval(id);
  }, [st?.running, poll]);

  // Fire onComplete on the running → finished transition, so the panels reload
  // once (with warm data) rather than on every poll.
  React.useEffect(() => {
    if (prevRunning.current && st && !st.running) onComplete?.();
    prevRunning.current = !!st?.running;
  }, [st, onComplete]);

  if (!st) return null;

  if (st.running) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-amber-600/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
        <Spinner size="h-4 w-4" />
        <span>Scanning the universe… this keeps running even if you switch tabs or close the app.</span>
      </div>
    );
  }

  if (st.status === "error") {
    return (
      <div className="flex items-center justify-between gap-2 rounded-lg border border-rose-600/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
        <span>Scan failed{st.error ? `: ${st.error}` : ""}.</span>
        <button onClick={rescan} className="rounded-md border border-rose-700 px-2 py-0.5 text-xs text-rose-200 hover:bg-rose-500/20">
          Rescan
        </button>
      </div>
    );
  }

  // Idle/done: a slim confirmation line with a manual rescan.
  return (
    <div className="flex items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-1.5 text-xs text-slate-500">
      <span>
        {st.fresh ? "Universe scan ready" : "No recent scan"}
        {st.finished_at && ` · updated ${st.finished_at.slice(11, 16)}`}
      </span>
      <button onClick={rescan} className="rounded-md border border-slate-700 px-2 py-0.5 text-slate-300 hover:bg-slate-800">
        Rescan
      </button>
    </div>
  );
}
