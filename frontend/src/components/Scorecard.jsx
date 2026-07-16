import React from "react";
import { api } from "../api.js";
import { Card, Pill, Light, Spinner, ErrorState, StockLights, fmt, pct, useApi } from "./ui.jsx";

// The per-symbol scan table, collapsed to the composable read:
//
//     SYMBOL | SECTOR | SYM | BASE | INST | RS | JUICE/WK | SCORE | VERDICT
//
// SYM = the per-name Symbol Genius color; BASE / INST = the two structure-classifier
// enums (both derived from the SINGLE classifier return the backend puts on the row);
// RS = the two-speed relative-strength state vs the sector (SHADOW); JUICE/WK = net
// weekly juice (% of LEAP cost, net of burn/slippage); SCORE = the composite 0–10
// quality rank (SHADOW — zero authority); VERDICT = the composed worst-signal-wins
// scan verdict (invisible market regime + SYM + structure entrability). RS and SCORE
// are displayed + logged but never feed the verdict, sizing, or Ready-to-Enter. Every
// other legacy readout (RS3M, ATR%, MFI, OBV, IVR, the four Genius lights, …) demotes
// to the expandable per-row drawer. One flat list (a SECTOR column, not sector-grouped
// sections), filterable by verdict AND by sector, sortable by any column; the default
// sort groups by verdict tier then SCORE desc within tier.

// ---------------------------------------------------------------------------
// Display constants — enum VALUE -> short label / tone / sort order. UI constants
// mapping from the enum values, never truncated strings scattered through the JSX.
// ---------------------------------------------------------------------------
const BASE_LABELS = {
  BASING: "BASING", EARLY_ADVANCE: "EARLY ADV", LATE_ADVANCE: "LATE ADV",
  TOPPING: "TOPPING", DECLINING: "DECLINING", INSUFFICIENT_DATA: "NO DATA",
};
const BASE_TONE = {
  EARLY_ADVANCE: "text-emerald-300", LATE_ADVANCE: "text-amber-300",
  BASING: "text-sky-300", TOPPING: "text-rose-300", DECLINING: "text-rose-400",
  INSUFFICIENT_DATA: "text-slate-500",
};
const INST_LABELS = {
  ACCUMULATING: "ACCUM", EARLY_INTEREST: "EARLY INT", NO_INTEREST: "NO INT",
  DISTRIBUTING: "DISTRIB", INSUFFICIENT_DATA: "NO DATA",
};
const INST_TONE = {
  ACCUMULATING: "text-emerald-300", EARLY_INTEREST: "text-sky-300",
  NO_INTEREST: "text-slate-400", DISTRIBUTING: "text-rose-300",
  INSUFFICIENT_DATA: "text-slate-500",
};
const VERDICT_STATUS = { READY: "ready", CAUTION: "caution", WATCH: "watch", BLOCKED: "blocked" };
const VERDICT_ORDER = { READY: 0, CAUTION: 1, WATCH: 2, BLOCKED: 3 };
const BASE_ORDER = { EARLY_ADVANCE: 0, LATE_ADVANCE: 1, BASING: 2, TOPPING: 3, DECLINING: 4, INSUFFICIENT_DATA: 5 };
const INST_ORDER = { ACCUMULATING: 0, EARLY_INTEREST: 1, NO_INTEREST: 2, DISTRIBUTING: 3, INSUFFICIENT_DATA: 4 };
const SYM_ORDER = { green: 0, yellow: 1, red: 2 };
// Two-speed RS (shadow): glyph = level sign (⊕ leading / ⊖ lagging), word = the
// four-state read. Order best->worst mirrors backend rs_state.ORDER.
const RS_LABELS = { RISING: "rising", FADING: "fading", TURNING: "turning", FALLING: "falling" };
const RS_GLYPH = { RISING: "⊕", FADING: "⊕", TURNING: "⊖", FALLING: "⊖" };
const RS_TONE = {
  RISING: "text-emerald-300", FADING: "text-amber-300",
  TURNING: "text-sky-300", FALLING: "text-rose-300",
};
const RS_ORDER = { RISING: 0, TURNING: 1, FADING: 2, FALLING: 3 };

