import React from "react";
import { Card, money, fmt } from "./ui.jsx";

// "The grove" — the position-as-a-whole companion to the Juice squeeze: the
// juice comes from fruit, and each open position IS one orange (the LEAP).
// The pulp level is LEAP intrinsic vs the cost basis deployed — a full orange
// means the capital is entirely stock-backed (intrinsic maintained); the leaf
// is the position watering itself or not (trailing weekly juice vs the LEAP's
// own weekly theta burn, leap_health.maintenance_status). Everything here is
// read straight off the enriched position — no new endpoints.

// intrinsic: prefer the leap_health block (live-priced), fall back to the
// enriched leap split. cost basis is what the orange must cover to be "full."
function pulpOf(p) {
  const lh = p.leap_health || {};
  const leap = p.leap || {};
  const intrinsic = lh.leap_intrinsic ?? leap.intrinsic ?? null;
  const basis = leap.cost_basis != null ? Number(leap.cost_basis) : null;
  const pct = intrinsic != null && basis ? (intrinsic / basis) * 100 : null;
  return { intrinsic, basis, pct };
}

const LEAF = {
  // maintenance_status -> leaf color + droop. A self-funding position holds
  // its leaf up green; a burning one wilts amber; unknown hangs gray.
  self_funding: { fill: "#34d399", tilt: 0 },
  burning: { fill: "#f59e0b", tilt: 42 },
  unknown: { fill: "#64748b", tilt: 20 },
};

