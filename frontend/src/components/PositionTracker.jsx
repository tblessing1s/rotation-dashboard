import React from "react";
import { api } from "../api.js";
import { Card, Meter, Loading, Modal, Light, ChartLink, SleeveBadge, money, fmt, useApi } from "./ui.jsx";
import RollModal from "./RollModal.jsx";
import PortfolioRisk from "./PortfolioRisk.jsx";
import { useToast } from "./Toast.jsx";
import { explainRec } from "../recWhy.js";
import { submitOrder } from "../orderFlow.js";

// Reconciliation review panel — shown when the position has open diffs against
// the broker (state.json vs Schwab). A frozen position (needs_review) blocks new
// entries/rolls until resolved; closing it is always allowed. Each diff gets its
// resolution action: one-click expiry booking for the benign carve-out; a
// compensating adjustment (typed reason) or acknowledgement for everything else.
function ReviewPanel({ ticker, diffs, onDone }) {
  const toast = useToast();
  if (!diffs || diffs.length === 0) return null;
  return (
    <div className="mt-4 rounded-lg border border-rose-800 bg-rose-500/10 p-3">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-rose-300">
        Reconciliation review — state.json diverged from the broker
      </div>
      <div className="space-y-2">
        {diffs.map((d) => <DiffRow key={d.id} ticker={ticker} diff={d} toast={toast} onDone={onDone} />)}
      </div>
    </div>
  );
}

const CLASS_LABEL = {
  MATCH: "match",
  MISSING_AT_BROKER: "missing at broker",
  UNEXPECTED_AT_BROKER: "unexpected at broker",
  QUANTITY_MISMATCH: "quantity mismatch",
  SHORT_STOCK_DETECTED: "SHORT STOCK — assignment",
  EXPIRED_WORTHLESS_PENDING: "expired worthless",
};

