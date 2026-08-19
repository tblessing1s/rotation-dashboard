import React from "react";
import { api } from "../api.js";
import { Card, Pill, Light, Spinner, ErrorState, StockLights, ChartLink, SleeveBadge, sleeveOf, fmt, pct, pctSigned, useApi } from "./ui.jsx";

// The per-symbol scan table, collapsed to the composable read:
//
//     SYMBOL | SECTOR | SYM | BASE | INST | RS | JUICE/WK | SCORE | VERDICT
//
// SYM = the per-name Symbol Genius color; BASE / INST = the two structure-classifier
// enums (both derived from the SINGLE classifier return the backend puts on the row);
// RS = the two-speed relative-strength state vs the sector (SHADOW); JUICE/WK = the
// weekly covered-call juice (% of share cost); SCORE = the composite 0–10
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
    key: "income_profile", label: "Sleeve", sortVal: (r) => (r.income_profile === "DIVIDEND_COMPOUNDER" ? 1 : 0),
    render: (r) => <SleeveBadge profile={r.income_profile} />,
  },
  {
    key: "lot_cost", label: "Lot cost", sortVal: (r) => r.lot_cost,
    render: (r) => {
      if (r.lot_cost == null) return <span className="text-slate-600">—</span>;
      const over = r.affordable === false;
      return (
        <span
          className={`tabular-nums ${over ? "text-rose-300" : "text-slate-300"}`}
          title={over
            ? `100 shares costs $${Math.round(r.lot_cost).toLocaleString()} — $${Math.round(r.lot_cost_over_by || 0).toLocaleString()} more than you can deploy right now ($${Math.round(r.max_lot_cost || 0).toLocaleString()}).`
            : `100 shares costs $${Math.round(r.lot_cost).toLocaleString()} — the whole lot a shares entry buys.`}
        >
          ${Math.round(r.lot_cost).toLocaleString()}
          {over && <span className="ml-1 text-[10px] uppercase">over</span>}
        </span>
      );
    },
  },
  {
    key: "juice_weekly_pct", label: "Gross/wk", sortVal: (r) => r.juice_weekly_pct,
    render: (r) => (r.juice_weekly_pct == null
      ? <span className="text-slate-600">—</span>
      : <span className="tabular-nums text-slate-300">{pctSigned(r.juice_weekly_pct, 2)}</span>),
  },
  {
    key: "combined_weekly_yield_pct", label: "Combined/wk",
    sortVal: (r) => r.combined_weekly_yield_pct,
    render: (r) => {
      if (r.combined_weekly_yield_pct == null) return <span className="text-slate-600">—</span>;
      const floor = r.shadow_floor || {};
      // The components stay SEPARABLE on hover — never one blended number, and
      // never gross premium presented as income (this is extrinsic + dividend).
      const parts = [
        `juice (extrinsic only) ${fmt(r.juice_weekly_pct, 2)}%/wk`,
        r.dividend_known
          ? `dividend ${fmt(r.dividend_weekly_pct, 3)}%/wk (${fmt(r.annual_dividend_yield_pct, 2)}%/yr ÷ 52)`
          : "dividend unknown — not counted",
        floor.floor_pct != null
          ? `SHADOW floor ${fmt(floor.floor_pct, 2)}%/wk on ${floor.basis}: ${
              floor.pass == null ? "not measurable" : floor.pass ? "clears" : "below"}${
              (floor.reasons || []).length ? ` (${floor.reasons.join(", ")})` : ""} — zero blocking authority`
          : null,
      ].filter(Boolean);
      return (
        <span
          className={`tabular-nums ${floor.pass === false ? "text-amber-300/80" : "text-slate-300"}`}
          title={parts.join("\n")}
        >
          {pctSigned(r.combined_weekly_yield_pct, 2)}
          {!r.dividend_known && <span className="text-slate-600"> *</span>}
        </span>
      );
    },
  },
  {
    key: "net_juice_weekly_pct", label: "Net/wk", sortVal: (r) => r.net_juice_weekly_pct,
    render: (r) => (r.net_juice_weekly_pct == null
      ? <span className="text-slate-600">—</span>
      : <span className={`tabular-nums font-medium ${r.net_juice_weekly_pct <= 0 ? "text-rose-300" : "text-emerald-300/90"}`}>
          {pctSigned(r.net_juice_weekly_pct, 2)}
        </span>),
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
  income_profile: "Income sleeve — which set of income expectations this name is judged against.\n" +
    "JUICE = the CFM juice engine (the default; every existing name). DIV = Travis's DIVIDEND_COMPOUNDER extension, for lower-volatility payers held for juice AND dividend.\n" +
    "Provenance: the dividend sleeve is NOT a CFM rule — the source methodology prefers volatile names for their juice and warns against 'safe' low-vol stocks. It is an extension the shares-primary model makes viable: owning the shares, the dividend is actually collected.\n" +
    "The sleeve changes exactly two things: the RS peer benchmark for the sector leg (a dividend ETF instead of the growth-tilted sector ETF), and which SHADOW floor the row is measured against. Trend quality, the YELLOW watchlist lockout, the earnings-window exclusion and the RS-vs-SPY kill switch are IDENTICAL for both.",
  lot_cost: "What one 100-share lot costs — spot x 100. A shares-primary entry buys the WHOLE lot, so this, not the share price, is the capital a position actually needs.\n" +
    "Names whose lot costs more than your current dry powder are hidden by default (see the bar above the table); a shown row marked OVER is one you asked to see anyway.",
  juice_weekly_pct: "Juice / week — the weekly covered-call extrinsic as % of SHARE cost (spot). The strategy's stated income bar; the juice ADEQUACY floor gates on this (below the floor → BLOCKED).",
  combined_weekly_yield_pct: "Combined weekly-equivalent yield — weekly juice + (trailing annual dividend ÷ 52). Hover a cell to split it back into its two components.\n" +
    "The juice half is EXTRINSIC ONLY — time value actually captured. Intrinsic passes through and nets to zero, and gross premium is never income.\n" +
    "A '*' means the dividend yield could not be resolved, so it is NOT counted (an unknown is never shown as a confident zero).\n" +
    "SHADOW: the floors this is compared against are logged and displayed for calibration and have ZERO blocking authority — no candidate is ever rejected on them, and there is no switch to enable that.",
  net_juice_weekly_pct: "Net juice / week — the income the setup actually pays, as % of SHARE cost. Owning the shares outright, there is no long-leg decay to net out, so this tracks the gross weekly juice. The Ready-to-Enter ranking key; net ≤ 0 is a hard BLOCK.",
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

// A row refreshed in an EARLIER session still carries its own stamp in the cached
// sweep (the server writes refreshed rows back), so the indicator survives a
// reload instead of silently reverting to looking like the rest of the sweep.
function rowRefreshedAt(row) {
  return row?.refreshed_at
    ? { at: row.refreshed_at, source: row.price_source }
    : undefined;
}

// A compact ↻ that force-pulls a live quote (one ticker, or a whole sector),
// bypassing the daily sweep. Spins while in flight; turns emerald once a name has
// been refreshed (this session or persisted from an earlier one), and red with a
// tooltip if the pull failed.
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

function ScoreRow({ row, expanded, onToggle, onRefresh, refreshing, refreshedAt, refreshError, innerRef }) {
  const blocked = row.verdict === "BLOCKED";
  const caution = row.verdict === "CAUTION";
  return (
    <>
      <tr
        ref={innerRef}
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
                <ChartLink ticker={row.ticker} size="h-3.5 w-3.5" />
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
                <Readout
                  label="HVR*"
                  value={row.hv_percentile == null ? "—" : (
                    <span className={row.hv_percentile >= 80 ? "text-amber-300" : "text-slate-300"}
                          title={`HV Rank ${fmt(row.hv_percentile, 0)} — realized-vol percentile vs its own trailing 252-day range (HV ${fmt(row.hv, 0)}%). A PROXY for IV rank from bars (starred, not true IVR); available for every name.`}>
                      {fmt(row.hv_percentile, 0)}
                    </span>
                  )}
                />
                <Readout label="%>MA21" value={pct(row.pct_above_ma21)} />
                <Readout label="ATR ext" value={fmt(row.atr_extension, 2)} />
                <Readout label="MFI" value={fmt(row.mfi, 0)} />
                <Readout label="Vol×" value={fmt(row.volume_ratio, 2)} />
                <Readout label="ATR mom" value={fmt(row.atr_momentum, 2)} />
                <Readout label="OBV" value={row.obv_above_ema == null ? "—" : row.obv_above_ema ? "↑ accum" : "↓ distrib"} />
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

export default function Scorecard({ regimeStatus, refreshKey, focusTicker, onFocusHandled }) {
  // MUST be declared before the useApi below: the fetch is keyed on it, and a
  // `const` read from the deps array before its declaration is a temporal-dead-zone
  // ReferenceError that throws on every render (the app has no error boundary, so
  // that blanks the whole page rather than just this card).
  const [showPricedOut, setShowPricedOut] = React.useState(false);
  const { data, error, loading, reload } = useApi(
    () => api.scorecard(null, { includeUnaffordable: showPricedOut }),
    [refreshKey, showPricedOut]);
  const banner = REGIME_BANNER[regimeStatus];
  const [verdictFilter, setVerdictFilter] = React.useState("ALL");
  const [sectorFilter, setSectorFilter] = React.useState("ALL");
  const [weekliesOnly, setWeekliesOnly] = React.useState(true);
  const [sort, setSort] = React.useState({ key: "verdict", dir: "asc" });
  const [open, setOpen] = React.useState({});
  const rowRefs = React.useRef({});
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
    // A row is never BOTH a WATCH and a BENCH: a bench row (structure-complete,
    // waiting on a schedule) counts only as BENCH; WATCH stays the pure intake tier.
    results.forEach((r) => {
      if (r.bench) c[BENCH] += 1;
      else if (c[r.verdict] != null) c[r.verdict] += 1;
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

  // Gate-ruleset divergence (shadow-first recalibration): how many rows the
  // PROPOSED rules would verdict differently from the SHIPPED ones. Display only —
  // the table's verdicts are always the authoritative ruleset's (row.gate_ruleset).
  const divergence = React.useMemo(() => {
    const rows = results.filter((r) => r.legacy_verdict != null || r.proposed_verdict != null);
    if (!rows.length) return null;
    const diverged = rows.filter((r) => r.ruleset_divergence);
    return {
      count: diverged.length,
      scored: rows.length,
      ruleset: rows[0].gate_ruleset,
      // The two most common transitions, so the badge says WHICH way it moves.
      pairs: Object.entries(
        diverged.reduce((acc, r) => {
          const k = `${r.legacy_verdict}→${r.proposed_verdict}`;
          acc[k] = (acc[k] || 0) + 1;
          return acc;
        }, {}),
      ).sort((a, b) => b[1] - a[1]).slice(0, 2),
    };
  }, [results]);

  const sectorOptions = React.useMemo(
    () => Array.from(new Set(results.map((r) => r.sector).filter(Boolean))).sort(),
    [results],
  );

  // WATCH and BENCH are disjoint views: the WATCH filter shows only pure intake
  // (verdict WATCH and NOT bench); BENCH shows the near-ready bench.
  const matchesVerdict = (r) =>
    verdictFilter === "ALL" ? true
      : verdictFilter === BENCH ? r.bench === true
      : verdictFilter === "WATCH" ? (r.verdict === "WATCH" && !r.bench)
      : r.verdict === verdictFilter;

  const filtered = results.filter(
    (r) =>
      matchesVerdict(r) &&
      (sectorFilter === "ALL" || r.sector === sectorFilter) &&
      (!weekliesOnly || r.has_weeklies !== false),
  );

  // In the BENCH view, sort by earliest expected trigger unless the operator has
  // clicked a specific column to sort by instead.
  const visible = React.useMemo(
    () => (verdictFilter === BENCH && sort.key === "verdict" ? sortBench(filtered) : sortRows(filtered, sort)),
    [filtered, sort, verdictFilter],
  );

  // A scan-transition deep link (?tab=Scan&ticker=X): clear filters that would hide
  // the row, expand it, and scroll it into view — then release the intent.
  React.useEffect(() => {
    const t = focusTicker?.ticker;
    if (!t || !data) return;
    if (!results.some((r) => r.ticker === t)) { onFocusHandled?.(); return; }
    setVerdictFilter("ALL");
    setSectorFilter("ALL");
    setWeekliesOnly(false);
    setOpen((o) => ({ ...o, [t]: true }));
    const id = setTimeout(() => {
      rowRefs.current[t]?.scrollIntoView({ behavior: "smooth", block: "center" });
      onFocusHandled?.();
    }, 120);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusTicker?.id, data]);

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
        {/* Shadow-ruleset divergence — non-blocking. The table above is unchanged;
            this only says how often the proposed gate would have disagreed. */}
        {divergence && (
          <span
            className={`rounded-full border px-2 py-0.5 ${divergence.count
              ? "border-violet-500/40 bg-violet-500/10 text-violet-200"
              : "border-slate-700 text-slate-500"}`}
            title={`Gate ruleset in force: ${divergence.ruleset}. `
              + `${divergence.count} of ${divergence.scored} rows would verdict differently `
              + `under the proposed rules (mandatory-core 3-of-4 + ATR not-expanding). `
              + `SHADOW — the verdicts shown are the ${divergence.ruleset} ruleset's.`
              + (divergence.pairs.length
                ? ` Most common: ${divergence.pairs.map(([k, n]) => `${k} ×${n}`).join(", ")}.`
                : "")}
          >
            shadow ruleset: {divergence.count}/{divergence.scored} diverge
          </span>
        )}
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
      <AffordabilityBar
        affordability={data?.affordability}
        showPricedOut={showPricedOut}
        onToggle={setShowPricedOut}
      />
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
                innerRef={(el) => { rowRefs.current[row.ticker] = el; }}
                expanded={!!open[row.ticker]}
                onToggle={() => setOpen((o) => ({ ...o, [row.ticker]: !o[row.ticker] }))}
                onRefresh={() => refreshTicker(row.ticker)}
                refreshing={!!busy[row.ticker]}
                refreshedAt={refreshedAt[row.ticker] || rowRefreshedAt(row)}
                refreshError={refreshErr[row.ticker]}
              />
            ))}
            {!loading && visible.length === 0 && (
              <tr><td colSpan={COLUMNS.length} className="py-6 text-center text-slate-500">No tickers.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <ShadowFloorLog />
    </Card>
  );
}

// Affordability (schema v21). A shares entry buys a WHOLE 100-share lot, so a name
// whose lot costs more than the dry powder available right now is not a candidate —
// it is filtered out of the scan by default. This states the bar and what it
// excluded, because a name absent because it is too expensive today is a different
// fact from a name that failed the gate, and neither should vanish silently.
const BINDING_LABEL = {
  cash_above_reserve: "cash above your defensive reserve",
  capital_cap: "the deployed-capital cap",
  per_position_cap: "the per-position lot cap",
};

function AffordabilityBar({ affordability, showPricedOut, onToggle }) {
  if (!affordability) return null;
  const { active, max_lot_cost: bar, priced_out: hidden, binding } = affordability;
  if (!active) {
    return (
      <div className="mt-2 rounded-lg border border-amber-700/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-200/90">
        Affordability filter is <strong>off</strong> — no operating cash is set, so the app
        can&apos;t tell what you can afford. Set your cash in Settings and the scan will hide
        names whose 100-share lot is out of reach.
      </div>
    );
  }
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-xs">
      <span className="text-slate-400">
        Dry powder — you can buy a lot up to{" "}
        <strong className="tabular-nums text-slate-200">
          {bar == null ? "—" : `$${Math.round(bar).toLocaleString()}`}
        </strong>
        <span className="text-slate-500">
          {" "}({BINDING_LABEL[binding] || "your limits"} is the binding limit)
        </span>
      </span>
      {hidden > 0 ? (
        <button
          onClick={() => onToggle(!showPricedOut)}
          className="rounded border border-slate-700 px-2 py-0.5 text-slate-300 hover:bg-slate-800"
        >
          {showPricedOut ? "Hide" : "Show"} {hidden} priced out
        </button>
      ) : (
        <span className="text-slate-600">nothing priced out</span>
      )}
    </div>
  );
}

// The SHADOW income-floor calibration surface (schema v21, TRAVIS_EXTENSION).
//
// Every floor in this feature is logged and displayed but has ZERO blocking
// authority — no candidate is ever rejected on one, and no config switch exists
// that would change that. Graduating a floor to real authority is a future work
// item that is CONTINGENT on this data: it is the evidence, gathered per
// candidate per day from the append-only scan rejection log, that a calibration
// decision would rest on. Which is why it is reviewable here rather than only
// computed.
function ShadowFloorLog() {
  const [open, setOpen] = React.useState(false);
  const { data, error, loading } = useApi(
    () => (open ? api.scanRejectionStats() : Promise.resolve(null)), [open], null);
  const floors = data?.shadow_floor || {};
  const reasons = data?.shadow_floor_reasons || {};
  const profiles = Object.keys(floors);
  return (
    <div className="mt-4 border-t border-slate-800 pt-3">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 text-xs uppercase tracking-wide text-slate-500 hover:text-slate-300"
      >
        <span className={`transition-transform ${open ? "rotate-90" : ""}`}>▸</span>
        Shadow income floors — calibration log
        <span className="rounded bg-slate-500/15 px-1.5 py-0.5 text-[9px] font-semibold text-slate-400">
          NO AUTHORITY
        </span>
      </button>
      {open && (
        <div className="mt-2 text-xs">
          {loading && <Spinner />}
          {error && <ErrorState error={error} />}
          {data && profiles.length === 0 && (
            <p className="text-slate-500">
              No floor evaluations recorded yet. The nightly scan sweep appends one
              record per candidate per day.
            </p>
          )}
          {data && profiles.length > 0 && (
            <>
              <table className="w-full">
                <thead>
                  <tr className="text-left text-[10px] uppercase tracking-wide text-slate-500">
                    <th className="py-1 pr-3">Sleeve</th>
                    <th className="py-1 pr-3">Evaluated</th>
                    <th className="py-1 pr-3">Would clear</th>
                    <th className="py-1 pr-3">Would fail</th>
                    <th className="py-1">Pass rate</th>
                  </tr>
                </thead>
                <tbody>
                  {profiles.map((p) => (
                    <tr key={p} className="border-t border-slate-800/60">
                      <td className="py-1 pr-3 text-slate-300">{sleeveOf(p).badge}</td>
                      <td className="py-1 pr-3 tabular-nums text-slate-400">{floors[p].evaluated}</td>
                      <td className="py-1 pr-3 tabular-nums text-emerald-300/80">{floors[p].pass}</td>
                      <td className="py-1 pr-3 tabular-nums text-amber-300/80">{floors[p].fail}</td>
                      <td className="py-1 tabular-nums text-slate-300">
                        {floors[p].pass_rate == null ? "—" : `${fmt(floors[p].pass_rate, 1)}%`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {Object.keys(reasons).length > 0 && (
                <p className="mt-2 text-[11px] text-slate-500">
                  Reasons:{" "}
                  {Object.entries(reasons).map(([r, n], i) => (
                    <span key={r}>{i > 0 && " · "}{r} ×{n}</span>
                  ))}
                </p>
              )}
              <p className="mt-2 text-[11px] text-slate-600">
                “Would clear/fail” is counterfactual. These floors block nothing today;
                this is the record a future decision to give one authority would rest on.
              </p>
            </>
          )}
        </div>
      )}
    </div>
  );
}
