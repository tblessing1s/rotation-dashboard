import React from "react";
import { api } from "../api.js";
import { Pill, Loading, fmt } from "./ui.jsx";

function dollars(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return "$" + Number(n).toFixed(2);
}
function bigDollars(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const v = Number(n);
  return (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
}

/**
 * Roll an open short call. The user decides two things independently:
 *   • week   — SAME week (keep the current expiration) or a DIFFERENT week
 *   • strike — SAME strike or a DIFFERENT one (e.g. deep-ITM into earnings)
 * The modal shows the live buy-to-close cost of the current short and the new
 * premium for the chosen leg, nets them, and submits a single roll_short action.
 */
export default function RollModal({ ticker, reason = "scheduled", onExecute, onClose }) {
  const [data, setData] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [weekMode, setWeekMode] = React.useState("same"); // same | different
  const [expiration, setExpiration] = React.useState(null);
  const [strike, setStrike] = React.useState(null);
  const [qty, setQty] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [execErr, setExecErr] = React.useState(null);

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

  const buyback = cur?.current_mark != null ? cur.current_mark * 100 * qtyNum : null;
  const newCredit = chosen?.mark != null ? chosen.mark * 100 * qtyNum : null;
  const netCredit = buyback != null && newCredit != null ? newCredit - buyback : null;

  const canExecute = qtyNum > 0 && cur && chosen && selectedExp
    && !(sameStrike && sameWeek); // rolling to the exact same leg is a no-op

  async function execute() {
    setBusy(true); setExecErr(null);
    try {
      await onExecute?.({
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
      });
      onClose?.();
    } catch (e) { setExecErr(e.message); }
    finally { setBusy(false); }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      role="dialog" aria-modal="true" onClick={onClose}
    >
      <div
        className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-xl border border-slate-700 bg-slate-900 p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-slate-100">Roll short · {ticker}</h2>
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
                {data.regime && <Pill status={data.regime}>{data.regime}</Pill>}
              </div>
              <div className="text-slate-200">
                {fmt(cur?.strike, 2)}C · {cur?.contracts}c · exp {cur?.expiration || "—"}
                {cur?.dte != null ? ` (${cur.dte} DTE)` : ""} · est. buyback{" "}
                <span className="font-semibold text-slate-100">{dollars(cur?.current_mark)}/sh</span>
              </div>
              {data.underlying_price != null && (
                <div className="mt-1 text-xs text-slate-500">
                  Spot {dollars(data.underlying_price)} · target {data.suggested_strike != null ? fmt(data.suggested_strike, 2) : "—"}{" "}
                  ({data.atr_mult}×ATR {fmt(data.atr, 2)}
                  {data.itm_pct != null ? ` / ${(data.itm_pct * 100).toFixed(0)}% ITM floor` : ""}
                  {data.posture ? `, ${data.posture}` : ""})
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
              {strikesForExp.length ? (
                <>
                  <div className="grid grid-cols-[auto_repeat(4,1fr)] gap-2 text-xs uppercase tracking-wide text-slate-500">
                    <span className="w-6" /><span>Strike</span><span>Bid / Ask</span><span>Mark</span><span>Extrinsic</span>
                  </div>
                  {strikesForExp.map((s) => (
                    <label
                      key={s.strike}
                      className={`grid grid-cols-[auto_repeat(4,1fr)] items-center gap-2 rounded-lg px-1 py-1 ${
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
                        {s.suggested && <span className="ml-1 text-[10px] font-normal text-emerald-400">{selectedExp?.deep_itm_suggested ? "DEEP-ITM" : "ATR"}</span>}
                      </span>
                      <span className="text-sm tabular-nums text-slate-300">{dollars(s.bid)} / {dollars(s.ask)}</span>
                      <span className="text-sm tabular-nums text-slate-400">{dollars(s.mark)}</span>
                      <span className="text-sm tabular-nums text-emerald-300">{dollars(s.extrinsic)}</span>
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
              <div className="mt-3 flex items-center justify-end gap-2">
                <button onClick={onClose} className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800">
                  Cancel
                </button>
                <button
                  onClick={execute}
                  disabled={!canExecute || busy}
                  className="rounded-lg bg-emerald-500/20 px-4 py-2 text-sm font-semibold text-emerald-300 hover:bg-emerald-500/30 disabled:opacity-40"
                >
                  {busy ? "Rolling…" : "Roll & log"}
                </button>
              </div>
              {execErr && <p className="mt-2 text-right text-xs text-rose-400">{execErr}</p>}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
