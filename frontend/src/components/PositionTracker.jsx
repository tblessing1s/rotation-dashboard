import React from "react";
import { api } from "../api.js";
import { Card, Meter, Pill, Light, Loading, money, fmt, pct, useApi } from "./ui.jsx";
import RollModal from "./RollModal.jsx";
import PortfolioRisk from "./PortfolioRisk.jsx";
import BurnPanel from "./BurnPanel.jsx";
import { Orange, pulpOf, balanceOf } from "./JuiceStand.jsx";
import { useToast } from "./Toast.jsx";
import { submitOrder } from "../orderFlow.js";

// Next-earnings chip. Amber when inside the warning window (roll deep-ITM or
// exit before the report); muted otherwise; nothing when the date is unknown.
function EarningsBadge({ earnings }) {
  if (!earnings || !earnings.date) {
    return <span className="text-xs text-slate-600">earnings —</span>;
  }
  const warn = earnings.warning;
  const suspect = earnings.conflict || earnings.stale;
  const d = earnings.days_until;
  const when = d == null ? "" : d < 0 ? ` (${Math.abs(d)}d ago)` : ` (${d}d)`;
  const title = earnings.conflict
    ? `Providers disagree — Alpha Vantage ${earnings.av_date} vs Schwab ${earnings.schwab_date}. Confirm the real report date before the cycle spans it.`
    : earnings.stale
    ? `Earnings date last refreshed ${earnings.fetched_at || "never"} — it may be out of date; a wrong date silently disarms the guardrail.`
    : warn ? "Earnings approaching — roll the short deep-ITM or exit" : "Next earnings report";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${
        warn || suspect ? "border-amber-500/40 bg-amber-500/15 text-amber-300" : "border-slate-700 bg-slate-800/40 text-slate-400"
      }`}
      title={title}
    >
      ⚠ earnings {earnings.date}{when}
      {suspect && <span className="text-rose-300">· {earnings.conflict ? "sources differ" : "stale"}</span>}
    </span>
  );
}

// Delta-coverage guardrail for the PMCC diagonal. The coverage payload is fetched
// once at the card level (useCoverage) and passed in, so the health verdict and
// this detail panel share a single chain hit. `bare` drops the section chrome for
// rendering inside an expander. Degrades to a muted note when live deltas aren't
// available (Schwab off / off-hours).
function DeltaCoverage({ data, bare = false }) {
  if (!data || data.status === "none") return null;
  const wrapCls = bare ? "" : "mt-4 border-t border-slate-800 pt-3";

  if (data.status === "unknown") {
    return (
      <div className={wrapCls}>
        {!bare && <div className="mb-1 text-xs uppercase tracking-wide text-slate-500">Delta coverage</div>}
        <div className="text-xs text-slate-400">{data.message || "Live deltas unavailable."}</div>
      </div>
    );
  }

  const tone = { red: "text-rose-300", yellow: "text-amber-300", green: "text-emerald-300" }[data.status] || "text-slate-300";
  const badge = data.status === "green" ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
    : data.status === "yellow" ? "border-amber-500/40 bg-amber-500/15 text-amber-300"
    : "border-rose-500/40 bg-rose-500/15 text-rose-300";
  const shorts = data.shorts || [];
  const leaps = data.leaps || (data.leap ? [data.leap] : []);
  const multiLeg = leaps.length > 1;
  const multiShort = shorts.length > 1;
  return (
    <div className={wrapCls}>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs uppercase tracking-wide text-slate-500">{bare ? "Deltas" : "Delta coverage"}</span>
        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase ${badge}`}>
          {data.covered ? "covered" : "uncovered"}
        </span>
      </div>
      {/* Contract-weighted totals — the honest apples-to-apples for coverage. The
          per-leg deltas sit underneath so a multi-tranche long still reads clearly. */}
      <div className="text-sm text-slate-300">
        long Δ <span className="font-semibold text-slate-100">{fmt(data.long_total_delta, 2)}</span>
        {multiLeg && <span className="text-slate-500"> ({leaps.length} legs)</span>}
        {shorts.length > 0 && (
          <> · short Δ <span className="font-semibold text-slate-100">{fmt(data.short_total_delta, 2)}</span></>
        )}
        {" · net Δ "}
        <span className={`font-semibold ${data.net_delta < 0 ? "text-rose-300" : "text-slate-100"}`}>
          {fmt(data.net_delta, 2)}
        </span>
      </div>
      {(multiLeg || multiShort) && (
        <div className="mt-0.5 text-xs text-slate-500">
          {multiLeg && (
            <>long {leaps.map((l, i) => (
              <span key={i}>{fmt(l.delta, 2)}×{l.contracts}{i < leaps.length - 1 ? " + " : ""}</span>
            ))}</>
          )}
          {multiLeg && multiShort && " · "}
          {multiShort && (
            <>short {shorts.map((s, i) => (
              <span key={i}>{fmt(s.delta, 2)}×{s.contracts}{i < shorts.length - 1 ? " + " : ""}</span>
            ))}</>
          )}
        </div>
      )}
      <div className={`mt-1 text-xs ${tone}`}>{data.message}</div>
    </div>
  );
}

