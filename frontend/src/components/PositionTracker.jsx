import React from "react";
import { api } from "../api.js";
import { Card, Stat, Meter, Pill, Loading, money, fmt, useApi } from "./ui.jsx";
import RollModal from "./RollModal.jsx";

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

export default function PositionTracker() {
  const { data, error, loading, reload } = useApi(api.positions, [], null);
  const [rolling, setRolling] = React.useState(null); // ticker being rolled

  if (loading && !data) return <Card title="Positions"><Loading /></Card>;
  if (error) return <Card title="Positions"><p className="text-sm text-rose-400">{error}</p></Card>;

  const positions = data?.positions || [];
  const cap = data?.capital || {};
  const ms = cap.milestones || {};

  async function runRoll(payload) {
    await api.execute(payload);
    reload();
  }

  return (
    <div className="grid gap-4">
      <Card title="Capital">
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Stat label="Deployed" value={money(cap.capital_deployed)} />
          <Stat label="Reserve req." value={money(cap.reserve_required)} tone={cap.reserve_ok ? "text-slate-100" : "text-rose-300"} />
          <Stat label="Operating cash" value={money(cap.operating_cash)} />
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
                <EarningsBadge earnings={p.earnings} />
                <Pill status={p.status === "active" ? "green" : "unknown"}>{p.status}</Pill>
              </div>
            }
          >
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
                <div className="text-sm text-slate-200">{fmt(p.stock_price, 2)}</div>
                <div className="text-xs text-slate-500">{shorts.length} open short(s)</div>
              </div>
            </div>

            {/* Open shorts — each rollable in place (pick week + strike) */}
            <div className="mt-4 border-t border-slate-800 pt-3">
              <div className="mb-2 flex items-center justify-between">
                <span className="text-xs uppercase tracking-wide text-slate-500">Short calls</span>
                {shorts.length > 0 && (
                  <button
                    onClick={() => setRolling(p.ticker)}
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
                      </span>
                      {sc.dte != null && sc.dte <= 2 && (
                        <span className="rounded-full border border-amber-500/40 bg-amber-500/15 px-2 py-0.5 text-[10px] font-medium text-amber-300">expiring</span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <DeltaCoverage ticker={p.ticker} />
          </Card>
        );
      })}

      {rolling && (
        <RollModal
          ticker={rolling}
          onExecute={runRoll}
          onClose={() => setRolling(null)}
        />
      )}
    </div>
  );
}
