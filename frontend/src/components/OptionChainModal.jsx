import React from "react";
import { api } from "../api.js";
import { Pill, fmt } from "./ui.jsx";

// Dollar formatter that tolerates nulls (—) for thin/closed quotes.
function dollars(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return "$" + Number(n).toFixed(2);
}

function StrikeRow({ s, cols = "grid-cols-4" }) {
  return (
    <div className={`grid ${cols} gap-2 py-1 text-sm tabular-nums`}>
      <span className="font-semibold text-slate-100">{fmt(s.strike, 2)}</span>
      <span className="text-slate-300">{dollars(s.bid)} / {dollars(s.ask)}</span>
      <span className="text-slate-400">{dollars(s.mark)}</span>
      <span className="text-emerald-300">{dollars(s.extrinsic)}</span>
    </div>
  );
}

/**
 * Option chain viewer. Auto-picks the LEAP (read-only) and an ATR-suggested
 * weekly short the user can adjust to a nearby strike, then locks both into the
 * Execute form via onConfirm. Closes itself (with an error message) when the
 * regime is RED or the chain fails to load.
 */
export default function OptionChainModal({ ticker, onConfirm, onClose }) {
  const [chain, setChain] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [weeklyStrike, setWeeklyStrike] = React.useState(null);

  React.useEffect(() => {
    let live = true;
    setLoading(true); setError(null);
    api.optionChain(ticker)
      .then((c) => {
        if (!live) return;
        setChain(c);
        const sug = c.weekly?.strikes?.find((s) => s.suggested) || c.weekly?.strikes?.[0];
        setWeeklyStrike(sug ? sug.strike : null);
      })
      .catch((e) => { if (live) setError(e.message); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [ticker]);

  const leap = chain?.leap;
  const weekly = chain?.weekly;
  const chosenWeekly = weekly?.strikes?.find((s) => s.strike === weeklyStrike) || null;

  function confirm() {
    onConfirm?.({
      ticker: chain.ticker,
      underlying_price: chain.underlying_price,
      regime: chain.regime,
      atr_mult: chain.atr_mult,
      leap: leap
        ? { strike: leap.strike, mark: leap.mark, contracts: leap.target_contracts, dte: leap.dte }
        : null,
      weekly: chosenWeekly
        ? { strike: chosenWeekly.strike, mark: chosenWeekly.mark, dte: weekly.dte }
        : null,
    });
    onClose?.();
  }

  const regimeBanner =
    chain?.regime === "green"
      ? "GREEN — uptrend · more juice, less protection"
      : chain?.regime === "yellow"
      ? "YELLOW — caution · balanced protection"
      : chain?.regime;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <div
        className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-xl border border-slate-700 bg-slate-900 p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-slate-100">Option Chain · {ticker}</h2>
          <button onClick={onClose} className="rounded-lg px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-200">✕</button>
        </div>

        {loading && <p className="py-8 text-center text-sm text-slate-400">Loading chain…</p>}

        {error && (
          <div className="rounded-lg border border-rose-800 bg-rose-500/10 p-4 text-sm text-rose-200">
            <p className="font-semibold">Could not load chain</p>
            <p className="mt-1 text-rose-300">{error}</p>
            <button onClick={onClose} className="mt-3 rounded-lg border border-rose-700 px-3 py-1.5 text-rose-200 hover:bg-rose-500/10">
              Close
            </button>
          </div>
        )}

        {chain && !loading && !error && (
          <div className="space-y-4">
            {/* Regime banner */}
            <div className="flex items-center justify-between rounded-lg border border-slate-800 bg-slate-950 p-3">
              <div className="flex items-center gap-2 text-sm">
                <span className="text-slate-400">Market regime</span>
                <Pill status={chain.regime}>{regimeBanner}</Pill>
              </div>
              <div className="text-sm text-slate-300">
                ATR ×<span className="font-semibold text-slate-100">{chain.atr_mult}</span>
                {chain.underlying_price != null && (
                  <span className="ml-3 text-slate-400">Spot {dollars(chain.underlying_price)}</span>
                )}
              </div>
            </div>

            {/* LEAP — auto-picked, read-only */}
            <div className="rounded-lg border border-slate-800 bg-slate-950 p-3">
              <div className="mb-2 flex items-center justify-between">
                <h3 className="text-sm font-semibold text-slate-200">LEAP (auto-picked, delta ~0.90)</h3>
                <span className="text-xs text-slate-500">read-only</span>
              </div>
              {leap ? (
                <div className="space-y-1">
                  <div className="grid grid-cols-4 gap-2 text-xs uppercase tracking-wide text-slate-500">
                    <span>Strike</span><span>Bid / Ask</span><span>Mark</span><span>Extrinsic</span>
                  </div>
                  <StrikeRow s={leap} />
                  <div className="pt-1 text-xs text-slate-500">
                    {leap.dte} DTE · delta {fmt(leap.delta, 2)} · {leap.target_contracts} contracts
                    {leap.expiration ? ` · exp ${leap.expiration}` : ""}
                  </div>
                </div>
              ) : (
                <p className="text-sm text-slate-400">No suitable LEAP strike found.</p>
              )}
            </div>

            {/* Weekly short — ATR-suggested, user-adjustable */}
            <div className="rounded-lg border border-slate-800 bg-slate-950 p-3">
              <div className="mb-2 flex items-center justify-between">
                <h3 className="text-sm font-semibold text-slate-200">Weekly short call (ATR-suggested)</h3>
                {weekly && (
                  <span className="text-xs text-slate-500">
                    {weekly.dte} DTE · {weekly.atr_mult}×ATR {fmt(weekly.atr, 2)}
                  </span>
                )}
              </div>
              {weekly?.strikes?.length ? (
                <>
                  <div className="grid grid-cols-[auto_repeat(4,1fr)] gap-2 text-xs uppercase tracking-wide text-slate-500">
                    <span className="w-6" /><span>Strike</span><span>Bid / Ask</span><span>Mark</span><span>Extrinsic</span>
                  </div>
                  {weekly.strikes.map((s) => (
                    <label
                      key={s.strike}
                      className={`grid grid-cols-[auto_repeat(4,1fr)] items-center gap-2 rounded-lg px-1 py-1 ${
                        s.strike === weeklyStrike ? "bg-emerald-500/10" : "hover:bg-slate-800/50"
                      }`}
                    >
                      <input
                        type="radio"
                        name="weekly-strike"
                        checked={s.strike === weeklyStrike}
                        onChange={() => setWeeklyStrike(s.strike)}
                        className="accent-emerald-400"
                      />
                      <span className="text-sm font-semibold tabular-nums text-slate-100">
                        {fmt(s.strike, 2)}
                        {s.suggested && <span className="ml-1 text-[10px] font-normal text-emerald-400">SUGGESTED</span>}
                      </span>
                      <span className="text-sm tabular-nums text-slate-300">{dollars(s.bid)} / {dollars(s.ask)}</span>
                      <span className="text-sm tabular-nums text-slate-400">{dollars(s.mark)}</span>
                      <span className="text-sm tabular-nums text-emerald-300">{dollars(s.extrinsic)}</span>
                    </label>
                  ))}
                </>
              ) : (
                <p className="text-sm text-slate-400">No weekly strikes available.</p>
              )}
            </div>

            <div className="flex items-center justify-end gap-2 pt-1">
              <button onClick={onClose} className="rounded-lg border border-slate-700 px-4 py-2 text-sm text-slate-300 hover:bg-slate-800">
                Cancel
              </button>
              <button
                onClick={confirm}
                disabled={!chosenWeekly && !leap}
                className="rounded-lg bg-emerald-500/20 px-4 py-2 text-sm font-semibold text-emerald-300 hover:bg-emerald-500/30 disabled:opacity-40"
              >
                Confirm strikes
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
