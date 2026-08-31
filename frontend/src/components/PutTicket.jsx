import React from "react";
import { api } from "../api.js";
import { Card, Pill, Loading, fmt } from "./ui.jsx";
import { useToast } from "./Toast.jsx";
import { submitOrder } from "../orderFlow.js";

// The cash-secured put ticket (schema v22).
//
// The CFM route selector says a name that is extended above its MA21 zone is a
// bad SHARES entry and a good PUT entry: sell a weekly put struck down at the
// price you actually want, and be paid to wait for it. This is the surface that
// turns that advice into a position. Before it existed the only way to open a
// put was a hand-rolled POST to /api/execute.
//
// ONE PAYLOAD, TWO OUTCOMES. The button sends `put_opened` either way; whether
// that PLACES an order at Schwab or BOOKS a fill you already made there is
// decided server-side by CSP_ORDER_PLACEMENT_ENABLED + live trading + a Schwab
// connection. The UI's only job is to tell the truth about which will happen —
// hence the label and the reasons list, rather than a greyed-out button that
// leaves you guessing which of three switches is off.

function StrikeRow({ row, selected, onSelect, spot }) {
  const itm = spot != null && row.strike > spot;
  return (
    <button
      onClick={() => onSelect(row)}
      className={`flex w-full items-center gap-3 rounded-lg border px-3 py-2 text-left text-xs ${
        selected
          ? "border-sky-600 bg-sky-500/10"
          : "border-slate-800 bg-slate-950 hover:bg-slate-900/60"
      }`}
    >
      <span className="w-16 font-semibold text-slate-100">
        {fmt(row.strike, 2)}
        {row.suggested && <span className="ml-1 text-[10px] text-sky-400">★</span>}
      </span>
      <span className="w-20 text-slate-300">
        {row.premium_per_share != null ? `$${fmt(row.premium_per_share, 2)}` : "—"}
        <span className="text-slate-600"> bid</span>
      </span>
      <span className="w-16 text-emerald-300">
        {row.juice_pct != null ? `${fmt(row.juice_pct, 2)}%` : "—"}
      </span>
      <span className="w-20 text-slate-400">
        {row.collateral != null ? `$${fmt(row.collateral, 0)}` : "—"}
      </span>
      <span className="w-14 text-slate-400">
        {row.delta_abs != null ? fmt(row.delta_abs, 2) : "—"}
        <span className="text-slate-600">Δ</span>
      </span>
      <span className={`w-14 ${row.spread_pct > 15 ? "text-rose-400" : "text-slate-500"}`}>
        {row.spread_pct != null ? `${fmt(row.spread_pct, 1)}%` : "—"}
      </span>
      {/* An ITM put is already in assignment territory — worth flagging, not
          forbidding: writing one deliberately is a way to get assigned sooner. */}
      {itm && <span className="text-[10px] text-amber-400">ITM</span>}
    </button>
  );
}