function rsTitle(row, benchLabel) {
  const state = row.rs_state;
  if (!state) return `Relative strength vs ${benchLabel}: no read (insufficient history)`;
  return `Relative strength vs ${benchLabel}: ${state} — level ${pct(row.rs_level)} (3-month), ` +
    `slope ${fmt(row.rs_slope, 2)} (21-day EMA). SHADOW — does not affect the verdict.`;
}

// Columns: key + label + optional render + optional sortVal (numeric sort key for
// an enum column). A BASE/INST column is fully declarative — one entry each.
const COLUMNS = [
  { key: "ticker", label: "Symbol" },
  {
    key: "sector", label: "Sector",
    render: (r) => <span className="text-slate-400">{r.sector || "—"}</span>,
  },
  {
    key: "sym", label: "SYM", sortVal: (r) => SYM_ORDER[r.sym] ?? 9,
    render: (r) => (r.sym ? (
      <span className="inline-flex items-center gap-1.5" title={`Symbol Genius: ${r.sym.toUpperCase()}${r.sym_greens != null ? ` (${r.sym_greens}/4 lights)` : ""}`}>
        <Light status={r.sym} /><span className="text-[10px] uppercase text-slate-500">{r.sym}</span>
      </span>
    ) : <span className="text-slate-600">—</span>),
  },
  {
    key: "base_stage", label: "Base", sortVal: (r) => BASE_ORDER[r.base_stage] ?? 9,
    render: (r) => <span className={BASE_TONE[r.base_stage] || "text-slate-400"}>{BASE_LABELS[r.base_stage] || "—"}</span>,
  },
  {
    key: "inst_flow", label: "Inst", sortVal: (r) => INST_ORDER[r.inst_flow] ?? 9,
    render: (r) => <span className={INST_TONE[r.inst_flow] || "text-slate-400"}>{INST_LABELS[r.inst_flow] || "—"}</span>,
  },
  {
    key: "rs_state", label: "RS", sortVal: (r) => RS_ORDER[r.rs_state] ?? 9,
    render: (r) => (r.rs_state ? (
      <span className={`inline-flex items-center gap-1 ${RS_TONE[r.rs_state] || "text-slate-400"}`} title={rsTitle(r, "sector")}>
        <span>{RS_GLYPH[r.rs_state]}</span>
        <span className="text-[10px] uppercase">{RS_LABELS[r.rs_state]}</span>
      </span>
    ) : <span className="text-slate-600">—</span>),
  },
  {
    key: "net_juice_weekly_pct", label: "Juice/wk", sortVal: (r) => r.net_juice_weekly_pct,
    render: (r) => (r.net_juice_weekly_pct == null
      ? <span className="text-slate-600">—</span>
      : <span className="tabular-nums text-slate-300">{fmt(r.net_juice_weekly_pct, 2)}%</span>),
  },
  {
    key: "score", label: "Score", sortVal: (r) => r.score,
    render: (r) => (r.score == null
      ? <span className="text-slate-600">—</span>
      : <span
          className={`tabular-nums ${r.score >= 7 ? "text-emerald-300" : r.score >= 4 ? "text-slate-300" : "text-slate-500"}`}
          title="Composite SCORE 0–10 (SHADOW — a rank over quality inputs; does not affect the verdict, sizing, or Ready-to-Enter)."
        >
          {fmt(r.score, 1)}
        </span>),
  },
  {
    key: "verdict", label: "Verdict", sortVal: (r) => VERDICT_ORDER[r.verdict] ?? 9,
    render: (r) => (
      <span className="inline-flex items-center gap-1.5">
        <Pill status={VERDICT_STATUS[r.verdict] || "unknown"}>{r.verdict || "—"}</Pill>
        {r.bench && (
          <span
            title={r.path_to_ready ? `Bench — path to READY: ${r.path_to_ready}` : "Bench — waiting on a calendar/conditional trigger"}
            className="text-[10px] font-medium uppercase tracking-wide text-sky-400"
          >
            bench{r.eligible_days != null ? ` ~${r.eligible_days}d` : ""}
          </span>
        )}
      </span>
    ),
  },
];

