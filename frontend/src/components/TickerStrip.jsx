import React from "react";
import { api } from "../api.js";
import { fmt, useApi } from "./ui.jsx";

// The per-position price strip in the app chrome: on every tab, every open
// position's live stock price and how far it sits from each short strike.
//
// The numbers come from /api/ticker-strip, which reads the same quote path and
// the same strike_gap derivation as the position card (so the two never
// disagree); this only lays them out. Clicking a name jumps to its card on the
// Positions tab through the same cfm-action event the alerts panel uses.
const POLL_MS = 45 * 1000;

function fmtExp(exp) {
  if (!exp) return "";
  const s = String(exp).slice(0, 10);
  const [, m, d] = s.split("-");
  return m && d ? `${Number(m)}/${Number(d)}` : s;
}

function Leg({ leg, spot }) {
  const label = `${fmt(leg.strike, leg.strike % 1 ? 2 : 0)}${leg.kind === "put" ? "P" : "C"}`;
  if (spot == null || leg.distance == null) {
    return <span className="text-slate-500" title="No live price for this name yet">{label} · —</span>;
  }
  const itm = leg.itm === true;
  const above = leg.distance > 0;
  const tone = itm ? "text-amber-300" : "text-slate-300";
  const meaning = leg.kind === "put"
    ? (itm ? "below the strike — assignment territory" : "above the strike — clear of assignment")
    : (itm ? "above the strike — called away at expiry unless rolled" : "below the strike — cushion before assignment");
  return (
    <span className={tone}
          title={`${leg.contracts ?? 1} × ${label}${leg.expiration ? ` exp ${leg.expiration}` : ""}: stock ${fmt(spot, 2)} is `
            + `${fmt(Math.abs(leg.distance), 2)} (${fmt(Math.abs(leg.distance_pct ?? 0), 1)}%) ${meaning}.`}>
      {label}{leg.expiration ? <span className="text-slate-500"> {fmtExp(leg.expiration)}</span> : null}
      {" "}
      <span className="font-semibold">{above ? "+" : "−"}{fmt(Math.abs(leg.distance), 2)}</span>
      {leg.distance_pct != null && <span className="text-slate-500"> ({fmt(Math.abs(leg.distance_pct), 1)}%)</span>}
      <span className={`ml-1 rounded px-1 text-[10px] font-bold ${itm ? "bg-amber-500/20 text-amber-300" : "bg-slate-800 text-slate-400"}`}>
        {leg.moneyness || (itm ? "ITM" : "OTM")}
      </span>
    </span>
  );
}

function Chip({ p }) {
  const go = () => window.dispatchEvent(new CustomEvent("cfm-action", { detail: { action: "focus", ticker: p.ticker } }));
  return (
    <button onClick={go}
            className="flex shrink-0 items-center gap-2 rounded-md border border-slate-800 bg-slate-900/60 px-2 py-1 text-xs hover:border-slate-600 hover:bg-slate-800/80"
            title={`${p.ticker} · ${p.shares} shares · open the position card`}>
      <span className="flex items-center gap-1 font-semibold text-slate-100">
        {p.needs_review && <span className="h-1.5 w-1.5 rounded-full bg-rose-400" title="Needs review — diverged from the broker" />}
        {p.ticker}
      </span>
      <span className="font-mono text-slate-200">{p.stock_price != null ? fmt(p.stock_price, 2) : <span className="text-slate-500">—</span>}</span>
      {p.legs.length === 0
        ? <span className="text-slate-500">no short</span>
        : p.legs.map((l, i) => <Leg key={i} leg={l} spot={p.stock_price} />)}
    </button>
  );
}

export default function TickerStrip() {
  // Polled; App also remounts it (key) on an account switch or a fill, so it
  // refetches immediately when what it shows has changed.
  const { data, error, reload } = useApi(api.tickerStrip, [], POLL_MS);
  const rows = data?.positions || [];
  if (!data && !error) return null;
  if (error) {
    return (
      <div className="-mx-3 border-t border-slate-800/60 px-3 py-1 text-[11px] text-rose-400/80">
        price strip unavailable — {error}
      </div>
    );
  }
  if (rows.length === 0) return null;
  const asOf = data?.as_of ? String(data.as_of).slice(11, 16) : null;
  return (
    <div className="-mx-3 flex items-center gap-2 overflow-x-auto border-t border-slate-800/60 px-3 py-1.5 no-scrollbar">
      {rows.map((p) => <Chip key={p.ticker} p={p} />)}
      {asOf && (
        <button onClick={reload} className="ml-auto shrink-0 pl-2 text-[10px] text-slate-600 hover:text-slate-400"
                title="Quotes as of (UTC) — click to refresh">
          {asOf}Z
        </button>
      )}
    </div>
  );
}
