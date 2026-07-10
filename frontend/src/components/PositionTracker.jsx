import React from "react";
import { api } from "../api.js";
import { Card, Meter, Loading, Modal, money, fmt, useApi } from "./ui.jsx";
import RollModal from "./RollModal.jsx";
import PortfolioRisk from "./PortfolioRisk.jsx";
import { Orange, pulpOf, balanceOf } from "./JuiceStand.jsx";
import { useToast } from "./Toast.jsx";
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
          Do NOT exercise the LEAP to cover — buy back the short stock or close the position.
          Exercising forfeits all remaining LEAP extrinsic.
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

// One-line ticket read: legs (instruction + strike + expiry) · order type · est net.
function ticketSummary(t) {
  if (!t) return "no ticket attached";
  const legs = (t.legs || [])
    .map((l) => {
      const when = l.expiration ? ` exp ${l.expiration}` : l.dte != null ? ` ${l.dte} DTE` : "";
      return `${(l.instruction || "").replaceAll("_", " ")} ${fmt(l.strike, 2)}${when}`;
    })
    .join(" / ");
  const net = t.estimates?.net_per_share;
  const netStr = net != null
    ? `est ${net < 0 ? "−" : ""}$${Math.abs(Number(net)).toFixed(2)}/sh ${net >= 0 ? "credit" : "debit"}`
    : "unpriced";
  return [legs, (t.order_type || "").replaceAll("_", " "), netStr].filter(Boolean).join(" · ");
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
function RecCard({ rec, now, expanded, onToggleDetail, onExecute, onDismiss }) {
  const v = validity(rec.valid_until, now);
  const t = rec.proposed_ticket;
  const badge = REC_BADGE[rec.action_type] || "border-slate-600 bg-slate-800/60 text-slate-300";
  return (
    <div className="rounded-lg border border-slate-700 bg-slate-950/60 px-3 py-2 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${badge}`}>
          {(rec.action_type || "").replaceAll("_", " ")}
        </span>
        <span className="font-mono text-xs text-slate-400" title="Trigger rule">{rec.trigger_rule}</span>
        {v && (
          <span className={`ml-auto text-xs ${v.tone}`} title={`valid until ${rec.valid_until}`}>
            {v.text}
          </span>
        )}
      </div>
      <p className="mt-1 text-xs text-slate-300">{ticketSummary(t)}</p>
      {expanded && t && (
        <div className="mt-2 rounded-lg border border-slate-800 bg-slate-900/60 p-2 text-xs text-slate-300">
          <div className="text-[10px] uppercase tracking-wide text-slate-500">Proposed ticket</div>
          <ul className="mt-1 space-y-0.5">
            {(t.legs || []).map((l, i) => (
              <li key={i}>
                {(l.instruction || "").replaceAll("_", " ")} {l.quantity != null ? `${l.quantity}× ` : ""}
                {fmt(l.strike, 2)}
                {l.expiration ? ` exp ${l.expiration}` : ""}{l.dte != null ? ` (${l.dte} DTE)` : ""}
                {l.role ? <span className="text-slate-500"> · {l.role}</span> : null}
              </li>
            ))}
          </ul>
          <div className="mt-1 text-slate-400">
            {(t.order_type || "").replaceAll("_", " ")}
            {" · "}limit {t.limit_price != null ? `$${Number(t.limit_price).toFixed(2)}` : "unpriced"}
            {t.min_acceptable_net_credit != null && <> · min net ${Number(t.min_acceptable_net_credit).toFixed(2)}</>}
            {t.max_slippage_pct_of_mid != null && <> · max slip {t.max_slippage_pct_of_mid}% of mid</>}
            {t.price_source && <> · priced from {t.price_source}</>}
          </div>
        </div>
      )}
      <div className="mt-2 flex items-center gap-2">
        <button
          onClick={onExecute}
          className="rounded-full border border-emerald-600/50 bg-emerald-500/10 px-2.5 py-0.5 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20"
        >
          Execute
        </button>
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

// The recommendation strip under a position's header. Actionable recs (never
// NO_ACTION) render as cards; when the ONLY open rec is an ALL_CLEAR/NO_ACTION,
// a single muted line says so with the valid-until time.
function RecSection({ p, recs, onRecsChanged, focusCard }) {
  const now = useNow();
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

  return (
    <div className="space-y-2 px-4 pb-3">
      {actionable.map((rec) => (
        <RecCard
          key={rec.rec_id} rec={rec} now={now}
          expanded={detailId === rec.rec_id}
          onToggleDetail={() => setDetailId((id) => (id === rec.rec_id ? null : rec.rec_id))}
          onExecute={() => execute(rec)}
          onDismiss={() => setDismissing(rec)}
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

// The card hero — the position's BALANCE, the thing the page is for now that
// income tracking lives on its own page: is the initial investment staying whole?
// Two halves tell it. INTRINSIC (left): the LEAP orange (pulp = intrinsic vs cost
// basis, leaf = self-funding) beside whether the LEAP's intrinsic still covers the
// shorts' intrinsic, so a stock move washes out. EXTRINSIC PAID BACK (right): how
// much of the LEAP's entry extrinsic the collected juice has recovered — the burn
// being earned back. The verdict badge sits above as the one-line roll-up.
// The extrinsic-payback "juice battery": the LEAP's burned time-value being earned
// back. Fills green from the bottom as collected juice recovers it — waves at the
// surface, bubbles rising — and the whole cell glows once the extrinsic is fully
// paid off. It keeps filling over the life of the position until 100% (or the
// position closes and the card drops away). Reuses the global juice-rise /
// juice-wave / juice-bubble animations so it reads as family with the stand.
function PaybackTank({ uid, pct, mini = false }) {
  const fill = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  const full = pct != null && pct >= 100;
  const innerTop = 15;
  const innerBottom = 110;
  const surfaceY = innerBottom - ((innerBottom - innerTop) * fill) / 100;
  const bubbles = fill >= 15 && !mini;
  return (
    <svg
      viewBox="0 0 72 122"
      className={`${mini ? "h-8 w-5" : "h-24 w-[3.4rem]"} shrink-0 ${full ? "drop-shadow-[0_0_8px_rgba(52,211,153,0.55)]" : ""}`}
      role="img"
      aria-label={pct == null ? "payback unknown" : `${fmt(pct, 0)}% of the LEAP extrinsic paid back`}
    >
      <defs>
        <linearGradient id={`pb-${uid}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#34d399" />
          <stop offset="1" stopColor="#059669" />
        </linearGradient>
        <clipPath id={`pbc-${uid}`}><rect x="10" y="15" width="52" height="95" rx="8" /></clipPath>
        <clipPath id={`pbl-${uid}`}><rect x="8" y={surfaceY} width="56" height={innerBottom - surfaceY} /></clipPath>
      </defs>
      {/* terminal cap — reads as a battery/tank */}
      <rect x="27" y="2" width="18" height="7" rx="2" fill="#475569" />
      {/* liquid: gradient body + animated wave crest + rising bubbles, clipped */}
      <g clipPath={`url(#pbc-${uid})`}>
        <g className="juice-rise">
          <rect x="8" y={surfaceY + 2} width="56" height={Math.max(0, innerBottom - surfaceY - 2) + 3} fill={`url(#pb-${uid})`} />
          {fill > 0 && (
            <g transform={`translate(0 ${surfaceY})`}>
              <path className="juice-wave"
                    d="M-40 0 Q-30 -4 -20 0 T0 0 T20 0 T40 0 T60 0 T80 0 T100 0 T120 0 V8 H-40 Z"
                    fill="#6ee7b7" />
            </g>
          )}
          {bubbles && (
            <g clipPath={`url(#pbl-${uid})`} fill="#a7f3d0" opacity="0.8">
              <circle className="juice-bubble" cx="26" cy={innerBottom - 6} r="1.7" />
              <circle className="juice-bubble" cx="38" cy={innerBottom - 4} r="2.2" style={{ animationDelay: "0.9s" }} />
              <circle className="juice-bubble" cx="48" cy={innerBottom - 8} r="1.4" style={{ animationDelay: "1.8s" }} />
            </g>
          )}
        </g>
      </g>
      {/* tank outline on top of the liquid */}
      <rect x="6" y="11" width="60" height="103" rx="11" fill="rgba(148,163,184,0.05)"
            stroke={full ? "#34d399" : "#475569"} strokeWidth="2" />
      {/* direct % label with a dark keyline so it reads on the liquid (full size
          only; the mini battery's number lives in the summary text beside it) */}
      {!mini && (
        <text x="36" y="67" textAnchor="middle" fontSize="15" fontWeight="700"
              fill="#f8fafc" stroke="#0f172a" strokeWidth="3" paintOrder="stroke">
          {pct == null ? "—" : `${fmt(Math.min(pct, 100), 0)}%`}
        </text>
      )}
    </svg>
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
                  <span
                    title={sc.assignment_risk.note}
                    className="cursor-help rounded-full border border-amber-500/40 bg-amber-500/15 px-2 py-0.5 text-[10px] font-medium text-amber-300"
                  >
                    {sc.assignment_risk.trigger === "extrinsic"
                      ? `assignment risk (extrinsic ${fmt(sc.assignment_risk.extrinsic, 2)})`
                      : `assignment risk (div ${fmt(sc.assignment_risk.dividend, 2)} ex ${sc.assignment_risk.ex_date})`}
                  </span>
                )}
                {sc.dte != null && sc.dte <= 2 && (
                  <span className="rounded-full border border-amber-500/40 bg-amber-500/15 px-2 py-0.5 text-[10px] font-medium text-amber-300">expiring</span>
                )}
              </span>
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
                     title="Extrinsic (time value) sold at entry is the target; capturing all of it is max profit on the short."
                   >
                     extrinsic captured {money(sc.extrinsic_captured_total)} / {money(sc.entry_extrinsic_total)}
                     {sc.extrinsic_remaining_total != null && (
                       <span className="text-slate-600"> · {money(sc.extrinsic_remaining_total)} left</span>
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

// (1) Intrinsic balance — the LEAP orange beside whether the LEAP's intrinsic
// (asset) still covers the shorts' intrinsic (liability), so a stock move washes out.
function IntrinsicBalance({ p }) {
  const pulp = pulpOf(p);
  const bal = balanceOf(p, (p.short_calls || []).map((sc) => ({ sc })));
  const hasBal = bal.longIntrinsic != null;
  const covered = bal.covered;
  return (
    <div className="flex items-center gap-3">
      <Orange uid={`bal-${p.ticker}`} pct={pulp.pct} maintenance={p.leap_health?.maintenance_status || "unknown"}
              maintained={pulp.pct != null && pulp.pct >= 100} />
      <div className="min-w-0">
        <div className="mb-1 flex items-center gap-1.5">
          <span className="text-[10px] uppercase tracking-wide text-slate-500">Intrinsic balance</span>
          {hasBal && (
            <span className={`rounded-full border px-1.5 py-0.5 text-[9px] font-semibold uppercase ${
              covered ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300" : "border-rose-500/40 bg-rose-500/15 text-rose-300"}`}>
              {covered ? "balanced" : "unbalanced"}
            </span>
          )}
        </div>
        {hasBal ? (
          <>
            <div className="text-sm text-slate-300">
              LEAP <span className="font-semibold text-slate-100">{money(bal.longIntrinsic)}</span>
              {" vs short "}<span className="font-semibold text-slate-100">{money(bal.shortIntrinsic)}</span>
            </div>
            <div className="text-xs text-slate-500">
              {covered ? `${money(bal.net)} cushion — a stock move washes out`
                       : `short intrinsic outruns the LEAP by ${money(-bal.net)}`}
            </div>
          </>
        ) : <div className="text-xs text-slate-500">No mark yet — intrinsic balance pending.</div>}
      </div>
    </div>
  );
}

// (2) Extrinsic burn-off — the filling juice-battery + the recovery numbers.
function ExtrinsicBurnoff({ ticker, payback }) {
  const pb = payback || {};
  const has = pb.leap_extrinsic_at_entry != null && pb.leap_extrinsic_at_entry > 0;
  const done = has && pb.pct_complete >= 100;
  return (
    <div className="flex items-center gap-3">
      {has && <PaybackTank uid={ticker} pct={pb.pct_complete} />}
      <div className="min-w-0">
        <div className="mb-1 text-[10px] uppercase tracking-wide text-slate-500">Extrinsic burn — paid back</div>
        {has ? (
          <>
            <div className={`text-2xl font-semibold leading-none ${done ? "text-emerald-300" : "text-slate-100"}`}>{fmt(pb.pct_complete, 0)}%</div>
            <div className="mt-1.5 text-xs text-slate-500">{money(pb.collected_to_date)} of {money(pb.leap_extrinsic_at_entry)} recovered</div>
            <div className="text-xs">
              {done ? <span className="text-emerald-300">fully paid back — the rest is gravy</span>
                    : <span className="text-slate-500">{money(pb.remaining_to_payback)} still to earn back</span>}
            </div>
          </>
        ) : <div className="text-xs text-slate-500">No entry extrinsic recorded — nothing to pay back.</div>}
      </div>
    </div>
  );
}

// Plain-language "where do I stand" for the whole book, at the top of the page:
// how the positions net out (balanced, paid-back, captured, needing attention)
// woven together with the book-level decision (market lean, engine, can-I-add)
// from the portfolio-risk payload. A narrative, not a dashboard — the tiles below
// carry the numbers.
function BookSummary({ positions, diffsByTicker, payback, risk }) {
  const n = positions.length;
  let balanced = 0;
  const unbalanced = [];
  let collected = 0, atEntry = 0, captured = 0, entry = 0, attention = 0;
  for (const p of positions) {
    const bal = balanceOf(p, (p.short_calls || []).map((sc) => ({ sc })));
    if (bal.covered === true) balanced++;
    else if (bal.covered === false) unbalanced.push(p.ticker);
    const pb = payback?.[p.ticker];
    if (pb?.leap_extrinsic_at_entry) { collected += pb.collected_to_date || 0; atEntry += pb.leap_extrinsic_at_entry; }
    for (const sc of p.short_calls || []) {
      if (sc.entry_extrinsic_total != null) { entry += Number(sc.entry_extrinsic_total); captured += Number(sc.extrinsic_captured_total || 0); }
    }
    if (p.needs_review || p.defend || p.whipsaw?.tripped || (diffsByTicker[p.ticker]?.length)) attention++;
  }
  const burnOff = atEntry > 0 ? (collected / atEntry) * 100 : null;
  const shortCap = entry > 0 ? (captured / entry) * 100 : null;

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
    (unbalanced.length
      ? ` — ${balanced} intrinsically balanced, ${unbalanced.join(", ")} unbalanced (the short's intrinsic has outrun the LEAP)`
      : ", all intrinsically balanced (each LEAP still covers its shorts)") + ".");
  if (burnOff != null || shortCap != null) {
    const a = burnOff != null ? `earned back ${fmt(burnOff, 0)}% of your LEAP extrinsic` : "";
    const b = shortCap != null ? `captured ${fmt(shortCap, 0)}% of this cycle's short premium` : "";
    sentences.push(`Across the book you've ${[a, b].filter(Boolean).join(" and ")}.`);
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
        <span><span className="text-slate-500">balanced </span><span className={`font-semibold ${unbalanced.length ? "text-amber-300" : "text-emerald-300"}`}>{balanced}/{n}</span></span>
        {burnOff != null && <span><span className="text-slate-500">burn off </span><span className="font-semibold text-slate-100">{fmt(burnOff, 0)}%</span></span>}
        {shortCap != null && <span><span className="text-slate-500">short capture </span><span className="font-semibold text-slate-100">{fmt(shortCap, 0)}%</span></span>}
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
// intrinsic balance, extrinsic burn-off, short-call capture. Expanded: those three
// in full (orange, juice-battery, short list), plus any active safety alert
// (reconciliation, defend, whipsaw) which also auto-opens the row.
function PositionRow({ p, diffs, payback, recs, onRecsChanged, focusCard, focused, setRolling, onOpenTicket, afterResolve }) {
  const shorts = p.short_calls || [];
  const hasAlert = !!(p.needs_review || p.defend || p.whipsaw?.tripped || (diffs && diffs.length));
  // Collapsed by default for a clean, scannable list; a tapped-alert deep link
  // (focused) opens the row so the operator lands on the thing to act on.
  const [open, setOpen] = React.useState(false);
  React.useEffect(() => { if (focused) setOpen(true); }, [focused]);

  const pulp = pulpOf(p);
  const bal = balanceOf(p, shorts.map((sc) => ({ sc })));
  const covered = bal.covered;
  const paid = payback?.pct_complete;
  const hasPay = payback?.leap_extrinsic_at_entry != null && payback.leap_extrinsic_at_entry > 0;
  const shortPct = shortCapturePct(shorts);

  return (
    <div className={`min-w-0 rounded-xl border bg-slate-900/60 transition ${focused ? "border-emerald-400/70 ring-1 ring-emerald-400/40" : "border-slate-800"}`}>
      <button onClick={() => setOpen((v) => !v)} aria-expanded={open}
              className="flex w-full items-center justify-between gap-3 p-4 text-left hover:bg-slate-900/40">
        <span className="flex min-w-0 items-center gap-2">
          <span className={`text-slate-500 transition-transform ${open ? "rotate-90" : ""}`}>▸</span>
          <span className="text-sm font-semibold text-slate-100">{p.ticker}</span>
          <span className="truncate text-xs text-slate-500">{p.sector}</span>
          {hasAlert && (
            <span title="Needs attention — expand to resolve"
                  className="rounded-full border border-rose-500/50 bg-rose-500/15 px-1.5 py-0.5 text-[9px] font-semibold text-rose-300">⚠</span>
          )}
        </span>
        {/* collapsed summary — the three things, each with its tiny visual */}
        <span className="flex flex-wrap items-center justify-end gap-x-4 gap-y-1 text-xs">
          <span className="flex items-center gap-1.5">
            <Orange uid={`mini-${p.ticker}`} pct={pulp.pct} maintenance="unknown" mini />
            <span className="text-slate-500">intrinsic</span>
            {bal.longIntrinsic == null
              ? <span className="text-slate-500">—</span>
              : <span className={`font-semibold ${covered ? "text-emerald-300" : "text-rose-300"}`}>{covered ? "balanced" : "unbalanced"}</span>}
          </span>
          <span className="flex items-center gap-1.5">
            {hasPay
              ? <PaybackTank uid={`mini-${p.ticker}`} pct={paid} mini />
              : <span className="inline-block w-5" />}
            <span className="text-slate-500">burn off</span>
            <span className={`font-semibold ${hasPay && paid >= 100 ? "text-emerald-300" : "text-slate-200"}`}>{hasPay ? `${fmt(paid, 0)}%` : "—"}</span>
          </span>
          <span className="flex items-center gap-1">
            <span className="text-slate-500">short</span>
            <span className="font-semibold text-slate-200">{shortPct == null ? "none" : `${fmt(shortPct, 0)}% cap`}</span>
          </span>
        </span>
      </button>

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

          {/* (1) intrinsic balance + (2) extrinsic burn-off */}
          <div className="grid gap-4 rounded-xl border border-slate-800 bg-slate-900/40 p-4 sm:grid-cols-2">
            <IntrinsicBalance p={p} />
            <div className="sm:border-l sm:border-slate-800 sm:pl-4">
              <ExtrinsicBurnoff ticker={p.ticker} payback={payback} />
            </div>
          </div>

          {/* (3) short-call capture */}
          <ShortCalls p={p} shorts={shorts} setRolling={setRolling} onOpenTicket={onOpenTicket} />

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

  // Open recommendations grouped by ticker for the position cards.
  const recsByTicker = React.useMemo(() => {
    const out = {};
    for (const r of recsData?.open || []) {
      const t = (r.ticker || "").toUpperCase();
      if (t) (out[t] ||= []).push(r);
    }
    return out;
  }, [recsData]);

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
        <BookSummary positions={positions} diffsByTicker={openDiffsByTicker}
                     payback={data?.extrinsic_payback} risk={risk} />
      )}
      <PortfolioRisk data={risk} />

      {positions.length === 0 && <Card>No open positions.</Card>}
      {positions.map((p) => (
        <div key={p.ticker} id={`pos-${p.ticker}`} className="scroll-mt-20">
          <PositionRow
            p={p}
            diffs={openDiffsByTicker[p.ticker]}
            payback={data?.extrinsic_payback?.[p.ticker]}
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