function DiffRow({ ticker, diff, toast, onDone }) {
  const [busy, setBusy] = React.useState(false);
  const [form, setForm] = React.useState({
    instrument_type: diff.instrument_type || "OPTION",
    strike: diff.strike ?? "",
    quantity_delta: "",
    reason: "",
  });
  const benign = diff.classification === "EXPIRED_WORTHLESS_PENDING";
  const critical = diff.classification === "SHORT_STOCK_DETECTED";

  const run = async (fn) => {
    setBusy(true);
    try { await fn(); onDone && onDone(); }
    catch (e) { toast.show(String(e.message || e), { type: "error" }); }
    finally { setBusy(false); }
  };

  const bookExpiry = () => run(async () => {
    await api.resolveExpiry(diff.id);
    toast.show(`Booked ${ticker} ${diff.strike} expiry at $0.00`, { type: "success" });
  });

  const submitAdjustment = () => run(async () => {
    if (!form.reason.trim()) throw new Error("a typed reason is required");
    if (form.quantity_delta === "") throw new Error("quantity delta is required");
    await api.execute({
      action: "adjustment", ticker,
      instrument_type: form.instrument_type,
      strike: form.strike === "" ? null : Number(form.strike),
      quantity_delta: Number(form.quantity_delta),
      reason: form.reason.trim(),
      linked_diff_id: diff.id,
    });
    toast.show(`Recorded adjustment for ${ticker}`, { type: "success" });
  });

  const acknowledge = () => {
    const reason = window.prompt(
      "Acknowledge this diff as a non-issue — a typed reason is required (logged):");
    if (!reason || !reason.trim()) return;
    run(async () => {
      await api.acknowledgeDiff(diff.id, reason.trim());
      toast.show("Diff acknowledged", { type: "success" });
    });
  };

  return (
    <div className={`rounded-lg border px-3 py-2 text-sm ${
      critical ? "border-rose-600 bg-rose-500/10" : benign ? "border-amber-700 bg-amber-500/5" : "border-slate-700 bg-slate-950/60"}`}>
      <div className="flex items-center gap-2">
        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase ${
          critical ? "border-rose-500/50 bg-rose-500/20 text-rose-200"
            : benign ? "border-amber-500/40 bg-amber-500/15 text-amber-300"
            : "border-slate-600 bg-slate-800/60 text-slate-300"}`}>
          {CLASS_LABEL[diff.classification] || diff.classification}
        </span>
        <span className="text-xs text-slate-500">{diff.id}</span>
      </div>
      <p className="mt-1 text-slate-300">{diff.summary}</p>
      {critical && (
        <p className="mt-1 text-xs font-medium text-rose-300">
          Short stock is naked risk — buy back the short shares or close the position.
        </p>
      )}

      {benign ? (
        <button onClick={bookExpiry} disabled={busy}
                className="mt-2 rounded-lg border border-amber-700 bg-amber-500/10 px-3 py-1 text-xs font-semibold text-amber-200 hover:bg-amber-500/20 disabled:opacity-50">
          {busy ? "Booking…" : "Book expiry at $0.00"}
        </button>
      ) : (
        <div className="mt-2 flex flex-wrap items-end gap-2">
          <label className="flex flex-col text-[10px] uppercase tracking-wide text-slate-500">
            leg
            <select value={form.instrument_type}
                    onChange={(e) => setForm((f) => ({ ...f, instrument_type: e.target.value }))}
                    className="mt-0.5 rounded border border-slate-700 bg-slate-900 px-1.5 py-1 text-sm text-slate-200">
              <option value="OPTION">OPTION</option>
              <option value="EQUITY">EQUITY</option>
            </select>
          </label>
          {form.instrument_type === "OPTION" && (
            <label className="flex flex-col text-[10px] uppercase tracking-wide text-slate-500">
              strike
              <input value={form.strike} onChange={(e) => setForm((f) => ({ ...f, strike: e.target.value }))}
                     className="mt-0.5 w-20 rounded border border-slate-700 bg-slate-900 px-1.5 py-1 text-sm text-slate-100" />
            </label>
          )}
          <label className="flex flex-col text-[10px] uppercase tracking-wide text-slate-500">
            qty Δ (signed)
            <input value={form.quantity_delta} onChange={(e) => setForm((f) => ({ ...f, quantity_delta: e.target.value }))}
                   placeholder="+5 / -500"
                   className="mt-0.5 w-24 rounded border border-slate-700 bg-slate-900 px-1.5 py-1 text-sm text-slate-100 placeholder:text-slate-600" />
          </label>
          <label className="flex min-w-[12rem] flex-1 flex-col text-[10px] uppercase tracking-wide text-slate-500">
            reason (required)
            <input value={form.reason} onChange={(e) => setForm((f) => ({ ...f, reason: e.target.value }))}
                   placeholder="e.g. assignment booked; short stock bought back"
                   className="mt-0.5 rounded border border-slate-700 bg-slate-900 px-1.5 py-1 text-sm text-slate-100 placeholder:text-slate-600" />
          </label>
          <button onClick={submitAdjustment} disabled={busy}
                  className="rounded-lg border border-emerald-700 bg-emerald-500/10 px-3 py-1 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-50">
            {busy ? "Recording…" : "Record adjustment"}
          </button>
          <button onClick={acknowledge} disabled={busy}
                  title="Mark this diff a non-issue (typed reason logged) without an execution"
                  className="rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50">
            Acknowledge
          </button>
        </div>
      )}
    </div>
  );
}

// Defensive roll-down recommendation, shown when a short strike is breached.
function DefendPanel({ ticker, onStage }) {
  const { data } = useApi(React.useCallback(() => api.defend(ticker), [ticker]), [ticker], null);
  if (!data || !data.breached) return null;
  return (
    <div className="mt-3 rounded-lg border border-rose-800 bg-rose-500/10 p-3 text-sm">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-rose-300">
          Defend — stock {fmt(data.stock_price, 2)} below the {fmt(data.current_short?.strike, 2)} short
        </span>
        <button
          onClick={onStage}
          className="rounded-lg border border-rose-700 bg-rose-500/10 px-3 py-1 text-xs font-semibold text-rose-200 hover:bg-rose-500/20"
        >
          Stage defensive roll
        </button>
      </div>
      <p className="mt-1 text-slate-300">
        Roll down to <span className="font-semibold text-slate-100">{fmt(data.recommended_strike, 2)}</span>{" "}
        ({data.regime?.toUpperCase()} / {data.posture}: {data.atr_mult}×ATR {fmt(data.atr, 2)}
        {data.itm_pct != null ? ` / ${(data.itm_pct * 100).toFixed(0)}% ITM floor` : ""})
        {data.net_total != null && (
          <> · est. net {data.net_total >= 0 ? "credit" : "debit"}{" "}
            <span className={data.net_total >= 0 ? "text-emerald-300" : "text-rose-300"}>
              {money(Math.abs(data.net_total))}
            </span>
          </>
        )}
        {data.new_extrinsic_per_share != null && <> · new extrinsic {fmt(data.new_extrinsic_per_share, 2)}/sh</>}
        {data.cost_basis_effect != null && (
          <> · cost basis {data.cost_basis_effect <= 0 ? "−" : "+"}{money(Math.abs(data.cost_basis_effect))}</>
        )}
      </p>
      {data.whipsaw?.tripped && (
        <p className="mt-2 rounded border border-rose-500/50 bg-rose-500/15 px-2 py-1 text-xs font-medium text-rose-200">
          ⚠ Whipsaw — consider EXITing instead of rolling again ({(data.whipsaw.reasons || []).join("; ")}).
          Another roll-down just locks a lower strike.
        </p>
      )}
      <p className="mt-0.5 text-xs text-slate-500">
        Estimated from trailing vol — the staged roll re-prices from the live chain.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Recommendation trust layer — engine-emitted action cards on the position.
// Display + staging only: Execute routes into the EXISTING roll flow (the same
// cfm-action intent AlertsPanel deep links dispatch, extended with rec_id so
// the fill carries source_rec_id back to the recommendation); Dismiss appends
// an immutable coded override record. Nothing here places an order on its own.
// ---------------------------------------------------------------------------
const REC_BADGE = {
  EXIT: "border-rose-500/40 bg-rose-500/15 text-rose-300",
  DEFEND: "border-amber-500/40 bg-amber-500/15 text-amber-300",
  ROLL_OUT: "border-sky-500/40 bg-sky-500/15 text-sky-300",
  ROLL_DOWN: "border-amber-500/40 bg-amber-500/15 text-amber-300",
  ENTER: "border-emerald-500/40 bg-emerald-500/15 text-emerald-300",
};

// The 6 coded override reasons the backend accepts (OTHER requires a note).
const OVERRIDE_REASONS = [
  ["DISAGREE_TIMING", "Disagree with the timing"],
  ["DISAGREE_STRIKE", "Disagree with the strike"],
  ["DISAGREE_ACTION", "Disagree with the action itself"],
  ["EXTERNAL_INFO", "Acting on external information"],
  ["DISCIPLINE_LAPSE", "Discipline lapse (logged honestly)"],
  ["OTHER", "Other — typed note required"],
];

// Minute tick shared by the validity countdowns — live-ish without per-card timers.
function useNow(intervalMs = 60000) {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

// "valid 26h" while comfortable, "expires in 3h" once inside 6h, "expired" past it.
function validity(validUntil, now) {
  const t = Date.parse(validUntil || "");
  if (Number.isNaN(t)) return null;
  const hrs = (t - now) / 3600000;
  if (hrs <= 0) return { text: "expired", tone: "text-rose-400" };
  if (hrs < 1) return { text: `expires in ${Math.max(1, Math.round(hrs * 60))}m`, tone: "text-amber-300" };
  if (hrs <= 6) return { text: `expires in ${Math.round(hrs)}h`, tone: "text-amber-300" };
  return { text: `valid ${Math.round(hrs)}h`, tone: "text-slate-500" };
}

const SETTLE_REASON_LABEL = {
  SETTLE_WINDOW: "post-open settle window",
  ENTRY_WINDOW: "entry window",
  CLOSE_BLACKOUT: "close blackout",
  MARKET_CLOSED: "market closed",
};

// Interpret the rec's settle block (the market-settle gate deferred the ORDER; the
// alert already fired). Returns null when there is no settle state to show.
function settleInfo(settle, now) {
  if (!settle || !settle.status) return null;
  if (settle.status !== "PENDING_SETTLE") return { status: settle.status };
  const t = Date.parse(settle.executable_at || "");
  const etTime = Number.isNaN(t)
    ? null
    : new Intl.DateTimeFormat("en-US", {
        hour: "numeric", minute: "2-digit", timeZone: "America/New_York", hour12: false,
      }).format(t);
  const mins = Number.isNaN(t) ? null : Math.round((t - now) / 60000);
  return {
    status: "PENDING_SETTLE",
    etTime,
    countdown: mins == null ? "" : mins > 0 ? `in ${mins}m` : "now",
    preApproved: !!settle.pre_approved,
    reason: SETTLE_REASON_LABEL[settle.reason] || settle.reason,
  };
}

// One-line ticket read: legs (instruction + strike + expiry) · order type · est net.
function ticketSummary(t) {
  if (!t) return "no ticket attached";
  const legs = (t.legs || [])
    .map((l) => {
      const when = l.expiration ? ` exp ${l.expiration}` : l.dte != null ? ` ${l.dte} DTE` : "";
      // A shares leg has no strike — its size IS the leg (100 shares, delta 1.0).
      const what = l.role === "shares" ? `${fmt(l.quantity, 0)} shares` : fmt(l.strike, 2);
      return `${(l.instruction || "").replaceAll("_", " ")} ${what}${when}`;
    })
    .join(" / ");
  // Each ticket shape nets under its own key: the LEAP exit/roll per share, the
  // shares exit as equity proceeds less the option buyback, the shares ENTRY as
  // a debit (share cost less the premium collected) — negated here so the
  // credit/debit wording below reads off one signed number.
  const est = t.estimates || {};
  const net = est.net_per_share != null ? est.net_per_share
    : est.net_credit_per_share != null ? est.net_credit_per_share
    : est.net_debit_per_share != null ? -Math.abs(est.net_debit_per_share)
    : null;
  const netStr = net != null
    ? `est ${net < 0 ? "−" : ""}$${Math.abs(Number(net)).toFixed(2)}/sh ${net >= 0 ? "credit" : "debit"}`
    : "unpriced";
  // What the lot actually COSTS — the number an entry decision turns on, and the
  // one thing a per-share figure hides (a $220/sh name and a $960/sh name read
  // alike until you see $22k next to $96k).
  const lot = t.estimates?.shares_notional;
  const lotStr = lot != null ? `${money(lot)} lot` : null;
  return [legs, (t.order_type || "").replaceAll("_", " "), lotStr, netStr]
    .filter(Boolean).join(" · ");
}

// Dismissal modal: one coded reason is mandatory; OTHER additionally demands a
// typed note (the backend 400s without one — the submit stays disabled until then).
function DismissRecModal({ rec, onClose, onDismissed }) {
  const toast = useToast();
  const [reason, setReason] = React.useState(null);
  const [note, setNote] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const noteRequired = reason === "OTHER";
  const canSubmit = !!reason && !(noteRequired && !note.trim()) && !busy;

  async function submit() {
    setBusy(true);
    try {
      await api.dismissRecommendation(rec.rec_id, reason, note.trim() || undefined);
      toast.show(`Dismissed ${rec.action_type} on ${rec.ticker} (${reason})`, { type: "success" });
      onDismissed?.();
      onClose?.();
    } catch (e) {
      toast.show(String(e.message || e), { type: "error" });
      setBusy(false);
    }
  }

  return (
    <Modal onClose={onClose} maxWidth="max-w-md">
      <Card title={`Dismiss recommendation — ${rec.ticker}`}>
        <p className="text-xs text-slate-500">
          {rec.action_type} · <span className="font-mono">{rec.trigger_rule}</span> ·{" "}
          <span className="font-mono">{rec.rec_id}</span>
        </p>
        <p className="mt-1 text-xs text-slate-400">
          A dismissal writes an immutable override record that feeds the trust scoreboard —
          pick the coded reason that honestly describes why you're not taking this action.
        </p>
        <div className="mt-3 space-y-1">
          {OVERRIDE_REASONS.map(([code, label]) => (
            <label
              key={code}
              className={`flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-sm ${
                reason === code ? "bg-sky-500/10" : "hover:bg-slate-800/50"
              }`}
            >
              <input
                type="radio" name={`dismiss-${rec.rec_id}`} checked={reason === code}
                onChange={() => setReason(code)} className="accent-sky-400"
              />
              <span className="text-slate-200">{label}</span>
              <span className="ml-auto font-mono text-[10px] text-slate-500">{code}</span>
            </label>
          ))}
        </div>
        <label className="mt-3 block text-[10px] uppercase tracking-wide text-slate-500">
          note{noteRequired ? " — required for OTHER" : " (optional)"}
          <textarea
            value={note} onChange={(e) => setNote(e.target.value)} rows={2}
            placeholder={noteRequired ? "A typed note is required for OTHER." : "Optional context…"}
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm normal-case tracking-normal text-slate-100 placeholder:text-slate-600"
          />
        </label>
        <div className="mt-3 flex items-center justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800"
          >
            Cancel
          </button>
          <button
            onClick={submit} disabled={!canSubmit}
            className="rounded-lg border border-rose-700 bg-rose-500/10 px-3 py-1.5 text-sm font-semibold text-rose-300 hover:bg-rose-500/20 disabled:opacity-40"
          >
            {busy ? "Dismissing…" : "Dismiss"}
          </button>
        </div>
      </Card>
    </Modal>
  );
}

// One actionable recommendation on its position card.
function RecCard({ rec, now, expanded, onToggleDetail, onExecute, onDismiss, onPreapprove,
                  showTicker = false }) {
  const v = validity(rec.valid_until, now);
  const t = rec.proposed_ticket;
  const badge = REC_BADGE[rec.action_type] || "border-slate-600 bg-slate-800/60 text-slate-300";
  const s = settleInfo(rec.settle, now);
  const pending = s?.status === "PENDING_SETTLE";
  // The plain-English read: which rule fired, what it saw (with the numbers),
  // and what taking the action does. Derived from the rec's frozen snapshot.
  const why = explainRec(rec);
  return (
    <div className="rounded-lg border border-slate-700 bg-slate-950/60 px-3 py-2 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        {/* Under a position card the ticker is in the header above; an ENTER rec
            has no position, so without this the card never says which name it
            is proposing. */}
        {showTicker && (
          <span className="font-semibold text-slate-100">{rec.ticker}</span>
        )}
        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${badge}`}>
          {(rec.action_type || "").replaceAll("_", " ")}
        </span>
        <span className="text-xs font-medium text-slate-300" title={`Trigger rule: ${rec.trigger_rule}`}>
          {why?.label || rec.trigger_rule}
        </span>
        {v && (
          <span className={`ml-auto text-xs ${v.tone}`} title={`valid until ${rec.valid_until}`}>
            {v.text}
          </span>
        )}
      </div>
      {why?.why && (
        <div className="mt-1.5 rounded-md border border-slate-800 bg-slate-900/50 px-2.5 py-1.5">
          <p className="text-xs leading-relaxed text-slate-200">{why.why}</p>
          {why.effect && (
            <p className="mt-1 text-[11px] text-emerald-300/90">
              <span className="font-semibold">{why.action}:</span> {why.effect}
            </p>
          )}
          {(why.numbers.length > 0 || why.also.length > 0) && (
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {why.numbers.map((n, i) => (
                <span key={i}
                      className="rounded-full border border-slate-700 bg-slate-950/60 px-2 py-0.5 text-[10px] text-slate-300">
                  <span className="text-slate-500">{n.k} </span>
                  <span className="font-semibold text-slate-100">{n.v}</span>
                </span>
              ))}
              {why.also.length > 0 && (
                <span className="rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-200"
                      title="Other rules that fired on the same pass; the card acts on the dominant one">
                  also: {why.also.join(", ")}
                </span>
              )}
            </div>
          )}
        </div>
      )}
      {pending && (
        <div className="mt-1.5 flex flex-wrap items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-200">
          <span title="The market-settle gate deferred the order; the alert already fired.">
            ⏳ Staged — executable{s.etTime ? ` ${s.etTime} ET` : " next session"}
            {s.countdown ? ` (${s.countdown})` : ""}
          </span>
          {s.reason && <span className="text-amber-300/70">· {s.reason}</span>}
          {s.preApproved && <span className="font-semibold text-emerald-300">· pre-approved</span>}
        </div>
      )}
      {!pending && s && (
        <div className="mt-1.5 text-[11px] text-slate-500">
          settle: {(s.status || "").replaceAll("_", " ").toLowerCase()}
        </div>
      )}
      <p className="mt-1 text-xs text-slate-300">{ticketSummary(t)}</p>
      {expanded && t && (
        <div className="mt-2 rounded-lg border border-slate-800 bg-slate-900/60 p-2 text-xs text-slate-300">
          <div className="text-[10px] uppercase tracking-wide text-slate-500">Proposed ticket</div>
          <ul className="mt-1 space-y-0.5">
            {(t.legs || []).map((l, i) => (
              <li key={i}>
                {(l.instruction || "").replaceAll("_", " ")}{" "}
                {l.role === "shares"
                  ? `${fmt(l.quantity, 0)} shares`
                  : `${l.quantity != null ? `${l.quantity}× ` : ""}${fmt(l.strike, 2)}`}
                {l.expiration ? ` exp ${l.expiration}` : ""}{l.dte != null ? ` (${l.dte} DTE)` : ""}
                {l.role ? <span className="text-slate-500"> · {l.role}</span> : null}
              </li>
            ))}
          </ul>
          <div className="mt-1 text-slate-400">
            {(t.order_type || "").replaceAll("_", " ")}
            {/* An EQUITY ticket is two orders (the equity leg plus its option
                cover), so it carries no single net limit — say that, rather than
                rendering a priced ticket as "limit unpriced". */}
            {t.order_type === "EQUITY"
              ? <> · two orders — the option leg re-prices from the live chain</>
              : <>{" · "}limit {t.limit_price != null ? `$${Number(t.limit_price).toFixed(2)}/sh` : "unpriced"}</>}
            {t.min_acceptable_net_credit != null && <> · min net ${Number(t.min_acceptable_net_credit).toFixed(2)}/sh</>}
            {t.max_slippage_pct_of_mid != null && <> · max slip {t.max_slippage_pct_of_mid}% of mid</>}
            {t.price_source && <> · priced from {t.price_source}</>}
          </div>
        </div>
      )}
      <div className="mt-2 flex items-center gap-2">
        {pending ? (
          <button
            onClick={() => onPreapprove(rec, !s.preApproved)}
            title="Auto-submit when the settle window opens — only if the trigger still holds then."
            className={`rounded-full border px-2.5 py-0.5 text-xs font-semibold ${
              s.preApproved
                ? "border-emerald-600/60 bg-emerald-500/20 text-emerald-200 hover:bg-emerald-500/30"
                : "border-amber-600/50 bg-amber-500/10 text-amber-200 hover:bg-amber-500/20"
            }`}
          >
            {s.preApproved ? "✓ Pre-approved" : "Pre-approve"}
          </button>
        ) : (
          <button
            onClick={onExecute}
            className="rounded-full border border-emerald-600/50 bg-emerald-500/10 px-2.5 py-0.5 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20"
          >
            Execute
          </button>
        )}
        <button
          onClick={onDismiss}
          className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-0.5 text-xs text-slate-300 hover:bg-slate-800"
        >
          Dismiss
        </button>
        {t && (
          <button
            onClick={onToggleDetail}
            className="ml-auto text-[11px] text-slate-500 hover:text-slate-300"
          >
            {expanded ? "hide ticket ▲" : "ticket details ▼"}
          </button>
        )}
      </div>
    </div>
  );
}

