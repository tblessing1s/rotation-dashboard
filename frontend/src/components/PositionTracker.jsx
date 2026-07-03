import React from "react";
import { api } from "../api.js";
import { Card, Stat, Meter, Pill, Loading, money, fmt, useApi } from "./ui.jsx";
import RollModal from "./RollModal.jsx";
import PortfolioRisk from "./PortfolioRisk.jsx";
import { useToast } from "./Toast.jsx";
import { submitOrder } from "../orderFlow.js";

// Next-earnings chip. Amber when inside the warning window (roll deep-ITM or
// exit before the report); muted otherwise; nothing when the date is unknown.
function EarningsBadge({ earnings }) {
  if (!earnings || !earnings.date) {
    return <span className="text-xs text-slate-600">earnings —</span>;
  }
  const warn = earnings.warning;
  const d = earnings.days_until;
  const when = d == null ? "" : d < 0 ? ` (${Math.abs(d)}d ago)` : ` (${d}d)`;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${
        warn ? "border-amber-500/40 bg-amber-500/15 text-amber-300" : "border-slate-700 bg-slate-800/40 text-slate-400"
      }`}
      title={warn ? "Earnings approaching — roll the short deep-ITM or exit" : "Next earnings report"}
    >
      ⚠ earnings {earnings.date}{when}
    </span>
  );
}

// Delta-coverage guardrail for the PMCC diagonal. Fetched lazily per position so
// the chain hit only happens on the Positions tab. Degrades to a muted note when
// live deltas aren't available (Schwab off / off-hours).
function DeltaCoverage({ ticker }) {
  const { data } = useApi(React.useCallback(() => api.coverage(ticker), [ticker]), [ticker], null);
  if (!data || data.status === "none") return null;

  const wrap = (body, tone = "text-slate-400") => (
    <div className="mt-4 border-t border-slate-800 pt-3">
      <div className="mb-1 text-xs uppercase tracking-wide text-slate-500">Delta coverage</div>
      <div className={`text-xs ${tone}`}>{body}</div>
    </div>
  );
  if (data.status === "unknown") return wrap(data.message || "Live deltas unavailable.");

  const tone = { red: "text-rose-300", yellow: "text-amber-300", green: "text-emerald-300" }[data.status] || "text-slate-300";
  const badge = data.status === "green" ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
    : data.status === "yellow" ? "border-amber-500/40 bg-amber-500/15 text-amber-300"
    : "border-rose-500/40 bg-rose-500/15 text-rose-300";
  const shorts = data.shorts || [];
  return (
    <div className="mt-4 border-t border-slate-800 pt-3">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs uppercase tracking-wide text-slate-500">Delta coverage</span>
        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase ${badge}`}>
          {data.covered ? "covered" : "uncovered"}
        </span>
      </div>
      <div className="text-sm text-slate-300">
        LEAP Δ <span className="font-semibold text-slate-100">{fmt(data.leap?.delta, 2)}</span>
        {shorts.length > 0 && (
          <> · short Δ {shorts.map((s, i) => (
            <span key={i} className="font-semibold text-slate-100">
              {fmt(s.delta, 2)}{i < shorts.length - 1 ? ", " : ""}
            </span>
          ))}</>
        )}
        {" · net Δ "}<span className="font-semibold text-slate-100">{fmt(data.net_delta, 2)}</span>
      </div>
      <div className={`mt-1 text-xs ${tone}`}>{data.message}</div>
    </div>
  );
}

// Compact LEAP long-leg health strip: DTE, extrinsic remaining (+ weeks-of-juice
// runway), net weekly maintenance (self-funding green / burning red), delta with
// a velocity trend arrow, and a ROLL LEAP DUE badge (mirrors the ROLL NOW badge).
// Tapping the badge fetches the roll-cost estimate + reserve check inline.
function LeapHealth({ ticker, health }) {
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
    <div className="mt-4 border-t border-slate-800 pt-3">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs uppercase tracking-wide text-slate-500">LEAP health</span>
        {health.roll_due && (
          <button
            onClick={showEstimate}
            title={(health.roll_reasons || []).join("; ") || "LEAP roll recommended"}
            className="rounded-full border border-amber-500/50 bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold text-amber-300 hover:bg-amber-500/25"
          >
            ROLL LEAP DUE
          </button>
        )}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-slate-300">
        <span>DTE <span className="font-semibold text-slate-100">{health.leap_dte ?? "—"}</span></span>
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
      <p className="mt-0.5 text-xs text-slate-500">
        Estimated from trailing vol — the staged roll re-prices from the live chain.
      </p>
    </div>
  );
}

