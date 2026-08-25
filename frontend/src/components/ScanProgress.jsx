import React from "react";
import { api } from "../api.js";
import { Spinner } from "./ui.jsx";

// Drives the full-universe scan as a DETACHED server-side job instead of letting
// each panel's fetch carry the heavy sweep. On mount it ensures a scan is running
// (a POST that returns immediately), then polls status. Because the work lives on
// the server, it keeps running even if this tab is backgrounded, switched, or the
// app is closed — so a returning client is served warm. When a running scan
// finishes, onComplete() lets the parent refresh the panels with the warm data.
export default function ScanProgress({ onComplete, onRunningChange }) {
  const [st, setSt] = React.useState(null);
  const prevRunning = React.useRef(false);
  // In-flight guard. The interval below fires on a fixed cadence regardless of
  // whether the previous poll came back, and the server is ONE gunicorn worker:
  // during a sweep its threads are starved by GIL-bound indicator math, so an
  // unguarded 2.5s poll piled up ~24 never-returning requests a minute until
  // every one of them hit the client's 60s abort. One poll at a time.
  const inFlight = React.useRef(false);

  const poll = React.useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try { setSt(await api.scanStatus()); } catch { /* transient — next poll retries */ }
    finally { inFlight.current = false; }
  }, []);

  const rescan = React.useCallback(async () => {
    try { setSt(await api.scanRefresh()); } catch { /* surfaced by the next poll */ }
  }, []);

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
    const id = setInterval(poll, 2500);
    // A backgrounded mobile tab throttles the interval; re-poll the moment it
    // returns to the foreground so the state is current immediately.
    const onVis = () => { if (!document.hidden) poll(); };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      alive = false;
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [poll, rescan]);

  // Fire onComplete on the running → finished transition, so the panels reload
  // once (with warm data) rather than on every poll.
  React.useEffect(() => {
    if (prevRunning.current && st && !st.running) onComplete?.();
    prevRunning.current = !!st?.running;
  }, [st, onComplete]);

  // Publish the running flag. The panels that read the full-universe sweep hold
  // their fetch while one is in flight rather than racing it — before this they
  // mounted alongside the scan they had just triggered and waited out the whole
  // sweep. `st === null` (status not yet known) is deliberately NOT "running":
  // it resolves in one poll, and treating it as running would stall a warm load.
  React.useEffect(() => {
    onRunningChange?.(!!st?.running);
  }, [st, onRunningChange]);

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
      <span
        title={st.scanned_at
          ? "The full universe is swept once per data epoch (once on the prior session's closing bars, once after today's close) and cached — opening this tab replays that sweep instead of re-running it. Rescan forces a fresh one."
          : undefined}
      >
        {st.fresh ? "Universe scan ready" : "No recent scan"}
        {/* When the sweep actually ran — not when this process last served a
            cached copy of it, which is what finished_at means. */}
        {st.scanned_at
          ? ` · swept ${st.scanned_at.slice(11, 16)}`
          : st.finished_at ? ` · updated ${st.finished_at.slice(11, 16)}` : ""}
      </span>
      <button onClick={rescan} title="Force a fresh full-universe sweep now"
              className="rounded-md border border-slate-700 px-2 py-0.5 text-slate-300 hover:bg-slate-800">
        Rescan
      </button>
    </div>
  );
}
