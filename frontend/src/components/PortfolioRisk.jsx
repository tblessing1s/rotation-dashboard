import React from "react";
import { api } from "../api.js";
import { Card, Meter, Loading, money, fmt, useApi } from "./ui.jsx";

// money() with an explicit sign, for signed exposures.
function signed(n) {
  if (n == null) return "—";
  return `${n < 0 ? "−" : "+"}$${Math.abs(Number(n)).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

// A book-level metric tile: a plain-language headline, a small visual, and one
// line of "what it means" so the number tells you what to do, not just what it is.
function Tile({ label, headline, tone = "text-slate-100", visual, meaning }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-3">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`mt-0.5 text-lg font-semibold leading-tight ${tone}`}>{headline}</div>
      <div className="mt-2">{visual}</div>
      <div className="mt-1.5 text-xs text-slate-500">{meaning}</div>
    </div>
  );
}

// Market-lean gauge: a centred track (short ← neutral → long) with a marker at the
// book's SPY-beta-adjusted delta as a fraction of deployed capital. Ends = the
// 1.5× "one directional bet" line.
function TiltGauge({ lean }) {
  const clamped = lean == null ? 0 : Math.max(-1.5, Math.min(1.5, lean));
  const posPct = 50 + (clamped / 1.5) * 50;
  const abs = Math.abs(lean ?? 0);
  const color = lean == null ? "#64748b" : abs >= 1.5 ? "#fb7185" : abs >= 0.5 ? "#fbbf24" : "#34d399";
  return (
    <div>
      <div className="relative h-2 rounded-full bg-gradient-to-r from-rose-500/25 via-slate-700 to-emerald-500/25">
        <div className="absolute left-1/2 top-1/2 h-3.5 w-px -translate-x-1/2 -translate-y-1/2 bg-slate-500" />
        {lean != null && (
          <div className="absolute top-1/2 h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-slate-950"
               style={{ left: `${posPct}%`, background: color }} />
        )}
      </div>
      <div className="mt-1 flex justify-between text-[9px] uppercase tracking-wide text-slate-600">
        <span>short</span><span>neutral</span><span>long</span>
      </div>
    </div>
  );
}

// One glance = "what should I do with the book right now": a verdict + an explicit
// can-I-add-a-position answer, then each risk as a plain-language, visual tile.
// The sector split and per-position greeks table are analytics, tucked behind
// "Details".
export default function PortfolioRisk() {
  const { data, error, loading } = useApi(api.portfolioRisk, [], null);
  const [open, setOpen] = React.useState(false);
  if (loading && !data) return <Card title="Portfolio risk"><Loading /></Card>;
  if (error) return <Card title="Portfolio risk"><p className="text-sm text-rose-400">{error}</p></Card>;

  const t = data?.totals || {};
  const cap = data?.capital || {};
  const sectors = data?.sector_exposure || [];
  const conc = data?.concentration || {};
  if (!data?.positions?.length) return null;

  // ---- decision logic -----------------------------------------------------
  const overCap = cap.deployed != null && cap.cap != null && cap.deployed > cap.cap;
  const reserveShort = cap.reserve_ok === false;
  const concentrated = !!conc.warn;
  const canAdd = !overCap && !reserveShort && !concentrated;
  const blockReason = overCap ? `over capacity (${fmt(cap.pct_of_cap, 0)}% of cap)`
    : reserveShort ? "defensive reserve underfunded"
    : concentrated ? "book too concentrated"
    : null;

  const thetaPos = t.theta_per_day != null && t.theta_per_day >= 0;
  const lean = t.delta_dollars_spy_adj != null && cap.deployed ? t.delta_dollars_spy_adj / cap.deployed : null;
  const absLev = Math.abs(lean ?? 0);
  const move1 = t.delta_dollars_spy_adj != null ? t.delta_dollars_spy_adj * 0.01 : null;
  const expoLabel = lean == null ? "—" : absLev < 0.5 ? "Near market-neutral" : lean > 0 ? "Net long the market" : "Net short the market";
  const expoTone = absLev >= 1.5 ? "text-rose-300" : absLev >= 0.5 ? "text-amber-300" : "text-emerald-300";

  const weeklyTheta = t.theta_per_day != null ? t.theta_per_day * 7 : null;
  const weeklyYield = weeklyTheta != null && cap.deployed ? (weeklyTheta / cap.deployed) * 100 : null;
  const reservePct = cap.reserve_required ? (cap.operating_cash / cap.reserve_required) * 100 : 100;

  const level = overCap || reserveShort ? "red" : concentrated ? "amber" : "green";
  const stripe = { red: "border-l-rose-500", amber: "border-l-amber-500", green: "border-l-emerald-500" }[level];
  const badge = {
    red: "border-rose-500/40 bg-rose-500/15 text-rose-300",
    amber: "border-amber-500/40 bg-amber-500/15 text-amber-300",
    green: "border-emerald-500/40 bg-emerald-500/15 text-emerald-300",
  }[level];
  const verdictLabel = overCap ? "Fully deployed — manage only"
    : reserveShort ? "Reserve short — manage only"
    : concentrated ? "Concentrated — add with caution"
    : "Healthy — room to add";
  const reasonBits = [
    thetaPos ? "the income engine is positive" : "the income engine is negative",
    absLev < 0.5 ? "the book is market-neutral" : `the book leans ${lean > 0 ? "long" : "short"}`,
    overCap ? `deployed is ${fmt(cap.pct_of_cap, 0)}% of cap` : "you're within your capital cap",
  ];

  return (
    <Card
      title="Portfolio risk"
      right={
        <button
          onClick={() => setOpen((v) => !v)}
          className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-800"
        >
          {open ? "Hide details ▲" : "Details ▼"}
        </button>
      }
    >
      {/* Verdict + the one decision this panel gates: can I open another position? */}
      <div className={`flex flex-wrap items-center justify-between gap-x-6 gap-y-2 rounded-xl border border-slate-800 border-l-2 bg-slate-900/40 p-3 ${stripe}`}>
        <div className="min-w-0">
          <span className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold uppercase tracking-wide ${badge}`}>
            {verdictLabel}
          </span>
          <p className="mt-1 text-sm text-slate-400">{reasonBits.join(" · ")}.</p>
        </div>
        <div className="text-right">
          <div className="text-[10px] uppercase tracking-wide text-slate-500">Add a position?</div>
          <div className={`text-lg font-semibold ${canAdd ? "text-emerald-300" : "text-rose-300"}`}>
            {canAdd ? "Yes — room to add" : "No"}
          </div>
          {!canAdd && <div className="text-xs text-rose-400/90">{blockReason}</div>}
        </div>
      </div>

      {/* Four plain-language, visual tiles. */}
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Tile
          label="Market exposure"
          headline={expoLabel}
          tone={expoTone}
          visual={<TiltGauge lean={lean} />}
          meaning={<>a 1% market move ≈ <span className={move1 >= 0 ? "text-emerald-300" : "text-rose-300"}>{signed(move1)}</span>. {absLev >= 1.5 ? "The spread is really one directional bet." : "Balanced against the index."}</>}
        />
        <Tile
          label="Income engine (theta)"
          headline={`${thetaPos ? "+" : ""}${money(t.theta_per_day)}/day`}
          tone={thetaPos ? "text-emerald-300" : "text-rose-300"}
          visual={<Meter pct={weeklyYield != null ? Math.min((weeklyYield / 2) * 100, 100) : 0} tone={thetaPos ? "bg-emerald-500" : "bg-rose-500"} />}
          meaning={thetaPos
            ? <>collecting more decay than the LEAPs burn{weeklyYield != null && <> — ≈ {fmt(weeklyYield, 1)}%/wk of deployed</>}. The machine is earning.</>
            : <>the LEAPs are burning faster than the shorts collect — roll shorts up/out.</>}
        />
        <Tile
          label="Capacity used"
          headline={`${fmt(cap.pct_of_cap, 0)}% of cap`}
          tone={overCap ? "text-rose-300" : "text-slate-100"}
          visual={<Meter pct={cap.pct_of_cap != null ? Math.min(cap.pct_of_cap, 100) : 0} tone={overCap ? "bg-rose-500" : "bg-sky-500"} />}
          meaning={overCap
            ? <>{money(cap.deployed)} of {money(cap.cap)} — over capacity. Rotate or trim before adding.</>
            : <>{money(cap.deployed)} of {money(cap.cap)} — {money(cap.cap - cap.deployed)} of headroom left.</>}
        />
        <Tile
          label="Defensive reserve"
          headline={cap.reserve_ok ? "Covered" : "Short"}
          tone={cap.reserve_ok ? "text-emerald-300" : "text-rose-300"}
          visual={<Meter pct={Math.min(reservePct, 100)} tone={cap.reserve_ok ? "bg-emerald-500" : "bg-rose-500"} />}
          meaning={cap.reserve_ok
            ? <>{money(cap.operating_cash)} cash vs {money(cap.reserve_required)} needed — enough to roll a breached short down.</>
            : <>{money(cap.operating_cash)} cash vs {money(cap.reserve_required)} needed — can't defend every position; hold cash.</>}
        />
      </div>

      {/* Vega is secondary for a LEAP book — one plain line, not a tile. */}
      {t.vega != null && (
        <p className="mt-2 text-xs text-slate-500">
          Volatility: a 1-point drop in IV trims the book by <span className="text-slate-300">{money(Math.abs(t.vega))}</span> (vega {money(t.vega)}) — LEAP-heavy, usually a minor drag.
        </p>
      )}

      {/* A live concentration warning stays visible even with details collapsed. */}
      {conc.warn && (
        <div className="mt-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3">
          <div className="flex flex-wrap items-center gap-2 text-sm font-semibold text-amber-300">
            <span>⚠ Concentration — your names move together; the diversification is thinner than 1-per-sector implies</span>
            {conc.max_correlation != null && (
              <span className="text-xs font-normal text-amber-400/80">
                max pair corr {fmt(conc.max_correlation, 2)}
                {conc.beta_adj_leverage != null && ` · β-adj Δ ${fmt(conc.beta_adj_leverage, 2)}× capital`}
              </span>
            )}
          </div>
          <ul className="mt-1 list-disc pl-5 text-xs text-amber-200/90">
            {conc.warnings.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
        </div>
      )}

      {open && (
        <div className="mt-4 border-t border-slate-800 pt-3">
          <div className="mb-1 text-xs text-slate-400">Sector exposure (LEAP capital)</div>
          <div className="flex h-3 w-full overflow-hidden rounded-full bg-slate-800">
            {sectors.map((s, i) => (
              <div
                key={s.sector}
                title={`${s.sector}: ${money(s.capital)} (${s.pct}%)`}
                className={["bg-sky-500", "bg-emerald-500", "bg-amber-500", "bg-rose-500", "bg-violet-500", "bg-teal-500"][i % 6]}
                style={{ width: `${s.pct}%` }}
              />
            ))}
          </div>
          <div className="mt-1 flex flex-wrap gap-x-3 text-xs text-slate-500">
            {sectors.map((s) => <span key={s.sector}>{s.sector} {fmt(s.pct, 0)}%</span>)}
          </div>

          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wide text-slate-500">
                  <th className="py-1 pr-3">Ticker</th>
                  <th className="py-1 pr-3">β</th>
                  <th className="py-1 pr-3">Δ shares</th>
                  <th className="py-1 pr-3">Δ $</th>
                  <th className="py-1 pr-3">Δ $ (β adj)</th>
                  <th className="py-1 pr-3">Θ/day</th>
                  <th className="py-1 pr-3">Vega</th>
                </tr>
              </thead>
              <tbody>
                {data.positions.map((r) => (
                  <tr key={r.ticker} className="border-t border-slate-800/60">
                    <td className="py-1.5 pr-3 font-semibold text-slate-100">
                      {r.ticker}
                      {!r.greeks_complete && (
                        <span className="ml-1 text-xs text-amber-400" title="Some legs lacked a usable mark — greeks partial">*</span>
                      )}
                    </td>
                    <td className="py-1.5 pr-3 text-slate-300">{fmt(r.beta, 2)}</td>
                    <td className="py-1.5 pr-3 text-slate-300">{fmt(r.delta_shares, 0)}</td>
                    <td className="py-1.5 pr-3 text-slate-300">{money(r.delta_dollars)}</td>
                    <td className="py-1.5 pr-3 text-slate-300">{money(r.delta_dollars_spy_adj)}</td>
                    <td className={`py-1.5 pr-3 ${r.theta_per_day >= 0 ? "text-emerald-300" : "text-rose-300"}`}>{money(r.theta_per_day)}</td>
                    <td className="py-1.5 pr-3 text-slate-300">{money(r.vega)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </Card>
  );
}