// Open ENTER recommendations, which have NO POSITION to hang under — the engine
// emits them against a ticker the book does not hold yet. They used to render
// nowhere: the strip below is inside positions.map, so an ENTER had no card, was
// counted in the Overview digest's "review on Positions" pointer, and could only
// resolve by expiring.
//
// They live here rather than on Scan deliberately. This is a COMMITTED CLAIM the
// engine made before the operator acted, and it is graded on that basis — a
// different thing from the scan's ranked shortlist, whose panel is built to a
// pressure guard that forbids flagging any row as the action to take. Keeping the
// two apart preserves both: the shortlist stays a ranking, this stays a claim.
function ProposedEntries({ recs, onRecsChanged, onEnter }) {
  const now = useNow();
  const [dismissing, setDismissing] = React.useState(null);
  const [detailId, setDetailId] = React.useState(null);
  const list = (recs || []).filter((r) => r.action_type === "ENTER");
  if (list.length === 0) return null;

  // Execute opens the entry-gate + order ticket for the ticker, carrying the rec
  // id so the resulting buy_shares execution is stamped source_rec_id. No new
  // order path: it is the same flow a scan pick opens.
  const execute = (rec) =>
    onEnter?.(rec.ticker, rec.rec_id);

  async function preapprove(rec, approve) {
    try {
      await api.preapproveRecommendation(rec.rec_id, approve);
      onRecsChanged?.();
    } catch (e) {
      /* the card keeps its current state; the next poll re-syncs */
    }
  }

  return (
    <Card title={`Proposed entries (${list.length})`}>
      <p className="mb-2 text-[11px] text-slate-500">
        The engine committed to these before you acted — every gate clear on a name
        you don&rsquo;t hold. Executing one stages the entry ticket; dismissing one
        records why. Either way the claim is graded.
      </p>
      <div className="space-y-2">
        {list.map((rec) => (
          <RecCard
            key={rec.rec_id} rec={rec} now={now}
            expanded={detailId === rec.rec_id}
            onToggleDetail={() => setDetailId((id) => (id === rec.rec_id ? null : rec.rec_id))}
            onExecute={() => execute(rec)}
            onDismiss={() => setDismissing(rec)}
            onPreapprove={preapprove}
            showTicker
          />
        ))}
      </div>
      {dismissing && (
        <DismissRecModal
          rec={dismissing}
          onClose={() => setDismissing(null)}
          onDismissed={() => { setDismissing(null); onRecsChanged?.(); }}
        />
      )}
    </Card>
  );
}


// The recommendation strip under a position's header. Actionable recs (never
// NO_ACTION) render as cards; when the ONLY open rec is an ALL_CLEAR/NO_ACTION,
// a single muted line says so with the valid-until time.
function RecSection({ p, recs, onRecsChanged, focusCard }) {
  const now = useNow();
  const toast = useToast();
  const [dismissing, setDismissing] = React.useState(null); // rec being dismissed
  const [detailId, setDetailId] = React.useState(null); // rec_id with ticket expanded
  const list = recs || [];
  const actionable = list.filter((r) => r.action_type !== "NO_ACTION");
  if (list.length === 0) return null;

  if (actionable.length === 0) {
    const r = list[list.length - 1];
    return (
      <div className="px-4 pb-2 text-[11px] text-slate-600">
        engine: all clear · valid until {(r.valid_until || "").slice(0, 16).replace("T", " ")}Z
      </div>
    );
  }

  function execute(rec) {
    if (rec.action_type === "EXIT") {
      // There is no in-app exit modal on this page (exits go through the order
      // ticket) — focus the card and show the full proposed ticket instead of
      // building a new order path.
      focusCard(p.ticker);
      setDetailId(rec.rec_id);
      return;
    }
    // ROLL_OUT / ROLL_DOWN / DEFEND ride the existing roll-staging intent — the
    // same cfm-action event AlertsPanel deep links dispatch (App.jsx listener →
    // positionIntent → RollModal), extended with rec_id so the /api/execute
    // payload carries source_rec_id.
    window.dispatchEvent(new CustomEvent("cfm-action", {
      detail: {
        action: "roll",
        ticker: rec.ticker,
        reason: rec.proposed_ticket?.roll_reason
          || (rec.action_type === "DEFEND" ? "defend" : "scheduled"),
        rec_id: rec.rec_id,
      },
    }));
  }

  async function preapprove(rec, approve) {
    try {
      await api.preapproveRecommendation(rec.rec_id, approve);
      onRecsChanged?.();
    } catch (e) {
      toast?.show?.(`Pre-approve failed: ${e.message || e}`, { type: "error" });
    }
  }

  return (
    <div className="space-y-2 px-4 pb-3">
      {actionable.map((rec) => (
        <RecCard
          key={rec.rec_id} rec={rec} now={now}
          expanded={detailId === rec.rec_id}
          onToggleDetail={() => setDetailId((id) => (id === rec.rec_id ? null : rec.rec_id))}
          onExecute={() => execute(rec)}
          onDismiss={() => setDismissing(rec)}
          onPreapprove={preapprove}
        />
      ))}
      {dismissing && (
        <DismissRecModal
          rec={dismissing}
          onClose={() => setDismissing(null)}
          onDismissed={onRecsChanged}
        />
      )}
    </div>
  );
}

