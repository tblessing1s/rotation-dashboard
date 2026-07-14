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
  const slip = theta?.slippage;
  if (!hurdle && !rollTotals.count && weeks.length === 0) return null;

  return (
    <>
      {slip && (
        <div
          className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200/90"
          title="Paper fills are booked at the quoted midpoint; deep-ITM options rarely fill at mid, so realized juice runs below these figures."
        >
          {slip.mid_fill_caveat ? (
            <>
              <span className="font-semibold text-amber-300">Mid-fill assumption.</span>{" "}
              These figures book paper fills at the quoted mid — realized fills run below them
              (~{fmt(slip.roundtrip_haircut_pct, 1)}% of premium per weekly round trip, {slip.source}).
              {" "}{slip.live_fills}/{slip.min_fills} live fills logged; the haircut becomes measured after that.
            </>
          ) : (
            <>
              <span className="font-semibold text-amber-300">Measured slippage.</span>{" "}
              Realized fills run ~{fmt(slip.effective_slippage_pct, 2)}% below mid per leg
              (from {slip.live_fills} live fills) — apply ~{fmt(slip.roundtrip_haircut_pct, 1)}% of premium
              per weekly round trip when reading these paper figures.
            </>
          )}
        </div>
      )}
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

// Humanize a coded exit reason (exit_reasons.ExitReason) for display, e.g.
// "KILL_SWITCH_SECTOR" -> "Kill switch sector". LEGACY_UNRECORDED reads plainly.
function exitLabel(code) {
  if (!code) return "—";
  return code.charAt(0) + code.slice(1).toLowerCase().replace(/_/g, " ");
}

function CycleRow({ c }) {
  const [open, setOpen] = React.useState(false);
  const ret = c.net_return_pct;
  const summary = c.entry_summary || {};
  const legacy = c.exit_reason === "LEGACY_UNRECORDED" || c.entry_context == null;
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
        <td className="py-2 pr-3 text-slate-400" title={c.exit_note || (legacy ? "closed before exit reasons were recorded" : "")}>
          {exitLabel(c.exit_reason)}{c.exit_note ? " ✎" : ""}
        </td>
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
              {c.exit_note && (
                <span className="text-xs text-slate-400">
                  Exit: <span className="font-semibold text-slate-200">{exitLabel(c.exit_reason)}</span> — {c.exit_note}
                </span>
              )}
              {!legacy ? (
                <span className="text-xs text-slate-400">
                  At entry: verdict <span className="font-semibold text-slate-200">{summary.verdict ?? "—"}</span>
                  {" · "}regime <span className="font-semibold text-slate-200">{summary.regime ?? "—"}</span>
                  {" · "}IV rank {summary.iv_rank != null ? `${fmt(summary.iv_rank, 0)}` : "—"}
                  {" · "}RS vs SPY {pct(summary.rs3m_vs_spy)} · RS vs Sec {pct(summary.rs3m_vs_sector)}
                </span>
              ) : (
                <span className="text-xs text-slate-500">
                  No entry snapshot — cycle closed before entry-context capture (LEGACY_UNRECORDED).
                </span>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

// Raw-data validation tables: the LIVE position legs (so a duplicate/mis-booked
// short is obvious) + the append-only execution log with every field that feeds
// the derived math. Read-only; nothing here mutates state.
const EXEC_COLS = [
  "id", "date", "action", "ticker", "strike", "contracts", "quantity_delta",
  "source", "transaction_id", "roll_group_id", "roll_leg", "mode",
  "premium_per_share", "close_price_per_share", "execution_price",
  "extrinsic_captured", "entry_extrinsic_per_share", "extrinsic_sold",
  "extrinsic_paid_back", "net_juice", "stock_price", "reversed_by", "reverses_action", "reason",
];

function cell(v) {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number") return Number.isInteger(v) ? v : v.toFixed(2);
  return String(v).length > 22 ? String(v).slice(0, 21) + "…" : String(v);
}

function RawData() {
  const { data, error, reload } = useApi(api.executionsRaw, [], null);
  const [rebuilding, setRebuilding] = React.useState(null);
  const [proposal, setProposal] = React.useState(null); // {ticker, legs}
  const [committing, setCommitting] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  if (error) return <Card title="Raw data (validation)"><p className="text-sm text-rose-400">{error}</p></Card>;
  const positions = data?.positions || [];
  const execs = data?.executions || [];

  // Step 1: fetch the proposed legs (broker truth + log-matched economics) to review.
  const propose = async (ticker) => {
    setRebuilding(ticker); setMsg(null); setProposal(null);
    try {
      const r = await api.rebuildPosition(ticker, { dry_run: true });
      setProposal({ ticker, legs: r.legs || [] });
    } catch (e) { setMsg(`${ticker}: ${String(e.message || e)}`); }
    finally { setRebuilding(null); }
  };
  const editLeg = (i, key, val) => setProposal((p) => ({
    ...p, legs: p.legs.map((l, j) => j === i ? { ...l, [key]: val } : l) }));
  // Step 2: commit the (possibly corrected) legs.
  const commit = async () => {
    setCommitting(true); setMsg(null);
    try {
      const legs = proposal.legs.map((l) => ({
        ...l,
        contracts: Number(l.contracts),
        ...(l.leg_type === "short"
          ? { premium_per_share: Number(l.premium_per_share), entry_extrinsic_per_share: Number(l.entry_extrinsic_per_share) }
          : { cost_per_contract: Number(l.cost_per_contract), extrinsic_per_contract: Number(l.extrinsic_per_contract) }),
      }));
      const r = await api.rebuildPosition(proposal.ticker, { legs });
      setMsg(`Rebuilt ${proposal.ticker}: ${r.short_calls.length} short + ${r.leap_legs.length} LEAP leg(s). Run "Reconcile now" to confirm CLEAN.`);
      setProposal(null);
      await reload();
    } catch (e) { setMsg(String(e.message || e)); }
    finally { setCommitting(false); }
  };

  return (
    <>
      <Card title="Live position legs — what state currently holds"
            right={<button onClick={reload}
              className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-800">Refresh</button>}>
        <p className="mb-2 text-xs text-slate-500">
          One row per open leg. A short with a null/0 <span className="font-mono">entry_extrinsic</span> is a mis-booked leg;
          two rows at the same strike <em>and</em> expiry means a duplicate (different expiries are separate weeklies).
        </p>
        <div className="mb-3 flex flex-wrap gap-2">
          {positions.filter((p) => p.status !== "closed").map((p) => (
            <button key={p.ticker} onClick={() => propose(p.ticker)} disabled={rebuilding === p.ticker}
                    title={`Propose ${p.ticker}'s legs from the broker's actual holdings; review/correct economics before committing`}
                    className="rounded-full border border-indigo-800 bg-indigo-950/40 px-2.5 py-1 text-xs font-semibold text-indigo-200 hover:bg-indigo-900/50 disabled:opacity-50">
              {rebuilding === p.ticker ? `Proposing ${p.ticker}…` : `Rebuild ${p.ticker} from broker`}
            </button>
          ))}
        </div>
        {proposal && (
          <div className="mb-3 rounded-md border border-indigo-800/60 bg-indigo-950/20 p-2">
            <p className="text-xs font-semibold text-indigo-200">
              Proposed {proposal.ticker} legs (broker truth) — review & correct any entry extrinsic the log got wrong, then Confirm:
            </p>
            <div className="mt-2 overflow-x-auto">
              <table className="w-full whitespace-nowrap text-xs">
                <thead><tr className="text-left uppercase tracking-wide text-slate-500">
                  {["leg", "strike", "contracts", "expiration", "premium/cost", "extrinsic", "from"].map((h) =>
                    <th key={h} className="py-1 pr-3">{h}</th>)}
                </tr></thead>
                <tbody className="font-mono">
                  {proposal.legs.map((l, i) => (
                    <tr key={i} className="border-t border-slate-800/50">
                      <td className={`py-1 pr-3 ${l.leg_type === "leap" ? "text-emerald-300" : "text-amber-300"}`}>{l.leg_type}</td>
                      <td className="py-1 pr-3">{l.strike}</td>
                      <td className="py-1 pr-3">
                        <input value={l.contracts} onChange={(e) => editLeg(i, "contracts", e.target.value)}
                               className="w-12 rounded border border-slate-700 bg-slate-900/60 px-1 text-slate-200" />
                      </td>
                      <td className="py-1 pr-3">{l.expiration || "—"}</td>
                      <td className="py-1 pr-3">
                        <input value={l.leg_type === "short" ? l.premium_per_share : l.cost_per_contract}
                               onChange={(e) => editLeg(i, l.leg_type === "short" ? "premium_per_share" : "cost_per_contract", e.target.value)}
                               className="w-20 rounded border border-slate-700 bg-slate-900/60 px-1 text-slate-200" />
                      </td>
                      <td className="py-1 pr-3">
                        <input value={l.leg_type === "short" ? l.entry_extrinsic_per_share : l.extrinsic_per_contract}
                               onChange={(e) => editLeg(i, l.leg_type === "short" ? "entry_extrinsic_per_share" : "extrinsic_per_contract", e.target.value)}
                               className="w-20 rounded border border-amber-700 bg-slate-900/60 px-1 text-amber-200" />
                      </td>
                      <td className="py-1 pr-3 text-slate-500">{l.econ_source || "—"}</td>
                    </tr>
                  ))}
                  {proposal.legs.length === 0 && (
                    <tr><td colSpan={7} className="py-3 text-center font-sans text-slate-500">Broker holds no option legs for {proposal.ticker}.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
            <div className="mt-2 flex gap-2">
              <button onClick={commit} disabled={committing || !proposal.legs.length}
                      className="rounded-full border border-emerald-800 bg-emerald-950/40 px-3 py-1 text-xs font-semibold text-emerald-200 hover:bg-emerald-900/50 disabled:opacity-50">
                {committing ? "Committing…" : "Confirm rebuild"}
              </button>
              <button onClick={() => setProposal(null)}
                      className="rounded-full border border-slate-700 bg-slate-800/60 px-3 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-800">
                Cancel
              </button>
            </div>
          </div>
        )}
        {msg && <p className="mb-2 text-xs text-slate-300">{msg}</p>}
        <div className="overflow-x-auto">
          <table className="w-full whitespace-nowrap text-xs">
            <thead>
              <tr className="text-left uppercase tracking-wide text-slate-500">
                {["ticker", "leg", "strike", "contracts", "expiration", "entry_extrinsic/sh",
                  "entry_premium_total", "cost_basis", "open/entry_date", "flags"].map((h) =>
                  <th key={h} className="py-1.5 pr-3">{h}</th>)}
              </tr>
            </thead>
            <tbody className="font-mono text-slate-300">
              {positions.flatMap((p) => [
                ...(p.short_calls || []).map((sc, i) => (
                  <tr key={`${p.ticker}-s${i}`} className="border-t border-slate-800/50">
                    <td className="py-1.5 pr-3 font-sans font-semibold text-slate-100">{p.ticker}</td>
                    <td className="py-1.5 pr-3 text-amber-300">SHORT</td>
                    <td className="py-1.5 pr-3">{cell(sc.strike)}</td>
                    <td className="py-1.5 pr-3">{cell(sc.contracts)}</td>
                    <td className="py-1.5 pr-3">{cell(sc.expiration)}</td>
                    <td className={`py-1.5 pr-3 ${!sc.entry_extrinsic_per_share ? "text-rose-400" : ""}`}>{cell(sc.entry_extrinsic_per_share)}</td>
                    <td className="py-1.5 pr-3">{cell(sc.entry_premium_total)}</td>
                    <td className="py-1.5 pr-3">—</td>
                    <td className="py-1.5 pr-3">{cell(sc.open_date)}</td>
                    <td className="py-1.5 pr-3 text-slate-500">{sc.restored ? "restored" : ""}</td>
                  </tr>
                )),
                ...(p.leap_legs || []).map((lg, i) => (
                  <tr key={`${p.ticker}-l${i}`} className="border-t border-slate-800/50">
                    <td className="py-1.5 pr-3 font-sans font-semibold text-slate-100">{p.ticker}</td>
                    <td className="py-1.5 pr-3 text-emerald-300">LEAP</td>
                    <td className="py-1.5 pr-3">{cell(lg.strike)}</td>
                    <td className="py-1.5 pr-3">{cell(lg.contracts)}</td>
                    <td className="py-1.5 pr-3">{cell(lg.expiration)}</td>
                    <td className={`py-1.5 pr-3 ${!lg.extrinsic_at_entry ? "text-rose-400" : ""}`}>{cell(lg.extrinsic_at_entry)}</td>
                    <td className="py-1.5 pr-3">—</td>
                    <td className="py-1.5 pr-3">{cell(lg.cost_basis)}</td>
                    <td className="py-1.5 pr-3">{cell(lg.entry_date)}</td>
                    <td className="py-1.5 pr-3 text-slate-500">{lg.restored ? "restored" : ""}</td>
                  </tr>
                )),
              ])}
              {positions.every((p) => !(p.short_calls || []).length && !(p.leap_legs || []).length) && (
                <tr><td colSpan={10} className="py-6 text-center font-sans text-slate-500">No open legs.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title={`Raw execution log — ${data?.execution_count ?? 0} records (newest first)`}>
        <div className="overflow-x-auto">
          <table className="w-full whitespace-nowrap text-xs">
            <thead>
              <tr className="text-left uppercase tracking-wide text-slate-500">
                {EXEC_COLS.map((h) => <th key={h} className="py-1.5 pr-3">{h}</th>)}
              </tr>
            </thead>
            <tbody className="font-mono text-slate-300">
              {execs.map((e, i) => (
                <tr key={e.id || i} className={`border-t border-slate-800/50 ${e.reversed_by ? "opacity-40" : ""} ${e.action === "adoption_reversal" ? "text-sky-300" : ""}`}>
                  {EXEC_COLS.map((c) => (
                    <td key={c} className={`py-1.5 pr-3 ${c === "source" && e[c] === "broker_manual" ? "text-amber-300" : ""}`}>{cell(e[c])}</td>
                  ))}
                </tr>
              ))}
              {execs.length === 0 && (
                <tr><td colSpan={EXEC_COLS.length} className="py-6 text-center font-sans text-slate-500">No executions.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
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

      <RawData />
    </div>
  );
}
