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

// ---- Bidirectional extrinsic ⇄ entry-price math ---------------------------
// The editors quote everything PER SHARE (LEAP cost is ÷100 on load, ×100 back
// on save), so the intrinsic split is the same for both leg types: extrinsic =
// premium − max(entry − strike, 0). Whichever field the operator edits, the
// other is derived here identically to what the backend will recompute from the
// entry price we send (executor._short_extrinsic / _leap_extrinsic_pc).
const _num = (v) => (v === "" || v == null || isNaN(Number(v)) ? null : Number(v));
const _round = (v, dp) => {
  const m = 10 ** dp;
  return Math.round(v * m) / m;
};

// extrinsic (per share) = premium per share − intrinsic per share.
function extrinsicFromEntry(price, strike, entry) {
  const p = _num(price), k = _num(strike), e = _num(entry);
  if (p == null || k == null || e == null) return null;
  return _round(Math.max(p - Math.max(e - k, 0), 0), 2);
}

// Inverse: the underlying entry price implied by an extrinsic value. At/out of
// the money the intrinsic is 0, so entry isn't uniquely determined — we pin it
// at the strike (the ITM boundary), which is what the extrinsic math assumes.
function entryFromExtrinsic(price, strike, extrinsic) {
  const p = _num(price), k = _num(strike), ext = _num(extrinsic);
  if (p == null || k == null || ext == null) return null;
  return _round(k + Math.max(p - ext, 0), 2);
}

// Given a row/leg with a `driver` ("entry" | "extrinsic"), recompute the OTHER
// (derived) field from the current per-share premium/strike. The driver field is
// left as the operator typed it; only the derived one changes.
function reconcileExtrinsic(row, price) {
  if (row.driver === "extrinsic") {
    const entry = entryFromExtrinsic(price, row.strike, row.extrinsic);
    return { ...row, entry_price: entry == null ? row.entry_price : entry };
  }
  const ext = extrinsicFromEntry(price, row.strike, row.entry_price);
  return { ...row, extrinsic: ext == null ? "" : ext };
}

// Which field should drive the derived one on load: prefer a captured entry
// stock price (the app-ordered / auto-populated case); fall back to a stored
// extrinsic when only that survived (a TOS-manual leg the operator set directly).
function seedDriver(entry, extrinsic) {
  if (entry !== "" && entry != null) return "entry";
  if (extrinsic !== "" && extrinsic != null && Number(extrinsic) > 0) return "extrinsic";
  return "entry";
}

// Convert a position's live legs into editable rows for the single-spot editor.
// `price` and `extrinsic` are both per share (LEAPs too, to match the shorts);
// whichever of entry/extrinsic is known becomes the driver.
function legsToRows(position) {
  const rows = [];
  (position.short_calls || []).forEach((sc) => {
    const entry_price = sc.entry_stock_price ?? "";
    const extrinsic = sc.entry_extrinsic_per_share ?? "";
    rows.push({
      leg_type: "short", strike: sc.strike ?? "", contracts: sc.contracts ?? 1,
      expiration: sc.expiration || "",
      price: sc.entry_premium_total && sc.contracts ? +(sc.entry_premium_total / (sc.contracts * 100)).toFixed(2) : (sc.current_bid ?? ""),
      entry_price, extrinsic, driver: seedDriver(entry_price, extrinsic),
    });
  });
  (position.leap_legs || []).forEach((lg) => {
    const entry_price = lg.entry_stock_price ?? "";
    // extrinsic_at_entry is per-contract-dollars × contracts; the editor quotes
    // per share, so ÷contracts (→ per contract) then ÷100 (→ per share).
    const extrinsic = lg.contracts ? +((lg.extrinsic_at_entry ?? 0) / lg.contracts / 100).toFixed(2) : "";
    rows.push({
      leg_type: "leap", strike: lg.strike ?? "", contracts: lg.contracts ?? 1,
      expiration: lg.expiration || "",
      // Editor edits LEAP cost per share (cost_basis is total; ÷contracts ÷100).
      price: lg.cost_basis && lg.contracts ? +(lg.cost_basis / lg.contracts / 100).toFixed(2) : (lg.cost_basis ?? ""),
      entry_price, extrinsic, driver: seedDriver(entry_price, extrinsic),
    });
  });
  return rows;
}