export default function PutTicket({ ticker, onExecuted }) {
  const toast = useToast();
  const [chain, setChain] = React.useState(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState(null);
  const [expIdx, setExpIdx] = React.useState(0);
  const [pick, setPick] = React.useState(null);
  const [contracts, setContracts] = React.useState(1);
  const [busy, setBusy] = React.useState(false);

  const load = React.useCallback(async (t) => {
    if (!t) return;
    setLoading(true); setError(null); setChain(null); setPick(null); setExpIdx(0);
    try {
      setChain(await api.putChain(t));
    } catch (e) { setError(e.message); }
    finally { setLoading(false); }
  }, []);

  const group = chain?.expirations?.[expIdx];
  // Default to the suggested strike (nearest the MA21 zone) whenever the
  // expiration changes, so the ticket always opens on the recommended trade.
  React.useEffect(() => {
    if (!group?.strikes?.length) { setPick(null); return; }
    setPick(group.strikes.find((s) => s.suggested) || group.strikes[0]);
  }, [group]);

  const placement = chain?.placement;
  const canPlace = !!placement?.can_place;
  const blocked = chain?.verdict === "BLOCKED";

  async function send() {
    if (!pick || !group) return;
    // RECORDING IS NOT PLACING, and the difference is worth one click. With any of
    // the three switches off this books a position into the ledger and sends
    // nothing to Schwab — a phantom position if the operator did not sell it at
    // the broker themselves. The button label says so; this makes it impossible
    // to skip past on muscle memory.
    if (!canPlace) {
      const ok = window.confirm(
        `NO ORDER WILL BE SENT TO SCHWAB.\n\n` +
          `This records a put you have ALREADY SOLD at the broker:\n` +
          `  Sell ${contracts} ${chain.ticker} ${group.expiration} ${pick.strike}P ` +
          `@ $${pick.premium_per_share}\n\n` +
          `${placement?.reasons?.length ? `Placement is off: ${placement.reasons.join("; ")}.\n\n` : ""}` +
          `Have you already sold this put at Schwab?`,
      );
      if (!ok) return;
    }
    setBusy(true);
    try {
      await submitOrder(api, toast, {
        action: "put_opened",
        ticker: chain.ticker,
        strike: pick.strike,
        expiration: group.expiration,
        contracts: Number(contracts) || 1,
        premium_per_share: pick.premium_per_share,
        stock_price: chain.underlying,
        // The executor re-checks the veto set at the ticket regardless; passing
        // the measured put-side spread lets it enforce the tradeability floor
        // against the same number shown above rather than a second estimate.
        put_spread_pct: pick.spread_pct,
        regime_at_entry: chain.route?.regime_color,
        extension_from_ma21: chain.route?.detail?.extension_atr,
      });
      onExecuted?.();
      await load(ticker);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }

  return (
    <Card
      title="Sell a Cash-Secured Put"
      right={chain ? <Pill status={blocked ? "no" : "ready"}>{chain.verdict}</Pill> : null}
    >
      {!chain && !loading && (
        <>
          <p className="mb-3 text-sm text-slate-400">
            Sell a weekly put struck at the MA21 zone — get paid to wait for the price
            you actually want. Weekly expiries only (max {"≤"} 10 DTE).
          </p>
          <button
            onClick={() => load(ticker)}
            disabled={!ticker}
            className="w-full rounded-lg border border-violet-700 bg-violet-500/10 py-2 text-sm font-semibold text-violet-300 hover:bg-violet-500/20 disabled:opacity-40"
          >
            {ticker ? `Load put chain for ${ticker}` : "Run the entry gate for a ticker first"}
          </button>
        </>
      )}

      {loading && <Loading label="Loading put chain…" />}
      {error && <p className="text-sm text-rose-400">{error}</p>}

      {chain && (
        <div className="grid gap-3">
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs text-slate-400">
            <span>Spot <span className="font-semibold text-slate-100">{fmt(chain.underlying, 2)}</span></span>
            <span>MA21 <span className="font-semibold text-slate-100">{fmt(chain.ma21, 2)}</span></span>
            {chain.route?.route === "CASH_SECURED_PUT" && (
              <span className="text-violet-300">Route says: sell the put</span>
            )}
            {chain.route?.route === "SHARES" && (
              <span className="text-amber-300">
                Route says: buy shares — the price is available today
              </span>
            )}
          </div>

          {blocked && (
            <p className="rounded-lg border border-rose-800 bg-rose-500/5 px-3 py-2 text-xs text-rose-300">
              <span className="font-semibold">The entry rules refuse this name right now:</span>{" "}
              {chain.blocked_by.join(", ")}. A put is a synthetic long position and gets
              no exemption for being an option — the executor will reject the ticket.
            </p>
          )}

          {chain.expirations.length === 0 ? (
            <p className="text-sm text-slate-500">
              No weekly put expiries within {chain.max_dte} DTE for {chain.ticker}.
            </p>
          ) : (
            <>
              <div className="flex gap-2">
                {chain.expirations.map((g, i) => (
                  <button
                    key={g.expiration}
                    onClick={() => setExpIdx(i)}
                    className={`rounded-lg border px-3 py-1 text-xs ${
                      i === expIdx
                        ? "border-sky-600 bg-sky-500/10 text-sky-200"
                        : "border-slate-800 text-slate-400 hover:bg-slate-900/60"
                    }`}
                  >
                    {g.expiration} <span className="text-slate-500">({g.dte}d)</span>
                  </button>
                ))}
              </div>

              <div>
                <div className="flex gap-3 px-3 pb-1 text-[10px] uppercase tracking-wide text-slate-600">
                  <span className="w-16">Strike</span>
                  <span className="w-20">Premium</span>
                  <span className="w-16">Juice</span>
                  <span className="w-20">Collateral</span>
                  <span className="w-14">Delta</span>
                  <span className="w-14">Spread</span>
                </div>
                <div className="grid gap-1">
                  {group.strikes.map((row) => (
                    <StrikeRow
                      key={row.strike}
                      row={row}
                      spot={chain.underlying}
                      selected={pick?.strike === row.strike}
                      onSelect={setPick}
                    />
                  ))}
                </div>
              </div>

              <div className="flex items-center gap-3">
                <label className="text-xs text-slate-400">Contracts</label>
                <input
                  type="number"
                  min="1"
                  value={contracts}
                  onChange={(e) => setContracts(e.target.value)}
                  className="w-20 rounded-lg border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-100"
                />
                {pick && (
                  <span className="text-xs text-slate-400">
                    Collateral{" "}
                    <span className="font-semibold text-slate-100">
                      ${fmt((pick.collateral || 0) * (Number(contracts) || 1), 0)}
                    </span>{" "}
                    · Premium{" "}
                    <span className="font-semibold text-emerald-300">
                      ${fmt((pick.premium_per_share || 0) * 100 * (Number(contracts) || 1), 2)}
                    </span>
                  </span>
                )}
              </div>

              {/* The truth about what the button will do, and why. */}
              {!canPlace && (
                <p className="rounded-lg border border-amber-700 bg-amber-500/5 px-3 py-2 text-xs text-amber-300">
                  <span className="font-semibold">Record only.</span> This books a put you
                  already sold at Schwab; no order is transmitted.{" "}
                  {placement?.reasons?.length ? `Placement is off: ${placement.reasons.join("; ")}.` : ""}
                </p>
              )}

              <button
                onClick={send}
                disabled={!pick || busy}
                className={`w-full rounded-lg border py-2 text-sm font-semibold disabled:opacity-40 ${
                  canPlace
                    ? "border-emerald-700 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20"
                    : "border-slate-700 bg-slate-900/40 text-slate-300 hover:bg-slate-900/70"
                }`}
              >
                {busy
                  ? "Working…"
                  : canPlace
                  ? `Sell ${contracts} ${chain.ticker} ${fmt(pick?.strike, 2)}P — LIVE ORDER`
                  : `Record ${contracts} ${chain.ticker} ${fmt(pick?.strike, 2)}P sold at broker`}
              </button>
            </>
          )}
        </div>
      )}
    </Card>
  );
}
