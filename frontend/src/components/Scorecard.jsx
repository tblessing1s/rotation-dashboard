import React from "react";
import { api } from "../api.js";
import { Card, Pill, Spinner, fmt, pct, useApi } from "./ui.jsx";

// The numeric CFM scorecard: one row per holding, a single composite verdict,
// no chart-reading required. Grouped by sector, filterable by verdict, sortable
// by any numeric column; click a row to see the reasons behind its verdict.

const VERDICT_STATUS = { GO: "go", CAUTION: "caution", AVOID: "avoid" };

// Columns: key -> how to render. `num` columns are sortable numerically.
const COLUMNS = [
  { key: "ticker", label: "Ticker", num: false },
  { key: "price", label: "Price", num: true, render: (r) => fmt(r.price, 2) },
  { key: "rs3m_vs_spy", label: "RS vs SPY", num: true, render: (r) => pct(r.rs3m_vs_spy) },
  { key: "rs3m_vs_sector", label: "RS vs Sec", num: true, render: (r) => pct(r.rs3m_vs_sector) },
  { key: "pct_above_ma21", label: "%>MA21", num: true, render: (r) => pct(r.pct_above_ma21) },
  { key: "atr_extension", label: "ATR ext", num: true, render: (r) => fmt(r.atr_extension, 2) },
  { key: "mfi", label: "MFI", num: true, render: (r) => fmt(r.mfi, 0) },
  { key: "volume_ratio", label: "Vol×", num: true, render: (r) => fmt(r.volume_ratio, 2) },
  { key: "atr_momentum", label: "ATR mom", num: true, render: (r) => fmt(r.atr_momentum, 2) },
  { key: "obv_above_ema", label: "OBV", num: false, render: (r) => (r.obv_above_ema == null ? "—" : r.obv_above_ema ? "↑" : "↓") },
  {
    key: "juice_weekly_pct", label: "Juice/wk", num: true,
    render: (r) =>
      r.juice_weekly_pct == null ? "—" : (
        <span
          title={`History-implied weekly extrinsic ÷ LEAP cost (target ≥ ${fmt(r.juice_target_pct, 2)}%/wk)`}
          className={r.juice_ok === false ? "text-rose-300" : "text-emerald-300"}
        >
          {fmt(r.juice_weekly_pct, 2)}%
        </span>
      ),
  },
  {
    key: "earnings_days", label: "Earnings", num: true,
    render: (r) =>
      r.earnings_date == null ? "—" : (
        <span
          title={`Next earnings ${r.earnings_date}`}
          className={r.earnings_days != null && r.earnings_days <= 7 ? "text-amber-300" : "text-slate-400"}
        >
          {r.earnings_days != null ? `${r.earnings_days}d` : r.earnings_date}
        </span>
      ),
  },
  { key: "verdict", label: "Verdict", num: false, render: (r) => <Pill status={VERDICT_STATUS[r.verdict] || "unknown"}>{r.verdict}</Pill> },
];

const VERDICT_ORDER = { AVOID: 0, CAUTION: 1, GO: 2 };

// When the market regime isn't green, the gate's Level 1 is the headline risk —
// a GO here means "best CFM setup once the tape clears," not "enter now". Surface
// that right on the table so a green verdict is never mistaken for a fresh-risk
// signal. Keyed to the same traffic-light tones as the regime card.
const REGIME_BANNER = {
  yellow: {
    cls: "border-amber-500/40 bg-amber-500/10 text-amber-200",
    text: "Market regime YELLOW — tighten criteria, no fresh risk. Verdicts below are a relative ranking, not entry signals.",
  },
  red: {
    cls: "border-rose-500/40 bg-rose-500/10 text-rose-200",
    text: "Market regime RED — risk-off, stand down. Verdicts below are a relative ranking, not entry signals.",
  },
};

function sortRows(rows, sort) {
  const { key, dir } = sort;
  const col = COLUMNS.find((c) => c.key === key);
  const mul = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    let av = a[key];
    let bv = b[key];
    if (key === "verdict") {
      av = VERDICT_ORDER[av] ?? 99;
      bv = VERDICT_ORDER[bv] ?? 99;
    }
    // Nulls always sort last regardless of direction.
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (col?.num || key === "verdict") return (av - bv) * mul;
    return String(av).localeCompare(String(bv)) * mul;
  });
}

