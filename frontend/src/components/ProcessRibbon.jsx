import React from "react";
import { api } from "../api.js";
import { Card, money, fmt, useApi } from "./ui.jsx";

// "The Cash Flow Machine — today": one illustrated ribbon across the top of the
// Overview that tells the whole CFM cycle as a story, left→right, in the
// juice-stand's hand-drawn SVG idiom. The pictures carry the numbers — a barrel
// three-quarters full IS the dry powder, a jar near its rim IS the month's nut
// in reach — so the plain readouts fall away and only the two figures a picture
// can't spell out remain (dollars to deploy, this week's pour). The substance
// literally flows between stages: water → growth → juice → cash.
//
//   💧 Dry Powder  →  🌱 Ready to Plant  →  🍊 The Grove  →  🫙 The Harvest
//     (blue water)     (green growth)        (orange fruit)    (emerald juice)
//
// Every number is derived on the server (capital_summary carries the deploy
// math; theta carries income); the only extra call is the ready-to-enter scan,
// loaded independently so a cold universe sweep never blocks the ribbon.

// Small-count words, so the story reads as prose ("two more trees") instead of
// a readout ("slots: 2"). Falls back to the digit past the handful that matters.
const NUM_WORD = ["no", "one", "two", "three", "four", "five", "six", "seven"];
const words = (n) => NUM_WORD[n] ?? String(n);
const cap1 = (s) => s.charAt(0).toUpperCase() + s.slice(1);

// ---------------------------------------------------------------------------
// A generic vessel that fills bottom-up with a gradient body and an animated
// wave crest — the shared Glass/Orange pour idiom, parameterized so the water
// barrel and the harvest jar share one implementation. No number is drawn on
// it: the fill height is the reading. An optional `capLabel` tags the rim with
// the goal the fill is climbing toward (the month's nut).
function Vessel({ uid, pct, top, bottom, clip, outline, from, to, wave, glow, capLabel }) {
  const fill = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  const surfaceY = bottom - ((bottom - top) * fill) / 100;
  return (
    <svg viewBox="0 0 80 100" className={`h-20 w-16 ${glow ? "drop-shadow-[0_0_12px_rgba(52,211,153,0.5)]" : ""}`}
         role="img" aria-label={pct == null ? "level unknown" : `${fmt(fill, 0)} percent full`}>
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
      {/* The rim goal — a dashed line + tag at the very top, so a full vessel
          reads as "reached it" without a percent. */}
      {capLabel && (
        <>
          <line x1="16" y1={top} x2="64" y2={top} stroke="#94a3b8" strokeWidth="1"
                strokeDasharray="3 2.5" opacity="0.7" />
          <text x="40" y={top - 3} textAnchor="middle" fontSize="8.5" fontWeight="700" fill="#cbd5e1">
            {capLabel}
          </text>
        </>
      )}
      <path d={outline} fill="rgba(148,163,184,0.05)" stroke={glow ? "#34d399" : "#475569"}
            strokeWidth="2" strokeLinejoin="round" />
    </svg>
  );
}

// A little sprout — one ready-to-enter pick pushing up out of the soil. The
// richer its juice, the brighter and taller it stands.
function Sprout({ tone = "#34d399", vigor = 1 }) {
  const lift = vigor >= 1 ? 0 : 2;
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5 shrink-0" role="img" aria-label="ready pick">
      <path d={`M12 22 V${11 + lift}`} stroke="#65a30d" strokeWidth="2" strokeLinecap="round" fill="none" />
      <path d={`M12 ${13 + lift} Q5 ${12 + lift} 4 ${6 + lift} Q11 ${6 + lift} 12 ${12 + lift} Z`} fill={tone} opacity="0.9" />
      <path d={`M12 ${15 + lift} Q19 ${14 + lift} 20 ${8 + lift} Q13 ${8 + lift} 12 ${14 + lift} Z`} fill={tone} opacity="0.7" />
    </svg>
  );
}