// Compact LEAP long-leg health strip: DTE, extrinsic remaining (+ weeks-of-juice
// runway), net weekly maintenance (self-funding green / burning red), delta with
// a velocity trend arrow, and a ROLL LEAP DUE badge (mirrors the ROLL NOW badge).
// Tapping the badge fetches the roll-cost estimate + reserve check inline.
function LeapHealth({ ticker, health, bare = false }) {
  const [est, setEst] = React.useState(null);
  const [open, setOpen] = React.useState(false);
  if (!health) return null;

  const m = health.net_weekly_maintenance;
  const mTone = m == null ? "text-slate-400" : m >= 0 ? "text-emerald-300" : "text-rose-300";
  const drop = health.delta_velocity?.drop;
  const arrow = drop == null ? "" : drop > 0.0001 ? " ▼" : drop < -0.0001 ? " ▲" : "";
  const arrowTone = drop > 0.0001 ? "text-rose-300" : drop < -0.0001 ? "text-emerald-300" : "text-slate-400";

  const showEstimate = async () => {
    setOpen((v) => !v);
    if (!est) {
      try { setEst(await api.leapRollEstimate(ticker)); } catch (e) { setEst({ error: String(e.message || e) }); }
    }
  };

  return (
    <div className={bare ? "" : "mt-4 border-t border-slate-800 pt-3"}>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs uppercase tracking-wide text-slate-500">{bare ? "Long leg" : "LEAP health"}</span>
        <span className="flex items-center gap-1.5">
          {health.juice_adequate === false && (
            <span
              title={`Trailing weekly juice ${fmt(health.weekly_juice_yield_pct, 2)}% of LEAP capital is below the ${fmt(health.juice_target_pct, 2)}% income target — this position no longer clears the strategy's income bar. Roll to a better strike/week or redeploy the capital.`}
              className="cursor-help rounded-full border border-amber-500/50 bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold text-amber-300"
            >
              JUICE LOW
            </span>
          )}
          {health.roll_due && (
            <button
              onClick={showEstimate}
              title={(health.roll_reasons || []).join("; ") || "LEAP roll recommended"}
              className="rounded-full border border-amber-500/50 bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold text-amber-300 hover:bg-amber-500/25"
            >
              ROLL LEAP DUE
            </button>
          )}
        </span>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-slate-300">
        <span>DTE <span className="font-semibold text-slate-100">{health.leap_dte ?? "—"}</span></span>
        {health.weekly_juice_yield_pct != null && (
          <span title={`Trailing weekly juice as a % of LEAP capital vs the ${fmt(health.juice_target_pct, 2)}% income target`}>
            juice <span className={`font-semibold ${health.juice_adequate === false ? "text-amber-300" : "text-slate-100"}`}>
              {fmt(health.weekly_juice_yield_pct, 2)}%
            </span>
            <span className="text-slate-500"> / {fmt(health.juice_target_pct, 2)}% tgt</span>
          </span>
        )}
        <span title={health.leap_extrinsic_below_intrinsic ? "Mark quoted below intrinsic — a liquidity signal, not real negative extrinsic" : ""}>
          extrinsic <span className="font-semibold text-slate-100">{money(health.leap_extrinsic_remaining)}</span>
          {health.leap_extrinsic_weeks_remaining != null && (
            <span className="text-slate-500"> (~{fmt(health.leap_extrinsic_weeks_remaining, 1)} wk juice)</span>
          )}
          {health.leap_extrinsic_below_intrinsic && <span className="text-amber-400"> ⚠</span>}
        </span>
        <span>maint. <span className={`font-semibold ${mTone}`}>{m == null ? "—" : `${m >= 0 ? "+" : ""}${money(m)}/wk`}</span></span>
        <span>Δ <span className="font-semibold text-slate-100">{fmt(health.leap_delta, 2)}</span>
          {arrow && <span className={arrowTone}>{arrow}</span>}
        </span>
      </div>
      {open && (
        <div className="mt-2 rounded-lg border border-amber-800 bg-amber-500/10 p-3 text-xs text-slate-300">
          {!est ? (
            <span className="text-slate-500">Estimating roll cost…</span>
          ) : est.error || est.new_leap == null ? (
            <span className="text-slate-500">{est.error || "Roll estimate unavailable."}</span>
          ) : (
            <>
              Roll into <span className="font-semibold text-slate-100">{fmt(est.new_leap?.strike, 1)}C</span> ~{est.new_leap?.target_dte} DTE ·
              {" "}est. net {est.net_debit >= 0 ? "debit" : "credit"}{" "}
              <span className={est.net_debit >= 0 ? "text-rose-300" : "text-emerald-300"}>{money(Math.abs(est.net_debit))}</span>
              {" · "}reserve{" "}
              <span className={est.reserve_ok ? "text-emerald-300" : "text-rose-300"}>{est.reserve_ok ? "OK" : "BREACH"}</span>
              {" "}(free after {money(est.free_cash_after)} vs {money(est.reserve_required)})
              <div className="mt-1 text-slate-500">Estimated from trailing vol; the staged roll re-prices from the live chain. The operator transmits.</div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

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

// Kill-switch verdict for one position (RS3M vs SPY / vs Sector), inlined on the
// card — this absorbed the old Kill Switch tab. Quiet single line when green;
// a toned banner with the suggested action when the switch has tripped.
function KillSwitchStrip({ ks, bare = false }) {
  if (!ks) return null;
  if (!ks.alert) {
    return (
      <div className={bare ? "" : "mt-4 border-t border-slate-800 pt-3"}>
        <div className="flex items-center justify-between text-xs">
          <span className="flex items-center gap-2 uppercase tracking-wide text-slate-500">
            Kill switch <Light status={ks.status} size="h-2.5 w-2.5" />
          </span>
          <span className="text-slate-500">
            RS3M vs SPY <span className={ks.rs3m_vs_spy < 0 ? "text-rose-400" : "text-slate-300"}>{pct(ks.rs3m_vs_spy)}</span>
            {" · "}vs Sector <span className={ks.rs3m_vs_sector < 0 ? "text-rose-400" : "text-slate-300"}>{pct(ks.rs3m_vs_sector)}</span>
          </span>
        </div>
      </div>
    );
  }
  const red = ks.status === "red";
  return (
    <div className={`mt-4 rounded-lg border p-3 text-sm ${
      red ? "border-rose-800 bg-rose-500/10" : "border-amber-800 bg-amber-500/10"}`}>
      <div className="flex items-center justify-between">
        <span className={`text-xs font-semibold uppercase tracking-wide ${red ? "text-rose-300" : "text-amber-300"}`}>
          Kill switch — {ks.status}
        </span>
        <span className="text-xs text-slate-400">
          vs SPY {pct(ks.rs3m_vs_spy)} · vs Sector {pct(ks.rs3m_vs_sector)}
        </span>
      </div>
      <p className={`mt-1 ${red ? "text-rose-200" : "text-amber-200"}`}>{ks.suggested_action}</p>
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

// A collapsed detail section. The two things the page is FOR — health and income —
// stay expanded up top; supporting detail (structure, long-leg internals, deltas,
// relative strength) tucks in here. `defaultOpen` forces it open when it carries
// an active alert (a tripped kill switch) so nothing important hides.
function Expander({ title, children, defaultOpen = false }) {
  return (
    <details open={defaultOpen} className="group mt-3 border-t border-slate-800 pt-3">
      <summary className="flex cursor-pointer list-none items-center justify-between text-xs uppercase tracking-wide text-slate-500 hover:text-slate-300">
        <span>{title}</span>
        <span className="text-slate-600 transition-transform group-open:rotate-90">▸</span>
      </summary>
      <div className="mt-2">{children}</div>
    </details>
  );
}

// Synthesize ONE health verdict per position from every guardrail already on the
// card. Worst-severity wins; the top reason becomes the headline, the rest a
// tooltip. This is the "is the position healthy?" answer the page leads with.
function positionHealth(p, coverage, ks, health) {
  const red = [];
  const amber = [];
  if (p.needs_review) red.push("State unverified vs the broker — resolve before trading");
  if (p.whipsaw?.tripped) red.push("Whipsaw — exit, don't defend again");
  if (p.circuit_breaker_status?.tripped) red.push("Circuit breaker — the exit line was hit");
  if (ks?.status === "red") red.push("Kill switch red — exit the position");
  if (p.defend) red.push("Stock is below the short strike — defend");
  if (coverage?.status === "red") red.push(coverage.message || "Delta uncovered — the long isn't covering the short");
  if (health?.leap_extrinsic_below_intrinsic) amber.push("LEAP marked below intrinsic (thin liquidity)");
  if (ks?.status === "yellow") amber.push("Kill switch watch — relative strength slipping");
  if (coverage?.status === "yellow") amber.push("Delta coverage thinning");
  if (health?.net_juice_per_week != null && health.net_juice_per_week < 0) amber.push("Burn is outpacing juice this week");
  else if (health?.coverage?.status === "flagged") amber.push("Juice is barely covering burn");
  if (health?.juice_adequate === false) amber.push("Juice below the income target");
  if (health?.roll_due) amber.push("LEAP roll due");
  if (p.earnings?.warning) amber.push("Earnings approaching — roll deep-ITM or exit");
  if (red.length) return { level: "red", label: "At risk", reason: red[0], all: red };
  if (amber.length) return { level: "amber", label: "Watch", reason: amber[0], all: amber };
  return { level: "green", label: "Healthy", reason: "Covered · juice ≥ burn · delta above the floor", all: [] };
}

const VERDICT_TONE = {
  red: { stripe: "border-l-rose-500", badge: "bg-rose-500/15 text-rose-300 border-rose-500/40", reason: "text-rose-200/90" },
  amber: { stripe: "border-l-amber-500", badge: "bg-amber-500/15 text-amber-300 border-amber-500/40", reason: "text-amber-200/90" },
  green: { stripe: "border-l-emerald-500", badge: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40", reason: "text-slate-400" },
};

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
function PaybackTank({ uid, pct }) {
  const fill = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  const full = pct != null && pct >= 100;
  const innerTop = 15;
  const innerBottom = 110;
  const surfaceY = innerBottom - ((innerBottom - innerTop) * fill) / 100;
  const bubbles = fill >= 15;
  return (
    <svg
      viewBox="0 0 72 122"
      className={`h-24 w-[3.4rem] shrink-0 ${full ? "drop-shadow-[0_0_10px_rgba(52,211,153,0.5)]" : ""}`}
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
      {/* direct % label with a dark keyline so it reads on the liquid */}
      <text x="36" y="67" textAnchor="middle" fontSize="15" fontWeight="700"
            fill="#f8fafc" stroke="#0f172a" strokeWidth="3" paintOrder="stroke">
        {pct == null ? "—" : `${fmt(Math.min(pct, 100), 0)}%`}
      </text>
    </svg>
  );
}

function BalanceHero({ p, verdict, health, payback }) {
  const t = VERDICT_TONE[verdict.level];
  const pulp = pulpOf(p);
  const maintenance = health?.maintenance_status || "unknown";
  // balanceOf expects the juice-stand's {sc, ...} row shape, not raw short_calls.
  const bal = balanceOf(p, (p.short_calls || []).map((sc) => ({ sc })));
  const hasBal = bal.longIntrinsic != null;
  const covered = bal.covered;
  const balBadge = covered
    ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
    : "border-rose-500/40 bg-rose-500/15 text-rose-300";

  const pb = payback || {};
  const paidPct = pb.pct_complete;
  const hasPayback = pb.leap_extrinsic_at_entry != null && pb.leap_extrinsic_at_entry > 0;
  const done = hasPayback && paidPct >= 100;

  return (
    <div className={`mt-4 rounded-xl border border-slate-800 border-l-2 bg-slate-900/40 p-4 ${t.stripe}`}>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold uppercase tracking-wide ${t.badge}`}
              title={verdict.all.length > 1 ? verdict.all.join(" · ") : undefined}>
          {verdict.label}
        </span>
        <span className={`text-sm ${t.reason}`}>
          {verdict.reason}
          {verdict.all.length > 1 && <span className="ml-1 text-xs text-slate-500">+{verdict.all.length - 1} more</span>}
        </span>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        {/* Intrinsic balance — the LEAP's intrinsic (asset) vs the shorts' (liability). */}
        <div className="flex items-center gap-3">
          <Orange uid={`bal-${p.ticker}`} pct={pulp.pct} maintenance={maintenance}
                  maintained={pulp.pct != null && pulp.pct >= 100} />
          <div className="min-w-0">
            <div className="mb-1 flex items-center gap-1.5">
              <span className="text-[10px] uppercase tracking-wide text-slate-500">Intrinsic balance</span>
              {hasBal && (
                <span className={`rounded-full border px-1.5 py-0.5 text-[9px] font-semibold uppercase ${balBadge}`}>
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
                  {covered
                    ? `${money(bal.net)} cushion — a stock move washes out`
                    : `short intrinsic outruns the LEAP by ${money(-bal.net)}`}
                </div>
              </>
            ) : (
              <div className="text-xs text-slate-500">No mark yet — intrinsic balance pending.</div>
            )}
          </div>
        </div>

        {/* Extrinsic paid back — the LEAP's entry extrinsic (the burn) recovered by
            juice, as a filling juice battery. */}
        <div className="flex items-center gap-3 sm:border-l sm:border-slate-800 sm:pl-4">
          {hasPayback && <PaybackTank uid={p.ticker} pct={paidPct} />}
          <div className="min-w-0">
            <div className="mb-1 text-[10px] uppercase tracking-wide text-slate-500">Extrinsic burn — paid back</div>
            {hasPayback ? (
              <>
                <div className={`text-2xl font-semibold leading-none ${done ? "text-emerald-300" : "text-slate-100"}`}>
                  {fmt(paidPct, 0)}%
                </div>
                <div className="mt-1.5 text-xs text-slate-500">
                  {money(pb.collected_to_date)} of {money(pb.leap_extrinsic_at_entry)} recovered
                </div>
                <div className="text-xs">
                  {done
                    ? <span className="text-emerald-300">fully paid back — the rest is gravy</span>
                    : <span className="text-slate-500">{money(pb.remaining_to_payback)} still to earn back</span>}
                </div>
              </>
            ) : (
              <div className="text-xs text-slate-500">No entry extrinsic recorded — nothing to pay back.</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// LEAP / shares / stock structure — the position's makeup. Demoted into an
// expander: it's reference, not a moment-to-moment decision.
function PositionFacts({ leap, sh, stockPrice, shortCount }) {
  return (
    <div className="grid gap-4 sm:grid-cols-3">
      <div>
        <div className="text-xs uppercase tracking-wide text-slate-500">LEAP</div>
        <div className="text-sm text-slate-200">{leap.contracts || 0} × {fmt(leap.strike, 0)}C · {leap.dte ?? "—"} DTE</div>
        <div className="text-xs text-slate-500">intrinsic {money(leap.intrinsic)} · extrinsic {money(leap.extrinsic)}</div>
      </div>
      <div>
        <div className="text-xs uppercase tracking-wide text-slate-500">Shares ({sh.count || 0}/{sh.cap || 500})</div>
        <Meter pct={sh.pct_to_cap} tone={sh.locked ? "bg-amber-500" : "bg-sky-500"} />
        <div className="mt-1 text-xs text-slate-500">{sh.locked ? "Cap reached — rotate to a new stock." : `${fmt(sh.pct_to_cap, 0)}% to cap`}</div>
      </div>
      <div>
        <div className="text-xs uppercase tracking-wide text-slate-500">Stock</div>
        <div className="text-sm text-slate-200">{fmt(stockPrice, 2)}</div>
        <div className="text-xs text-slate-500">{shortCount} open short(s)</div>
      </div>
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
                   {sc.extrinsic_captured_pct != null && (
                     <span className={sc.extrinsic_captured_pct >= 75 ? "text-emerald-300" : "text-slate-400"}>
                       {fmt(sc.extrinsic_captured_pct, 0)}% captured
                     </span>
                   )}
                 </div>
                 <div className="mt-1">
                   <Meter
                     pct={sc.extrinsic_captured_pct}
                     tone={sc.extrinsic_captured_pct >= 75 ? "bg-emerald-400" : "bg-sky-500"}
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
function PositionCard({ p, ks, diffs, payback, focused, setRolling, onOpenTicket, afterResolve }) {
  const { data: coverage } = useApi(React.useCallback(() => api.coverage(p.ticker), [p.ticker]), [p.ticker], null);
  const leap = p.leap || {};
  const sh = p.shares || {};
  const shorts = p.short_calls || [];
  const health = p.leap_health_agg || p.leap_health;
  const verdict = positionHealth(p, coverage, ks, health);

  return (
    <Card
      className={focused ? "ring-2 ring-emerald-400/70 transition" : "transition"}
      title={`${p.ticker} · ${p.sector || ""}`}
      right={
        <div className="flex items-center gap-2">
          {p.wash_sale_flag && (
            <span
              title={p.wash_sale_flag.note}
              className="cursor-help rounded-full border border-amber-500/40 bg-amber-500/15 px-2 py-0.5 text-xs font-medium text-amber-300"
            >
              wash-sale window
            </span>
          )}
          <EarningsBadge earnings={p.earnings} />
          <Pill status={p.status === "active" ? "green" : "unknown"}>{p.status}</Pill>
        </div>
      }
    >
      {/* Active alerts — always visible, never behind an expander. */}
      <ReviewPanel ticker={p.ticker} diffs={diffs} onDone={afterResolve} />
      {p.needs_review && (
        <p className="mt-3 text-xs italic text-rose-400/80">
          State unverified against the broker — the metrics below are computed off
          state.json and may not reflect the account until this position is resolved.
        </p>
      )}

      {/* HERO: is the position staying balanced — intrinsic covered + burn paid back. */}
      <BalanceHero p={p} verdict={verdict} health={health} payback={payback} />

      {p.whipsaw?.tripped && (
        <div className="mt-3 rounded-lg border border-rose-500/50 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          <span className="font-semibold text-rose-300">⚠ Whipsaw — exit, don't defend again.</span>{" "}
          {(p.whipsaw.reasons || []).join("; ")}. The roll-down spiral is bleeding this
          position while the kill switch and circuit breaker stay quiet — another roll-down
          just locks a lower strike.
        </div>
      )}

      {/* Income engine — the open shorts and the juice each is capturing. */}
      <ShortCalls p={p} shorts={shorts} setRolling={setRolling} onOpenTicket={onOpenTicket} />

      {p.defend && (
        <DefendPanel ticker={p.ticker} onStage={() => setRolling({ ticker: p.ticker, reason: "defend" })} />
      )}

      {/* A tripped kill switch is an active alert — surface its banner, don't tuck it. */}
      {ks?.alert && <KillSwitchStrip ks={ks} />}

      {/* Supporting detail — one tap away. */}
      {/* Income economics — juice vs burn, take-home, weekly bars. Demoted here;
          this moves to its own Income page. Kept accessible until it does. */}
      {health && (
        <Expander title="Income economics">
          <BurnPanel ticker={p.ticker} health={health} />
        </Expander>
      )}
      <Expander title="Position structure">
        <PositionFacts leap={leap} sh={sh} stockPrice={p.stock_price} shortCount={shorts.length} />
      </Expander>
      {health && (
        <Expander title="LEAP long-leg health">
          <LeapHealth ticker={p.ticker} health={health} bare />
        </Expander>
      )}
      {coverage && coverage.status !== "none" && (
        <Expander title="Delta coverage" defaultOpen={coverage.status === "red"}>
          <DeltaCoverage data={coverage} bare />
        </Expander>
      )}
      {ks && !ks.alert && (
        <Expander title="Relative strength (kill switch)">
          <KillSwitchStrip ks={ks} bare />
        </Expander>
      )}
    </Card>
  );
}

export default function PositionTracker({ intent, onIntentHandled, onOpenTicket } = {}) {
  const toast = useToast();
  const { data, error, loading, reload } = useApi(api.positions, [], null);
  const { data: recon, reload: reloadRecon } = useApi(api.reconcile, [], null);
  const { data: kill } = useApi(api.killSwitch, [], 5 * 60 * 1000);
  const [rolling, setRolling] = React.useState(null); // {ticker, reason}
  const [focusedTicker, setFocusedTicker] = React.useState(null);
  const handledIntentId = React.useRef(null);

  // Deep-link intent from a tapped alert: open the prefilled roll ticket for the
  // ticker, or (for exit/kill-switch alerts) scroll to and highlight its card.
  React.useEffect(() => {
    if (!intent || !data || handledIntentId.current === intent.id) return;
    handledIntentId.current = intent.id;
    const posList = (data.positions || []).filter((p) => p.status !== "closed");
    const pos = posList.find((p) => p.ticker === intent.ticker);
    if (pos && intent.action === "roll" && (pos.short_calls || []).length > 0) {
      setRolling({ ticker: intent.ticker, reason: intent.reason || "scheduled" });
    } else if (pos) {
      setFocusedTicker(intent.ticker);
      requestAnimationFrame(() =>
        document.getElementById(`pos-${intent.ticker}`)
          ?.scrollIntoView({ behavior: "smooth", block: "center" }));
      setTimeout(() => setFocusedTicker((t) => (t === intent.ticker ? null : t)), 2500);
    }
    onIntentHandled?.();
  }, [intent, data, onIntentHandled]);

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
  const killByTicker = {};
  for (const k of kill?.positions || []) killByTicker[k.ticker] = k;

  return (
    <div className="grid gap-4">
      <PortfolioRisk />

      {positions.length === 0 && <Card>No open positions.</Card>}
      {positions.map((p) => (
        <div key={p.ticker} id={`pos-${p.ticker}`} className="scroll-mt-20">
          <PositionCard
            p={p}
            ks={killByTicker[p.ticker]}
            diffs={openDiffsByTicker[p.ticker]}
            payback={data?.extrinsic_payback?.[p.ticker]}
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
          onExecute={runRoll}
          onClose={() => setRolling(null)}
        />
      )}
    </div>
  );
}