const COLUMN_HELP = {
  ticker: "The symbol. Click the row (▸) to expand the full indicator readout + verdict inputs. ETF / no-weeklies tags flag special handling.",
  sector: "The name's sector ETF. Sort to cluster by sector, or use the Sector filter above to show one sector at a time.",
  sym: "Symbol Genius — the per-name four-light instance (Close > SMA50 · SMA50 > SMA200 · SAR below price · ROC10 > 0).\n" +
    "4 green = GREEN · exactly 3 = YELLOW (watchlist) · ≤2 or insufficient history = RED. The fourth light (SMA50 > SMA200) diverges from the market regime's EMA21 > SMA50 on purpose — a longer structural clock.",
  base_stage: "Structure — where the name sits in its base→advance→decline cycle.\n" +
    "EARLY ADV / LATE ADV / BASING / TOPPING / DECLINING (from the 150-day slope, price position, base count, ATR posture). Only EARLY ADV is READY-eligible; TOPPING / DECLINING block.",
  inst_flow: "Institutional flow — accumulation vs distribution.\n" +
    "ACCUM / EARLY INT / NO INT / DISTRIB (from 50-day up/down volume, OBV vs its 20-EMA with a price-divergence check, and accumulation/distribution day counts). DISTRIB blocks.",
  rs_state: "Two-speed relative strength vs the sector (SHADOW — does not affect the verdict).\n" +
    "Level = 3-month RS (leading ⊕ / lagging ⊖); slope = the 21-day EMA direction of the RS line.\n" +
    "⊕ rising (leading, improving) · ⊕ fading (leading, rolling over) · ⊖ turning (lagging, recovering) · ⊖ falling (lagging, worsening). vs SPY is in the row drawer.",
  net_juice_weekly_pct: "Net juice / week — weekly extrinsic as % of LEAP cost, NET of the LEAP's model theta burn and slippage. The income the setup actually pays; the Ready-to-Enter ranking key.",
  score: "Composite SCORE 0–10 (SHADOW — zero authority).\n" +
    "A quality rank over the non-blocking inputs (sector strength, base maturity, InstFlow grade, ATR posture, MA21 distance, net juice/wk, RS state). All weights are PROPOSED_DEFAULT and logged for calibration. It does NOT feed the verdict, Ready-to-Enter, or sizing — it only ranks names within a tier.",
  verdict: "The composed verdict — worst-signal-wins of the (invisible) market regime, Symbol Genius, and the structure cell, folded with the FULL entry gate.\n" +
    "READY (the whole L1–L4 gate clears — will pass Execute) · CAUTION (entrable with care) · WATCH (valid setup, not entrable) · BLOCKED. A RED market regime forces every row to BLOCKED even though regime has no column.\n" +
    "BENCH tag = a WATCH row that is one calendar/conditional trigger away from READY (no safety block). Hover it for the path to READY.",
};

// The filter set. BENCH is not a verdict value — it's a derived VIEW over WATCH rows
// whose only blockers are calendar/conditional triggers (server flag row.bench).
const BENCH = "BENCH";

// When the market regime isn't green it's the invisible input driving BLOCKED/WATCH
// verdicts below — surface it so the table's verdicts are read in context.
const REGIME_BANNER = {
  yellow: {
    cls: "border-amber-500/40 bg-amber-500/10 text-amber-200",
    text: "Market regime YELLOW — the invisible verdict input caps every row at WATCH (no fresh entries). Structure/SYM still rank names for when the tape clears.",
  },
  red: {
    cls: "border-rose-500/40 bg-rose-500/10 text-rose-200",
    text: "Market regime RED — risk-off. Every VERDICT below is BLOCKED regardless of SYM/structure; the columns are a relative ranking, not entry signals.",
  },
};

// A numeric "bigger is better" tie-break: non-null wins over null, then desc.
function descNullsLast(a, b) {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  return b - a;
}

function sortRows(rows, sort) {
  const { key, dir } = sort;
  const mul = dir === "asc" ? 1 : -1;
  // The Verdict column (also the default sort) groups by verdict tier, then ranks
  // within a tier by SCORE desc, then JUICE/WK desc — so the strongest entrable
  // names surface at the top of the READY tier. Toggling only flips the tier order.
  if (key === "verdict") {
    return [...rows].sort((a, b) => {
      const av = VERDICT_ORDER[a.verdict] ?? 9;
      const bv = VERDICT_ORDER[b.verdict] ?? 9;
      if (av !== bv) return (av - bv) * mul;
      return descNullsLast(a.score, b.score) || descNullsLast(a.net_juice_weekly_pct, b.net_juice_weekly_pct);
    });
  }
  // Sorting by Sector clusters the sectors, then ranks within each by verdict tier
  // then SCORE desc — so a sector's strongest entrable names lead its block.
  if (key === "sector") {
    return [...rows].sort((a, b) => {
      const c = String(a.sector || "").localeCompare(String(b.sector || "")) * mul;
      if (c) return c;
      return ((VERDICT_ORDER[a.verdict] ?? 9) - (VERDICT_ORDER[b.verdict] ?? 9))
        || descNullsLast(a.score, b.score);
    });
  }
  const col = COLUMNS.find((c) => c.key === key);
  const valOf = (r) => (col?.sortVal ? col.sortVal(r) : r[key]);
  return [...rows].sort((a, b) => {
    const av = valOf(a);
    const bv = valOf(b);
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "number" && typeof bv === "number") return (av - bv) * mul;
    return String(av).localeCompare(String(bv)) * mul;
  });
}

