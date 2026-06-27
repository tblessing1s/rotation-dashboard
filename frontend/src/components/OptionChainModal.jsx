import React from "react";
import { api } from "../api.js";
import { Pill, fmt } from "./ui.jsx";

// Dollar formatter that tolerates nulls (—) for thin/closed quotes.
function dollars(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return "$" + Number(n).toFixed(2);
}
function bigDollars(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return "$" + Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
}

const ACTION_LABELS = {
  buy_leap: "Buy LEAP (deep ITM)",
  sell_short: "Sell weekly short call",
  close_short: "Close / roll short call",
  close_leap: "Close LEAP (sell to close)",
};

function StrikeHead({ extra }) {
  return (
    <div className={`grid ${extra || "grid-cols-4"} gap-2 text-xs uppercase tracking-wide text-slate-500`}>
      <span>Strike</span><span>Bid / Ask</span><span>Mark</span><span>Extrinsic</span>
    </div>
  );
}

/**
 * Option chain viewer + order ticket. Auto-detects the next action from the
 * user's current position, auto-picks the LEAP and an ATR-suggested weekly
 * strike, shows whether IV is rich vs realized vol, and estimates how long the
 * short juice takes to cover the LEAP's extrinsic. The user typically only sets
 * quantity, then executes straight from the chain (or just fills the form).
 */