// Accrual toward the next 100-share lot (schema v21, TRAVIS_EXTENSION).
//
// The balance is REALIZED extrinsic at cycle close plus received dividends —
// nothing else. Roll-down credits are excluded by construction (they are a
// deferred intrinsic obligation, and compounding them is the Martingale trap),
// as are unrealized juice and every intrinsic component.
//
// Accrued cash is CASH, never exposure: it changes no covered-call math until a
// whole lot is actually bought, and the app never auto-executes the add.
function AccrualProgress({ accrual }) {
  if (!accrual || accrual.threshold == null) return null;
  const src = accrual.by_source || {};
  const ready = accrual.ready;
  return (
    <div className="mt-4 border-t border-slate-800 pt-3">
      <div className="mb-1.5 flex items-center justify-between text-xs">
        <span className="uppercase tracking-wide text-slate-500">Accrual — next lot</span>
        <span
          className="cursor-help tabular-nums text-slate-400"
          title={[
            `realized extrinsic ${money(src.REALIZED_EXTRINSIC || 0)}`,
            `dividends ${money(src.DIVIDEND || 0)}`,
            "Excluded by construction: roll-down credits (deferred intrinsic obligation),",
            "unrealized juice, and any intrinsic component.",
          ].join("\n")}
        >
          {money(accrual.accrued_cash)} / {money(accrual.threshold)}
        </span>
      </div>
      <Meter pct={accrual.pct_to_next_lot} tone={ready ? "bg-emerald-400" : "bg-sky-500/70"} />
      <p className="mt-1 text-[11px] text-slate-500">
        {ready ? (
          <span className="text-emerald-300">
            Enough for another {accrual.shares_per_lot}-share lot. Whether the add clears
            the Level 5 gate is checked when you act — and never auto-executed.
          </span>
        ) : (
          <>{money(accrual.remaining)} more toward another {accrual.shares_per_lot}-share lot
            {accrual.lot_cost != null && <> (lot {money(accrual.lot_cost)} + {fmt((accrual.buffer_pct || 0) * 100, 0)}% buffer)</>}.
          </>
        )}
      </p>
    </div>
  );
}