// One orange in the grove — fill is intrinsic vs cost basis (how stock-backed
// the fruit is), the ring is the position's health verdict. Mirrors the juice
// stand's per-orange read, no number needed.
function GroveOrange({ uid, pct, tone }) {
  const fill = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  const top = 33, bottom = 82;
  const surfaceY = bottom - ((bottom - top) * fill) / 100;
  return (
    <svg viewBox="0 0 60 72" className="h-11 w-9 shrink-0" role="img"
         aria-label={pct == null ? "coverage unknown" : `${fmt(fill, 0)} percent intrinsic-backed`}>
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

// This week's juice — the detailed tumbler from the juice stand, brought up to
// the ribbon: a straw, a poured-in wave crest, rising bubbles, and a dashed
// "pace" line at the 1%/week target so a glass filled past it is a week on
// pace. Fill is this week's juice against the 2% stretch (a full glass), so the
// picture reads the whole story and the dollar figure sits below it.
function WeeklyGlass({ pct, paceFrac, glow }) {
  const fill = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  const innerTop = 13, innerBottom = 101;
  const surfaceY = innerBottom - ((innerBottom - innerTop) * fill) / 100;
  const paceY = innerBottom - (innerBottom - innerTop) * (paceFrac ?? 0.5);
  const bubbles = fill >= 18;
  return (
    <svg viewBox="0 0 80 112" className={`h-24 w-[4.3rem] ${glow ? "drop-shadow-[0_0_12px_rgba(52,211,153,0.55)]" : ""}`}
         role="img" aria-label={pct == null ? "this week's juice unknown" : `this week's juice, glass ${fmt(fill, 0)} percent to target`}>
      <defs>
        <linearGradient id="wjg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#34d399" />
          <stop offset="1" stopColor="#059669" />
        </linearGradient>
        <clipPath id="wjc"><path d="M17 13 L63 13 L56.5 97 Q56 101 51 101 L29 101 Q24 101 23.5 97 Z" /></clipPath>
        <clipPath id="wjb"><rect x="14" y={surfaceY} width="52" height={innerBottom - surfaceY} /></clipPath>
      </defs>
      {/* Straw, behind the rim. */}
      <g transform="rotate(16 53 12)">
        <rect x="50.5" y="-6" width="5" height="46" rx="2.5" fill="#cbd5e1" opacity="0.9" />
        <rect x="50.5" y="0" width="5" height="4" fill="#fb7185" opacity="0.85" />
        <rect x="50.5" y="10" width="5" height="4" fill="#fb7185" opacity="0.85" />
        <rect x="50.5" y="20" width="5" height="4" fill="#fb7185" opacity="0.85" />
      </g>
      {/* Liquid: gradient body + wave crest + bubbles, clipped to the glass. */}
      <g clipPath="url(#wjc)">
        <g className="juice-rise">
          <rect x="14" y={surfaceY + 3} width="52" height={Math.max(0, innerBottom - surfaceY - 3) + 2} fill="url(#wjg)" />
          {fill > 0 && (
            <g transform={`translate(0 ${surfaceY})`}>
              <path className="juice-wave"
                    d="M-40 0 Q-30 -4 -20 0 T0 0 T20 0 T40 0 T60 0 T80 0 T100 0 T120 0 V8 H-40 Z" fill="#6ee7b7" />
            </g>
          )}
          {bubbles && (
            <g clipPath="url(#wjb)" fill="#a7f3d0" opacity="0.8">
              <circle className="juice-bubble" cx="31" cy={innerBottom - 6} r="1.7" />
              <circle className="juice-bubble" cx="42" cy={innerBottom - 4} r="2.3" style={{ animationDelay: "0.9s" }} />
              <circle className="juice-bubble" cx="52" cy={innerBottom - 8} r="1.4" style={{ animationDelay: "1.8s" }} />
            </g>
          )}
        </g>
      </g>
      {/* The week's pace line — juice above it is a week on target. */}
      <line x1="18" y1={paceY} x2="62" y2={paceY} stroke="#64748b" strokeWidth="1" strokeDasharray="3 2.5" />
      <path d="M14 10 L66 10 L59 98 Q58 104 52 104 L28 104 Q21 104 21 98 Z"
            fill="rgba(148,163,184,0.06)" stroke={glow ? "#34d399" : "#475569"} strokeWidth="2" strokeLinejoin="round" />
    </svg>
  );
}

// The connector between two stages: the substance of the stage it leaves flows
// on to the next — water out of the barrel, sap up the rows, juice into the jar.
function Flow({ vertical, color = "#64748b" }) {
  return (
    <div className={vertical ? "flex justify-center py-1" : "flex shrink-0 items-center px-1"}
         aria-hidden="true">
      <svg viewBox="0 0 40 24" className={vertical ? "h-6 w-6 rotate-90" : "h-6 w-10"}>
        <line x1="2" y1="12" x2="30" y2="12" stroke={color} strokeWidth="2.5" className="sap-flow" />
        <path d="M30 6 L38 12 L30 18 Z" fill={color} />
      </svg>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The weather over the grove — the market regime. Green is clear skies (good
// weather to plant), yellow is overcast (tighten up), red is a storm (stand
// down). It presides over the whole ribbon, the same way regime is market-wide
// context that governs every entry. Pure SVG, gently animated.
function Weather({ status }) {
  if (status === "green") {
    return (
      <svg viewBox="0 0 64 48" className="h-14 w-16" role="img" aria-label="clear skies">
        <circle className="sun-glow" cx="32" cy="24" r="16" fill="#fde68a" opacity="0.6" />
        <g className="sun-rays" fill="none" stroke="#fbbf24" strokeWidth="2.5" strokeLinecap="round">
          {Array.from({ length: 8 }).map((_, i) => {
            const a = (i * Math.PI) / 4;
            const x = 32 + Math.cos(a), y = 24 + Math.sin(a);
            return <line key={i} x1={x + Math.cos(a) * 13} y1={y + Math.sin(a) * 13}
                         x2={x + Math.cos(a) * 20} y2={y + Math.sin(a) * 20} />;
          })}
        </g>
        <circle cx="32" cy="24" r="11" fill="#facc15" stroke="#f59e0b" strokeWidth="1.5" />
      </svg>
    );
  }
  if (status === "yellow") {
    return (
      <svg viewBox="0 0 64 48" className="h-14 w-16" role="img" aria-label="overcast">
        <circle cx="24" cy="20" r="9" fill="#fcd34d" opacity="0.85" />
        <g className="cloud-drift">
          <ellipse cx="34" cy="30" rx="16" ry="10" fill="#94a3b8" />
          <ellipse cx="24" cy="28" rx="9" ry="8" fill="#cbd5e1" />
          <ellipse cx="44" cy="28" rx="9" ry="8" fill="#cbd5e1" />
        </g>
      </svg>
    );
  }
  if (status === "red") {
    return (
      <svg viewBox="0 0 64 48" className="h-14 w-16" role="img" aria-label="storm">
        <g className="cloud-drift">
          <ellipse cx="34" cy="20" rx="18" ry="11" fill="#475569" />
          <ellipse cx="22" cy="18" rx="10" ry="9" fill="#64748b" />
          <ellipse cx="46" cy="18" rx="10" ry="9" fill="#64748b" />
        </g>
        <path className="lightning" d="M32 22 L26 34 L31 34 L27 44 L40 30 L34 30 L38 22 Z"
              fill="#fbbf24" stroke="#f59e0b" strokeWidth="0.5" />
        <g stroke="#60a5fa" strokeWidth="2" strokeLinecap="round">
          {[16, 26, 44, 52].map((x, i) => (
            <line key={i} className="rain-drop" x1={x} y1="32" x2={x - 2} y2="38"
                  style={{ animationDelay: `${i * 0.25}s` }} />
          ))}
        </g>
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 64 48" className="h-14 w-16" role="img" aria-label="sky unreadable">
      <ellipse className="cloud-drift" cx="32" cy="26" rx="18" ry="10" fill="#475569" opacity="0.6" />
      <ellipse cx="24" cy="24" rx="9" ry="7" fill="#64748b" opacity="0.5" />
    </svg>
  );
}

const WEATHER = {
  green: {
    sky: "from-sky-500/20 via-sky-500/5 to-transparent border-sky-500/30",
    head: "Clear skies", headTone: "text-sky-200",
    story: "Good weather to plant — clear to hunt entries.",
  },
  yellow: {
    sky: "from-amber-500/15 via-amber-500/5 to-transparent border-amber-500/30",
    head: "Overcast", headTone: "text-amber-200",
    story: "Tighten the criteria — no fresh risk while it's grey.",
  },
  red: {
    sky: "from-slate-600/40 via-rose-900/20 to-transparent border-rose-500/40",
    head: "Storm overhead", headTone: "text-rose-200",
    story: "Stand down — tend what you hold, don't plant into the storm.",
  },
  unknown: {
    sky: "from-slate-700/30 to-transparent border-slate-700",
    head: "Sky unread", headTone: "text-slate-300",
    story: "Regime unknown — read the tape before you plant.",
  },
};

function WeatherBanner({ regime }) {
  const status = regime?.status || "unknown";
  const w = WEATHER[status] || WEATHER.unknown;
  const bits = [];
  if (regime?.breadth != null) bits.push(`breadth ${fmt(regime.breadth, 0)}%`);
  if (regime?.vix != null) bits.push(`VIX ${fmt(regime.vix, 1)}`);
  if (regime?.spy_trend) bits.push(`SPY ${regime.spy_trend}`);
  return (
    <div
      className={`mb-2 flex items-center gap-3 rounded-xl border bg-gradient-to-b ${w.sky} px-3 py-2`}
      title={bits.length ? `Market regime — ${bits.join(" · ")}` : "Market regime"}
    >
      <Weather status={status} />
      <div className="min-w-0">
        <div className={`text-sm font-semibold ${w.headTone}`}>{w.head}</div>
        <div className="text-[12px] italic leading-snug text-slate-300">{w.story}</div>
      </div>
      <span className="ml-auto hidden text-[10px] uppercase tracking-wide text-slate-500 sm:inline">
        weather over the grove
      </span>
    </div>
  );
}

// The frame every stage shares: an emoji cap, a title, the illustration, one
// narrative line (the story), and — only where a picture can't spell it — one
// hero figure. So the four read as one sentence you scan left to right.
function Stage({ emoji, title, tone, children, hero, story, onClick, badge }) {
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
      {hero != null && (
        <div className="relative text-xl font-semibold leading-tight text-slate-100">{hero}</div>
      )}
      <div className="relative mt-0.5 min-h-[2.5rem] px-1 text-[12px] italic leading-snug text-slate-300">
        {story}
      </div>
    </Wrap>
  );
}

// Slot pips — the plots in the grove: planted (filled) vs open ground (ring).
function Slots({ used, total }) {
  return (
    <span className="mt-1 inline-flex gap-1" aria-label={`${used} of ${total} plots planted`}>
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
  critical: { ring: "#fb7185", leaf: "#fb7185", label: "needs you now" },
  warn: { ring: "#fbbf24", leaf: "#f59e0b", label: "wants tending" },
  good: { ring: "#34d399", leaf: "#34d399", label: "thriving" },
  unknown: { ring: "#64748b", leaf: "#64748b", label: "no mark yet" },
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

// Segment colors — the substance flowing on: water, growth, juice.
const FLOW = { water: "#38bdf8", growth: "#84cc16", juice: "#34d399" };

// ---------------------------------------------------------------------------
export default function ProcessRibbon({ capital, positions, killByTicker, theta, regime, nav }) {
  const ready = useApi(api.scanReady, [], 5 * 60 * 1000);

  const capData = capital || {};
  const totals = theta?.totals || {};
  const rollup = theta?.net_juice_rollup || {};
  const ms = capData.milestones || {};

  // ---- 1. Dry Powder — the rain barrel. Fill = deployable vs a full allocation.
  const deployable = capData.deployable;
  const maxDeployed = capData.max_deployed;
  const powderPct = deployable != null && maxDeployed ? (deployable / maxDeployed) * 100 : null;
  const slotsUsed = capData.open_positions ?? 0;
  const slotsTotal = capData.max_positions ?? 0;
  const slotsOpen = capData.slots_open ?? 0;
  const reserveOk = capData.reserve_ok !== false;
  const canDeploy = deployable > 0 && slotsOpen > 0;
  const powderTone = !reserveOk
    ? "border-rose-500/40 bg-rose-500/5"
    : canDeploy
      ? "border-sky-500/40 bg-sky-500/5"
      : "border-slate-800 bg-slate-900/40";
  const powderStory = !reserveOk
    ? "Dipping below the reserve line — top up before you plant."
    : slotsOpen <= 0
      ? "The grove's full — no ground left to plant."
      : canDeploy
        ? cap1(`${words(slotsOpen)} more tree${slotsOpen === 1 ? "" : "s"} could take root.`)
        : "The barrel's run dry — no water to spare.";

  // ---- 2. Ready to Plant — saplings that clear every level, richest sap first.
  const readyList = React.useMemo(
    () => [...(ready.data?.ready || [])].sort((a, b) => (b.juice_weekly_pct ?? 0) - (a.juice_weekly_pct ?? 0)),
    [ready.data],
  );
  const readyLoading = ready.loading && !ready.data;
  const topReady = readyList.slice(0, 4);
  const bestJuice = readyList[0]?.juice_weekly_pct;
  const canPlant = slotsOpen > 0 && deployable > 0;
  const readyTone = readyList.length && canPlant
    ? "border-emerald-500/40 bg-emerald-500/5"
    : "border-slate-800 bg-slate-900/40";
  const plantStory = readyLoading
    ? "Walking the rows…"
    : !readyList.length
      ? (canPlant ? "Nothing worth planting yet." : "Waiting on water and open ground.")
      : !canPlant
        ? cap1(`${words(readyList.length)} ready, but there's no ground to plant.`)
        : readyList.length === 1
          ? `${readyList[0].ticker}'s sap runs rich — ready for soil.`
          : `${cap1(words(readyList.length))} saplings ready — ${readyList[0].ticker} runs richest.`;

  // ---- 3. The Grove — one health-toned orange per open position.
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
  const hasCritical = grove.some((g) => g.health === "critical");
  const groveTone = hasCritical
    ? "border-rose-500/40 bg-rose-500/5"
    : attention
      ? "border-amber-500/40 bg-amber-500/5"
      : grove.length
        ? "border-emerald-500/40 bg-emerald-500/5"
        : "border-slate-800 bg-slate-900/40";
  const groveStory = !grove.length
    ? "Bare grove — plant a tree to begin."
    : attention === 0
      ? (grove.length === 1 ? "One tree, well-tended." : cap1(`${words(grove.length)} trees, all thriving.`))
      : `${cap1(words(attention))} ${attention === 1 ? "tree wants" : "trees want"} tending.`;
  const groveToneText = hasCritical ? "text-rose-300" : attention ? "text-amber-300" : grove.length ? "text-emerald-300" : "text-slate-400";

  // ---- 4. The Harvest — this week's juice, a detailed glass filling to pace.
  // Fill is this week against the 2%/week stretch (a full glass); the dashed
  // pace line sits at the 1% "on-pace" target, so juice past it is a good week.
  const weekJuice = totals.this_week;
  const netWk = rollup.net_juice_per_week;
  const wt = theta?.weekly_target || {};
  const targetLow = wt.target_low;
  const targetHigh = wt.target_high;
  const weekPct = weekJuice != null && targetHigh
    ? (weekJuice / targetHigh) * 100
    : (weekJuice > 0 ? 100 : 0);
  const paceFrac = targetHigh ? Math.min(0.9, Math.max(0.15, (targetLow || 0) / targetHigh)) : 0.5;
  const onPace = targetLow != null && weekJuice != null && weekJuice >= targetLow;
  const aboveTarget = targetHigh != null && weekJuice != null && weekJuice >= targetHigh;
  const draining = netWk != null && netWk < 0;
  let harvestStory = !weekJuice || weekJuice <= 0
    ? "Nothing poured yet this week."
    : aboveTarget
      ? "Glass overflowing — juice above target."
      : onPace
        ? "A solid pour — juice on pace this week."
        : "A slow trickle — under this week's pace.";
  if (draining) harvestStory += " The burn's outrunning the juice.";
  const harvestTone = draining
    ? "border-rose-500/40 bg-rose-500/5"
    : aboveTarget
      ? "border-emerald-400/50 bg-emerald-500/10"
      : "border-emerald-500/40 bg-emerald-500/5";

  return (
    <Card className="overflow-hidden">
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h3 className="text-sm font-semibold text-slate-200">The Cash Flow Machine — today</h3>
        <span className="hidden text-[11px] text-slate-500 sm:inline">💧 water → 🍊 fruit → 🥤 juice → 💰 cash</span>
      </div>

      {/* The weather over the grove — the market regime presiding over it all. */}
      <WeatherBanner regime={regime} />

      <div className="flex flex-col items-stretch gap-1 sm:flex-row sm:items-stretch">
        {/* 1 — DRY POWDER (the rain barrel) */}
        <Stage
          emoji="💧" title="Dry Powder" tone={powderTone}
          hero={deployable != null ? money(deployable) : "—"}
          story={<span className={reserveOk ? "" : "text-rose-300"}>{powderStory}</span>}
          onClick={() => nav?.tab?.("Positions")}
        >
          <div className="flex flex-col items-center">
            <Vessel
              uid="powder" pct={powderPct} top={22} bottom={78}
              from="#38bdf8" to="#0284c7" wave="#7dd3fc"
              clip="M18 22 H62 V70 Q62 78 54 78 H26 Q18 78 18 70 Z"
              outline="M15 18 H65 V70 Q65 82 53 82 H27 Q15 82 15 70 Z M15 22 H65"
            />
            <Slots used={slotsUsed} total={slotsTotal} />
          </div>
        </Stage>

        <Flow color={FLOW.water} />
        <div className="sm:hidden"><Flow vertical color={FLOW.water} /></div>

        {/* 2 — READY TO PLANT (the saplings) */}
        <Stage
          emoji="🌱" title="Ready to Plant" tone={readyTone}
          badge={readyLoading ? "…" : readyList.length || 0}
          story={<span className={readyList.length && canPlant ? "text-emerald-200" : ""}>{plantStory}</span>}
          onClick={() => nav?.tab?.("Scan")}
        >
          {readyLoading ? (
            <div className="text-[11px] italic text-slate-500">walking the rows…</div>
          ) : topReady.length ? (
            <div className="flex flex-col items-stretch gap-1">
              {topReady.map((r) => (
                <button
                  key={r.ticker}
                  onClick={(e) => { e.stopPropagation(); nav?.enter?.(r.ticker); }}
                  className="flex items-center gap-1.5 rounded-md border border-emerald-600/40 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-semibold text-emerald-200 hover:bg-emerald-500/20"
                  title={`${r.sector || ""} · ${fmt(r.juice_weekly_pct, 2)}%/wk sap — plant ${r.ticker}`}
                >
                  <Sprout tone={r.juice_weekly_pct >= (bestJuice ?? 0) ? "#6ee7b7" : "#34d399"}
                          vigor={r.juice_weekly_pct >= (bestJuice ?? 0) ? 1 : 0} />
                  <span className="min-w-0 flex-1 text-left">{r.ticker}</span>
                  <span className="font-normal text-emerald-400/80">{fmt(r.juice_weekly_pct, 1)}%</span>
                </button>
              ))}
              {readyList.length > topReady.length && (
                <span className="text-[10px] text-slate-500">+{readyList.length - topReady.length} more in the beds →</span>
              )}
            </div>
          ) : (
            <div className="flex flex-col items-center gap-1 text-slate-600">
              <Sprout tone="#475569" vigor={0} />
              <span className="text-[11px] italic">bare plot</span>
            </div>
          )}
        </Stage>

        <Flow color={FLOW.growth} />
        <div className="sm:hidden"><Flow vertical color={FLOW.growth} /></div>

        {/* 3 — THE GROVE (the trees) */}
        <Stage
          emoji="🍊" title="The Grove" tone={groveTone}
          badge={grove.length || 0}
          story={<span className={groveToneText}>{groveStory}</span>}
          onClick={() => nav?.tab?.("Positions")}
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
            <div className="text-[11px] italic text-slate-500">no trees yet 🌱</div>
          )}
        </Stage>

        <Flow color={FLOW.juice} />
        <div className="sm:hidden"><Flow vertical color={FLOW.juice} /></div>

        {/* 4 — THE HARVEST (this week's juice glass) */}
        <Stage
          emoji="🥤" title="The Harvest" tone={harvestTone}
          hero={<span className={draining ? "text-rose-300" : "text-emerald-300"}>{money(weekJuice)}</span>}
          story={<span className={draining ? "text-rose-300" : ""}>{harvestStory}</span>}
          onClick={() => nav?.tab?.("History")}
        >
          <WeeklyGlass pct={weekPct} paceFrac={paceFrac} glow={aboveTarget} />
        </Stage>
      </div>
    </Card>
  );
}