export default function OptionChainModal({ ticker, onConfirm, onExecute, onClose }) {
  const [chain, setChain] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [weeklyStrike, setWeeklyStrike] = React.useState(null);
  const [action, setAction] = React.useState(null);
  const [qty, setQty] = React.useState("");
  const [execErr, setExecErr] = React.useState(null);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    let live = true;
    setLoading(true); setError(null);
    api.optionChain(ticker)
      .then((c) => {
        if (!live) return;
        setChain(c);
        const sug = c.weekly?.strikes?.find((s) => s.suggested) || c.weekly?.strikes?.[0];
        setWeeklyStrike(sug ? sug.strike : null);
        setAction(c.suggested_action || "buy_leap");
        const sa = c.suggested_action;
        const defQty =
          sa === "close_short" && c.position?.open_short?.contracts ? c.position.open_short.contracts
          : sa === "close_leap" && c.position?.existing_leap?.contracts ? c.position.existing_leap.contracts
          : c.quantity_default ?? 5;
        setQty(String(defQty));
      })
      .catch((e) => { if (live) setError(e.message); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [ticker]);

  const leap = chain?.leap;
  const weekly = chain?.weekly;
  const iv = chain?.iv;
  const position = chain?.position;
  const openShort = position?.open_short;
  const existingLeap = position?.existing_leap;
  // Management-only (RED tape): entries are blocked, so the only moves are
  // closing the open short and/or selling the LEAP to exit.
  const mgmt = !!chain?.management_only;
  const actionOptions = mgmt
    ? {
        ...(position?.open_short_count ? { close_short: ACTION_LABELS.close_short } : {}),
        ...(position?.has_leap ? { close_leap: ACTION_LABELS.close_leap } : {}),
      }
    : ACTION_LABELS;
  const showPayoff = !mgmt && action !== "close_leap";
  const chosenWeekly = weekly?.strikes?.find((s) => s.strike === weeklyStrike) || null;
  const qtyNum = Number(qty) || 0;

  // Live payoff: how much LEAP extrinsic must be covered, and ~weeks for the
  // selected weekly's juice to cover it. Existing-LEAP cover is a fixed remaining
  // balance; a new entry scales with the chosen quantity.
  const coverTotal = position?.has_leap
    ? chain?.payoff?.leap_extrinsic_to_cover
    : leap?.extrinsic != null ? leap.extrinsic * 100 * qtyNum : null;
  const weeklyJuice = chosenWeekly?.extrinsic != null ? chosenWeekly.extrinsic * 100 * qtyNum : null;
  const weeks = coverTotal && weeklyJuice && weeklyJuice > 0 ? Math.ceil(coverTotal / weeklyJuice) : null;

  function buildPayload() {
    const base = { action, ticker: chain.ticker, contracts: qtyNum };
    if (chain.underlying_price != null) base.stock_price = chain.underlying_price;
    if (action === "buy_leap" && leap) {
      base.strike = leap.strike;
      if (leap.expiration) base.expiration = leap.expiration;
      if (leap.dte != null) base.dte = leap.dte;
      if (leap.mark != null) base.execution_price = Math.round(leap.mark * 100 * 100) / 100;
    } else if (action === "sell_short" && chosenWeekly) {
      base.strike = chosenWeekly.strike;
      if (chosenWeekly.mark != null) base.premium_per_share = chosenWeekly.mark;
    } else if (action === "close_short" && openShort) {
      base.strike = openShort.strike;
      base.contracts = qtyNum || openShort.contracts;
      if (openShort.current_mark != null) base.close_price_per_share = openShort.current_mark;
    } else if (action === "close_leap" && existingLeap) {
      base.strike = existingLeap.strike;
      base.contracts = qtyNum || existingLeap.contracts;
      // close_price is per-contract total dollars (mirrors buy_leap).
      if (existingLeap.current_mark != null) base.close_price = Math.round(existingLeap.current_mark * 100 * 100) / 100;
      if (existingLeap.cost_basis != null) base.cost_basis = existingLeap.cost_basis;
    }
    return base;
  }

  const canExecute =
    qtyNum > 0 &&
    ((action === "buy_leap" && leap) ||
      (action === "sell_short" && chosenWeekly) ||
      (action === "close_short" && openShort) ||
      (action === "close_leap" && existingLeap));

  async function execute() {
    setBusy(true); setExecErr(null);
    try {
      await onExecute?.(buildPayload());
      onClose?.();
    } catch (e) { setExecErr(e.message); }
    finally { setBusy(false); }
  }

  function confirm() {
    onConfirm?.({
      ticker: chain.ticker,
      underlying_price: chain.underlying_price,
      regime: chain.regime,
      atr_mult: chain.atr_mult,
      leap: leap ? { strike: leap.strike, mark: leap.mark, contracts: qtyNum, dte: leap.dte, expiration: leap.expiration } : null,
      weekly: chosenWeekly ? { strike: chosenWeekly.strike, mark: chosenWeekly.mark, dte: weekly.dte } : null,
    });
    onClose?.();
  }

  const regimeBanner =
    chain?.regime === "green" ? "GREEN — uptrend"
    : chain?.regime === "yellow" ? "YELLOW — caution"
    : chain?.regime;
  const ivStatus = { rich: "green", cheap: "red", fair: "yellow" }[iv?.premium] || "unknown";

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
          <h2 className="text-lg font-semibold text-slate-100">Option Chain · {ticker}</h2>
          <button onClick={onClose} className="rounded-lg px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-200">✕</button>
        </div>

        {loading && <p className="py-8 text-center text-sm text-slate-400">Loading chain…</p>}

        {error && (
          <div className="rounded-lg border border-rose-800 bg-rose-500/10 p-4 text-sm text-rose-200">
            <p className="font-semibold">Could not load chain</p>
            <p className="mt-1 text-rose-300">{error}</p>
            <button onClick={onClose} className="mt-3 rounded-lg border border-rose-700 px-3 py-1.5 text-rose-200 hover:bg-rose-500/10">Close</button>
          </div>
        )}

        {chain && !loading && !error && (
          <div className="space-y-4">
            {/* Regime + IV banner */}
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-950 p-3 text-sm">
              <div className="flex items-center gap-2">
                <span className="text-slate-400">Regime</span>
                <Pill status={chain.regime}>{regimeBanner}</Pill>
                <span className="text-slate-400">ATR ×<span className="font-semibold text-slate-100">{chain.atr_mult}</span></span>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-slate-400">IV</span>
                <Pill status={ivStatus}>{iv?.premium || "?"}</Pill>
                {chain.underlying_price != null && <span className="text-slate-400">Spot {dollars(chain.underlying_price)}</span>}
              </div>
            </div>
            {iv?.label && <p className="-mt-2 px-1 text-xs text-slate-500">{iv.label}</p>}

            {mgmt && (
              <div className="rounded-lg border border-rose-800 bg-rose-500/10 p-3 text-sm text-rose-200">
                Market is <span className="font-semibold">RED</span> — new entries are blocked. You can
                still close or roll an open short to de-risk and exit.
              </div>
            )}

            {/* Order ticket — auto-detected action, quantity, payoff, execute */}
            <div className="rounded-lg border border-sky-800 bg-sky-500/5 p-3">
              <div className="mb-2 text-xs uppercase tracking-wide text-sky-400">Order (auto-detected)</div>
              <p className="mb-3 text-xs text-slate-400">{chain.action_reason}</p>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <label className="text-slate-400">Action
                  <select
                    value={action || ""}
                    onChange={(e) => setAction(e.target.value)}
                    disabled={Object.keys(actionOptions).length <= 1}
                    className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-slate-100 disabled:opacity-60"
                  >
                    {Object.entries(actionOptions).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                  </select>
                </label>
                <label className="text-slate-400">Quantity (contracts)
                  <input
                    value={qty}
                    onChange={(e) => setQty(e.target.value.replace(/[^0-9]/g, ""))}
                    inputMode="numeric"
                    className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-slate-100"
                  />
                </label>
              </div>

              {/* Payoff estimate — entry context only; irrelevant when exiting */}
              {showPayoff && (
                <>
                  <div className="mt-3 grid grid-cols-3 gap-2 rounded-lg border border-slate-800 bg-slate-950 p-3 text-center">
                    <div>
                      <div className="text-[10px] uppercase tracking-wide text-slate-500">LEAP extrinsic to cover</div>
                      <div className="text-base font-semibold text-amber-300">{bigDollars(coverTotal)}</div>
                    </div>
                    <div>
                      <div className="text-[10px] uppercase tracking-wide text-slate-500">Est. weekly juice</div>
                      <div className="text-base font-semibold text-emerald-300">{bigDollars(weeklyJuice)}</div>
                    </div>
                    <div>
                      <div className="text-[10px] uppercase tracking-wide text-slate-500">≈ income-positive</div>
                      <div className="text-base font-semibold text-slate-100">{weeks != null ? `~${weeks} wk` : "—"}</div>
                    </div>
                  </div>
                  <p className="mt-1 text-[11px] text-slate-500">
                    Rough estimate: weekly extrinsic ÷ LEAP extrinsic, {chain.payoff?.cover_basis}. Assumes the short is rolled at a similar credit each week.
                  </p>
                </>
              )}

              <div className="mt-3 flex items-center justify-end gap-2">
                <button onClick={confirm} className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800">
                  Just fill form
                </button>
                <button
                  onClick={execute}
                  disabled={!canExecute || busy}
                  className="rounded-lg bg-emerald-500/20 px-4 py-2 text-sm font-semibold text-emerald-300 hover:bg-emerald-500/30 disabled:opacity-40"
                >
                  {busy ? "Executing…" : `Execute ${ACTION_LABELS[action]?.split(" ")[0] || ""} & log`}
                </button>
              </div>
              {execErr && <p className="mt-2 text-right text-xs text-rose-400">{execErr}</p>}
            </div>

            {/* LEAP — auto-picked, read-only (entry context only) */}
            {!mgmt && (
            <div className={`rounded-lg border bg-slate-950 p-3 ${action === "buy_leap" ? "border-sky-700" : "border-slate-800"}`}>
              <div className="mb-2 flex items-center justify-between">
                <h3 className="text-sm font-semibold text-slate-200">LEAP (auto-picked, delta ~0.90)</h3>
                <span className="text-xs text-slate-500">read-only</span>
              </div>
              {leap ? (
                <div className="space-y-1">
                  <StrikeHead />
                  <div className="grid grid-cols-4 gap-2 py-1 text-sm tabular-nums">
                    <span className="font-semibold text-slate-100">{fmt(leap.strike, 2)}</span>
                    <span className="text-slate-300">{dollars(leap.bid)} / {dollars(leap.ask)}</span>
                    <span className="text-slate-400">{dollars(leap.mark)}</span>
                    <span className="text-emerald-300">{dollars(leap.extrinsic)}</span>
                  </div>
                  <div className="pt-1 text-xs text-slate-500">
                    {leap.dte} DTE · delta {fmt(leap.delta, 2)} · IV {leap.volatility != null ? `${fmt(leap.volatility, 1)}%` : "—"}
                    {leap.extrinsic_total != null ? ` · total extrinsic ${bigDollars(leap.extrinsic_total)}` : ""}
                  </div>
                </div>
              ) : <p className="text-sm text-slate-400">No suitable LEAP strike found.</p>}
            </div>
            )}

            {/* Open short buyback (only when rolling/closing) */}
            {openShort && (
              <div className={`rounded-lg border bg-slate-950 p-3 ${action === "close_short" ? "border-sky-700" : "border-slate-800"}`}>
                <h3 className="mb-2 text-sm font-semibold text-slate-200">Open short (buy to close)</h3>
                <div className="text-sm text-slate-300">
                  {fmt(openShort.strike, 2)} · {openShort.contracts}c · {openShort.dte} DTE · est. buyback{" "}
                  <span className="font-semibold text-slate-100">{dollars(openShort.current_mark)}/sh</span>
                </div>
              </div>
            )}

            {/* Existing LEAP sell-to-close (when exiting/rolling the long) */}
            {action === "close_leap" && existingLeap && (
              <div className="rounded-lg border border-sky-700 bg-slate-950 p-3">
                <h3 className="mb-2 text-sm font-semibold text-slate-200">Existing LEAP (sell to close)</h3>
                <div className="text-sm text-slate-300">
                  {fmt(existingLeap.strike, 2)} · {existingLeap.contracts}c
                  {existingLeap.current_dte != null ? ` · ${existingLeap.current_dte} DTE` : ""} · est. sell{" "}
                  <span className="font-semibold text-slate-100">{dollars(existingLeap.current_mark)}/sh</span>
                </div>
                <div className="mt-1 text-xs text-slate-500">
                  Cost basis {bigDollars(existingLeap.cost_basis)}
                  {existingLeap.current_mark != null && existingLeap.contracts != null && (
                    <> · est. proceeds {bigDollars(existingLeap.current_mark * 100 * existingLeap.contracts)}</>
                  )}
                  {existingLeap.extrinsic_remaining != null && (
                    <> · {bigDollars(existingLeap.extrinsic_remaining)} extrinsic still unrecovered</>
                  )}
                </div>
              </div>
            )}

            {/* Weekly short — ATR-suggested, user-adjustable (entry only) */}
            {!mgmt && (
            <div className={`rounded-lg border bg-slate-950 p-3 ${action === "sell_short" ? "border-sky-700" : "border-slate-800"}`}>
              <div className="mb-2 flex items-center justify-between">
                <h3 className="text-sm font-semibold text-slate-200">Weekly short call (ATR-suggested)</h3>
                {weekly && <span className="text-xs text-slate-500">{weekly.dte} DTE · {weekly.atr_mult}×ATR {fmt(weekly.atr, 2)}</span>}
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
                        type="radio" name="weekly-strike"
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
              ) : <p className="text-sm text-slate-400">No weekly strikes available.</p>}
            </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