// One orange in an 80×100 viewBox. Pulp fills a clipped inner circle bottom-up
// (same pour/wave idiom as the juice glasses, in fruit hues); seeds drift in
// the pulp instead of bubbles.
function Orange({ uid, pct, maintenance, maintained }) {
  const fill = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  const innerTop = 31;
  const innerBottom = 85;
  const surfaceY = innerBottom - ((innerBottom - innerTop) * fill) / 100;
  const leaf = LEAF[maintenance] || LEAF.unknown;

  return (
    <svg
      viewBox="0 0 80 100"
      className={`h-24 w-20 ${maintained ? "drop-shadow-[0_0_10px_rgba(52,211,153,0.35)]" : ""}`}
      role="img"
      aria-label={pct == null ? "intrinsic coverage unknown" : `${fmt(pct, 0)}% of LEAP cost basis covered by intrinsic`}
    >
      <defs>
        <linearGradient id={`og-${uid}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#fb923c" />
          <stop offset="1" stopColor="#ea580c" />
        </linearGradient>
        <clipPath id={`oc-${uid}`}>
          <circle cx="40" cy="58" r="27" />
        </clipPath>
        <clipPath id={`op-${uid}`}>
          <rect x="10" y={surfaceY} width="60" height={innerBottom - surfaceY} />
        </clipPath>
      </defs>

      {/* Stem + leaf. The leaf is the maintenance verdict: green upright when
          juice covers the LEAP's burn, amber and drooping when it doesn't. */}
      <rect x="38.6" y="21" width="2.8" height="9" rx="1.4" fill="#78716c" />
      <g transform={`rotate(${leaf.tilt} 41 24)`}>
        <path d="M41 24 Q49 13 60 16 Q53 27 41 24 Z" fill={leaf.fill} opacity="0.9" />
      </g>

      {/* Pulp: gradient body + wave crest + drifting seeds, clipped to fruit. */}
      <g clipPath={`url(#oc-${uid})`}>
        <g className="juice-rise">
          <rect x="10" y={surfaceY + 3} width="60" height={Math.max(0, innerBottom - surfaceY - 3) + 4}
                fill={`url(#og-${uid})`} />
          {fill > 0 && (
            <g transform={`translate(0 ${surfaceY})`}>
              <path
                className="juice-wave"
                d="M-40 0 Q-30 -4 -20 0 T0 0 T20 0 T40 0 T60 0 T80 0 T100 0 T120 0 V8 H-40 Z"
                fill="#fdba74"
              />
            </g>
          )}
          {fill >= 20 && (
            <g clipPath={`url(#op-${uid})`} fill="#ffedd5" opacity="0.4">
              <ellipse cx="32" cy="72" rx="2.4" ry="3.4" transform="rotate(-18 32 72)" />
              <ellipse cx="47" cy="66" rx="2.1" ry="3" transform="rotate(24 47 66)" />
              <ellipse cx="40" cy="79" rx="1.9" ry="2.7" transform="rotate(6 40 79)" />
            </g>
          )}
        </g>
      </g>

      {/* Peel outline + a soft gloss so it reads as fruit, not a gauge. */}
      <circle cx="40" cy="58" r="29" fill="rgba(148,163,184,0.06)"
              stroke={maintained ? "#34d399" : "#475569"} strokeWidth="2" />
      <path d="M22 44 Q27 33 38 31" fill="none" stroke="#f8fafc" strokeWidth="3"
            strokeLinecap="round" opacity="0.14" />

      {/* Direct label — actual coverage, even past 100%. */}
      <text x="40" y="63" textAnchor="middle" fontSize="15" fontWeight="700"
            fill="#f8fafc" stroke="#0f172a" strokeWidth="3" paintOrder="stroke">
        {pct == null ? "—" : `${fmt(pct, 0)}%`}
      </text>
    </svg>
  );
}

const GROVE_FLAG = {
  "self-funding": "border-emerald-500/40 bg-emerald-500/15 text-emerald-300",
  burning: "border-rose-500/40 bg-rose-500/15 text-rose-300",
  hollow: "border-rose-500/40 bg-rose-500/15 text-rose-300",
  "roll due": "border-amber-500/40 bg-amber-500/15 text-amber-300",
};

export default function GroveCard({ positions, nav }) {
  const trees = React.useMemo(() => {
    const out = (positions || [])
      .filter((p) => p.leap)
      .map((p) => ({ p, ...pulpOf(p) }));
    out.sort((a, b) => (b.pct ?? -1) - (a.pct ?? -1));
    return out;
  }, [positions]);

  if (trees.length === 0) return null;

  const backed = trees.reduce((s, t) => s + (t.intrinsic ?? 0), 0);
  const deployed = trees.reduce((s, t) => s + (t.basis ?? 0), 0);
  const overallPct = deployed > 0 ? (backed / deployed) * 100 : null;

  return (
    <Card
      title="The grove — LEAP engines"
      right={
        <span className="text-xs text-slate-400">
          {"intrinsic "}
          <span className="font-semibold text-orange-300">{money(backed)}</span>
          {" on "}{money(deployed)} deployed
        </span>
      }
    >
      {/* Aggregate: how much of all deployed LEAP capital is real intrinsic. */}
      {overallPct != null && (
        <div className="mb-4">
          <div className="h-2.5 w-full overflow-hidden rounded-full bg-slate-800">
            <div className="h-full rounded-full bg-gradient-to-r from-orange-600 to-orange-400"
                 style={{ width: `${Math.max(0, Math.min(100, overallPct))}%` }} />
          </div>
          <div className="mt-1 text-right text-[11px] text-slate-500">
            {fmt(overallPct, 0)}% of deployed capital covered by intrinsic
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-end justify-center gap-x-5 gap-y-4 sm:justify-start">
        {trees.map(({ p, intrinsic, basis, pct }) => {
          const lh = p.leap_health || {};
          const flags = [];
          // A hollow orange — no intrinsic left at all — outranks the calm
          // self-funding pill: the stock is at/below the LEAP strike and the
          // deployed capital is pure time value.
          if (pct != null && pct <= 0) flags.push("hollow");
          else if (lh.maintenance_status === "self_funding") flags.push("self-funding");
          if (lh.maintenance_status === "burning") flags.push("burning");
          if (lh.roll_due) flags.push("roll due");
          const juice = lh.trailing_avg_weekly_juice;
          const burn = lh.leap_weekly_burn;
          return (
            <button
              key={p.ticker}
              onClick={() => nav?.focus?.(p.ticker)}
              className="group flex flex-col items-center rounded-lg px-1 pb-1 transition hover:bg-slate-800/50"
              title={`${p.ticker} — intrinsic ${money(intrinsic)} of ${money(basis)} LEAP basis${
                pct != null ? ` (${fmt(pct, 0)}%)` : ""
              }${juice != null && burn != null
                ? ` · juice ${money(juice)}/wk vs burn ${money(burn)}/wk` : ""
              }${lh.leap_dte != null ? ` · LEAP ${lh.leap_dte} DTE` : ""}`}
            >
              <Orange uid={p.ticker} pct={pct}
                      maintenance={lh.maintenance_status || "unknown"}
                      maintained={pct != null && pct >= 100} />
              <div className="mt-1 text-xs font-semibold text-slate-100 group-hover:text-orange-300">
                {p.ticker}
              </div>
              <div className="text-[10px] text-slate-500">
                {intrinsic != null && basis != null
                  ? `${money(intrinsic)} of ${money(basis)}`
                  : "no mark yet"}
                {lh.leap_dte != null ? ` · ${lh.leap_dte}d` : ""}
              </div>
              {juice != null && burn != null && (
                <div className="text-[10px] text-slate-500">
                  juice {money(juice)}/wk · burn {money(burn)}/wk
                </div>
              )}
              {flags.length > 0 && (
                <div className="mt-1 flex flex-wrap justify-center gap-1">
                  {flags.map((f) => (
                    <span key={f}
                          className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${GROVE_FLAG[f]}`}>
                      {f}
                    </span>
                  ))}
                </div>
              )}
            </button>
          );
        })}
      </div>

      <div className="mt-3 border-t border-slate-800 pt-2 text-[11px] text-slate-500">
        Pulp = LEAP intrinsic vs cost basis — a full orange means the deployed capital is
        entirely stock-backed. Leaf = the position watering itself: green when weekly juice
        covers the LEAP's burn, drooping amber when it's burning.
      </div>
    </Card>
  );
}