function ScoreRow({ row, expanded, onToggle }) {
  const weak = row.verdict === "AVOID";
  const caution = row.verdict === "CAUTION";
  return (
    <>
      <tr
        onClick={onToggle}
        className={`cursor-pointer border-t border-slate-800 hover:bg-slate-800/40 ${
          weak ? "bg-rose-500/5" : caution ? "bg-amber-500/5" : ""
        }`}
      >
        {COLUMNS.map((c) => (
          <td key={c.key} className={`py-2 pr-3 ${c.key === "ticker" ? "font-semibold text-slate-100" : "text-slate-300"}`}>
            {c.key === "ticker" ? (
              <span className="flex items-center gap-1.5">
                <span className="text-slate-500">{expanded ? "▾" : "▸"}</span>
                {row.ticker}
                {row.is_sector_etf && (
                  <span
                    title="Sector ETF — a valid CFM candidate in its own right. RS vs Sector is N/A (it IS the sector)."
                    className="rounded border border-sky-600/50 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-sky-400"
                  >
                    ETF
                  </span>
                )}
                {row.has_weeklies === false && (
                  <span
                    title="No weekly options — can't run CFM (weekly short) on this name"
                    className="rounded border border-slate-600/60 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-400"
                  >
                    no weeklies
                  </span>
                )}
              </span>
            ) : c.render ? (
              c.render(row)
            ) : (
              row[c.key]
            )}
          </td>
        ))}
      </tr>
      {expanded && (
        <tr className="border-t border-slate-800/50 bg-slate-900/40">
          <td colSpan={COLUMNS.length} className="px-4 py-3">
            {row.reasons?.length ? (
              <ul className="list-disc space-y-1 pl-5 text-sm text-slate-300">
                {row.reasons.map((reason, i) => (
                  <li key={i}>{reason}</li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-emerald-300">Clean — passes the entry gate and every scorecard rule.</p>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

export default function Scorecard({ regimeStatus }) {
  const { data, error, loading } = useApi(api.scorecard, []);
  const banner = REGIME_BANNER[regimeStatus];
  const [verdictFilter, setVerdictFilter] = React.useState("ALL");
  const [weekliesOnly, setWeekliesOnly] = React.useState(true);
  const [sort, setSort] = React.useState({ key: "verdict", dir: "asc" });
  const [open, setOpen] = React.useState({});

  const results = data?.results || [];
  // Monthly-only names can't run CFM (no weekly short); count them so the toggle
  // can show how many are being hidden. `null` = undetermined, treated as tradeable.
  const noWeeklies = React.useMemo(
    () => results.filter((r) => r.has_weeklies === false).length,
    [results],
  );
  const counts = React.useMemo(() => {
    const c = { GO: 0, CAUTION: 0, AVOID: 0 };
    results.forEach((r) => { if (c[r.verdict] != null) c[r.verdict] += 1; });
    return c;
  }, [results]);

  const filtered = results.filter(
    (r) =>
      (verdictFilter === "ALL" || r.verdict === verdictFilter) &&
      (!weekliesOnly || r.has_weeklies !== false),
  );

  // Group by sector, then sort within each group by the active column.
  const groups = React.useMemo(() => {
    const by = {};
    filtered.forEach((r) => { (by[r.sector] || (by[r.sector] = [])).push(r); });
    return Object.entries(by)
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([sector, rows]) => [sector, sortRows(rows, sort)]);
  }, [filtered, sort]);

  function toggleSort(key) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));
  }

  const filterBtn = (val, label) => (
    <button
      onClick={() => setVerdictFilter(val)}
      className={`rounded-lg border px-3 py-1.5 text-sm ${
        verdictFilter === val
          ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-300"
          : "border-slate-700 text-slate-400 hover:text-slate-200"
      }`}
    >
      {label}
    </button>
  );

  return (
    <Card
      title="Scorecard (numeric CFM lens)"
      right={loading ? <span className="flex items-center gap-1.5 text-xs text-slate-500"><Spinner size="h-3 w-3" />scoring…</span> : data?.as_of ? <span className="text-xs text-slate-500">as of {data.as_of}</span> : null}
    >
      {banner && (
        <div className={`mb-4 rounded-lg border px-3 py-2 text-sm ${banner.cls}`}>
          {banner.text}
        </div>
      )}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {filterBtn("ALL", `All (${results.length})`)}
        {filterBtn("GO", `GO ${counts.GO}`)}
        {filterBtn("CAUTION", `CAUTION ${counts.CAUTION}`)}
        {filterBtn("AVOID", `AVOID ${counts.AVOID}`)}
        <label
          className="ml-auto flex cursor-pointer items-center gap-2 text-sm text-slate-400"
          title="CFM sells a weekly short — hide names whose option chain has no weeklies"
        >
          <input
            type="checkbox"
            checked={weekliesOnly}
            onChange={(e) => setWeekliesOnly(e.target.checked)}
            className="h-4 w-4 accent-emerald-500"
          />
          Weeklies only{noWeeklies > 0 ? ` (${noWeeklies} hidden)` : ""}
        </label>
      </div>
      {error && <p className="text-sm text-rose-400">{error}</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase tracking-wide text-slate-500">
              {COLUMNS.map((c) => (
                <th
                  key={c.key}
                  onClick={() => toggleSort(c.key)}
                  className="cursor-pointer select-none py-2 pr-3 hover:text-slate-300"
                  title="Sort"
                >
                  {c.label}
                  {sort.key === c.key ? (sort.dir === "asc" ? " ▲" : " ▼") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {groups.map(([sector, rows]) => (
              <React.Fragment key={sector}>
                <tr className="bg-slate-800/30">
                  <td colSpan={COLUMNS.length} className="px-2 py-1.5 text-xs font-semibold uppercase tracking-wide text-slate-400">
                    {sector || "—"}
                  </td>
                </tr>
                {rows.map((row) => (
                  <ScoreRow
                    key={row.ticker}
                    row={row}
                    expanded={!!open[row.ticker]}
                    onToggle={() => setOpen((o) => ({ ...o, [row.ticker]: !o[row.ticker] }))}
                  />
                ))}
              </React.Fragment>
            ))}
            {!loading && filtered.length === 0 && (
              <tr><td colSpan={COLUMNS.length} className="py-6 text-center text-slate-500">No tickers.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