// A compact ↻ that force-pulls a live quote (one ticker, or a whole sector),
// bypassing the daily cache. Spins while in flight; turns emerald once a name
// has been refreshed this session, and red with a tooltip if the pull failed.
function RefreshButton({ onClick, busy, error, title, refreshedAt }) {
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

// One demoted readout in the expand drawer (label over value).
function Readout({ label, value }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="text-slate-300">{value}</div>
    </div>
  );
}

function ScoreRow({ row, expanded, onToggle, onRefresh, refreshing, refreshedAt, refreshError }) {
  const blocked = row.verdict === "BLOCKED";
  const caution = row.verdict === "CAUTION";
  return (
    <>
      <tr
        onClick={onToggle}
        className={`cursor-pointer border-t border-slate-800 hover:bg-slate-800/40 ${
          blocked ? "bg-rose-500/5" : caution ? "bg-amber-500/5" : ""
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
            <div className="space-y-3">
              {/* Why this verdict — the binding constraint (the first, worst input)
                  leads for a non-READY row; any remaining inputs follow. */}
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="uppercase tracking-wide text-slate-500">
                  {row.verdict === "READY" ? "Verdict inputs" : "Binding constraint"}
                </span>
                {(row.verdict_reasons?.length ? row.verdict_reasons : ["all clear"]).map((reason, i) => (
                  <span
                    key={i}
                    className={`rounded border px-1.5 py-0.5 ${
                      i === 0 && row.verdict !== "READY"
                        ? "border-rose-500/50 bg-rose-500/10 font-medium text-rose-200"
                        : "border-slate-700 text-slate-300"
                    }`}
                  >
                    {reason}
                  </span>
                ))}
              </div>
              {/* Path to READY — the forward-looking trigger legs (calendar dates +
                  EST days) that clear each blocker, derived from the SAME gate read
                  that produced the binding constraint. */}
              {row.path_to_ready && (
                <div className="flex flex-wrap items-start gap-2 text-xs">
                  <span className="uppercase tracking-wide text-slate-500">Path to READY</span>
                  <span className="text-sky-300">→ {row.path_to_ready}</span>
                </div>
              )}
              {/* The four Genius stock lights + right-spot (the old Lights column, demoted). */}
              <div className="flex items-center gap-3 text-xs text-slate-400">
                <span className="uppercase tracking-wide text-slate-500">Genius lights</span>
                <StockLights lights={row.lights} />
                {row.right_spot ? (
                  <span className={row.right_spot.pass ? "text-emerald-400" : "text-rose-400"}>
                    {row.right_spot.pass ? "spot✓" : "spot✗"}
                  </span>
                ) : null}
                {row.sym_greens != null && <span>SYM {row.sym_greens}/4</span>}
              </div>
              {/* The demoted numeric readouts. */}
              <div className="grid grid-cols-3 gap-x-6 gap-y-2 sm:grid-cols-4 lg:grid-cols-6">
                <Readout label="Price" value={fmt(row.price, 2)} />
                <Readout label="RS3M SPY" value={pct(row.rs3m_vs_spy)} />
                <Readout label="RS3M Sec" value={pct(row.rs3m_vs_sector)} />
                <Readout
                  label="RS vs SPY"
                  value={row.rs_state_spy
                    ? <span className={RS_TONE[row.rs_state_spy]} title={rsTitle({ rs_state: row.rs_state_spy, rs_level: row.rs_spy_level, rs_slope: row.rs_spy_slope }, "SPY")}>
                        {RS_GLYPH[row.rs_state_spy]} {RS_LABELS[row.rs_state_spy]}
                      </span>
                    : "—"}
                />
                <Readout
                  label="IVR"
                  value={row.iv_rank == null ? "—" : (
                    <span className={row.iv_rank >= 80 ? "text-amber-300" : "text-slate-300"}
                          title={`IV Rank ${fmt(row.iv_rank, 0)} (percentile ${fmt(row.iv_percentile, 0)}). High IVR + high juice = suspicion, not a signal to chase.`}>
                      {fmt(row.iv_rank, 0)}
                    </span>
                  )}
                />
                <Readout label="%>MA21" value={pct(row.pct_above_ma21)} />
                <Readout label="ATR ext" value={fmt(row.atr_extension, 2)} />
                <Readout label="MFI" value={fmt(row.mfi, 0)} />
                <Readout label="Vol×" value={fmt(row.volume_ratio, 2)} />
                <Readout label="ATR mom" value={fmt(row.atr_momentum, 2)} />
                <Readout label="OBV" value={row.obv_above_ema == null ? "—" : row.obv_above_ema ? "↑ accum" : "↓ distrib"} />
                <Readout label="Gross juice/wk" value={row.juice_weekly_pct == null ? "—" : `${fmt(row.juice_weekly_pct, 2)}%`} />
                <Readout label="Earnings" value={row.earnings_days != null ? `${row.earnings_days}d` : (row.earnings_date || "—")} />
                <Readout label="Suitability" value={row.suitability || "—"} />
              </div>
              {/* The CFM-suitability reasons (the internal GO/CAUTION/AVOID lens). */}
              {row.suitability_reasons?.length ? (
                <ul className="list-disc space-y-0.5 pl-5 text-xs text-slate-400">
                  {row.suitability_reasons.map((reason, i) => <li key={i}>{reason}</li>)}
                </ul>
              ) : null}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

const FILTERS = ["ALL", "READY", BENCH, "CAUTION", "WATCH", "BLOCKED"];

// Bench sort: earliest expected trigger first (a concrete calendar/EST day count
// ascending, purely-conditional rows with no day count last), then SCORE / JUICE
// desc — so the names about to clear lead the bench.
function sortBench(rows) {
  return [...rows].sort((a, b) => {
    const ad = a.eligible_days, bd = b.eligible_days;
    if (ad == null && bd == null) return descNullsLast(a.score, b.score) || descNullsLast(a.net_juice_weekly_pct, b.net_juice_weekly_pct);
    if (ad == null) return 1;
    if (bd == null) return -1;
    return (ad - bd) || descNullsLast(a.score, b.score);
  });
}

export default function Scorecard({ regimeStatus, refreshKey }) {
  const { data, error, loading, reload } = useApi(api.scorecard, [refreshKey]);
  const banner = REGIME_BANNER[regimeStatus];
  const [verdictFilter, setVerdictFilter] = React.useState("ALL");
  const [sectorFilter, setSectorFilter] = React.useState("ALL");
  const [weekliesOnly, setWeekliesOnly] = React.useState(true);
  const [sort, setSort] = React.useState({ key: "verdict", dir: "asc" });
  const [open, setOpen] = React.useState({});
  const [overrides, setOverrides] = React.useState({});
  const [busy, setBusy] = React.useState({});
  const [refreshedAt, setRefreshedAt] = React.useState({});
  const [refreshErr, setRefreshErr] = React.useState({});

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
  const noWeeklies = React.useMemo(
    () => results.filter((r) => r.has_weeklies === false).length,
    [results],
  );
  const counts = React.useMemo(() => {
    const c = { READY: 0, CAUTION: 0, WATCH: 0, BLOCKED: 0, [BENCH]: 0 };
    results.forEach((r) => {
      if (c[r.verdict] != null) c[r.verdict] += 1;
      if (r.bench) c[BENCH] += 1;
    });
    return c;
  }, [results]);

  // Pipeline throughput (a fold over row verdicts + triggers — no separate
  // computation): READY now, then bench names eligible within 14 days, then beyond.
  const pipeline = React.useMemo(() => {
    const bench = results.filter((r) => r.bench);
    const le14 = bench.filter((r) => r.eligible_days != null && r.eligible_days <= 14).length;
    return { ready: counts.READY, le14, beyond: bench.length - le14 };
  }, [results, counts.READY]);

  const sectorOptions = React.useMemo(
    () => Array.from(new Set(results.map((r) => r.sector).filter(Boolean))).sort(),
    [results],
  );

  const filtered = results.filter(
    (r) =>
      (verdictFilter === "ALL" || (verdictFilter === BENCH ? r.bench === true : r.verdict === verdictFilter)) &&
      (sectorFilter === "ALL" || r.sector === sectorFilter) &&
      (!weekliesOnly || r.has_weeklies !== false),
  );

  // In the BENCH view, sort by earliest expected trigger unless the operator has
  // clicked a specific column to sort by instead.
  const visible = React.useMemo(
    () => (verdictFilter === BENCH && sort.key === "verdict" ? sortBench(filtered) : sortRows(filtered, sort)),
    [filtered, sort, verdictFilter],
  );

  function toggleSort(key) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));
  }

  const filterBtn = (val) => (
    <button
      key={val}
      onClick={() => setVerdictFilter(val)}
      title={val === BENCH
        ? "Near-ready bench — WATCH names one calendar/conditional trigger from READY (no safety block). Sorted by earliest expected trigger."
        : undefined}
      className={`rounded-lg border px-3 py-1.5 text-sm ${
        verdictFilter === val
          ? val === BENCH
            ? "border-sky-500/50 bg-sky-500/10 text-sky-300"
            : "border-emerald-500/50 bg-emerald-500/10 text-emerald-300"
          : "border-slate-700 text-slate-400 hover:text-slate-200"
      }`}
    >
      {val === "ALL" ? `All (${results.length})` : `${val} ${counts[val] ?? 0}`}
    </button>
  );

  return (
    <Card
      title="Scan — per-symbol verdict"
      right={loading ? <span className="flex items-center gap-1.5 text-xs text-slate-500"><Spinner size="h-3 w-3" />scoring…</span> : data?.as_of ? <span className="text-xs text-slate-500">as of {data.as_of}</span> : null}
    >
      {banner && (
        <div className={`mb-4 rounded-lg border px-3 py-2 text-sm ${banner.cls}`}>
          {banner.text}
        </div>
      )}
      {/* Pipeline throughput — the scan as a pipeline, not a snapshot: how many are
          READY now, how many bench names come eligible within two weeks, the rest. */}
      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
        <span className="uppercase tracking-wide text-slate-500">Pipeline</span>
        <span className="text-emerald-300">{pipeline.ready} READY now</span>
        <span className="text-sky-300">{pipeline.le14} eligible ≤14d</span>
        <span className="text-slate-500">{pipeline.beyond} beyond</span>
      </div>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {FILTERS.map(filterBtn)}
        <div className="flex items-center gap-1.5" title="Filter the table to one sector">
          <select
            value={sectorFilter}
            onChange={(e) => setSectorFilter(e.target.value)}
            className="rounded-lg border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-300 hover:border-slate-600 focus:outline-none"
          >
            <option value="ALL">All sectors ({sectorOptions.length})</option>
            {sectorOptions.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          {sectorFilter !== "ALL" && (
            <RefreshButton
              onClick={() => refreshSector(sectorFilter)}
              busy={!!busy[`sector:${sectorFilter}`]}
              error={refreshErr[`sector:${sectorFilter}`]}
              title={`Refresh all of ${sectorFilter} — pull live quotes for the sector`}
            />
          )}
        </div>
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
      <div className="max-h-[70vh] overflow-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase tracking-wide text-slate-500">
              {COLUMNS.map((c) => (
                <th
                  key={c.key}
                  onClick={() => toggleSort(c.key)}
                  className="sticky top-0 z-10 cursor-pointer select-none bg-slate-900 py-2 pr-3 hover:text-slate-300"
                  title={COLUMN_HELP[c.key] ? `${COLUMN_HELP[c.key]}\n\n(click to sort)` : "Sort"}
                >
                  <span className="inline-flex items-center gap-1">
                    {c.label}
                    {COLUMN_HELP[c.key] && <span aria-hidden className="text-[10px] text-slate-600">ⓘ</span>}
                  </span>
                  {sort.key === c.key ? (sort.dir === "asc" ? " ▲" : " ▼") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((row) => (
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
            {!loading && visible.length === 0 && (
              <tr><td colSpan={COLUMNS.length} className="py-6 text-center text-slate-500">No tickers.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
