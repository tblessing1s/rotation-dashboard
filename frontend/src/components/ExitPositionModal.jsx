import React from "react";
import { api } from "../api.js";
import { fmt } from "./ui.jsx";
import { useTradeMode, TradeModeBadge } from "../tradeMode.jsx";
import { explainRec } from "../recWhy.js";

// Coded exit reasons an operator may pick from this ticket (mirrors
// backend/exit_reasons.py's OPERATOR_SELECTABLE). CALLED_AWAY is excluded —
// that's an already-happened assignment recorded elsewhere
// (executor._close_shares_assigned), not a reason to transmit a fresh sell
// order here. LEAP_ROLL is mechanical-only and never operator-set.
const EXIT_REASONS = [
  { value: "OPERATOR_DISCRETION", label: "Operator discretion (note required)" },
  { value: "TARGET_REACHED", label: "Target reached" },
  { value: "KILL_SWITCH_SPY", label: "Kill switch — losing to SPY" },
  { value: "CB_DRAWDOWN_15", label: "Circuit breaker — 15% drawdown" },
  { value: "CB_MA50_3CLOSE", label: "Circuit breaker — 3 closes below 50-day MA" },
  { value: "CB_MA200_CLOSE", label: "Circuit breaker — close below 200-day MA" },
  { value: "CB_MANUAL_LINE", label: "Circuit breaker — manual line in the sand" },
  { value: "WHIPSAW_BREAKER", label: "Whipsaw — stop defending" },
  { value: "DELTA_COVERAGE", label: "Coverage floor breached" },
  { value: "EARNINGS_WINDOW", label: "Earnings window" },
  { value: "RECONCILIATION", label: "Reconciliation — broker divergence" },
];
const NOTE_REQUIRED = new Set(["OPERATOR_DISCRETION"]);

function stepLabel(step) {
  if (step.leg === "close_short") return `Close ${fmt(step.strike, 2)}C`;
  if (step.leg === "sell_shares") return "Sell shares";
  return step.leg;
}

/**
 * Full exit: closes every open short call, then sells every owned share, in
 * one blocking server call (executor.exit_position via POST
 * /api/positions/<ticker>/exit). There is no working-order status to poll —
 * the backend already waits for each leg to fill before moving to the next
 * one — so this is a single request/response, not the roll ticket's
 * submit-then-poll lifecycle.
 */
