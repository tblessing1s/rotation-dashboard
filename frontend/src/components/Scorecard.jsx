import React from "react";
import { api } from "../api.js";
import { Card, Pill, Spinner, ErrorState, StockLights, fmt, pct, useApi } from "./ui.jsx";

// The numeric CFM scorecard: one row per holding, a single composite verdict,
// no chart-reading required. Grouped by sector, filterable by verdict, sortable
// by any numeric column; click a row to see the reasons behind its verdict.

const VERDICT_STATUS = { GO: "go", CAUTION: "caution", AVOID: "avoid" };

// Columns: key -> how to render. `num` columns are sortable numerically.
const COLUMNS = [
  { key: "ticker", label: "Ticker", num: false },
  {
    // The per-name Genius four lights + the right-spot gate — the SAME indicator
    // system as the market regime, at a glance. The verdict (green/yellow/red) is
    // the four lights + vetoes; the right-spot check is separate (a dot).
    key: "lights", label: "Lights", num: false,
    render: (r) => (
      <span className="inline-flex items-center gap-2">
        <StockLights lights={r.lights} />
        {r.right_spot ? (
          <span
            title={`Right spot: ${r.right_spot.pass ? "in spot" : (r.right_spot.blocked_by || []).join(", ") || "blocked"}`}
            className={`text-[10px] uppercase ${r.right_spot.pass ? "text-emerald-400" : "text-rose-400"}`}
          >
            {r.right_spot.pass ? "spot✓" : "spot✗"}
          </span>
        ) : null}
      </span>
    ),
  },
  { key: "price", label: "Price", num: true, render: (r) => fmt(r.price, 2) },
  { key: "rs3m_vs_spy", label: "RS3M SPY", num: true, render: (r) => pct(r.rs3m_vs_spy) },
  { key: "rs3m_vs_sector", label: "RS3M Sec", num: true, render: (r) => pct(r.rs3m_vs_sector) },
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

// A compact ↻ that force-pulls a live quote (one ticker, or a whole sector),
// bypassing the daily cache. Spins while in flight; turns emerald once a name
// has been refreshed this session, and red with a tooltip if the pull failed.
function RefreshButton({ onClick, busy, error, title, refreshedAt }) {
  // refreshedAt is { at, source } once a name has been pulled. A "cache" source
  // means the live providers didn't answer — flag it amber, not emerald, so a
  // stale price never masquerades as a fresh live quote.
  const source = refreshedAt?.source;
  const stale = source === "cache";
  const tip = error
    ? `Refresh failed: ${error}`
    : refreshedAt
      ? `${stale ? "No live quote available — showing cached close" : `Live quote (${source})`} · as of ${refreshedAt.at}`
      : title;
  const tone = error
    ? "text-rose-400"
    : stale
      ? "text-amber-400"
      : refreshedAt
        ? "text-emerald-400"
        : "text-slate-500 hover:text-slate-200";
  return (
    <button
      type="button"
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      disabled={busy}
      title={tip}
      aria-label={title}
      className={`inline-flex h-5 w-5 items-center justify-center rounded text-xs hover:bg-slate-700/60 disabled:opacity-60 ${tone}`}
    >
      {busy ? <Spinner size="h-3 w-3" /> : error ? "!" : "↻"}
    </button>
  );
}

function ScoreRow({ row, expanded, onToggle, onRefresh, refreshing, refreshedAt, refreshError }) {
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
                {(row.is_etf || row.is_sector_etf) && (
                  <span
                    title={row.is_sector_etf
                      ? "Sector ETF — a valid CFM candidate in its own right. RS vs Sector is N/A (it IS the sector). Runs on the lower ETF juice bar."
                      : "ETF — steadier, lower-IV income sleeve. Clears a lower weekly-juice bar than growth stocks."}
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
                <RefreshButton
                  onClick={onRefresh}
                  busy={refreshing}
                  error={refreshError}
                  refreshedAt={refreshedAt}
                  title={`Refresh ${row.ticker} — pull a live quote now`}
                />
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

export default function Scorecard({ regimeStatus, refreshKey }) {
  const { data, error, loading, reload } = useApi(api.scorecard, [refreshKey]);
  const banner = REGIME_BANNER[regimeStatus];
  const [verdictFilter, setVerdictFilter] = React.useState("ALL");
  const [weekliesOnly, setWeekliesOnly] = React.useState(true);
  const [sort, setSort] = React.useState({ key: "verdict", dir: "asc" });
  const [open, setOpen] = React.useState({});
  // On-demand live-quote refresh: rows we've force-refreshed since the last full
  // sweep (fresher than the memoized scorecard), plus per-key in-flight/when/error.
  const [overrides, setOverrides] = React.useState({});
  const [busy, setBusy] = React.useState({});
  const [refreshedAt, setRefreshedAt] = React.useState({});
  const [refreshErr, setRefreshErr] = React.useState({});

  // A new full sweep supersedes every manual override — drop them so the newest
  // scorecard wins (keyed on as_of, which only changes on a real reload).
  React.useEffect(() => {
    setOverrides({});
    setRefreshedAt({});
    setRefreshErr({});
  }, [data?.as_of]);

  async function refreshTicker(ticker) {
    setBusy((b) => ({ ...b, [ticker]: true }));
    setRefreshErr((e) => ({ ...e, [ticker]: null }));
    try {
      const res = await api.refreshTicker(ticker);
      const row = (res.rows || [])[0];
      if (row) {
        setOverrides((o) => ({ ...o, [row.ticker]: row }));
        setRefreshedAt((t) => ({ ...t, [row.ticker]: { at: res.as_of, source: row.price_source } }));
      }
    } catch (err) {
      setRefreshErr((e) => ({ ...e, [ticker]: err.message || "failed" }));
    } finally {
      setBusy((b) => ({ ...b, [ticker]: false }));
    }
  }

  async function refreshSector(sector) {
    const key = `sector:${sector}`;
    setBusy((b) => ({ ...b, [key]: true }));
    setRefreshErr((e) => ({ ...e, [key]: null }));
    try {
      const res = await api.refreshSector(sector);
      const patch = {};
      const at = {};
      (res.rows || []).forEach((r) => {
        patch[r.ticker] = r;
        at[r.ticker] = { at: res.as_of, source: r.price_source };
      });
      setOverrides((o) => ({ ...o, ...patch }));
      setRefreshedAt((t) => ({ ...t, ...at }));
    } catch (err) {
      setRefreshErr((e) => ({ ...e, [key]: err.message || "failed" }));
    } finally {
      setBusy((b) => ({ ...b, [key]: false }));
    }
  }

  const results = React.useMemo(
    () => (data?.results || []).map((r) => overrides[r.ticker] || r),
    [data, overrides],
  );
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
      {error && <ErrorState error={error} onRetry={reload} />}
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
                    <span className="flex items-center gap-2">
                      {sector || "—"}
                      {sector && (
                        <RefreshButton
                          onClick={() => refreshSector(sector)}
                          busy={!!busy[`sector:${sector}`]}
                          error={refreshErr[`sector:${sector}`]}
                          title={`Refresh all of ${sector} — pull live quotes for the sector`}
                        />
                      )}
                    </span>
                  </td>
                </tr>
                {rows.map((row) => (
                  <ScoreRow
                    key={row.ticker}
                    row={row}
                    expanded={!!open[row.ticker]}
                    onToggle={() => setOpen((o) => ({ ...o, [row.ticker]: !o[row.ticker] }))}
                    onRefresh={() => refreshTicker(row.ticker)}
                    refreshing={!!busy[row.ticker]}
                    refreshedAt={refreshedAt[row.ticker]}
                    refreshError={refreshErr[row.ticker]}
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
