import React from "react";
import { api } from "../api.js";
import { Card, money, fmt, useApi } from "./ui.jsx";

// "The Cash Flow Machine — today": one illustrated ribbon across the top of the
// Overview that reads the whole CFM cycle left→right, in the juice-stand's
// hand-drawn SVG idiom (fill vessels with a pour-in + wave, warm/cool hues, a
// sap-line flowing between stages):
//
//   💧 Dry Powder  →  🌱 Ready to Plant  →  🍊 The Grove  →  🫙 The Harvest
//   water to deploy    picks that clear      positions and       weekly juice
//   (cash × slots)      every level          their health        into the jar
//
// Water grows the fruit, the fruit is squeezed, the juice fills the jar — the
// same story the detail cards below tell, gathered into a single glance. Every
// number is derived on the server (capital_summary carries the deploy-capacity
// math; theta carries income); the only extra call is the ready-to-enter scan,
// which loads independently so a slow universe sweep never blocks the ribbon.

// ---------------------------------------------------------------------------
// A generic tapered/rounded vessel that fills bottom-up with a gradient body and
// an animated wave crest — the shared Glass/Orange pour idiom, parameterized so
// the water barrel and the harvest jar share one implementation. `clipPath` is
// the vessel interior; [top,bottom] are the y-bounds the fill sweeps between.
function Vessel({ uid, pct, top, bottom, clip, outline, from, to, wave, glow }) {
  const fill = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  const surfaceY = bottom - ((bottom - top) * fill) / 100;
  return (
    <svg viewBox="0 0 80 100" className={`h-20 w-16 ${glow ? "drop-shadow-[0_0_10px_rgba(52,211,153,0.35)]" : ""}`}
         role="img" aria-label={pct == null ? "level unknown" : `${fmt(fill, 0)}% full`}>
      <defs>
        <linearGradient id={`vg-${uid}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={from} />
          <stop offset="1" stopColor={to} />
        </linearGradient>
        <clipPath id={`vc-${uid}`}><path d={clip} /></clipPath>
      </defs>
      <g clipPath={`url(#vc-${uid})`}>
        <g className="juice-rise">
          <rect x="4" y={surfaceY + 2} width="72" height={Math.max(0, bottom - surfaceY - 2) + 6}
                fill={`url(#vg-${uid})`} />
          {fill > 0 && (
            <g transform={`translate(0 ${surfaceY})`}>
              <path className="juice-wave"
                    d="M-40 0 Q-30 -4 -20 0 T0 0 T20 0 T40 0 T60 0 T80 0 T100 0 T120 0 V8 H-40 Z"
                    fill={wave} />
            </g>
          )}
        </g>
      </g>
      <path d={outline} fill="rgba(148,163,184,0.05)" stroke={glow ? "#34d399" : "#475569"}
            strokeWidth="2" strokeLinejoin="round" />
      <text x="40" y="60" textAnchor="middle" fontSize="15" fontWeight="700"
            fill="#f8fafc" stroke="#0f172a" strokeWidth="3" paintOrder="stroke">
        {pct == null ? "—" : `${fmt(fill, 0)}%`}
      </text>
    </svg>
  );
}

// A little sprout — one ready-to-enter pick. Grows a stem + two leaves; the
// brighter it is the more juice it promises. Pure SVG, no chart lib.
function Sprout({ tone = "#34d399" }) {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5 shrink-0" role="img" aria-label="ready pick">
      <path d="M12 22 V11" stroke="#65a30d" strokeWidth="2" strokeLinecap="round" fill="none" />
      <path d="M12 13 Q5 12 4 6 Q11 6 12 12 Z" fill={tone} opacity="0.9" />
      <path d="M12 15 Q19 14 20 8 Q13 8 12 14 Z" fill={tone} opacity="0.7" />
    </svg>
  );
}

// One orange in the grove, sized small — fill is intrinsic vs cost basis, ring
// color is the position's health verdict. Mirrors JuiceStand's per-orange read.
function GroveOrange({ uid, pct, tone }) {
  const fill = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  const top = 33, bottom = 82;
  const surfaceY = bottom - ((bottom - top) * fill) / 100;
  return (
    <svg viewBox="0 0 60 72" className="h-11 w-9 shrink-0" role="img"
         aria-label={pct == null ? "coverage unknown" : `${fmt(fill, 0)}% intrinsic-backed`}>
      <defs>
        <linearGradient id={`gg-${uid}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#fb923c" />
          <stop offset="1" stopColor="#ea580c" />
        </linearGradient>
        <clipPath id={`gc-${uid}`}><circle cx="30" cy="46" r="21" /></clipPath>
      </defs>
      <rect x="28.6" y="12" width="2.4" height="8" rx="1.2" fill="#78716c" />
      <path d="M31 15 Q37 7 46 9 Q40 18 31 15 Z" fill={tone.leaf} opacity="0.85" />
      <g clipPath={`url(#gc-${uid})`}>
        <g className="juice-rise">
          <rect x="6" y={surfaceY + 2} width="48" height={Math.max(0, bottom - surfaceY - 2) + 4}
                fill={`url(#gg-${uid})`} />
          {fill > 0 && (
            <g transform={`translate(0 ${surfaceY})`}>
              <path className="juice-wave"
                    d="M-40 0 Q-30 -4 -20 0 T0 0 T20 0 T40 0 T60 0 T80 0 V8 H-40 Z" fill="#fdba74" />
            </g>
          )}
        </g>
      </g>
      <circle cx="30" cy="46" r="22" fill="rgba(148,163,184,0.05)" stroke={tone.ring} strokeWidth="2.5" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// The sap connector between two stages: a flowing dashed line + arrowhead. Runs
// horizontal on the wide layout, and the wrapper rotates it vertical on mobile.
function Flow({ vertical }) {
  return (
    <div className={vertical ? "flex justify-center py-1" : "flex shrink-0 items-center px-1"}
         aria-hidden="true">
      <svg viewBox="0 0 40 24" className={vertical ? "h-6 w-6 rotate-90" : "h-6 w-10"}>
        <line x1="2" y1="12" x2="30" y2="12" stroke="#475569" strokeWidth="2" className="sap-flow" />
        <path d="M30 6 L38 12 L30 18 Z" fill="#64748b" />
      </svg>
    </div>
  );
}

// The frame every stage shares: an emoji cap, a title, the illustration, a
// headline value, and a caption line — so the four read as one system.
function Stage({ emoji, title, tone, children, headline, caption, onClick, badge }) {
  const Wrap = onClick ? "button" : "div";
  return (
    <Wrap
      onClick={onClick}
      className={`group relative flex min-w-0 flex-1 flex-col items-center rounded-xl border p-3 text-center transition ${
        onClick ? "hover:brightness-125" : ""
      } ${tone}`}
    >
      <span className="ribbon-sheen pointer-events-none absolute inset-0 rounded-xl bg-gradient-to-b from-white/5 to-transparent" />
      <div className="relative flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-300">
        <span aria-hidden="true">{emoji}</span>
        {title}
        {badge != null && (
          <span className="ml-0.5 rounded-full bg-slate-950/60 px-1.5 py-0.5 text-[10px] font-bold text-slate-200">
            {badge}
          </span>
        )}
      </div>
      <div className="relative mt-1 flex min-h-[5rem] items-center justify-center">{children}</div>
      <div className="relative text-lg font-semibold leading-tight text-slate-100">{headline}</div>
      <div className="relative mt-0.5 min-h-[2rem] text-[11px] leading-snug text-slate-400">{caption}</div>
    </Wrap>
  );
}

// Slot pips: how many of the position slots are filled (●) vs open (○).
function Slots({ used, total }) {
  return (
    <span className="inline-flex gap-1" aria-label={`${used} of ${total} position slots used`}>
      {Array.from({ length: total }).map((_, i) => (
        <span key={i} className={`h-2 w-2 rounded-full ${i < used ? "bg-sky-400" : "border border-slate-600"}`} />
      ))}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Per-position health verdict for the grove — reuses the JuiceStand signals
// (kill switch / review / defend / roll / maintenance) folded to one tone.
const HEALTH = {
  critical: { ring: "#fb7185", leaf: "#fb7185", label: "act now" },
  warn: { ring: "#fbbf24", leaf: "#f59e0b", label: "watch" },
  good: { ring: "#34d399", leaf: "#34d399", label: "healthy" },
  unknown: { ring: "#64748b", leaf: "#64748b", label: "no mark" },
};
function healthOf(p, ks) {
  const lh = p.leap_health_agg || p.leap_health || {};
  if (p.needs_review || ks?.status === "red") return "critical";
  if (
    p.defend ||
    p.earnings?.warning ||
    ks?.alert ||
    lh.maintenance_status === "burning" ||
    (p.short_calls || []).some((sc) => (sc.dte != null && sc.dte <= 2) || sc.below_strike)
  ) {
    return "warn";
  }
  if (lh.maintenance_status === "self_funding" || lh.maintenance_status === "unknown") return "good";
  return lh.maintenance_status ? "good" : "unknown";
}
function pulpPctOf(p) {
  const t = p.leap_totals;
  if (t && t.intrinsic != null && t.cost_basis) return (t.intrinsic / t.cost_basis) * 100;
  const leap = p.leap || {};
  const lh = p.leap_health || {};
  const intrinsic = lh.leap_intrinsic ?? leap.intrinsic;
  const basis = leap.cost_basis;
  return intrinsic != null && basis ? (intrinsic / basis) * 100 : null;
}

// ---------------------------------------------------------------------------
export default function ProcessRibbon({ capital, positions, killByTicker, theta, nav }) {
  // The ready-to-enter scan is the one figure not in the overview payload; load
  // it here, independently, so a cold universe sweep never blocks the ribbon.
  const ready = useApi(api.scanReady, [], 5 * 60 * 1000);

  const cap = capital || {};
  const totals = theta?.totals || {};
  const rollup = theta?.net_juice_rollup || {};
  const ms = cap.milestones || {};

  // Stage 1 — Dry Powder. Fill = deployable vs the deployed-capital cap, so a
  // full barrel means a full allocation is fundable right now.
  const deployable = cap.deployable;
  const maxDeployed = cap.max_deployed;
  const powderPct = deployable != null && maxDeployed ? (deployable / maxDeployed) * 100 : null;
  const slotsUsed = cap.open_positions ?? 0;
  const slotsTotal = cap.max_positions ?? 0;
  const reserveOk = cap.reserve_ok !== false;
  const powderTone = !reserveOk
    ? "border-rose-500/40 bg-rose-500/5"
    : deployable > 0 && cap.slots_open > 0
      ? "border-sky-500/40 bg-sky-500/5"
      : "border-slate-800 bg-slate-900/40";

  // Stage 2 — Ready to Plant. The GO ∩ Level-5 shortlist, richest juice first.
  const readyList = React.useMemo(
    () => [...(ready.data?.ready || [])].sort((a, b) => (b.juice_weekly_pct ?? 0) - (a.juice_weekly_pct ?? 0)),
    [ready.data],
  );
  const readyLoading = ready.loading && !ready.data;
  const topReady = readyList.slice(0, 4);
  const bestJuice = readyList[0]?.juice_weekly_pct;
  const canPlant = (cap.slots_open ?? 0) > 0 && deployable > 0;
  const readyTone = readyList.length && canPlant
    ? "border-emerald-500/40 bg-emerald-500/5"
    : "border-slate-800 bg-slate-900/40";

  // Stage 3 — The Grove. One orange per open position, health-toned.
  const open = React.useMemo(
    () => (positions || []).filter((p) => p.status !== "closed"),
    [positions],
  );
  const grove = React.useMemo(
    () => open.map((p) => ({
      p,
      pulp: pulpPctOf(p),
      health: healthOf(p, killByTicker?.[p.ticker]),
    })).sort((a, b) => {
      const rank = { critical: 0, warn: 1, good: 2, unknown: 3 };
      return (rank[a.health] - rank[b.health]) || (b.pulp ?? -1) - (a.pulp ?? -1);
    }),
    [open, killByTicker],
  );
  const attention = grove.filter((g) => g.health === "critical" || g.health === "warn").length;
  const groveTone = grove.some((g) => g.health === "critical")
    ? "border-rose-500/40 bg-rose-500/5"
    : attention
      ? "border-amber-500/40 bg-amber-500/5"
      : grove.length
        ? "border-emerald-500/40 bg-emerald-500/5"
        : "border-slate-800 bg-slate-900/40";

  // Stage 4 — The Harvest. This week's juice into the jar, filling toward the
  // nearest live monthly milestone (half-nut, else quit-safe).
  const weekJuice = totals.this_week;
  const monthJuice = totals.this_month;
  const netWk = rollup.net_juice_per_week;
  const milestone = (ms.half_nut?.target ? ms.half_nut : ms.quit_safe?.target ? ms.quit_safe : null);
  const jarPct = milestone?.pct != null
    ? milestone.pct
    : (monthJuice != null && ms.half_nut?.target ? (monthJuice / ms.half_nut.target) * 100 : null);

  return (
    <Card className="overflow-hidden">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-200">The Cash Flow Machine — today</h3>
        <span className="hidden text-[11px] text-slate-500 sm:inline">
          water → fruit → juice → cash
        </span>
      </div>

      {/* Wide: a horizontal ribbon with sap flowing between stages. Narrow: the
          same four stages stacked, sap flowing downward. */}
      <div className="flex flex-col items-stretch gap-1 sm:flex-row sm:items-stretch">
        {/* 1 — DRY POWDER */}
        <Stage
          emoji="💧" title="Dry Powder" tone={powderTone}
          headline={deployable != null ? money(deployable) : "—"}
          onClick={() => nav?.tab?.("Positions")}
          caption={
            <>
              deployable now · {money(cap.capital_deployed)} of {money(maxDeployed)} in play
              <br />
              <span className={reserveOk ? "text-slate-400" : "text-rose-300"}>
                reserve {reserveOk ? "funded" : "short"}
              </span>
            </>
          }
        >
          <div className="flex flex-col items-center gap-1.5">
            <Vessel
              uid="powder" pct={powderPct} top={22} bottom={78}
              from="#38bdf8" to="#0284c7" wave="#7dd3fc"
              clip="M18 22 H62 V70 Q62 78 54 78 H26 Q18 78 18 70 Z"
              outline="M15 18 H65 V70 Q65 82 53 82 H27 Q15 82 15 70 Z M15 22 H65"
            />
            <Slots used={slotsUsed} total={slotsTotal} />
          </div>
        </Stage>

        <Flow vertical={false} />
        <div className="sm:hidden"><Flow vertical /></div>

        {/* 2 — READY TO PLANT */}
        <Stage
          emoji="🌱" title="Ready to Plant" tone={readyTone}
          badge={readyLoading ? "…" : readyList.length || 0}
          headline={
            readyLoading ? "scanning…" : readyList.length
              ? `${readyList.length} pick${readyList.length === 1 ? "" : "s"}`
              : "none yet"
          }
          onClick={() => nav?.tab?.("Scan")}
          caption={
            bestJuice != null
              ? <>best {fmt(bestJuice, 2)}%/wk juice{!canPlant && <><br /><span className="text-amber-300">no room to plant</span></>}</>
              : "nothing clears every level right now"
          }
        >
          {readyLoading ? (
            <div className="text-[11px] italic text-slate-500">scanning the universe…</div>
          ) : topReady.length ? (
            <div className="flex flex-col items-stretch gap-1">
              {topReady.map((r) => (
                <button
                  key={r.ticker}
                  onClick={(e) => { e.stopPropagation(); nav?.enter?.(r.ticker); }}
                  className="flex items-center gap-1.5 rounded-md border border-emerald-600/40 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-semibold text-emerald-200 hover:bg-emerald-500/20"
                  title={`${r.sector || ""} · ${fmt(r.juice_weekly_pct, 2)}%/wk — click to enter`}
                >
                  <Sprout tone={r.juice_weekly_pct >= (bestJuice ?? 0) ? "#6ee7b7" : "#34d399"} />
                  <span className="min-w-0 flex-1 text-left">{r.ticker}</span>
                  <span className="font-normal text-emerald-400/80">{fmt(r.juice_weekly_pct, 1)}%</span>
                </button>
              ))}
              {readyList.length > topReady.length && (
                <span className="text-[10px] text-slate-500">+{readyList.length - topReady.length} more →</span>
              )}
            </div>
          ) : (
            <div className="flex flex-col items-center gap-1 text-slate-600">
              <Sprout tone="#475569" />
              <span className="text-[11px] italic">bare plot</span>
            </div>
          )}
        </Stage>

        <Flow vertical={false} />
        <div className="sm:hidden"><Flow vertical /></div>

        {/* 3 — THE GROVE */}
        <Stage
          emoji="🍊" title="The Grove" tone={groveTone}
          badge={grove.length || 0}
          headline={
            grove.length
              ? attention
                ? <span className="text-amber-300">{attention} need{attention === 1 ? "s" : ""} you</span>
                : <span className="text-emerald-300">all healthy</span>
              : "empty"
          }
          onClick={() => nav?.tab?.("Positions")}
          caption={
            grove.length
              ? "intrinsic-backed fruit; ring = health — click to manage"
              : "no positions working — plant one to open the grove"
          }
        >
          {grove.length ? (
            <div className="flex max-w-[12rem] flex-wrap items-end justify-center gap-1">
              {grove.map(({ p, pulp, health }) => (
                <button
                  key={p.ticker}
                  onClick={(e) => { e.stopPropagation(); nav?.focus?.(p.ticker); }}
                  className="flex flex-col items-center rounded-md px-0.5 hover:bg-slate-800/50"
                  title={`${p.ticker} — ${HEALTH[health].label}${pulp != null ? ` · ${fmt(pulp, 0)}% intrinsic-backed` : ""}`}
                >
                  <GroveOrange uid={p.ticker} pct={pulp} tone={HEALTH[health]} />
                  <span className="text-[10px] font-semibold text-slate-300">{p.ticker}</span>
                </button>
              ))}
            </div>
          ) : (
            <div className="text-[11px] italic text-slate-500">no fruit yet 🍊</div>
          )}
        </Stage>

        <Flow vertical={false} />
        <div className="sm:hidden"><Flow vertical /></div>

        {/* 4 — THE HARVEST */}
        <Stage
          emoji="🫙" title="The Harvest" tone="border-emerald-500/40 bg-emerald-500/5"
          headline={<span className="text-emerald-300">{money(weekJuice)}</span>}
          onClick={() => nav?.tab?.("History")}
          caption={
            <>
              juice this week{netWk != null && <> · net/wk <span className={netWk >= 0 ? "text-emerald-400" : "text-rose-400"}>{money(netWk)}</span></>}
              <br />
              month {money(monthJuice)}
              {milestone?.target ? ` · ${fmt(jarPct, 0)}% to ${milestone === ms.half_nut ? "half-nut" : "quit-safe"}` : ""}
            </>
          }
        >
          <Vessel
            uid="jar" pct={jarPct} top={20} bottom={80}
            from="#34d399" to="#059669" wave="#6ee7b7" glow={jarPct != null && jarPct >= 100}
            clip="M20 24 H60 V72 Q60 78 54 78 H26 Q20 78 20 72 Z"
            outline="M22 14 H58 M18 20 H62 V72 Q62 80 54 80 H26 Q18 80 18 72 Z"
          />
        </Stage>
      </div>
    </Card>
  );
}
