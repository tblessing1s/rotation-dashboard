import React from "react";
import { api } from "../api.js";
import { Pill, Loading, fmt } from "./ui.jsx";
import { useTradeMode, TradeModeBadge, LiveOrderConfirm } from "../tradeMode.jsx";
import { totalDollars } from "../units.js";

function dollars(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return "$" + Number(n).toFixed(2);
}
function bigDollars(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const v = Number(n);
  return (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
}
function pct(n, d = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${Number(n).toFixed(d)}%`;
}

// juice/wk and cushion-in-ATR-units (roll-dialog audit §1.1/§1.3) are computed
// SERVER-SIDE ONLY (option_chain.roll_options -> roll_advisor.juice_per_week/
// cushion_atr) and arrive pre-attached on every strike row as
// juice_per_week_pct/cushion_atr — this component does no price arithmetic of
// its own, only ranks/displays what's already there, so there is exactly one
// implementation of the money math to keep correct.

/**
 * Roll an open short call. The user decides two things independently:
 *   • week   — SAME week (keep the current expiration) or a DIFFERENT week
 *   • strike — SAME strike or a DIFFERENT one (e.g. deep-ITM into earnings)
 * The modal shows the live buy-to-close cost of the current short and the new
 * premium for the chosen leg, nets them, and submits a single roll_short action.
 * sourceRecId (optional): the trust-layer recommendation that staged this roll —
 * carried into the /api/execute payload as source_rec_id so the execution
 * matches back to its recommendation.
 */
// A client-generated order reference: opaque, unique, and the ONLY thing that lets
// the backend collapse a retry/refresh into one order. Prefer crypto.randomUUID;
// fall back for older/non-secure contexts.
function newOrderRef() {
  try {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return `cor_${crypto.randomUUID()}`;
  } catch { /* fall through */ }
  return `cor_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

export default function RollModal({ ticker, reason = "scheduled", sourceRecId, onExecute, onClose }) {
  const [data, setData] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [weekMode, setWeekMode] = React.useState("same"); // same | different
  const [expiration, setExpiration] = React.useState(null);
  const [strike, setStrike] = React.useState(null);
  const [qty, setQty] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [execErr, setExecErr] = React.useState(null);
  const tradeMode = useTradeMode(); // "paper" | "live" | null — is this roll routed to Schwab?
  const [pendingLive, setPendingLive] = React.useState(null); // live roll awaiting explicit confirm

  // A stable client_order_ref for THIS staged roll: generated once when the modal
  // opens and reused across every retry so a lost-response retry — or a mid-flight
  // browser refresh that remounts this modal — can never place a SECOND order (the
  // backend keys idempotency on it). Persisted in sessionStorage so a reload resumes
  // the same ref instead of minting a new one; cleared on a confirmed terminal
  // outcome so the next distinct roll starts fresh.
  const refKey = `cfm-roll-ref:${ticker}`;
  const clientOrderRef = React.useRef(null);
  if (clientOrderRef.current === null) {
    let stored = null;
    try { stored = sessionStorage.getItem(refKey); } catch { /* private mode */ }
    clientOrderRef.current = stored || newOrderRef();
    try { sessionStorage.setItem(refKey, clientOrderRef.current); } catch { /* ignore */ }
  }
  function clearOrderRef() {
    clientOrderRef.current = null;
    try { sessionStorage.removeItem(refKey); } catch { /* ignore */ }
  }

  React.useEffect(() => {
    let live = true;
    setLoading(true); setError(null);
    api.rollOptions(ticker)
      .then((d) => {
        if (!live) return;
        if (d.error) { setError(d.error); return; }
        setData(d);
        const cur = d.expirations.find((e) => e.is_current_week) || d.expirations[0];
        setExpiration(cur?.expiration || null);
        const sug = cur?.strikes?.find((s) => s.suggested) || cur?.strikes?.[0];
        setStrike(sug ? sug.strike : null);
        setQty(String(d.current_short?.contracts ?? 1));
      })
      .catch((e) => { if (live) setError(e.message); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [ticker]);

  const cur = data?.current_short;
  const exps = data?.expirations || [];
  const currentExp = exps.find((e) => e.is_current_week) || exps[0] || null;

  // Switching same/different week re-anchors the selected expiration and strike.
  function chooseWeekMode(mode) {
    setWeekMode(mode);
    if (mode === "same") {
      setExpiration(currentExp?.expiration || null);
    } else {
      const other = exps.find((e) => !e.is_current_week);
      setExpiration(other?.expiration || currentExp?.expiration || null);
    }
  }

  const selectedExp = exps.find((e) => e.expiration === expiration) || null;
  const strikesForExp = selectedExp?.strikes || [];
  // Keep the chosen strike valid when the expiration changes under it.
  React.useEffect(() => {
    if (!strikesForExp.length) return;
    if (!strikesForExp.some((s) => s.strike === strike)) {
      const sug = strikesForExp.find((s) => s.suggested) || strikesForExp[0];
      setStrike(sug ? sug.strike : null);
    }
  }, [expiration]); // eslint-disable-line react-hooks/exhaustive-deps

  const chosen = strikesForExp.find((s) => s.strike === strike) || null;
  const qtyNum = Number(qty) || 0;
  const sameStrike = cur && chosen && cur.strike === chosen.strike;
  const sameWeek = cur && selectedExp && cur.expiration === selectedExp.expiration;

  const buyback = cur?.current_mark != null ? totalDollars(cur.current_mark, qtyNum) : null;
  const newCredit = chosen?.mark != null ? totalDollars(chosen.mark, qtyNum) : null;
  const netCredit = buyback != null && newCredit != null ? newCredit - buyback : null;
  const parityBand = data?.juice_parity_band_pct ?? 0.05;
  const juiceFloor = data?.weekly_juice_floor_pct;

  // For the SAME strike currently chosen, compare EXTRINSIC YIELD (juice/wk, not
  // raw net debit) across every expiration that happens to offer it (roll-dialog
  // audit §1.1). Ranked by juice/wk — "best rate" — not by which is cheapest to
  // reach, since a further-dated contract is ALWAYS cheaper for the same strike
  // (more remaining extrinsic to offset the buyback) regardless of whether it's
  // actually the better rate of return. Net debit is still shown (it's what hits
  // the cash-reserve check) but demoted. Two weeks within parityBand %/wk of each
  // other are treated as a tie, broken toward the SHORTER DTE. Advisory only —
  // purely informational, never changes which weeks are selectable.
  const weekComparison = React.useMemo(() => {
    if (buyback == null || strike == null) return [];
    const rows = [];
    for (const exp of exps) {
      const s = (exp.strikes || []).find((s) => s.strike === strike);
      if (!s || s.mark == null) continue;
      const credit = totalDollars(s.mark, qtyNum);
      rows.push({
        expiration: exp.expiration, dte: exp.dte, netCredit: credit - buyback,
        juicePerWeekPct: s.juice_per_week_pct,
      });
    }
    if (rows.length < 2) return [];
    const priced = rows.filter((r) => r.juicePerWeekPct != null);
    const best = (priced.length ? priced : rows).reduce((a, b) => {
      if (a.juicePerWeekPct == null) return b;
      if (b.juicePerWeekPct == null) return a;
      const within = Math.abs(b.juicePerWeekPct - a.juicePerWeekPct) <= parityBand;
      if (within) return b.dte < a.dte ? b : a;   // tie -> shorter DTE
      return b.juicePerWeekPct > a.juicePerWeekPct ? b : a;
    });
    return rows.map((r) => ({ ...r, isBest: r.expiration === best.expiration }));
  }, [exps, strike, buyback, qtyNum, parityBand]);

  // Cushion + juice/wk per strike, already computed server-side and attached
  // to each strike row (§1.3) — just renamed to camelCase for JSX use.
  const strikeRows = React.useMemo(() => strikesForExp.map((s) => ({
    ...s,
    cushionAtr: s.cushion_atr,
    juicePerWeekPct: s.juice_per_week_pct,
  })), [strikesForExp]);

  // Juice-floor advisory at the regime target (§1.4) — the regime-depth target
  // may sit deep enough ITM that its extrinsic yield can't clear the weekly-
  // juice floor. Never auto-selects a shallower strike; just names the
  // shallowest one (in THIS expiration's ladder) that would clear it.
  const floorAdvisory = React.useMemo(() => {
    const targetStrike = data?.regime_target?.strike;
    if (targetStrike == null || juiceFloor == null || !strikeRows.length) return null;
    const atTarget = strikeRows.find((s) => s.strike === targetStrike);
    const juiceAtTarget = atTarget ? atTarget.juicePerWeekPct : null;
    if (juiceAtTarget == null || juiceAtTarget >= juiceFloor) return null;
    const candidate = strikeRows
      .filter((s) => s.strike >= targetStrike && s.juicePerWeekPct != null && s.juicePerWeekPct >= juiceFloor)
      .sort((a, b) => a.strike - b.strike)[0] || null;
    return { targetStrike, juiceAtTarget, floor: juiceFloor, candidate };
  }, [data, strikeRows, juiceFloor]);

  // Roll-up guard (§1.5, TRAVIS_EXTENSION, SHADOW): rolling to a strike ABOVE
  // the current one is economically a fresh entry at that strike — surfaced as
  // PASS/FAIL/UNKNOWN per Level-5-equivalent check, worst-signal-wins summary,
  // ZERO blocking authority. Only shown when it applies.
  const rollUpGuard = React.useMemo(() => {
    if (!cur || !chosen || chosen.strike <= cur.strike) return null;
    const chosenRow = strikeRows.find((s) => s.strike === chosen.strike);
    const chosenJuice = chosenRow?.juicePerWeekPct ?? null;
    const exDivKnown = !!data?.ex_div?.ex_date;
    const checks = [
      { id: "earnings", label: "No earnings inside cycle",
        status: selectedExp?.earnings_in_week ? "FAIL" : "PASS" },
      { id: "ex_div", label: "Ex-div inside cycle",
        status: !exDivKnown ? "UNKNOWN" : (selectedExp?.ex_div_in_week ? "FAIL" : "PASS") },
      { id: "juice", label: "Weekly-juice adequacy",
        status: chosenJuice == null || juiceFloor == null ? "UNKNOWN"
          : (chosenJuice >= juiceFloor ? "PASS" : "FAIL") },
      { id: "cash_reserve", label: "Cash reserve",
        status: (netCredit == null || data?.operating_cash == null || data?.reserve_required == null)
          ? "UNKNOWN"
          : ((data.operating_cash + netCredit >= data.reserve_required) ? "PASS" : "FAIL") },
    ];
    const summary = checks.some((c) => c.status === "FAIL") ? "FAIL"
      : checks.some((c) => c.status === "UNKNOWN") ? "UNKNOWN" : "PASS";
    return { checks, summary, chosenJuice };
  }, [cur, chosen, selectedExp, data, netCredit, juiceFloor, strikeRows]);

  // Quote staleness (§1.7) — advisory badge only, never blocks. quote_fetched_at
  // is the backend chain-cache's actual fetch time (option_chain._fetch_chain),
  // not the request-serve time, so this reflects the true snapshot age even on
  // a cache hit.
  const quoteAgeSeconds = data?.quote_fetched_at != null
    ? (Date.now() / 1000) - data.quote_fetched_at : null;
  const quoteStale = quoteAgeSeconds != null && data?.quote_stale_after_seconds != null
    && quoteAgeSeconds > data.quote_stale_after_seconds;

  const canExecute = qtyNum > 0 && cur && chosen && selectedExp
    && !(sameStrike && sameWeek); // rolling to the exact same leg is a no-op

  function buildPayload() {
    const chosenRow = strikeRows.find((s) => s.strike === chosen?.strike);
    return {
      action: "roll_short",
      ticker: data.ticker,
      contracts: qtyNum,
      from_strike: cur.strike,
      from_expiration: cur.expiration,
      close_price_per_share: cur.current_mark,
      to_strike: chosen.strike,
      to_expiration: selectedExp.expiration,
      to_dte: selectedExp.dte,
      premium_per_share: chosen.mark,
      stock_price: data.underlying_price,
      roll_reason: reason, // whipsaw-ledger key: scheduled | 75%-rule | defend | earnings | kill-switch-exit
      client_order_ref: clientOrderRef.current, // idempotency key — one order per staged roll
      ...(sourceRecId ? { source_rec_id: sourceRecId } : {}),
      // ROLL_STRIKE_CHOICE (§1.6, telemetry-only) — what was recommended vs.
      // what was actually chosen; logged on the open leg's execution, never
      // consumed by the theta/accrual ledgers (see executor._sell_short).
      roll_strike_choice: {
        regime: data.regime,
        regime_target_strike: data.regime_target?.strike ?? null,
        floor_strike: floorAdvisory?.candidate?.strike ?? null,
        chosen_strike: chosen.strike,
        juice_per_week_at_chosen: chosenRow?.juicePerWeekPct ?? null,
        cushion_atr_at_chosen: chosenRow?.cushionAtr ?? null,
      },
    };
  }

  function execute() {
    const payload = buildPayload();
    // Confirm unless we KNOW this is paper (null/unresolved errs toward confirm).
    if (tradeMode !== "paper") { setPendingLive(payload); return; }
    doExecute(payload);
  }

  async function doExecute(payload) {
    setBusy(true); setExecErr(null);
    try {
      const res = await onExecute?.(payload);
      // Retire the idempotency ref ONLY on a confirmed terminal outcome — a distinct
      // next roll should mint a fresh ref. On UNKNOWN ("confirming…") keep the ref so
      // a retry reuses it and cannot place a second order for the same intent.
      const terminal = !res || ["filled", "canceled", "rejected", "logged"].includes(res.status)
        || res.mode === "logged";
      if (terminal) clearOrderRef();
      setPendingLive(null);
      onClose?.();
    } catch (e) { setExecErr(e.message); setPendingLive(null); }
    finally { setBusy(false); }
  }

  return (
    <>
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      role="dialog" aria-modal="true" onClick={onClose}
    >
      <div
        className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-xl border border-slate-700 bg-slate-900 p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-semibold text-slate-100">Roll short · {ticker}</h2>
            <TradeModeBadge mode={tradeMode} />
          </div>
          <button onClick={onClose} className="rounded-lg px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-200">✕</button>
        </div>

        {loading && <Loading label="Loading roll options…" className="py-8" />}

        {error && (
          <div className="rounded-lg border border-rose-800 bg-rose-500/10 p-4 text-sm text-rose-200">
            <p className="font-semibold">Could not load roll options</p>
            <p className="mt-1 text-rose-300">{error}</p>
            <button onClick={onClose} className="mt-3 rounded-lg border border-rose-700 px-3 py-1.5 text-rose-200 hover:bg-rose-500/10">Close</button>
          </div>
        )}

        {data && !loading && !error && (
          <div className="space-y-4">
            {/* Current short being rolled */}
            <div className="rounded-lg border border-slate-800 bg-slate-950 p-3 text-sm">
              <div className="mb-1 flex items-center justify-between">
                <span className="text-xs uppercase tracking-wide text-slate-500">Current short (buy to close)</span>
                <div className="flex items-center gap-1.5">
                  {quoteStale && (
                    <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-amber-300"
                      title={`Quote snapshot is ${Math.round(quoteAgeSeconds)}s old`}>
                      stale
                    </span>
                  )}
                  {data.regime && <Pill status={data.regime}>{data.regime}</Pill>}
                </div>
              </div>
              <div className="text-slate-200">
                {fmt(cur?.strike, 2)}C · {cur?.contracts}c · exp {cur?.expiration || "—"}
                {cur?.dte != null ? ` (${cur.dte} DTE)` : ""} · est. buyback{" "}
                <span className="font-semibold text-slate-100">{dollars(cur?.current_mark)}/sh</span>
              </div>
              {data.underlying_price != null && (
                <div className="mt-1 text-xs text-slate-500">
                  Spot {dollars(data.underlying_price)} · target {data.suggested_strike != null ? fmt(data.suggested_strike, 2) : "—"}{" "}
                  ({data.regime ? `${data.regime[0].toUpperCase()}${data.regime.slice(1)} · ` : ""}
                  {data.atr_mult}×ATR {fmt(data.atr, 2)}
                  {data.itm_pct != null ? ` / ${(data.itm_pct * 100).toFixed(0)}% ITM floor` : ""})
                  {data.roll_up_blocked && (
                    <span className="ml-1 text-rose-300">
                      — {data.regime} blocks rolling up; capped at the current strike ({fmt(data.regime_target?.rule_strike, 2)} uncapped)
                    </span>
                  )}
                  {data.target_deadband?.held && <span className="ml-1 text-slate-600">(held)</span>}
                </div>
              )}
              {data.iv_rank?.iv_rank != null && (
                <div className="mt-1 text-xs">
                  <span className={`font-semibold ${data.iv_rank.iv_rank >= 50 ? "text-emerald-300" : data.iv_rank.iv_rank <= 25 ? "text-slate-400" : "text-slate-300"}`}>
                    IV rank {fmt(data.iv_rank.iv_rank, 0)}
                  </span>
                  <span className="text-slate-500">
                    {" "}(IV {fmt(data.iv_rank.iv_now, 1)}% vs {fmt(data.iv_rank.iv_min, 1)}–{fmt(data.iv_rank.iv_max, 1)}%, {data.iv_rank.days}d)
                    {data.iv_rank.iv_rank >= 50 ? " — rich vs its own year, good week to sell" : data.iv_rank.iv_rank <= 25 ? " — cheap vs its own year" : ""}
                  </span>
                </div>
              )}
            </div>

            {/* Roll-timing advisory — informational only, never blocks anything below */}
            {data.roll_readiness && data.roll_readiness.ready !== null && (
              <div className={`rounded-lg border p-3 text-sm ${
                data.roll_readiness.ready
                  ? "border-violet-500/40 bg-violet-500/10 text-violet-200"
                  : "border-slate-800 bg-slate-950 text-slate-400"
              }`}>
                <div className="flex items-center gap-2">
                  <span className={`rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide ${
                    data.roll_readiness.ready ? "bg-violet-500/15 text-violet-300" : "bg-slate-800 text-slate-500"
                  }`}>
                    advisory
                  </span>
                  <span className="font-semibold">
                    {data.roll_readiness.ready ? "Clear to roll early" : "No rush — theta still working"}
                  </span>
                </div>
                <p className="mt-1 text-xs text-current/90">
                  {data.roll_readiness.extrinsic_captured_pct != null && (
                    <>extrinsic {fmt(data.roll_readiness.extrinsic_captured_pct, 0)}% captured (threshold {fmt(data.roll_readiness.decay_threshold_pct, 0)}%)</>
                  )}
                  {data.roll_readiness.itm_buffer_pct != null && (
                    <>{data.roll_readiness.extrinsic_captured_pct != null ? " · " : ""}ITM buffer {fmt(data.roll_readiness.itm_buffer_pct, 1)}% (floor {fmt(data.roll_readiness.itm_floor_pct, 0)}%)</>
                  )}
                  {!data.roll_readiness.ready && " — rolling now trades cheap late-cycle decay for a slower-burning fresh contract; usually better to wait unless the strike itself needs to change."}
                </p>
              </div>
            )}

            {/* Week choice */}
            <div className="rounded-lg border border-slate-800 bg-slate-950 p-3">
              <div className="mb-2 text-xs uppercase tracking-wide text-slate-500">Roll to week</div>
              <div className="mb-3 flex gap-2">
                <button
                  onClick={() => chooseWeekMode("same")}
                  className={`flex-1 rounded-lg border px-3 py-1.5 text-sm font-medium ${
                    weekMode === "same" ? "border-emerald-700 bg-emerald-500/10 text-emerald-300" : "border-slate-700 text-slate-300 hover:bg-slate-800"
                  }`}
                >
                  Same week{currentExp ? ` (${currentExp.expiration})` : ""}
                </button>
                <button
                  onClick={() => chooseWeekMode("different")}
                  disabled={exps.filter((e) => !e.is_current_week).length === 0}
                  className={`flex-1 rounded-lg border px-3 py-1.5 text-sm font-medium disabled:opacity-40 ${
                    weekMode === "different" ? "border-emerald-700 bg-emerald-500/10 text-emerald-300" : "border-slate-700 text-slate-300 hover:bg-slate-800"
                  }`}
                >
                  Different week
                </button>
              </div>
              {weekMode === "different" && (
                <select
                  value={expiration || ""}
                  onChange={(e) => setExpiration(e.target.value)}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-slate-100"
                >
                  {exps.map((e) => (
                    <option key={e.expiration} value={e.expiration}>
                      {e.expiration} ({e.dte} DTE){e.is_current_week ? " · current" : ""}
                    </option>
                  ))}
                </select>
              )}
            </div>

            {weekComparison.length > 1 && (
              <div className="rounded-lg border border-slate-800 bg-slate-950 p-3 text-xs text-slate-400">
                <span className="uppercase tracking-wide text-slate-500">{fmt(strike, 2)} strike, by week — ranked by juice/wk</span>
                <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1">
                  {weekComparison.map((r) => (
                    <span key={r.expiration} className={r.isBest ? "font-semibold text-emerald-300" : ""}>
                      {r.expiration} ({r.dte}d): {r.juicePerWeekPct != null ? pct(r.juicePerWeekPct, 2) + "/wk" : "—"}
                      <span className="text-slate-500"> · net {bigDollars(r.netCredit)}</span>
                      {r.isBest ? " · best rate" : ""}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* Juice-floor advisory at the regime target (§1.4) — SHADOW, never
               auto-selects a shallower strike */}
            {floorAdvisory && (
              <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-200">
                <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-amber-300">advisory</span>{" "}
                Regime target ({fmt(floorAdvisory.targetStrike, 2)}) yields {pct(floorAdvisory.juiceAtTarget, 2)}/wk, below floor {pct(floorAdvisory.floor, 2)}/wk.
                {floorAdvisory.candidate
                  ? <> Shallowest strike meeting floor in this week: <span className="font-semibold">{fmt(floorAdvisory.candidate.strike, 2)}</span> ({fmt(floorAdvisory.candidate.cushionAtr, 1)}×ATR cushion).</>
                  : " No strike in this week's ladder clears the floor."}
              </div>
            )}

            {/* Roll-up guard (§1.5) — SHADOW, zero blocking authority */}
            {rollUpGuard && (
              <div className={`rounded-lg border p-3 text-xs ${
                rollUpGuard.summary === "FAIL" ? "border-rose-500/40 bg-rose-500/5 text-rose-200"
                  : rollUpGuard.summary === "UNKNOWN" ? "border-slate-700 bg-slate-950 text-slate-400"
                  : "border-emerald-500/30 bg-emerald-500/5 text-emerald-200"
              }`}>
                <div className="flex items-center gap-2">
                  <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-slate-400">advisory · shadow</span>
                  <span className="font-semibold uppercase">{rollUpGuard.summary}</span>
                  <span className="text-slate-400">— rolling to {fmt(chosen?.strike, 2)} is above the current strike ({fmt(cur?.strike, 2)}), treated as a fresh entry</span>
                </div>
                <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5">
                  {rollUpGuard.checks.map((c) => (
                    <span key={c.id} className={
                      c.status === "FAIL" ? "text-rose-300" : c.status === "UNKNOWN" ? "text-slate-500" : "text-emerald-300"
                    }>
                      {c.label}: {c.status}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {selectedExp?.earnings_in_week && (
              <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-200">
                <span className="font-semibold">Earnings before this expiration</span>
                {data.earnings_date ? ` (${data.earnings_date})` : ""} — the short would span the report.
                {selectedExp.deep_itm_suggested
                  ? " Suggested strike is rolled deep-ITM for protection; pick a different week to avoid the report entirely."
                  : " Roll deep-ITM for protection or pick a week that clears the report."}
              </div>
            )}

            {/* Strike choice */}
            <div className="rounded-lg border border-slate-800 bg-slate-950 p-3">
              <div className="mb-2 flex items-center justify-between">
                <span className="text-xs uppercase tracking-wide text-slate-500">Roll to strike</span>
                {selectedExp && <span className="text-xs text-slate-500">exp {selectedExp.expiration} · {selectedExp.dte} DTE</span>}
              </div>
              {strikeRows.length ? (
                <>
                  <div className="grid grid-cols-[auto_repeat(6,1fr)] gap-2 text-xs uppercase tracking-wide text-slate-500">
                    <span className="w-6" /><span>Strike</span><span>Bid / Ask</span><span>Mark</span><span>Extrinsic</span><span>Cushion</span><span>Juice/wk</span>
                  </div>
                  {strikeRows.map((s) => (
                    <label
                      key={s.strike}
                      className={`grid grid-cols-[auto_repeat(6,1fr)] items-center gap-2 rounded-lg px-1 py-1 ${
                        s.strike === strike ? "bg-emerald-500/10" : "hover:bg-slate-800/50"
                      }`}
                    >
                      <input
                        type="radio" name="roll-strike"
                        checked={s.strike === strike}
                        onChange={() => setStrike(s.strike)}
                        className="accent-emerald-400"
                      />
                      <span className="text-sm font-semibold tabular-nums text-slate-100">
                        {fmt(s.strike, 2)}
                        {cur && s.strike === cur.strike && <span className="ml-1 text-[10px] font-normal text-sky-300">SAME</span>}
                        {s.suggested && <span className="ml-1 text-[10px] font-normal text-emerald-400">{selectedExp?.deep_itm_suggested ? "DEEP-ITM" : "REGIME"}</span>}
                      </span>
                      <span className="text-sm tabular-nums text-slate-300">{dollars(s.bid)} / {dollars(s.ask)}</span>
                      <span className="text-sm tabular-nums text-slate-400">{dollars(s.mark)}</span>
                      <span className="text-sm tabular-nums text-emerald-300">{dollars(s.extrinsic)}</span>
                      <span className="text-sm tabular-nums text-slate-400">{s.cushionAtr != null ? `${s.cushionAtr.toFixed(1)}×` : "—"}</span>
                      <span className="text-sm tabular-nums text-sky-300">{s.juicePerWeekPct != null ? pct(s.juicePerWeekPct, 2) : "—"}</span>
                    </label>
                  ))}
                </>
              ) : <p className="text-sm text-slate-400">No strikes available for this expiration.</p>}
            </div>

            {/* Net credit + quantity + execute */}
            <div className="rounded-lg border border-sky-800 bg-sky-500/5 p-3">
              <div className="grid grid-cols-2 gap-3 text-sm">
                <label className="text-slate-400">Contracts
                  <input
                    value={qty}
                    onChange={(e) => setQty(e.target.value.replace(/[^0-9]/g, ""))}
                    inputMode="numeric"
                    className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-slate-100"
                  />
                </label>
                <div className="text-slate-400">Net credit / (debit)
                  <div className={`mt-1 text-xl font-semibold ${netCredit == null ? "text-slate-400" : netCredit >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
                    {bigDollars(netCredit)}
                  </div>
                </div>
              </div>
              <p className="mt-2 text-[11px] text-slate-500">
                New premium {bigDollars(newCredit)} − buyback {bigDollars(buyback)}.
                {sameStrike && sameWeek ? " Choose a different week or strike to roll." : ""}
              </p>
              {tradeMode === "paper" && (
                <p className="mt-1 text-[11px] text-amber-300/90">
                  Paper mode — logged to your ledger only; no order reaches Schwab.
                </p>
              )}
              <div className="mt-3 flex items-center justify-end gap-2">
                <button onClick={onClose} className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800">
                  Cancel
                </button>
                <button
                  onClick={execute}
                  disabled={!canExecute || busy}
                  className="rounded-lg bg-emerald-500/20 px-4 py-2 text-sm font-semibold text-emerald-300 hover:bg-emerald-500/30 disabled:opacity-40"
                >
                  {busy ? "Rolling…" : `Roll & log${tradeMode === "paper" ? " (paper)" : ""}`}
                </button>
              </div>
              {execErr && <p className="mt-2 text-right text-xs text-rose-400">{execErr}</p>}
            </div>
          </div>
        )}
      </div>
    </div>
    {pendingLive && (
      <LiveOrderConfirm
        payload={pendingLive}
        busy={busy}
        onConfirm={() => doExecute(pendingLive)}
        onCancel={() => setPendingLive(null)}
      />
    )}
    </>
  );
}