export default function ExitPositionModal({ ticker, position, rec, sourceRecId, onExecuted, onClose }) {
  const tradeMode = useTradeMode();
  const p = position || {};
  const shareCount = Number(p.shares?.count || 0);
  const shortCalls = (p.short_calls || []).filter((sc) => Number(sc.contracts || 0) > 0);
  const why = rec ? explainRec(rec) : null;
  const hintedReason = rec?.proposed_ticket?.exit_reason_code;

  const [exitReason, setExitReason] = React.useState(
    EXIT_REASONS.some((r) => r.value === hintedReason) ? hintedReason : "OPERATOR_DISCRETION",
  );
  const [exitNote, setExitNote] = React.useState("");
  const [confirmed, setConfirmed] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);
  const [result, setResult] = React.useState(null); // last response, success or partial failure

  const needsNote = NOTE_REQUIRED.has(exitReason);
  const canSubmit = !busy && exitReason && (!needsNote || exitNote.trim().length > 0) && confirmed;

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.exitPosition(ticker, {
        exit_reason: exitReason,
        exit_note: needsNote ? exitNote.trim() : undefined,
        source_rec_id: sourceRecId,
      });
      // A clean run resolves here (res.ok is always true — request() throws on
      // any non-2xx status, which is exactly how a stopped-partway exit
      // (result.ok === false, HTTP 409) comes back, so that case is handled
      // in the catch below instead.
      onExecuted?.(res);
      onClose?.();
    } catch (e) {
      // A stopped-partway exit (executor.exit_position returning ok:false) is
      // a 409 whose body carries steps/error — surface it in full and refresh
      // the position, since one or more legs may have actually executed even
      // though the sequence as a whole did not finish.
      if (e.data && (e.data.steps || "position_closed" in (e.data || {}))) {
        setResult(e.data);
        onExecuted?.(e.data);
      }
      setError(e.data?.frozen
        ? `Frozen for reconciliation — resolve the review first. ${e.message}`
        : e.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      role="dialog" aria-modal="true" onClick={busy ? undefined : onClose}
    >
      <div
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-xl border border-rose-800 bg-slate-900 p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-semibold text-slate-100">Exit position · {ticker}</h2>
            <TradeModeBadge mode={tradeMode} />
          </div>
          <button onClick={onClose} disabled={busy}
                  className="rounded-lg px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-200 disabled:opacity-40">✕</button>
        </div>

        <div className="rounded-lg border border-slate-800 bg-slate-950 p-3 text-sm text-slate-300">
          <p className="font-semibold text-rose-300">This closes every open short call, then sells every share you own — in that order.</p>
          <p className="mt-1 text-xs text-slate-500">
            Each leg is priced from a fresh quote and waited on until it fills before the next one is
            sent. This call blocks until the whole sequence finishes or stops on a leg that didn't fill.
          </p>
          <ul className="mt-2 space-y-0.5 text-xs">
            {shortCalls.map((sc) => (
              <li key={`${sc.strike}-${sc.expiration}`}>
                Close <span className="font-semibold text-slate-200">{fmt(sc.strike, 2)}C</span>{" "}
                × {sc.contracts} · exp {sc.expiration}
              </li>
            ))}
            {shareCount > 0 && (
              <li>Sell <span className="font-semibold text-slate-200">{shareCount}</span> shares</li>
            )}
            {shortCalls.length === 0 && shareCount === 0 && (
              <li className="text-slate-500">Nothing open to close on this position.</li>
            )}
          </ul>
        </div>

        {why?.why && (
          <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950 p-3 text-xs text-slate-400">
            <span className="uppercase tracking-wide text-slate-500">Engine's call</span>
            <p className="mt-1">{why.why}</p>
          </div>
        )}

        <div className="mt-3">
          <label className="block text-xs uppercase tracking-wide text-slate-500">Exit reason</label>
          <select
            value={exitReason}
            onChange={(e) => setExitReason(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-slate-100"
          >
            {EXIT_REASONS.map((r) => (
              <option key={r.value} value={r.value}>{r.label}</option>
            ))}
          </select>
        </div>

        {needsNote && (
          <label className="mt-3 block text-xs text-amber-200">
            Required — why are you exiting now?
            <textarea
              value={exitNote}
              onChange={(e) => setExitNote(e.target.value)}
              placeholder="e.g. taking the win before earnings, don't want to hold through the report"
              rows={2}
              className="mt-1 w-full rounded-lg border border-amber-700 bg-slate-950 px-3 py-1.5 text-sm text-slate-100"
            />
          </label>
        )}

        {tradeMode === "paper" && (
          <p className="mt-3 text-xs text-amber-300/90">
            Paper mode — logged to your ledger only; no order reaches Schwab.
          </p>
        )}

        <label className="mt-3 flex items-start gap-2 text-xs text-slate-300">
          <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)}
                 className="mt-0.5 accent-rose-400" />
          {tradeMode === "paper"
            ? "I understand this closes and sells the whole position now, recorded to the ledger."
            : "I understand this transmits real orders to Schwab to close and sell the whole position now."}
        </label>

        {result && !result.ok && (
          <div className="mt-3 rounded-lg border border-rose-800 bg-rose-500/10 p-3 text-sm text-rose-200">
            <p className="font-semibold">Stopped partway through</p>
            <p className="mt-1 text-rose-300">{result.error}</p>
            {result.steps?.length > 0 && (
              <ul className="mt-2 space-y-0.5 text-xs">
                {result.steps.map((s, i) => (
                  <li key={i} className={s.ok ? "text-emerald-300" : "text-rose-300"}>
                    {s.ok ? "✓" : "✗"} {stepLabel(s)}{s.error ? ` — ${s.error}` : ""}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {error && !result && <p className="mt-2 text-xs text-rose-400">{error}</p>}

        <div className="mt-4 flex items-center justify-end gap-2">
          <button onClick={onClose} disabled={busy}
                  className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800 disabled:opacity-40">
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={!canSubmit}
            className="rounded-lg bg-rose-500/20 px-4 py-2 text-sm font-semibold text-rose-300 hover:bg-rose-500/30 disabled:opacity-40"
          >
            {busy ? "Exiting…" : `Exit position now${tradeMode === "paper" ? " (paper)" : ""}`}
          </button>
        </div>
      </div>
    </div>
  );
}
