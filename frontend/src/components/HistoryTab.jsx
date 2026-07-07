import React from "react";
import { api } from "../api.js";
import { Card, Pill, Stat, Loading, money, fmt, pct, useApi } from "./ui.jsx";

// Closed-cycle history: the learning loop. Every number derives from the
// immutable execution log (see logging_handler.recompute_derived).
// Also home to the theta ledger (absorbed from the old Theta tab): the LEAP
// extrinsic hurdle, roll totals, and the per-week closes table. Live juice
// totals and per-ticker payback meters stay on Overview.

function ThetaLedgerCards({ theta }) {
  const summary = theta?.extrinsic_summary || {};
  const hurdle = summary.leap_extrinsic_at_entry || 0;
  const weeks = theta?.weeks || [];
  const rollByTicker = theta?.roll_ledger?.by_ticker || {};
  const rollTotals = Object.values(rollByTicker).reduce(
    (a, r) => ({ count: a.count + (r.count || 0), net: a.net + (r.net_total || 0), drag: a.drag + (r.drag_total || 0) }),
    { count: 0, net: 0, drag: 0 },
  );
  if (!hurdle && !rollTotals.count && weeks.length === 0) return null;

  return (
    <>
      {(hurdle > 0 || rollTotals.count > 0) && (
        <Card title="Theta ledger">
          {hurdle > 0 && (
            <div className="grid grid-cols-3 gap-4">
              <Stat label="LEAP extrinsic hurdle" value={money(hurdle)} sub="income needed to net positive" />
              <Stat label="Remaining to fill" value={money(summary.remaining_to_payback)}
                    tone={summary.income_positive ? "text-emerald-300" : "text-amber-300"} />
              <Stat label="Net income" value={money(summary.net_income)}
                    tone={summary.income_positive ? "text-emerald-300" : "text-rose-300"}
                    sub={summary.income_positive ? "income-positive ✓" : "still filling the LEAP"} />
            </div>
          )}
          {rollTotals.count > 0 && (
            <div className={`grid grid-cols-3 gap-4 ${hurdle > 0 ? "mt-4 border-t border-slate-800 pt-4" : ""}`}>
              <Stat label="Rolls executed" value={rollTotals.count} sub="paired close+open tickets" />
              <Stat label="Roll net" value={money(rollTotals.net)}
                    tone={rollTotals.net >= 0 ? "text-emerald-300" : "text-rose-300"}
                    sub="credits − buybacks across all rolls" />
              <Stat label="Roll drag" value={money(rollTotals.drag)}
                    tone={rollTotals.drag < 0 ? "text-rose-300" : "text-slate-100"}
                    sub="debits paid on defensive rolls (whipsaw cost)" />
            </div>
          )}
        </Card>
      )}

      <Card title="Per-week closes">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wide text-slate-500">
                <th className="py-2 pr-3">Week</th>
                <th className="py-2 pr-3">Ticker</th>
                <th className="py-2 pr-3">Extrinsic sold</th>
                <th className="py-2 pr-3">Paid back</th>
                <th className="py-2 pr-3">Net juice</th>
              </tr>
            </thead>
            <tbody>
              {weeks.map((w, i) => (
                <tr key={i} className="border-t border-slate-800">
                  <td className="py-2 pr-3 text-slate-300">{w.week}</td>
                  <td className="py-2 pr-3 font-semibold text-slate-100">{w.ticker}</td>
                  <td className="py-2 pr-3">{money(w.extrinsic_sold)}</td>
                  <td className="py-2 pr-3">{money(w.extrinsic_paid_back)}</td>
                  <td className="py-2 pr-3 text-emerald-300">{money(w.net_juice)}</td>
                </tr>
              ))}
              {weeks.length === 0 && <tr><td colSpan={5} className="py-6 text-center text-slate-500">No closes logged yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}

function WeeklyJuiceChart({ data }) {
  const weeks = data?.weeks || [];
  if (!weeks.length) return <p className="text-sm text-slate-500">No weekly juice logged yet.</p>;
  const values = weeks.map((w) => w.net_juice);
  const maxVal = Math.max(...values, data.target_high || 0, 1);
  return (
    <div>
      <div className="flex items-end gap-1" style={{ height: 120 }}>
        {weeks.map((w) => {
          const h = Math.max((Math.abs(w.net_juice) / maxVal) * 100, 2);
          const onPace = data.target_low != null && w.net_juice >= data.target_low;
          return (
            <div key={w.week} className="group relative flex-1">
              <div
                className={`w-full rounded-t ${w.net_juice < 0 ? "bg-rose-500/70" : onPace ? "bg-emerald-500/80" : "bg-sky-500/60"}`}
                style={{ height: `${h}px`, maxHeight: 110 }}
                title={`${w.week}: ${money(w.net_juice)}`}
              />
            </div>
          );
        })}
      </div>
      <div className="mt-1 flex justify-between text-[10px] text-slate-600">
        <span>{weeks[0]?.week}</span>
        <span>{weeks[weeks.length - 1]?.week}</span>
      </div>
      {data.target_low != null && data.capital_deployed > 0 && (
        <p className="mt-1 text-xs text-slate-500">
          Target band (1–2%/wk of {money(data.capital_deployed)} deployed):{" "}
          <span className="text-emerald-300">{money(data.target_low)}–{money(data.target_high)}</span>/week.
          Green bars are on pace.
        </p>
      )}
    </div>
  );
}

function CycleRow({ c }) {
  const [open, setOpen] = React.useState(false);
  const ret = c.net_return_pct;
  const retTone = ret == null ? "text-slate-400" : ret >= 0 ? "text-emerald-300" : "text-rose-300";
  return (
    <>
      <tr onClick={() => setOpen(!open)} className="cursor-pointer border-t border-slate-800 hover:bg-slate-800/40">
        <td className="py-2 pr-3 font-semibold text-slate-100">
          <span className="mr-1 text-slate-500">{open ? "▾" : "▸"}</span>{c.ticker}
        </td>
        <td className="py-2 pr-3 text-slate-300">{c.entry_date} → {c.exit_date}</td>
        <td className="py-2 pr-3 text-slate-300">{c.days_held ?? "—"}d</td>
        <td className="py-2 pr-3 text-slate-300">{money(c.capital_deployed)}</td>
        <td className="py-2 pr-3 text-emerald-300">{money(c.gross_juice)}</td>
        <td className={`py-2 pr-3 ${c.roll_drag < 0 ? "text-rose-300" : "text-slate-400"}`}>{money(c.roll_drag)}</td>
        <td className={`py-2 pr-3 ${c.leap_pnl >= 0 ? "text-slate-300" : "text-rose-300"}`}>{money(c.leap_pnl)}</td>
        <td className={`py-2 pr-3 font-semibold ${retTone}`}>{pct(ret)}</td>
        <td className="py-2 pr-3">
          <Pill status={c.target_met ? "go" : ret != null && ret < 0 ? "avoid" : "caution"}>
            {c.target_met ? "target" : ret != null && ret < 0 ? "loss" : "under"}
          </Pill>
        </td>
        <td className="py-2 pr-3 text-slate-400">{c.exit_reason}</td>
        <td className="py-2 pr-3">
          {c.wash_sale && (
            <span
              title={c.wash_sale.status === "flagged"
                ? `Loss ${money(c.wash_sale.loss)} re-entered ${c.wash_sale.reentry_date} — wash sale likely`
                : `Loss ${money(c.wash_sale.loss)} — window open until ${c.wash_sale.window_ends}`}
              className="cursor-help rounded-full border border-amber-500/40 bg-amber-500/15 px-2 py-0.5 text-[10px] font-medium text-amber-300"
            >
              wash {c.wash_sale.status === "flagged" ? "⚑" : "⏳"}
            </span>
          )}
        </td>
      </tr>
      {open && (
        <tr className="border-t border-slate-800/50 bg-slate-900/40">
          <td colSpan={11} className="px-4 py-3 text-sm text-slate-300">
            <div className="grid gap-1">
              <span>
                {c.roll_count} roll(s), net {money(c.roll_net)} · target {c.target_range_pct?.[0]}–{c.target_range_pct?.[1]}%
              </span>
              {c.entry_snapshot ? (
                <span className="text-xs text-slate-400">
                  At entry: verdict <span className="font-semibold text-slate-200">{c.entry_snapshot.verdict}</span>
                  {c.entry_snapshot.reasons?.length > 0 && <> ({c.entry_snapshot.reasons.join("; ")})</>}
                  {" · "}RS vs SPY {pct(c.entry_snapshot.rs3m_vs_spy)} · RS vs Sec {pct(c.entry_snapshot.rs3m_vs_sector)}
                  {" · "}ATR ext {fmt(c.entry_snapshot.atr_extension, 2)} · MFI {fmt(c.entry_snapshot.mfi, 0)}
                  {" · "}juice/wk {c.entry_snapshot.juice_weekly_pct != null ? `${fmt(c.entry_snapshot.juice_weekly_pct, 2)}%` : "—"}
                </span>
              ) : (
                <span className="text-xs text-slate-500">No entry snapshot (position predates snapshots).</span>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

export default function HistoryTab() {
  const { data, error, loading } = useApi(api.history, [], null);
  const { data: theta } = useApi(api.thetaLedger, [], null);
  if (loading && !data) return <Card title="History"><Loading /></Card>;
  if (error) return <Card title="History"><p className="text-sm text-rose-400">{error}</p></Card>;

  const agg = data?.aggregates || {};
  const cycles = data?.cycles || [];

  return (
    <div className="grid gap-4">
      <Card
        title="Closed cycles — aggregate"
        right={
          <div className="flex gap-2 text-xs">
            <a href="/api/export/juice-journal?format=csv" download
               className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 font-semibold text-slate-300 hover:bg-slate-800">
              Export CSV
            </a>
            <a href="/api/export/juice-journal?format=md" download
               className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 font-semibold text-slate-300 hover:bg-slate-800">
              Export MD
            </a>
          </div>
        }
      >
        {agg.count ? (
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-5">
            <Stat label="Cycles" value={agg.count} />
            <Stat label="Win rate" value={`${fmt(agg.win_rate, 0)}%`}
                  tone={agg.win_rate >= 50 ? "text-emerald-300" : "text-rose-300"} />
            <Stat label="Avg return" value={pct(agg.avg_return_pct)}
                  tone={agg.avg_return_pct >= 0 ? "text-emerald-300" : "text-rose-300"}
                  sub={`target ${fmt(agg.target_hit_rate, 0)}% hit`} />
            <Stat label="Avg juice/wk" value={money(agg.avg_juice_per_week)} tone="text-emerald-300" />
            <Stat label="Avg roll drag" value={money(agg.avg_roll_drag)}
                  tone={agg.avg_roll_drag < 0 ? "text-rose-300" : "text-slate-100"} />
          </div>
        ) : (
          <p className="text-sm text-slate-500">No closed cycles yet — exits will land here with full derived math.</p>
        )}
      </Card>

      <Card title="Weekly net juice vs target">
        <WeeklyJuiceChart data={data?.weekly_juice} />
      </Card>

      <ThetaLedgerCards theta={theta} />

      <Card title="Cycle log">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wide text-slate-500">
                <th className="py-2 pr-3">Ticker</th>
                <th className="py-2 pr-3">Dates</th>
                <th className="py-2 pr-3">Held</th>
                <th className="py-2 pr-3">Capital</th>
                <th className="py-2 pr-3">Juice</th>
                <th className="py-2 pr-3">Roll drag</th>
                <th className="py-2 pr-3">LEAP P&L</th>
                <th className="py-2 pr-3">Return</th>
                <th className="py-2 pr-3">vs 15–25%</th>
                <th className="py-2 pr-3">Exit</th>
                <th className="py-2 pr-3">Tax</th>
              </tr>
            </thead>
            <tbody>
              {cycles.map((c) => <CycleRow key={c.id} c={c} />)}
              {cycles.length === 0 && (
                <tr><td colSpan={11} className="py-6 text-center text-slate-500">No cycles closed yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