export default function PositionTracker() {
  const toast = useToast();
  const { data, error, loading, reload } = useApi(api.positions, [], null);
  const { data: recon, reload: reloadRecon } = useApi(api.reconcile, [], null);
  const [rolling, setRolling] = React.useState(null); // {ticker, reason}

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

  // Closed positions live on the History tab as cycle records.
  const positions = (data?.positions || []).filter((p) => p.status !== "closed");
  const cap = data?.capital || {};
  const ms = cap.milestones || {};

  return (
    <div className="grid gap-4">
      <PortfolioRisk />
      <Card title="Capital">
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Stat label="Deployed" value={money(cap.capital_deployed)} />
          <Stat label="Reserve req." value={money(cap.reserve_required)} tone={cap.reserve_ok ? "text-slate-100" : "text-rose-300"} />
          <Stat
            label="Operating cash"
            value={money(cap.operating_cash)}
            sub={
              cap.operating_cash_source === "schwab"
                ? "live from Schwab"
                : cap.operating_cash_source === "manual"
                ? (cap.operating_cash_error ? `manual (Schwab: ${cap.operating_cash_error})` : "manual entry")
                : undefined
            }
          />
          <Stat label="Juice YTD" value={money(cap.juice_ytd)} tone="text-emerald-300" />
        </div>
        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          {["half_nut", "quit_safe"].map((k) => (
            ms[k] && (
              <div key={k}>
                <div className="mb-1 flex justify-between text-sm">
                  <span className="text-slate-300">{k === "half_nut" ? "Half-nut ($/mo)" : "Quit-safe ($/mo)"}</span>
                  <span className="text-slate-400">{money(ms[k].current)} / {money(ms[k].target)}</span>
                </div>
                <Meter pct={ms[k].pct} tone="bg-emerald-500" />
              </div>
            )
          ))}
        </div>
      </Card>

      {positions.length === 0 && <Card>No open positions.</Card>}
      {positions.map((p) => {
        const leap = p.leap || {};
        const sh = p.shares || {};
        const shorts = p.short_calls || [];
        return (
          <Card
            key={p.ticker}
            title={`${p.ticker} · ${p.sector || ""}`}
            right={
              <div className="flex items-center gap-2">
                {p.needs_review && (
                  <span
                    title={p.review?.summary || "state.json diverged from the broker — resolve before trading this position"}
                    className="cursor-help rounded-full border border-rose-500/50 bg-rose-500/20 px-2 py-0.5 text-xs font-semibold uppercase text-rose-200"
                  >
                    ⚠ needs review
                  </span>
                )}
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
            <ReviewPanel ticker={p.ticker} diffs={openDiffsByTicker[p.ticker]} onDone={afterResolve} />

            {p.needs_review && (
              <p className="mt-3 text-xs italic text-rose-400/80">
                State unverified against the broker — the metrics below are computed off
                state.json and may not reflect the account until this position is resolved.
              </p>
            )}

            <div className="mt-4 grid gap-4 sm:grid-cols-3">
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
                <div className="text-sm text-slate-200">{fmt(p.stock_price, 2)}</div>
                <div className="text-xs text-slate-500">{shorts.length} open short(s)</div>
              </div>
            </div>

            <LeapHealth ticker={p.ticker} health={p.leap_health} />

            {/* Open shorts — each rollable in place (pick week + strike) */}
            <div className="mt-4 border-t border-slate-800 pt-3">
              <div className="mb-2 flex items-center justify-between">
                <span className="text-xs uppercase tracking-wide text-slate-500">
                  Short calls
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
                <p className="text-xs text-slate-500">No open short — sell this week's call from the Execute tab.</p>
              ) : (
                <div className="space-y-1">
                  {shorts.map((sc, i) => (
                    <div key={i} className="flex items-center justify-between rounded-lg bg-slate-950/60 px-3 py-1.5 text-sm">
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
                            assignment risk (div {fmt(sc.assignment_risk.dividend, 2)} ex {sc.assignment_risk.ex_date})
                          </span>
                        )}
                        {sc.dte != null && sc.dte <= 2 && (
                          <span className="rounded-full border border-amber-500/40 bg-amber-500/15 px-2 py-0.5 text-[10px] font-medium text-amber-300">expiring</span>
                        )}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {p.defend && (
              <DefendPanel
                ticker={p.ticker}
                onStage={() => setRolling({ ticker: p.ticker, reason: "defend" })}
              />
            )}

            <DeltaCoverage ticker={p.ticker} />
          </Card>
        );
      })}

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