const BLANK_LEG = { leg_type: "short", strike: "", contracts: 1, expiration: "", price: "", entry_price: "", extrinsic: "", driver: "entry" };

// THE single-spot editor: edit a position's legs directly and Save. Entry price
// and extrinsic are two views of the same fact — edit EITHER and the other is
// computed live (premium − intrinsic ⇄ strike + intrinsic), so an app-ordered
// leg auto-populates both while a TOS-manual leg lets you set whichever you have.
function PositionLegsEditor({ ticker, initialRows, onSaved, onCancel }) {
  const [rows, setRows] = React.useState(initialRows.length ? initialRows : [{ ...BLANK_LEG }]);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  // Update a field, then re-derive the paired field. Editing entry/extrinsic
  // also makes that field the driver; editing price/strike/type re-derives from
  // whichever driver is already active.
  const set = (i, k, v) => setRows((rs) => rs.map((r, j) => {
    if (j !== i) return r;
    let nr = { ...r, [k]: v };
    if (k === "entry_price") nr.driver = "entry";
    else if (k === "extrinsic") nr.driver = "extrinsic";
    return reconcileExtrinsic(nr, nr.price);
  }));
  const add = () => setRows((rs) => [...rs, { ...BLANK_LEG }]);
  const del = (i) => setRows((rs) => rs.filter((_, j) => j !== i));

  const save = async () => {
    setBusy(true); setErr(null);
    try {
      const legs = rows.map((r) => {
        // Always send the canonical entry_price so the backend recomputes the
        // extrinsic identically (single source of truth). When the operator drove
        // by extrinsic, entry_price is the value we derived from it (pinned to the
        // strike at the at/OTM boundary, where the extrinsic still resolves right).
        const entry = reconcileExtrinsic(r, r.price).entry_price;
        return {
          leg_type: r.leg_type, strike: Number(r.strike), contracts: Number(r.contracts),
          expiration: r.expiration || null,
          entry_price: entry === "" || entry == null ? null : Number(entry),
          // Editor edits LEAP cost per share; backend wants per contract, so ×100.
          ...(r.leg_type === "short" ? { premium_per_share: Number(r.price) } : { cost_per_contract: Number(r.price) * 100 }),
        };
      });
      await api.setPositionLegs(ticker, legs, "manual leg edit");
      onSaved();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  };

  const inp = "rounded border border-slate-700 bg-slate-900/60 px-1 text-slate-200";
  // The driven field is highlighted (amber, what you typed); the derived field
  // is dimmed (what we computed) but still editable to flip which one leads.
  const driverCls = (r, field) => r.driver === field
    ? "border-amber-600 bg-amber-950/20 text-amber-200"
    : "border-slate-700 text-slate-400 italic";
  return (
    <div className="mb-3 rounded-md border border-emerald-800/60 bg-emerald-950/15 p-2">
      <p className="text-xs font-semibold text-emerald-200">Editing {ticker} legs — set them to match your broker, then Save.</p>
      <p className="mt-0.5 text-[11px] text-slate-500">
        Enter premium/cost per share (LEAPs too), then set
        <span className="text-amber-300"> either</span> the entry price
        <span className="text-amber-300"> or</span> the extrinsic — the other is computed, and the one you're driving is highlighted amber.
        Short premium e.g. 5.10; LEAP cost e.g. 53.05.
      </p>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full whitespace-nowrap text-xs">
          <thead><tr className="text-left uppercase tracking-wide text-slate-500">
            {["", "type", "strike", "qty", "expiration", "premium/cost", "entry price", "extrinsic", ""].map((h, i) =>
              <th key={i} className="py-1 pr-2">{h}</th>)}
          </tr></thead>
          <tbody className="font-mono">
            {rows.map((r, i) => {
              const overPremium = r.extrinsic !== "" && Number(r.extrinsic) > Number(r.price) + 1e-9;
              return (
                <tr key={i} className="border-t border-slate-800/50">
                  <td className="py-1 pr-2">{i + 1}</td>
                  <td className="py-1 pr-2">
                    <select value={r.leg_type} onChange={(ev) => set(i, "leg_type", ev.target.value)}
                            className={`${inp}`}>
                      <option value="short">short</option>
                      <option value="leap">leap</option>
                    </select>
                  </td>
                  <td className="py-1 pr-2"><input value={r.strike} onChange={(ev) => set(i, "strike", ev.target.value)} className={`${inp} w-16`} /></td>
                  <td className="py-1 pr-2"><input value={r.contracts} onChange={(ev) => set(i, "contracts", ev.target.value)} className={`${inp} w-10`} /></td>
                  <td className="py-1 pr-2"><input value={r.expiration} placeholder="YYYY-MM-DD" onChange={(ev) => set(i, "expiration", ev.target.value)} className={`${inp} w-28`} /></td>
                  <td className="py-1 pr-2"><input value={r.price} onChange={(ev) => set(i, "price", ev.target.value)} className={`${inp} w-20`} /></td>
                  <td className="py-1 pr-2"><input value={r.entry_price} placeholder="underlying" title={r.driver === "entry" ? "driving — extrinsic is computed from this" : "computed from extrinsic — type here to drive by entry price"}
                             onChange={(ev) => set(i, "entry_price", ev.target.value)} className={`rounded border bg-slate-900/60 px-1 w-24 ${driverCls(r, "entry")}`} /></td>
                  <td className="py-1 pr-2"><input value={r.extrinsic} placeholder="ext/sh" title={r.driver === "extrinsic" ? "driving — entry price is computed from this" : "computed from entry price — type here to drive by extrinsic"}
                             onChange={(ev) => set(i, "extrinsic", ev.target.value)}
                             className={`rounded border bg-slate-900/60 px-1 w-20 ${overPremium ? "border-rose-600 text-rose-300" : driverCls(r, "extrinsic")}`} /></td>
                  <td className="py-1 pr-2"><button onClick={() => del(i)} className="rounded border border-rose-800 bg-rose-950/40 px-1.5 text-[10px] text-rose-300">✕</button></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {rows.some((r) => r.extrinsic !== "" && Number(r.extrinsic) > Number(r.price) + 1e-9) && (
        <p className="mt-1 text-[11px] text-rose-400">Extrinsic can't exceed the premium/cost — it will be capped at the premium (entry pinned to the strike) on save.</p>
      )}
      <div className="mt-2 flex items-center gap-2">
        <button onClick={add} className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-800">+ Add leg</button>
        <button onClick={save} disabled={busy} className="rounded-full border border-emerald-800 bg-emerald-950/40 px-3 py-1 text-xs font-semibold text-emerald-200 hover:bg-emerald-900/50 disabled:opacity-50">{busy ? "Saving…" : "Save legs"}</button>
        <button onClick={onCancel} className="rounded-full border border-slate-700 bg-slate-800/60 px-3 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-800">Cancel</button>
      </div>
      {err && <p className="mt-1 text-xs text-rose-400">{err}</p>}
    </div>
  );
}

// The one editable transaction table (ToS-like). Each fulfilled execution is a
// row; edit strike/qty/expiry/price and the linked entry-stock-price <-> extrinsic
// pair (edit either, the other computes). Save applies the edits AND derives the
// open position from the transactions — the transactions are the source of truth.
const _FILL = new Set(["buy_leap", "sell_short", "close_short", "close_leap"]);
// LEAP prices are stored per-contract (execution_price / close_price); show them
// per-share (÷100) so the PRICE column reads the same units as the shorts.
function _price(e) {
  const ps = (v) => (v === null || v === undefined ? v : v / 100);
  return e.action === "buy_leap" ? ps(e.execution_price)
    : e.action === "sell_short" ? e.premium_per_share
    : e.action === "close_short" ? e.close_price_per_share : ps(e.close_price);
}
// LEAP extrinsic is stored per-contract total (extrinsic_captured); show it
// per-share (÷100÷contracts) so the column matches the shorts.
function _extr(e) {
  const c = e.contracts || 1;
  return e.action === "buy_leap"
    ? (e.extrinsic_captured === null || e.extrinsic_captured === undefined
        ? e.extrinsic_captured : +(e.extrinsic_captured / (100 * c)).toFixed(4))
    : e.action === "sell_short" ? e.entry_extrinsic_per_share : null;
}
function _toRow(e) {
  return {
    id: e.id, date: (e.date || "").slice(0, 10), action: e.action,
    isLeap: e.action === "buy_leap" || e.action === "close_leap",
    isOpen: e.action === "buy_leap" || e.action === "sell_short",
    source: e.source, roll: e.roll_group_id,
    strike: e.strike ?? "", contracts: e.contracts ?? 1, expiration: e.expiration || "",
    price: _price(e) ?? "", stock_price: e.stock_price ?? "", extrinsic: _extr(e) ?? "",
  };
}
function _calcExt(r, stock) {
  // price and extrinsic are both per-share here (LEAPs are displayed ÷100).
  const perShare = Number(r.price), strike = Number(r.strike);
  if (stock === "" || isNaN(Number(stock)) || isNaN(perShare) || isNaN(strike)) return "";
  const extPs = Math.max(perShare - Math.max(Number(stock) - strike, 0), 0);
  return +extPs.toFixed(4);
}
function _calcStock(r, ext) {
  const perShare = Number(r.price), strike = Number(r.strike);
  if (ext === "" || isNaN(Number(ext)) || isNaN(perShare) || isNaN(strike)) return "";
  return +(strike + Math.max(perShare - Number(ext), 0)).toFixed(2);
}

function TransactionEditor() {
  const { data, reload } = useApi(api.executionsRaw, [], null);
  const [rows, setRows] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  const loadedRef = React.useRef(null);

  React.useEffect(() => {
    if (data && loadedRef.current !== data) {
      loadedRef.current = data;
      const fills = (data.executions || []).filter((e) => _FILL.has(e.action) && !e.reversed_by && !e.excluded);
      setRows(fills.slice().reverse().map(_toRow));  // oldest first, like a trade log
    }
  }, [data]);

  if (rows === null) return <Card title="Transactions (editable)"><Loading /></Card>;

  const set = (i, k, v) => setRows((rs) => rs.map((r, j) => j === i ? { ...r, [k]: v } : r));
  const onStock = (i, v) => setRows((rs) => rs.map((r, j) => j === i ? { ...r, stock_price: v, extrinsic: _calcExt(r, v) } : r));
  const onExt = (i, v) => setRows((rs) => rs.map((r, j) => j === i ? { ...r, extrinsic: v, stock_price: _calcStock(r, v) } : r));

  const save = async () => {
    setBusy(true); setMsg(null);
    try {
      const edits = rows.map((r) => ({
        id: r.id, strike: Number(r.strike), contracts: Number(r.contracts),
        expiration: r.expiration || null,
        // The backend stores LEAP price per-contract and extrinsic per-contract
        // total; the table edits both per-share, so scale LEAPs back up on the
        // way out (price ×100, extrinsic ×100×contracts).
        price: r.price === "" ? null : (r.isLeap ? Number(r.price) * 100 : Number(r.price)),
        stock_price: r.stock_price === "" ? null : Number(r.stock_price),
        extrinsic: r.extrinsic === "" ? null
          : (r.isLeap ? Number(r.extrinsic) * 100 * (Number(r.contracts) || 1) : Number(r.extrinsic)),
      }));
      const res = await api.saveTransactions(edits);
      setMsg(`Saved ${res.edited} transaction(s); position derived for ${(res.tickers || []).join(", ") || "—"}.`);
      loadedRef.current = null;   // allow reseed from fresh data
      await reload();
    } catch (e) { setMsg(String(e.message || e)); }
    finally { setBusy(false); }
  };

  const inp = "rounded border border-slate-700 bg-slate-900/60 px-1 text-slate-200";
  return (
    <Card title="Transactions (editable) — the source of truth"
          right={<button onClick={save} disabled={busy}
            className="rounded-full border border-emerald-800 bg-emerald-950/40 px-3 py-1 text-xs font-semibold text-emerald-200 hover:bg-emerald-900/50 disabled:opacity-50">
            {busy ? "Saving…" : "Save transactions"}</button>}>
      <p className="mb-2 text-xs text-slate-500">
        One row per fill. App-ordered fills come pre-filled; for a trade done in ToS you usually only
        need the <span className="text-amber-300">entry stock price</span> or <span className="text-amber-300">extrinsic</span> —
        edit either and the other is computed. Set the <span className="font-mono">expiration</span> so same-strike weeklies stay separate.
        Prices and extrinsic are <span className="text-slate-400">per share</span> (LEAPs too — a $53.05 LEAP shows as 53.05, extrinsic 6.49).
        Save derives your open position from these transactions.
      </p>
      <div className="overflow-x-auto">
        <table className="w-full whitespace-nowrap text-xs">
          <thead><tr className="text-left uppercase tracking-wide text-slate-500">
            {["date", "action", "strike", "qty", "expiration", "price", "entry stock", "extrinsic", "roll"].map((h) =>
              <th key={h} className="py-1.5 pr-2">{h}</th>)}
          </tr></thead>
          <tbody className="font-mono text-slate-300">
            {rows.map((r, i) => (
              <tr key={r.id} className="border-t border-slate-800/50">
                <td className="py-1 pr-2 font-sans text-slate-500">{r.date}</td>
                <td className={`py-1 pr-2 ${r.isLeap ? "text-emerald-300" : "text-amber-300"}`}>{r.action}</td>
                <td className="py-1 pr-2"><input value={r.strike} onChange={(e) => set(i, "strike", e.target.value)} className={`${inp} w-16`} /></td>
                <td className="py-1 pr-2"><input value={r.contracts} onChange={(e) => set(i, "contracts", e.target.value)} className={`${inp} w-10`} /></td>
                <td className="py-1 pr-2"><input value={r.expiration} placeholder="YYYY-MM-DD" onChange={(e) => set(i, "expiration", e.target.value)} className={`${inp} w-28`} /></td>
                <td className="py-1 pr-2"><input value={r.price} onChange={(e) => set(i, "price", e.target.value)} className={`${inp} w-20`} /></td>
                <td className="py-1 pr-2">
                  <input value={r.stock_price} placeholder={r.isOpen ? "underlying" : "—"} disabled={!r.isOpen}
                         onChange={(e) => onStock(i, e.target.value)} className={`${inp} w-24 ${r.isOpen ? "border-amber-700 text-amber-200" : "opacity-40"}`} />
                </td>
                <td className="py-1 pr-2">
                  <input value={r.extrinsic} placeholder={r.isOpen ? "extrinsic" : "—"} disabled={!r.isOpen}
                         onChange={(e) => onExt(i, e.target.value)} className={`${inp} w-24 ${r.isOpen ? "border-amber-700 text-amber-200" : "opacity-40"}`} />
                </td>
                <td className="py-1 pr-2 font-sans text-slate-600">{r.roll || ""}</td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={9} className="py-6 text-center font-sans text-slate-500">No transactions.</td></tr>}
          </tbody>
        </table>
      </div>
      {msg && <p className="mt-2 text-xs text-slate-300">{msg}</p>}
    </Card>
  );
}

function RawData() {
  const { data, error, reload } = useApi(api.executionsRaw, [], null);
  const [rebuilding, setRebuilding] = React.useState(null);
  const [proposal, setProposal] = React.useState(null); // {ticker, legs}
  const [committing, setCommitting] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  const [fulfilledOnly, setFulfilledOnly] = React.useState(true);
  const [editing, setEditing] = React.useState(null); // ticker being directly edited
  if (error) return <Card title="Raw data (validation)"><p className="text-sm text-rose-400">{error}</p></Card>;
  const positions = data?.positions || [];
  const allExecs = data?.executions || [];
  // "Fulfilled orders only" = the actual broker fills, hiding bookkeeping noise:
  // reversal / rebuild / adjustment markers and any execution that was undone
  // (reversed_by) — i.e. legs that didn't actually end up on the books.
  const FILL_ACTIONS = new Set(["buy_leap", "sell_short", "close_short", "close_leap", "resolve_expiry"]);
  const execs = fulfilledOnly
    ? allExecs.filter((e) => FILL_ACTIONS.has(e.action) && !e.reversed_by && !e.excluded)
    : allExecs;

  const voidExec = async (e) => {
    const isVoided = !!e.excluded;
    if (!window.confirm(isVoided
      ? `Restore ${e.id} back into the history and ledgers?`
      : `Void ${e.id} (${e.action} ${e.strike ?? ""})? It drops out of history + derived ledgers `
        + `but stays on the immutable log. Use for pre-trading/test entries.`)) return;
    setMsg(null);
    try {
      if (isVoided) await api.restoreExecutions([e.id]);
      else await api.voidExecutions([e.id], "pruned pre-trading/test entry");
      await reload();
    } catch (err) { setMsg(String(err.message || err)); }
  };

  // Per-leg premium/cost accessor — both per share here (LEAP cost was ÷100 on
  // load), so the same intrinsic split applies to shorts and LEAPs alike.
  const legPrice = (l) => (l.leg_type === "short" ? l.premium_per_share : l.cost_per_contract);
  // Step 1: fetch the proposed legs (broker truth + log-matched economics) to
  // review. Seed each leg's editable extrinsic + which field drives it (a leg
  // that matched an entry stock price drives by entry; one that didn't lets the
  // operator set the extrinsic directly).
  const propose = async (ticker) => {
    setRebuilding(ticker); setMsg(null); setProposal(null);
    try {
      const r = await api.rebuildPosition(ticker, { dry_run: true });
      const legs = (r.legs || []).map((l) => {
        // Edit LEAP cost per share (÷100) to match the shorts; convert back on commit.
        const ps = l.leg_type === "short" || l.cost_per_contract == null
          ? l : { ...l, cost_per_contract: +(Number(l.cost_per_contract) / 100).toFixed(2) };
        const hasEntry = ps.entry_price !== "" && ps.entry_price != null;
        const extrinsic = hasEntry ? extrinsicFromEntry(legPrice(ps), ps.strike, ps.entry_price) : "";
        return { ...ps, extrinsic: extrinsic == null ? "" : extrinsic, driver: hasEntry ? "entry" : "extrinsic" };
      });
      setProposal({ ticker, legs });
    } catch (e) { setMsg(`${ticker}: ${String(e.message || e)}`); }
    finally { setRebuilding(null); }
  };
  const editLeg = (i, key, val) => setProposal((p) => ({
    ...p, legs: p.legs.map((l, j) => {
      if (j !== i) return l;
      let nl = { ...l, [key]: val };
      if (key === "entry_price") nl.driver = "entry";
      else if (key === "extrinsic") nl.driver = "extrinsic";
      return reconcileExtrinsic(nl, legPrice(nl));
    }) }));
  // Step 2: commit the (possibly corrected) legs. Extrinsic is computed server-
  // side from the total premium and the entry price, so we send the canonical
  // entry price — derived from the extrinsic when the operator drove by that.
  const commit = async () => {
    setCommitting(true); setMsg(null);
    try {
      const legs = proposal.legs.map((l) => {
        // Send the canonical entry price (derived from the extrinsic when that's
        // the driver); the backend recomputes the stored extrinsic from it.
        const entry = reconcileExtrinsic(l, legPrice(l)).entry_price;
        return {
          ...l,
          contracts: Number(l.contracts),
          entry_price: entry === "" || entry == null ? null : Number(entry),
          ...(l.leg_type === "short"
            ? { premium_per_share: Number(l.premium_per_share) }
            : { cost_per_contract: Number(l.cost_per_contract) * 100 }),   // per-share -> per-contract
        };
      });
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
            <React.Fragment key={p.ticker}>
              <button onClick={() => { setEditing(editing === p.ticker ? null : p.ticker); setProposal(null); }}
                      className="rounded-full border border-emerald-800 bg-emerald-950/40 px-2.5 py-1 text-xs font-semibold text-emerald-200 hover:bg-emerald-900/50">
                {editing === p.ticker ? `Close ${p.ticker} editor` : `Edit ${p.ticker} legs`}
              </button>
              <button onClick={() => propose(p.ticker)} disabled={rebuilding === p.ticker}
                      title={`Propose ${p.ticker}'s legs from the broker's actual holdings`}
                      className="rounded-full border border-indigo-800 bg-indigo-950/40 px-2.5 py-1 text-xs font-semibold text-indigo-200 hover:bg-indigo-900/50 disabled:opacity-50">
                {rebuilding === p.ticker ? `Proposing ${p.ticker}…` : `Rebuild ${p.ticker} from broker`}
              </button>
            </React.Fragment>
          ))}
        </div>
        {editing && (
          <PositionLegsEditor
            ticker={editing}
            initialRows={legsToRows(positions.find((p) => p.ticker === editing) || {})}
            onSaved={() => { setEditing(null); setMsg(`Saved ${editing} legs.`); reload(); }}
            onCancel={() => setEditing(null)}
          />
        )}
        {proposal && (
          <div className="mb-3 rounded-md border border-indigo-800/60 bg-indigo-950/20 p-2">
            <p className="text-xs font-semibold text-indigo-200">
              Proposed {proposal.ticker} legs (broker truth) — set the entry price <span className="text-amber-300">or</span> the extrinsic on any leg, then Confirm:
            </p>
            <p className="mt-1 text-[11px] text-slate-500">
              Entry price and extrinsic are two views of one fact (premium − max(entry − strike, 0)):
              edit <span className="text-amber-300">either</span> and the other is computed. The field you're driving is highlighted amber.
            </p>
            <div className="mt-2 overflow-x-auto">
              <table className="w-full whitespace-nowrap text-xs">
                <thead><tr className="text-left uppercase tracking-wide text-slate-500">
                  {["leg", "strike", "contracts", "expiration", "premium/cost", "entry price", "extrinsic", "from"].map((h) =>
                    <th key={h} className="py-1 pr-3">{h}</th>)}
                </tr></thead>
                <tbody className="font-mono">
                  {proposal.legs.map((l, i) => {
                    const isShort = l.leg_type === "short";
                    // price is per-share for both leg types (LEAP cost was ÷100 on load).
                    const price = Number(isShort ? l.premium_per_share : l.cost_per_contract);
                    const overPremium = l.extrinsic !== "" && l.extrinsic != null && Number(l.extrinsic) > price + 1e-9;
                    const driverCls = (field) => l.driver === field
                      ? "border-amber-600 bg-amber-950/20 text-amber-200"
                      : "border-slate-700 text-slate-400 italic";
                    return (
                      <tr key={i} className="border-t border-slate-800/50">
                        <td className={`py-1 pr-3 ${isShort ? "text-amber-300" : "text-emerald-300"}`}>{l.leg_type}</td>
                        <td className="py-1 pr-3">{l.strike}</td>
                        <td className="py-1 pr-3">
                          <input value={l.contracts} onChange={(e) => editLeg(i, "contracts", e.target.value)}
                                 className="w-12 rounded border border-slate-700 bg-slate-900/60 px-1 text-slate-200" />
                        </td>
                        <td className="py-1 pr-3">{l.expiration || "—"}</td>
                        <td className="py-1 pr-3">
                          <input value={isShort ? l.premium_per_share : l.cost_per_contract}
                                 onChange={(e) => editLeg(i, isShort ? "premium_per_share" : "cost_per_contract", e.target.value)}
                                 className="w-20 rounded border border-slate-700 bg-slate-900/60 px-1 text-slate-200" />
                        </td>
                        <td className="py-1 pr-3">
                          <input value={l.entry_price ?? ""} placeholder="underlying"
                                 title={l.driver === "entry" ? "driving — extrinsic computed from this" : "computed from extrinsic — type to drive by entry price"}
                                 onChange={(e) => editLeg(i, "entry_price", e.target.value)}
                                 className={`w-24 rounded border bg-slate-900/60 px-1 ${driverCls("entry")}`} />
                        </td>
                        <td className="py-1 pr-3">
                          <input value={l.extrinsic ?? ""} placeholder="per share"
                                 title={l.driver === "extrinsic" ? "driving — entry price computed from this" : "computed from entry price — type to drive by extrinsic"}
                                 onChange={(e) => editLeg(i, "extrinsic", e.target.value)}
                                 className={`w-24 rounded border bg-slate-900/60 px-1 ${overPremium ? "border-rose-600 text-rose-300" : driverCls("extrinsic")}`} />
                        </td>
                        <td className="py-1 pr-3 text-slate-500">{l.econ_source || "—"}</td>
                      </tr>
                    );
                  })}
                  {proposal.legs.length === 0 && (
                    <tr><td colSpan={8} className="py-3 text-center font-sans text-slate-500">Broker holds no option legs for {proposal.ticker}.</td></tr>
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
                    <td className={`py-1.5 pr-3 ${!lg.extrinsic_at_entry ? "text-rose-400" : ""}`}>{cell(lg.extrinsic_at_entry != null && lg.contracts ? lg.extrinsic_at_entry / (100 * lg.contracts) : lg.extrinsic_at_entry)}</td>
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

      <Card title={`Raw execution log — ${execs.length}${fulfilledOnly ? " fulfilled" : ""} of ${data?.execution_count ?? 0}`}
            right={
              <label className="flex items-center gap-1.5 text-xs text-slate-400">
                <input type="checkbox" checked={fulfilledOnly} onChange={(e) => setFulfilledOnly(e.target.checked)} />
                Fulfilled orders only
              </label>
            }>
        {fulfilledOnly && (
          <p className="mb-2 text-[11px] text-slate-500">
            Showing actual broker fills (buy/sell/close). Reversed adopt legs, undo/rebuild markers, and
            adjustments are hidden — untick to see the full audit log.
          </p>
        )}
        <div className="overflow-x-auto">
          <table className="w-full whitespace-nowrap text-xs">
            <thead>
              <tr className="text-left uppercase tracking-wide text-slate-500">
                {!fulfilledOnly && <th className="py-1.5 pr-3"></th>}
                {EXEC_COLS.map((h) => <th key={h} className="py-1.5 pr-3">{h}</th>)}
              </tr>
            </thead>
            <tbody className="font-mono text-slate-300">
              {execs.map((e, i) => (
                <tr key={e.id || i} className={`border-t border-slate-800/50 ${e.reversed_by || e.excluded ? "opacity-40" : ""} ${e.excluded ? "line-through" : ""} ${e.action === "adoption_reversal" ? "text-sky-300" : ""}`}>
                  {!fulfilledOnly && (
                    <td className="py-1.5 pr-3 no-underline">
                      <button onClick={() => voidExec(e)}
                              className={`rounded border px-1.5 text-[10px] font-semibold ${e.excluded
                                ? "border-emerald-800 bg-emerald-950/40 text-emerald-300"
                                : "border-rose-800 bg-rose-950/40 text-rose-300"} hover:opacity-80`}>
                        {e.excluded ? "restore" : "void"}
                      </button>
                    </td>
                  )}
                  {EXEC_COLS.map((c) => (
                    <td key={c} className={`py-1.5 pr-3 ${c === "source" && e[c] === "broker_manual" ? "text-amber-300" : ""}`}>{cell(e[c])}</td>
                  ))}
                </tr>
              ))}
              {execs.length === 0 && (
                <tr><td colSpan={EXEC_COLS.length + 1} className="py-6 text-center font-sans text-slate-500">No executions.</td></tr>
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

      <TransactionEditor />
      <RawData />
    </div>
  );
}