// Open shorts — the weekly income engine: each short's extrinsic capture (the
// juice we're collecting) with its roll/assignment flags, each rollable in place.
function ShortCalls({ p, shorts, setRolling, onOpenTicket }) {
  return (
    <div className="mt-4 border-t border-slate-800 pt-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs uppercase tracking-wide text-slate-500">
          Short calls — juice captured
          {p.roll_summary?.count > 0 && (
            <span
              className="ml-2 normal-case tracking-normal text-slate-500"
              title="Cumulative roll net across this position (drag = debits paid on defensive rolls)"
            >
              {p.roll_summary.count} roll(s) · net {money(p.roll_summary.net_total)}
              {p.roll_summary.drag_total < 0 && (
                <span className="text-rose-400"> · drag {money(p.roll_summary.drag_total)}</span>
              )}
            </span>
          )}
        </span>
        {shorts.length > 0 && (
          <button
            onClick={() => setRolling({ ticker: p.ticker, reason: "scheduled" })}
            className="rounded-lg border border-sky-700 bg-sky-500/10 px-3 py-1 text-xs font-semibold text-sky-300 hover:bg-sky-500/20"
          >
            Roll short
          </button>
        )}
      </div>
      {shorts.length === 0 ? (
        <div className="flex items-center justify-between">
          <p className="text-xs text-slate-500">No open short — sell this week's call.</p>
          {onOpenTicket && (
            <button
              onClick={() => onOpenTicket(p.ticker)}
              className="rounded-lg border border-sky-700 bg-sky-500/10 px-3 py-1 text-xs font-semibold text-sky-300 hover:bg-sky-500/20"
            >
              Open order ticket
            </button>
          )}
        </div>
      ) : (
        <div className="space-y-1.5">
          {shorts.map((sc, i) => (
            <div key={i} className="rounded-lg bg-slate-950/60 px-3 py-1.5 text-sm">
             <div className="flex items-center justify-between">
              <span className="text-slate-200">
                {fmt(sc.strike, 2)}C · {sc.contracts}c
                {sc.expiration ? ` · exp ${sc.expiration}` : ""}
                {sc.dte != null ? ` · ${sc.dte} DTE` : ""}
                {sc.decay_pct != null && (
                  <span
                    className={`ml-2 text-xs ${sc.decay_pct >= 75 ? "text-emerald-300" : "text-slate-500"}`}
                    title={`Sold ${fmt(sc.sold_per_share, 2)}/sh, now ${fmt(sc.current_bid, 2)}/sh`}
                  >
                    {fmt(sc.decay_pct, 0)}% decayed
                  </span>
                )}
              </span>
              <span className="flex items-center gap-1.5">
                {sc.roll_now && (
                  <button
                    onClick={() => setRolling({ ticker: p.ticker, reason: "75%-rule" })}
                    title="75% buyback rule: ≥75% of the sale premium captured with >2 DTE — roll early to capture juice"
                    className="rounded-full border border-emerald-500/50 bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold text-emerald-300 hover:bg-emerald-500/25"
                  >
                    ROLL NOW
                  </button>
                )}
                {sc.below_strike && (
                  <span className="rounded-full border border-rose-500/40 bg-rose-500/15 px-2 py-0.5 text-[10px] font-medium text-rose-300">
                    below strike
                  </span>
                )}
                {sc.assignment_risk && (
                  /* The dividend-triggered variant (EARLY_ASSIGNMENT_RISK) is
                     time-critical — it fires on a specific ex-date — so it carries
                     DEFENSE-level severity styling, the same rose treatment as
                     "below strike". The bare extrinsic-collapse trigger has no
                     deadline and stays amber. */
                  <span
                    title={sc.assignment_risk.note}
                    className={`cursor-help rounded-full border px-2 py-0.5 text-[10px] font-medium ${
                      sc.assignment_risk.trigger === "dividend"
                        ? "border-rose-500/50 bg-rose-500/15 text-rose-300"
                        : "border-amber-500/40 bg-amber-500/15 text-amber-300"}`}
                  >
                    {sc.assignment_risk.trigger === "extrinsic"
                      ? `assignment risk (extrinsic ${fmt(sc.assignment_risk.extrinsic, 2)})`
                      : `early assignment risk — roll before ex ${sc.assignment_risk.ex_date} (div ${fmt(sc.assignment_risk.dividend, 2)} > extrinsic ${fmt(sc.assignment_risk.extrinsic, 2)})`}
                  </span>
                )}
                {sc.dte != null && sc.dte <= 2 && (
                  <span className="rounded-full border border-amber-500/40 bg-amber-500/15 px-2 py-0.5 text-[10px] font-medium text-amber-300">expiring</span>
                )}
              </span>
             </div>
             <div className="mt-0.5 text-[11px]">
               <StrikeGap leg={sc} />
             </div>
             {/* Extrinsic capture: what you sold (the target), how much theta
                 you've collected back so far, and what's left. An ITM short's
                 premium is intrinsic (tracks the stock) + extrinsic (the juice) —
                 this isolates the juice. */}
             {sc.entry_extrinsic_total != null && (
               <div className="mt-1.5">
                 <div className="flex items-center justify-between text-[11px]">
                   <span
                     className="text-slate-500"
                     title="Extrinsic (time value) sold at entry is the target; capturing all of it is max profit on the short. When the current time value has risen ABOVE what you sold (an IV event), there's nothing captured yet and the leg is underwater — the figure shown is what it's worth now, not what's left to collect."
                   >
                     extrinsic captured {money(sc.extrinsic_captured_total)} / {money(sc.entry_extrinsic_total)}
                     {sc.extrinsic_remaining_total != null && (
                       sc.extrinsic_above_entry ? (
                         <span className="text-amber-400/80"> · now worth {money(sc.extrinsic_remaining_total)} (rose above the {money(sc.entry_extrinsic_total)} sold)</span>
                       ) : (
                         <span className="text-slate-600"> · {money(sc.extrinsic_remaining_total)} left</span>
                       )
                     )}
                   </span>
                   {sc.extrinsic_above_entry ? (
                     /* The short's extrinsic has risen ABOVE what we sold it at (a
                        vol/IV event) — the clamped "% captured" would read 0% and
                        hide it. Surface the signed raw figure and a clear flag so an
                        underwater leg is visible at defend-decision time. */
                     <span
                       className="text-amber-300"
                       title="Current extrinsic is above the extrinsic sold at entry — the short leg is underwater on time value (an IV event). The payout meter floors this at 0% captured; this is the honest signed figure."
                     >
                       {sc.extrinsic_captured_pct_raw != null
                         ? `${fmt(sc.extrinsic_captured_pct_raw, 0)}% captured`
                         : "underwater"}
                     </span>
                   ) : (
                     sc.extrinsic_captured_pct != null && (
                       <span className={sc.extrinsic_captured_pct >= 75 ? "text-emerald-300" : "text-slate-400"}>
                         {fmt(sc.extrinsic_captured_pct, 0)}% captured
                       </span>
                     )
                   )}
                 </div>
                 {sc.extrinsic_above_entry && (
                   <div className="mt-0.5 text-[10px] text-amber-400/90">
                     extrinsic above entry (IV event) — leg underwater on time value
                   </div>
                 )}
                 <div className="mt-1">
                   <Meter
                     pct={sc.extrinsic_above_entry ? 0 : sc.extrinsic_captured_pct}
                     tone={sc.extrinsic_above_entry
                       ? "bg-amber-500"
                       : sc.extrinsic_captured_pct >= 75 ? "bg-emerald-400" : "bg-sky-500"}
                   />
                 </div>
               </div>
             )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// One position, restructured around the two questions the page answers: is it
// healthy, and how much income is it making. Verdict + income lead; long-leg
// internals, structure, deltas and relative strength are one tap away. Active
// alerts (reconciliation, defend, whipsaw, a tripped kill switch) never hide.
// Spot against ONE leg's strike — the reading an operator makes first on a short:
// how far is the stock from the price at which this leg starts to matter.
//
// The sign convention comes from the server (position_manager.strike_gap), so a
// call and a put can't disagree about which side is in the money; this only says
// it in words. For a covered call, ITM is called-away territory and the gap is
// what a roll has to climb over; OTM, the gap is the cushion the stock has before
// that becomes true.
function StrikeGap({ leg, put = false, className = "" }) {
  const spot = leg.stock_price;
  const gap = leg.strike_distance;
  if (spot == null || gap == null) {
    return (
      <span className={`text-slate-600 ${className}`} title="No live price for this name yet">
        spot unavailable
      </span>
    );
  }
  const itm = leg.itm === true;
  const above = gap > 0;
  const pct = leg.strike_distance_pct;
  const tone = itm ? "text-amber-300" : "text-slate-400";
  const meaning = put
    ? (itm ? "below the strike — assignment territory"
           : "above the strike — clear of assignment")
    : (itm ? "above the strike — called away at expiry unless rolled"
           : "below the strike — cushion before assignment");
  return (
    <span
      className={`${tone} ${className}`}
      title={`Stock ${fmt(spot, 2)} vs the ${fmt(leg.strike, 2)} strike: ${meaning}.`}
    >
      spot {fmt(spot, 2)} · {dollars(Math.abs(gap))}
      {pct == null ? "" : ` (${fmt(Math.abs(pct), 1)}%)`}{" "}
      {above ? "above" : "below"} the {fmt(leg.strike, 2)} strike
      <span className="ml-1 font-semibold">· {leg.moneyness || (itm ? "ITM" : "OTM")}</span>
    </span>
  );
}

// Aggregate short-call capture: how much of the extrinsic sold across every open
// short has decayed into our pocket. Null when no short carries entry extrinsic.
function shortCapturePct(shorts) {
  let captured = 0, entry = 0, any = false;
  for (const sc of shorts || []) {
    if (sc.entry_extrinsic_total != null) {
      entry += Number(sc.entry_extrinsic_total);
      captured += Number(sc.extrinsic_captured_total || 0);
      any = true;
    }
  }
  if (!any) return null;
  return entry > 0 ? (captured / entry) * 100 : 0;
}

// Exact dollars-and-cents for the validation breakdowns. money() rounds to whole
// dollars for the headline tiles; reconciling a figure against a broker statement
// needs the cents, so the "show the math" panels use these instead.
function dollars(n) {
  if (n == null) return "—";
  return "$" + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function signedDollars(n) {
  if (n == null) return "—";
  return `${n < 0 ? "−" : "+"}${dollars(Math.abs(Number(n)))}`;
}

// A collapsible "show the math" disclosure: the derivation of a headline number —
// its inputs and the arithmetic — laid out so the operator can validate it line by
// line against the account instead of trusting the rolled-up figure. The three
// numbers that matter for maintaining a position (intrinsic balance, extrinsic
// burn-off, leftover realized) each get one.
function ShowMath({ children, label = "Show the math" }) {
  const [open, setOpen] = React.useState(false);
  return (
    <div className="mt-2">
      <button
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }}
        aria-expanded={open}
        className="text-[11px] font-medium text-sky-400/90 hover:text-sky-300"
      >
        {open ? "Hide the math ▲" : `${label} ▾`}
      </button>
      {open && (
        <div className="mt-1.5 space-y-1 rounded-lg border border-slate-800 bg-slate-950/60 p-2.5 font-mono text-[11px] leading-relaxed text-slate-400">
          {children}
        </div>
      )}
    </div>
  );
}

// One derivation line: a label on the left, the arithmetic and/or result on the right.
function MathRow({ label, value, strong = false, tone = "" }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="whitespace-pre text-slate-500">{label}</span>
      <span className={`text-right ${strong ? "font-semibold text-slate-200" : "text-slate-300"} ${tone}`}>{value}</span>
    </div>
  );
}

// Sum one enriched field across legs, keeping null distinct from zero: a leg
// missing the field is skipped, and an all-missing set reads "—" rather than $0.00
// (no data and nothing owed are different answers to "what does the short owe").
function sumField(rows, key) {
  let total = 0, any = false;
  for (const r of rows || []) {
    if (r[key] == null) continue;
    total += Number(r[key]);
    any = true;
  }
  return any ? total : null;
}

// The short-call side of the same directional bet, netted against the shares.
//
// A covered call's premium is intrinsic + extrinsic. Only the INTRINSIC half
// tracks the stock 1:1 against the share base: it is the obligation that grows as
// the stock climbs (gains above the strike get handed over at assignment) and
// melts as the stock falls (that cash stays ours). So the intrinsic swing since
// each short was sold — position_manager's signed `intrinsic_captured_total`
// (entry intrinsic − intrinsic owed now) — is exactly the piece that offsets the
// shares' unrealized move, and netting the two is the "is this balancing out"
// read the share block alone can't give.
//
// What this is NOT: a position P/L. The two legs are measured from their OWN
// entries (shares from cost basis, the short from its sale price), so the net says
// how the two moves offset each other, not what the position is worth. Extrinsic
// is deliberately excluded — it is theta income, not a directional offset, and it
// has its own capture meter on the short leg.
function ShortIntrinsicMath({ p, unreal }) {
  const spot = p.stock_price;
  const shorts = (p.short_calls || []).filter((sc) => Number(sc.contracts || 0) > 0);
  if (!shorts.length) return null;
  const owedNow = sumField(shorts, "current_intrinsic_total");
  const soldAtEntry = sumField(shorts, "entry_intrinsic_total");
  const swing = sumField(shorts, "intrinsic_captured_total");
  const net = unreal != null && swing != null ? unreal + swing : null;
  const anyItm = shorts.some((sc) => sc.itm === true);
  // Only spell the swing out as "entry − now" when EVERY leg carries both halves;
  // with a partial set the two sums cover different legs and the subtraction would
  // not reproduce the swing shown beside it.
  const complete = shorts.every((sc) => sc.entry_intrinsic_total != null
    && sc.current_intrinsic_total != null && sc.intrinsic_captured_total != null);
  return (
    <>
      <div className="border-t border-slate-800 pt-1" />
      <div className="text-slate-500">Short-call intrinsic = max(spot − strike, 0) × 100 × contracts</div>
      {shorts.map((sc, i) => (
        <MathRow
          key={i}
          label={`  ${sc.contracts}× ${fmt(sc.strike, 2)}C${sc.expiration ? ` ${String(sc.expiration).slice(5)}` : ""}`}
          value={`max(${fmt(spot, 2)} − ${fmt(sc.strike, 2)}, 0) = ${fmt(sc.current_intrinsic_per_share, 2)}/sh → ${dollars(sc.current_intrinsic_total)}`}
          tone={sc.itm ? "text-amber-300" : ""}
        />
      ))}
      <MathRow label="  intrinsic owed now" value={dollars(owedNow)} strong
        tone={owedNow ? "text-amber-300" : ""} />
      <MathRow label="  intrinsic sold at entry" value={dollars(soldAtEntry)} />
      <MathRow label="  = intrinsic swing (entry − now)"
        value={complete
          ? `${dollars(soldAtEntry)} − ${dollars(owedNow)} = ${signedDollars(swing)}`
          : signedDollars(swing)}
        strong tone={swing != null && swing >= 0 ? "text-emerald-300" : "text-rose-300"} />
      <div className="pt-1 text-slate-500">Balance = share unrealized + intrinsic swing</div>
      <MathRow label="  net directional"
        value={net != null ? `${signedDollars(unreal)} + ${signedDollars(swing)} = ${signedDollars(net)}` : "—"}
        strong tone={net != null && net >= 0 ? "text-emerald-300" : "text-rose-300"} />
      <div className="text-slate-600">
        {spot == null
          ? "No live spot — the short's intrinsic can't be priced yet."
          : anyItm
            ? "A short is ITM — the shares' gain above that strike is what the intrinsic owed hands back at assignment."
            : "Every short is OTM — nothing owed, so the shares carry the move alone."}
      </div>
      <div className="text-slate-600">
        Each leg is measured from its own entry (shares from cost basis, short from its sale),
        so this is how the two moves offset — not a position P/L. Extrinsic is theta income,
        not a directional offset, and is excluded here.
      </div>
    </>
  );
}

// Derivation of the share base: the owned lot's cost against its current market
// value, the short calls' intrinsic netted against that move (ShortIntrinsicMath —
// the "is this balancing out" read), and the covered-lot capacity the short leg is
// allowed to sell into.
// Per-line results are the payload's own enriched fields (position_manager's
// covered_lots), so the pieces shown sum exactly to the totals above them.
function SharesBaseMath({ p }) {
  const spot = p.stock_price;
  const sh = p.shares || {};
  const cov = p.coverage || {};
  const count = Number(sh.count || 0);
  const cps = sh.cost_basis_per_share;
  const cost = cps != null ? cps * count : null;
  const value = spot != null ? spot * count : null;
  const shorts = p.short_calls || [];
  const sold = cov.short_contracts != null
    ? cov.short_contracts
    : shorts.reduce((s, sc) => s + Number(sc.contracts || 0), 0);
  const lots = cov.coverable_lots ?? sh.coverable_lots;
  const fragment = cov.fragment_shares ?? sh.fragment_shares;
  return (
    <>
      <MathRow label="Spot price" value={fmt(spot, 2)} strong />
      <div className="pt-1 text-slate-500">Share base = shares × cost basis per share</div>
      <MathRow label={`  ${count} sh × ${dollars(cps)}`} value={dollars(cost)} strong />
      <MathRow label={`  market value = ${count} sh × ${fmt(spot, 2)}`} value={dollars(value)} />
      <MathRow label="  = unrealized"
        value={cost != null && value != null ? `${dollars(value)} − ${dollars(cost)} = ${signedDollars(value - cost)}` : "—"}
        strong tone={cost != null && value != null && value >= cost ? "text-emerald-300" : "text-rose-300"} />
      <ShortIntrinsicMath p={p} unreal={cost != null && value != null ? value - cost : null} />
      <div className="border-t border-slate-800 pt-1" />
      <div className="text-slate-500">Coverable lots = floor(shares ÷ 100) — a fragment is never coverable</div>
      <MathRow label={`  floor(${count} ÷ 100)`} value={`${lots ?? "—"} lot${lots === 1 ? "" : "s"}`} strong />
      {fragment > 0 && (
        <MathRow label="  fragment (uncoverable)" value={`${fragment} sh`} tone="text-amber-300" />
      )}
      <MathRow label="  short contracts sold" value={`${sold}`} />
      <MathRow label="Covered = lots − sold"
        value={lots != null ? `${lots} − ${sold} = ${lots - sold}` : "—"}
        strong tone={cov.naked_short ? "text-rose-300" : "text-emerald-300"} />
      <div className="text-slate-600">{cov.naked_short
        ? "< 0 → NAKED: more calls sold than owned lots. Buy one back."
        : "≥ 0 → every short call is backed by 100 owned shares."}</div>
    </>
  );
}

// The card hero — the position's BASE: the owned shares the whole engine sits on.
// Is the capital intact, and is every lot actually covered? The share block (cost
// basis vs market value, unrealized) sits beside the covered-lot count, which is
// the hard guardrail — short contracts may never exceed floor(shares/100). Renders
// a prompt instead when the position holds no shares yet (a freshly opened row).
// A cash-secured put holds NO base leg. It is rendered in this same card — there
// is no separate put screen — but the share readouts are UNDEFINED for it, not
// zero: a 0% cap meter and a satisfied-looking coverage line would both be
// answers to questions that do not apply until assignment. The server marks them
// `not_applicable` and this renders "n/a", never a number.
function PutBase({ p }) {
  const legs = p.short_puts || [];
  const collateral = p.put_collateral || 0;
  const rg = p.put_regate;
  const tempo = p.tempo;
  return (
    <div>
      <div className="mb-2 flex items-baseline gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-violet-300">
          Cash-secured put
        </span>
        <span className="text-xs text-slate-500">no shares held until assignment</span>
      </div>
      {legs.map((l, i) => {
        const itm = l.itm === true;
        return (
          <div key={i} className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-sm">
            <span className="font-semibold text-slate-100">
              {l.contracts}× {fmt(l.strike, 2)}P {String(l.expiration || "").slice(5)}
            </span>
            <span className="text-slate-400">
              collateral <span className="tabular-nums text-slate-200">{money(l.collateral)}</span>
            </span>
            <span className="text-slate-400">
              premium <span className="tabular-nums text-emerald-300">{money(l.premium_per_share * 100 * l.contracts)}</span>
            </span>
            {/* Only the EXTRINSIC half is income. Intrinsic is a share-purchase
                obligation and is shown separately so the two never blur. */}
            <span className="text-slate-400">
              extrinsic <span className="tabular-nums text-slate-200">
                {l.extrinsic_per_share == null ? "—" : money(l.extrinsic_per_share * 100 * l.contracts)}
              </span>
            </span>
            <StrikeGap leg={l} put className="text-xs" />
            {itm && (
              <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-amber-300">
                ITM — assignment likely
              </span>
            )}
          </div>
        );
      })}
      {/* The daily re-gate: would this name be admitted today? A HOLD means
          assignment is a good entry and should be allowed to happen. A CLOSE
          means the thesis broke — and the only remedy offered is closing, never
          rolling: a put roll is a debit and a Martingale structure. */}
      {rg && (
        <div className={`mt-3 rounded-lg px-3 py-2 text-xs ${
          rg.action === "close"
            ? "bg-rose-500/10 text-rose-200"
            : "bg-emerald-500/10 text-emerald-200"}`}>
          {rg.action === "close" ? (
            <>
              <span className="font-semibold">Close the put</span> — the entry rules would
              refuse this name today ({(rg.blocked_by || []).join(", ")}). Do not accept
              assignment, and do not roll it.
            </>
          ) : (
            <>
              <span className="font-semibold">Hold</span> — the name still passes the entry
              rules, so assignment is a good entry. Let it happen.
            </>
          )}
        </div>
      )}
      {/* Tempo only. Shown, never acted on — an 8/21 cross closes nothing. */}
      {tempo?.signal && (
        <div className="mt-1 text-[11px] text-slate-500">
          Tempo {tempo.signal === "TEMPO_UP" ? "↑" : "↓"} (8/21 EMA) —
          <span className="text-slate-600"> a pace read, not a close signal.</span>
        </div>
      )}
      <div className="mt-2 text-xs text-slate-500">
        Collateral <span className="tabular-nums">{money(collateral)}</span> counts against the
        deployed-capital cap and does not draw the ATR reserve. Coverage, cap progress and
        roll drag are <span className="text-slate-400">n/a</span> — this position holds no shares.
      </div>
    </div>
  );
}

function EmptyPositionNotice({ ticker, onCleared }) {
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  async function clear() {
    if (!window.confirm(
      `Clear the empty ${ticker} position?\n\n` +
      `It holds no shares, no calls and no puts — nothing you own is removed. ` +
      `The execution log keeps an immutable record that it was retired.`)) return;
    setBusy(true); setErr(null);
    try {
      await api.closeEmptyPositions(`operator cleared empty ${ticker} from Positions`);
      onCleared?.();
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }
  return (
    <div className="mt-2 rounded-lg border border-amber-700/60 bg-amber-500/5 px-3 py-2 text-amber-300">
      <span className="font-semibold">This position holds nothing.</span> No shares,
      no calls, no puts — most likely an order that was never actually sent to the
      broker. It is not a holding and can be cleared.
      {err && <div className="mt-1 text-rose-400">{err}</div>}
      <button
        onClick={clear}
        disabled={busy}
        className="mt-2 rounded-md border border-amber-600 px-2.5 py-1 text-xs font-semibold text-amber-200 hover:bg-amber-500/10 disabled:opacity-40"
      >
        {busy ? "Clearing…" : "Clear this empty position"}
      </button>
    </div>
  );
}


function SharesBase({ p, onCleared }) {
  const sh = p.shares || {};
  const cov = p.coverage || {};
  const count = Number(sh.count || 0);
  const cps = sh.cost_basis_per_share;
  const spot = p.stock_price;
  const cost = cps != null && count ? cps * count : null;
  const value = spot != null && count ? spot * count : null;
  const unreal = cost != null && value != null ? value - cost : null;
  const lots = cov.coverable_lots ?? sh.coverable_lots;
  const sold = cov.short_contracts != null
    ? cov.short_contracts
    : (p.short_calls || []).reduce((s, sc) => s + Number(sc.contracts || 0), 0);
  const naked = cov.naked_short === true;
  const free = lots != null ? lots - sold : null;

  if (!count) {
    // A position holding NOTHING is a shell, not a holding — an order that was
    // never sent, or one the broker refused after the position record already
    // existed. It reads exactly like something you own, so offer the way out
    // right here rather than leaving the operator to reconcile it by hand.
    const empty = !(p.short_calls || []).length && !(p.short_puts || []).length
      && !(p.leap_legs || []).length && !p.leap;
    return (
      <div className="text-xs text-slate-500">
        No shares held — buy the base lot to start the engine.
        {empty && <EmptyPositionNotice ticker={p.ticker} onCleared={onCleared} />}
      </div>
    );
  }
  return (
    <div className="min-w-0">
      <div className="mb-1 flex flex-wrap items-center gap-1.5">
        <span className="text-[10px] uppercase tracking-wide text-slate-500">Share base</span>
        <span className={`rounded-full border px-1.5 py-0.5 text-[9px] font-semibold uppercase ${
          naked ? "border-rose-500/40 bg-rose-500/15 text-rose-300"
                : "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"}`}>
          {naked ? "naked short" : "covered"}
        </span>
        {sh.locked && (
          <span className="rounded-full border border-amber-500/40 bg-amber-500/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase text-amber-300">
            at share cap
          </span>
        )}
      </div>
      <div className="text-2xl font-semibold leading-none text-slate-100">
        {count}<span className="ml-1 text-sm font-normal text-slate-500">shares</span>
      </div>
      <div className="mt-1.5 text-xs text-slate-500">
        {money(cost)} cost{cps != null && <> ({fmt(cps, 2)}/sh)</>}
        {value != null && <> · {money(value)} now</>}
        {spot != null && <> ({fmt(spot, 2)}/sh)</>}
      </div>
      {unreal != null && (
        <div className={`text-xs font-semibold ${unreal >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
          {signedDollars(unreal)} unrealized
        </div>
      )}
      <div className="mt-1 text-xs text-slate-500">
        {lots == null ? "coverage pending" : (
          <>
            {sold} of {lots} lot{lots === 1 ? "" : "s"} sold against
            {free != null && free > 0 && <span className="text-emerald-300"> — {free} free to sell</span>}
            {naked && <span className="text-rose-300"> — more calls sold than lots owned</span>}
          </>
        )}
        {sh.fragment_shares > 0 && (
          <span className="text-amber-300"> · {sh.fragment_shares}sh fragment (never coverable)</span>
        )}
      </div>
      {sh.accumulation_blocked && (
        <div className="mt-1 text-[11px] text-amber-300/90">
          Adding blocked — {sh.accumulation_block_reason || "relative strength deteriorating"}.
        </div>
      )}
      <ShowMath><SharesBaseMath p={p} /></ShowMath>
    </div>
  );
}

// Plain-language "where do I stand" for the whole book, at the top of the page:
// how the positions net out (lots covered, premium captured, needing attention)
// woven together with the book-level decision (market lean, engine, can-I-add)
// from the portfolio-risk payload. A narrative, not a dashboard — the tiles below
// carry the numbers.
function BookSummary({ positions, diffsByTicker, risk }) {
  const n = positions.length;
  let covered = 0;
  const naked = [];
  let lots = 0, sold = 0, captured = 0, entry = 0, attention = 0;
  for (const p of positions) {
    const cov = p.coverage || {};
    const sh = p.shares || {};
    if (cov.naked_short === true) naked.push(p.ticker);
    else if (Number(sh.count || 0) > 0) covered++;
    lots += Number(cov.coverable_lots ?? sh.coverable_lots ?? 0);
    sold += Number(cov.short_contracts ?? 0);
    for (const sc of p.short_calls || []) {
      if (sc.entry_extrinsic_total != null) { entry += Number(sc.entry_extrinsic_total); captured += Number(sc.extrinsic_captured_total || 0); }
    }
    if (p.needs_review || p.defend || p.whipsaw?.tripped || (diffsByTicker[p.ticker]?.length)) attention++;
  }
  const shortCap = entry > 0 ? (captured / entry) * 100 : null;
  const lotsWorking = lots > 0 ? (sold / lots) * 100 : null;

  const t = risk?.totals || {};
  const cap = risk?.capital || {};
  const conc = risk?.concentration || {};
  const overCap = cap.deployed != null && cap.cap != null && cap.deployed > cap.cap;
  const reserveShort = cap.reserve_ok === false;
  const concentrated = !!conc.warn;
  const canAdd = risk ? (!overCap && !reserveShort && !concentrated) : null;
  const blockReason = overCap ? `fully deployed (${fmt(cap.pct_of_cap, 0)}% of cap)`
    : reserveShort ? "short on defensive reserve"
    : concentrated ? "too concentrated to add safely" : null;
  const thetaPos = t.theta_per_day != null && t.theta_per_day >= 0;
  const lean = t.delta_dollars_spy_adj != null && cap.deployed ? t.delta_dollars_spy_adj / cap.deployed : null;
  const expoLabel = lean == null ? null : Math.abs(lean) < 0.5 ? "market-neutral" : lean > 0 ? "leaning long the market" : "leaning short the market";

  const sentences = [];
  sentences.push(
    `You hold ${n} position${n === 1 ? "" : "s"}` +
    (naked.length
      ? ` — ${covered} fully covered, ${naked.join(", ")} NAKED (more calls sold than owned lots)`
      : ", every short call backed by 100 owned shares") + ".");
  if (lotsWorking != null || shortCap != null) {
    const a = lotsWorking != null ? `${sold} of ${lots} owned lots working (${fmt(lotsWorking, 0)}%)` : "";
    const b = shortCap != null ? `captured ${fmt(shortCap, 0)}% of this cycle's call premium` : "";
    sentences.push(`Across the book you've got ${[a, b].filter(Boolean).join(", and you've ")}.`);
  }
  if (expoLabel || t.theta_per_day != null) {
    const parts = [];
    if (expoLabel) parts.push(`the book is ${expoLabel}`);
    if (t.theta_per_day != null) parts.push(`the engine is ${thetaPos ? `collecting ${money(t.theta_per_day)}/day` : `bleeding ${money(Math.abs(t.theta_per_day))}/day`}`);
    sentences.push(parts.join(" and ").replace(/^\w/, (c) => c.toUpperCase()) + ".");
  }
  if (canAdd === true) sentences.push("You have room to open another position.");
  else if (canAdd === false) sentences.push(`You're ${blockReason} — manage what you have rather than adding.`);

  const attn = attention > 0
    ? `${attention} position${attention === 1 ? "" : "s"} need${attention === 1 ? "s" : ""} your attention`
    : "Nothing needs immediate attention";

  return (
    <Card title="Your book right now">
      <p className="text-sm leading-relaxed text-slate-300">{sentences.join(" ")}</p>
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-slate-800 pt-3 text-xs">
        <span><span className="text-slate-500">positions </span><span className="font-semibold text-slate-100">{n}</span></span>
        <span><span className="text-slate-500">covered </span><span className={`font-semibold ${naked.length ? "text-rose-300" : "text-emerald-300"}`}>{covered}/{n}</span></span>
        {lots > 0 && <span><span className="text-slate-500">lots working </span><span className="font-semibold text-slate-100">{sold}/{lots}</span></span>}
        {shortCap != null && <span><span className="text-slate-500">call capture </span><span className="font-semibold text-slate-100">{fmt(shortCap, 0)}%</span></span>}
        <span className={attention > 0 ? "text-rose-300" : "text-slate-500"}>{attention > 0 && "⚠ "}{attn}</span>
        {canAdd != null && (
          <span className={`ml-auto rounded-full border px-2 py-0.5 font-semibold uppercase tracking-wide ${
            canAdd ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300" : "border-rose-500/40 bg-rose-500/15 text-rose-300"}`}>
            {canAdd ? "room to add" : "manage only"}
          </span>
        )}
      </div>
    </Card>
  );
}

// One ticker, collapsible. Collapsed: a summary of the three things that matter —
// the share base, covered-lot capacity, short-call capture. Expanded: those in
// full (share block, short list, accrual), plus any active safety alert
// (reconciliation, defend, whipsaw) which also auto-opens the row.
function PositionRow({ p, diffs, recs, onRecsChanged, focusCard, focused, setRolling, onOpenTicket, afterResolve }) {
  const shorts = p.short_calls || [];
  const hasAlert = !!(p.needs_review || p.defend || p.whipsaw?.tripped || (diffs && diffs.length));
  // Collapsed by default for a clean, scannable list; a tapped-alert deep link
  // (focused) opens the row so the operator lands on the thing to act on.
  const [open, setOpen] = React.useState(false);
  React.useEffect(() => { if (focused) setOpen(true); }, [focused]);

  const sh = p.shares || {};
  const cov = p.coverage || {};
  const count = Number(sh.count || 0);
  const lots = cov.coverable_lots ?? sh.coverable_lots;
  const sold = cov.short_contracts ?? shorts.reduce((s, sc) => s + Number(sc.contracts || 0), 0);
  const naked = cov.naked_short === true;
  const shortPct = shortCapturePct(shorts);

  return (
    <div className={`min-w-0 rounded-xl border bg-slate-900/60 transition ${focused ? "border-emerald-400/70 ring-1 ring-emerald-400/40" : "border-slate-800"}`}>
      <div className="flex items-center">
        {/* Candlestick chart link sits outside the toggle button (no <a> nested
            in a <button>); it opens the ticker's external chart in a new tab. */}
        <ChartLink ticker={p.ticker} size="h-4 w-4" className="ml-3" />
        <button onClick={() => setOpen((v) => !v)} aria-expanded={open}
              className="flex min-w-0 flex-1 items-center justify-between gap-3 py-4 pr-4 pl-2 text-left hover:bg-slate-900/40">
        <span className="flex min-w-0 items-center gap-2">
          <span className={`text-slate-500 transition-transform ${open ? "rotate-90" : ""}`}>▸</span>
          <span className="text-sm font-semibold text-slate-100">{p.ticker}</span>
          <span className="truncate text-xs text-slate-500">{p.sector}</span>
          {p.income_profile === "DIVIDEND_COMPOUNDER" && (
            <SleeveBadge profile={p.income_profile} />
          )}
          {p.symbol_genius?.color && (
            <span
              className="flex items-center gap-1"
              title={`Symbol Genius: ${p.symbol_genius.color.toUpperCase()}`
                + (p.symbol_genius.greens != null ? ` (${p.symbol_genius.greens}/4 lights)` : "")
                + " — the per-name four-light structural read (SMA50>SMA200 fourth light). "
                + "YELLOW (3 green) or RED is an early structural warning alongside the RS-based kill switch."}
            >
              <Light status={p.symbol_genius.color} size="h-2 w-2" />
              {p.symbol_genius.color !== "green" && (
                <span className={`text-[9px] font-semibold uppercase ${p.symbol_genius.color === "yellow" ? "text-amber-300" : "text-rose-300"}`}>
                  SYM {p.symbol_genius.color}
                </span>
              )}
            </span>
          )}
          {hasAlert && (
            <span title="Needs attention — expand to resolve"
                  className="rounded-full border border-rose-500/50 bg-rose-500/15 px-1.5 py-0.5 text-[9px] font-semibold text-rose-300">⚠</span>
          )}
        </span>
        {/* collapsed summary — the three things, each with its tiny visual */}
        <span className="flex flex-wrap items-center justify-end gap-x-4 gap-y-1 text-xs">
          {/* Spot first: every other number on the row is a consequence of it,
              and "where is the stock" is the question asked before the row is
              even opened. */}
          <span className="flex items-center gap-1" title="Last price used for every figure on this card">
            <span className="text-slate-500">spot</span>
            <span className="font-semibold text-slate-200">
              {p.stock_price == null ? "—" : fmt(p.stock_price, 2)}
            </span>
          </span>
          <span className="flex items-center gap-1">
            <span className="text-slate-500">shares</span>
            <span className="font-semibold text-slate-200">{count || "—"}</span>
          </span>
          <span className="flex items-center gap-1">
            <span className="text-slate-500">lots</span>
            {lots == null
              ? <span className="text-slate-500">—</span>
              : <span className={`font-semibold ${naked ? "text-rose-300" : "text-slate-200"}`}>
                  {sold}/{lots}{naked && " naked"}
                </span>}
          </span>
          <span className="flex items-center gap-1">
            <span className="text-slate-500">call</span>
            <span className="font-semibold text-slate-200">{shortPct == null ? "none" : `${fmt(shortPct, 0)}% cap`}</span>
          </span>
        </span>
        </button>
      </div>

      {/* Engine recommendations stay visible even when the row is collapsed —
          they're the "act now" layer, not detail. */}
      <RecSection p={p} recs={recs} onRecsChanged={onRecsChanged} focusCard={focusCard} />

      {open && (
        <div className="border-t border-slate-800 p-4">
          {/* Active safety alerts — surfaced, never hidden. */}
          <ReviewPanel ticker={p.ticker} diffs={diffs} onDone={afterResolve} />
          {p.needs_review && (
            <p className="mb-1 text-xs italic text-rose-400/80">
              State unverified against the broker — the numbers below are computed off state.json
              and may not reflect the account until this position is resolved.
            </p>
          )}
          {p.whipsaw?.tripped && (
            <div className="mb-3 rounded-lg border border-rose-500/50 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
              <span className="font-semibold text-rose-300">⚠ Whipsaw — exit, don't defend again.</span>{" "}
              {(p.whipsaw.reasons || []).join("; ")}.
            </div>
          )}

          {/* (1) the base the engine sits on — shares, or a put's collateral */}
          <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
            {p.position_type === "CASH_SECURED_PUT"
              ? <PutBase p={p} />
              : <SharesBase p={p} onCleared={afterResolve} />}
          </div>

          {/* (2) weekly covered-call capture */}
          <ShortCalls p={p} shorts={shorts} setRolling={setRolling} onOpenTicket={onOpenTicket} />

          {/* (3) accrual toward the next lot */}
          <AccrualProgress accrual={p.accrual} />

          {p.defend && (
            <DefendPanel ticker={p.ticker} onStage={() => setRolling({ ticker: p.ticker, reason: "defend" })} />
          )}
        </div>
      )}
    </div>
  );
}

export default function PositionTracker({ intent, onIntentHandled, onOpenTicket } = {}) {
  const toast = useToast();
  const { data, error, loading, reload } = useApi(api.positions, [], null);
  const { data: recon, reload: reloadRecon } = useApi(api.reconcile, [], null);
  const { data: risk } = useApi(api.portfolioRisk, [], null);
  // Open engine recommendations (trust layer) — refetched whenever the tab
  // remounts (the execNonce key) and after a dismissal.
  const { data: recsData, reload: reloadRecs } = useApi(api.recommendations, [], null);
  const [rolling, setRolling] = React.useState(null); // {ticker, reason, recId?}
  const [focusedTicker, setFocusedTicker] = React.useState(null);
  const handledIntentId = React.useRef(null);

  // Scroll-to + highlight for a position card (deep links, EXIT rec Execute).
  const focusCard = React.useCallback((ticker) => {
    setFocusedTicker(ticker);
    requestAnimationFrame(() =>
      document.getElementById(`pos-${ticker}`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" }));
    setTimeout(() => setFocusedTicker((t) => (t === ticker ? null : t)), 2500);
  }, []);

  // Deep-link intent from a tapped alert: open the prefilled roll ticket for the
  // ticker, or (for exit/kill-switch alerts) scroll to and highlight its card.
  // A recommendation-card Execute travels the same path with intent.recId set,
  // which rides into RollModal so the fill carries source_rec_id.
  React.useEffect(() => {
    if (!intent || !data || handledIntentId.current === intent.id) return;
    handledIntentId.current = intent.id;
    const posList = (data.positions || []).filter((p) => p.status !== "closed");
    const pos = posList.find((p) => p.ticker === intent.ticker);
    if (pos && intent.action === "roll" && (pos.short_calls || []).length > 0) {
      setRolling({ ticker: intent.ticker, reason: intent.reason || "scheduled", recId: intent.recId });
    } else if (pos) {
      focusCard(intent.ticker);
    }
    onIntentHandled?.();
  }, [intent, data, onIntentHandled, focusCard]);

  // Open recommendations grouped by ticker for the position cards. ENTER recs are
  // split out: they name a ticker the book does not hold, so grouping them here
  // put them under a position card that will never render.
  const recsByTicker = React.useMemo(() => {
    const out = {};
    for (const r of recsData?.open || []) {
      if (r.action_type === "ENTER") continue;
      const t = (r.ticker || "").toUpperCase();
      if (t) (out[t] ||= []).push(r);
    }
    return out;
  }, [recsData]);

  const enterRecs = React.useMemo(
    () => (recsData?.open || []).filter((r) => r.action_type === "ENTER"),
    [recsData]);

  // Open (unresolved) reconciliation diffs indexed by ticker — drives the review
  // panel + the state-unverified marker on frozen positions.
  const openDiffsByTicker = React.useMemo(() => {
    const out = {};
    const diffs = recon?.last?.broker_ok ? (recon.last.diffs || []) : [];
    for (const d of diffs) {
      if (d.resolution && d.resolution.status) continue; // resolved / acknowledged
      (out[d.ticker] ||= []).push(d);
    }
    return out;
  }, [recon]);

  const afterResolve = React.useCallback(() => { reload(); reloadRecon(); }, [reload, reloadRecon]);

  // Drive the roll through the shared toast lifecycle (submit → fill/cancel),
  // then refresh positions. Defined before the early returns so hook order holds.
  const runRoll = React.useCallback(async (payload) => {
    const res = await submitOrder(api, toast, payload);
    reload();
    return res;
  }, [toast, reload]);

  if (loading && !data) return <Card title="Positions"><Loading /></Card>;
  if (error) return <Card title="Positions"><p className="text-sm text-rose-400">{error}</p></Card>;

  // Closed positions live on the History tab as cycle records; the capital /
  // milestones summary is on Overview (its one home).
  const positions = (data?.positions || []).filter((p) => p.status !== "closed");

  return (
    <div className="grid gap-3">
      {positions.length > 0 && (
        <BookSummary positions={positions} diffsByTicker={openDiffsByTicker} risk={risk} />
      )}
      <PortfolioRisk data={risk} />
      <ProposedEntries recs={enterRecs} onRecsChanged={reloadRecs} onEnter={onOpenTicket} />

      {positions.length === 0 && <Card>No open positions.</Card>}
      {positions.map((p) => (
        <div key={p.ticker} id={`pos-${p.ticker}`} className="scroll-mt-20">
          <PositionRow
            p={p}
            diffs={openDiffsByTicker[p.ticker]}
            recs={recsByTicker[(p.ticker || "").toUpperCase()]}
            onRecsChanged={reloadRecs}
            focusCard={focusCard}
            focused={focusedTicker === p.ticker}
            setRolling={setRolling}
            onOpenTicket={onOpenTicket}
            afterResolve={afterResolve}
          />
        </div>
      ))}

      {rolling && (
        <RollModal
          ticker={rolling.ticker}
          reason={rolling.reason}
          sourceRecId={rolling.recId}
          onExecute={runRoll}
          onClose={() => setRolling(null)}
        />
      )}
    </div>
  );
}
