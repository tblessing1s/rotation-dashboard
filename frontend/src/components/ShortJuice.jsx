import React from "react";
import { Card, money, fmt } from "./ui.jsx";

// "Juice squeeze" — the Overview's fun tracker for the short calls working
// right now. Each open short is a glass of juice that fills as its extrinsic
// decays into our pocket; the dashed line on every glass is the 75% buyback
// rule (HARD_CFM_RULE), so a glass filling past the line literally means
// "squeeze it — roll now." An aggregate bar up top answers "of all the juice
// on the table, how much have I collected?"

// Prefer the honest extrinsic-capture numbers (intrinsic tracks the stock and
// isn't ours to collect); fall back to whole-premium decay when the entry
// extrinsic wasn't recorded, so old positions still pour.
function juiceOf(sc) {
  if (sc.extrinsic_captured_pct != null) {
    return {
      pct: sc.extrinsic_captured_pct,
      captured: sc.extrinsic_captured_total,
      total: sc.entry_extrinsic_total,
    };
  }
  if (sc.decay_pct != null) {
    const total = sc.entry_premium_total != null ? Number(sc.entry_premium_total) : null;
    return {
      pct: sc.decay_pct,
      captured: total != null ? (total * sc.decay_pct) / 100 : null,
      total,
    };
  }
  return { pct: null, captured: null, total: null };
}

// One tapered tumbler in an 80×112 viewBox. Inner (clip) region: y 13→101,
// so liquid height maps pct onto those 88 units. Pure SVG — no chart lib.
function Glass({ uid, pct, rollNow }) {
  const fill = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  const innerTop = 13;
  const innerBottom = 101;
  const surfaceY = innerBottom - ((innerBottom - innerTop) * fill) / 100;
  const y75 = innerBottom - (innerBottom - innerTop) * 0.75;
  const bubbles = fill >= 18; // need some liquid for bubbles to rise through

  return (
    <svg
      viewBox="0 0 80 112"
      className={`h-28 w-20 ${rollNow ? "drop-shadow-[0_0_10px_rgba(52,211,153,0.4)]" : ""}`}
      role="img"
      aria-label={pct == null ? "juice level unknown" : `${fmt(pct, 0)}% of juice captured`}
    >
      <defs>
        <linearGradient id={`jg-${uid}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#34d399" />
          <stop offset="1" stopColor="#059669" />
        </linearGradient>
        {/* Glass interior — trims liquid, wave and bubbles to the tumbler. */}
        <clipPath id={`jc-${uid}`}>
          <path d="M17 13 L63 13 L56.5 97 Q56 101 51 101 L29 101 Q24 101 23.5 97 Z" />
        </clipPath>
        {/* Liquid-only region so bubbles pop at the surface, not above it. */}
        <clipPath id={`jb-${uid}`}>
          <rect x="14" y={surfaceY} width="52" height={innerBottom - surfaceY} />
        </clipPath>
      </defs>

      {/* Straw (behind the rim so the glass edge overlaps it). */}
      <g transform="rotate(16 53 12)">
        <rect x="50.5" y="-6" width="5" height="46" rx="2.5" fill="#cbd5e1" opacity="0.9" />
        <rect x="50.5" y="0" width="5" height="4" fill="#fb7185" opacity="0.85" />
        <rect x="50.5" y="10" width="5" height="4" fill="#fb7185" opacity="0.85" />
        <rect x="50.5" y="20" width="5" height="4" fill="#fb7185" opacity="0.85" />
      </g>

      {/* Liquid: gradient body + animated wave crest + bubbles, all clipped. */}
      <g clipPath={`url(#jc-${uid})`}>
        <g className="juice-rise">
          <rect x="14" y={surfaceY + 3} width="52" height={Math.max(0, innerBottom - surfaceY - 3) + 2}
                fill={`url(#jg-${uid})`} />
          {fill > 0 && (
            <g transform={`translate(0 ${surfaceY})`}>
              <path
                className="juice-wave"
                d="M-40 0 Q-30 -4 -20 0 T0 0 T20 0 T40 0 T60 0 T80 0 T100 0 T120 0 V8 H-40 Z"
                fill="#6ee7b7"
              />
            </g>
          )}
          {bubbles && (
            <g clipPath={`url(#jb-${uid})`} fill="#a7f3d0" opacity="0.8">
              <circle className="juice-bubble" cx="31" cy={innerBottom - 6} r="1.7" />
              <circle className="juice-bubble" cx="42" cy={innerBottom - 4} r="2.3"
                      style={{ animationDelay: "0.9s" }} />
              <circle className="juice-bubble" cx="52" cy={innerBottom - 8} r="1.4"
                      style={{ animationDelay: "1.8s" }} />
            </g>
          )}
        </g>
      </g>

      {/* The 75% buyback rule — past this line, roll and re-sell. */}
      <line x1="18" y1={y75} x2="62" y2={y75} stroke="#64748b" strokeWidth="1"
            strokeDasharray="3 2.5" />

      {/* Glass outline on top of everything inside it. */}
      <path d="M14 10 L66 10 L59 98 Q58 104 52 104 L28 104 Q22 104 21 98 Z"
            fill="rgba(148,163,184,0.06)" stroke={rollNow ? "#34d399" : "#475569"}
            strokeWidth="2" strokeLinejoin="round" />

      {/* Direct label — text ink with a dark keyline so it reads on liquid. */}
      <text x="40" y="64" textAnchor="middle" fontSize="15" fontWeight="700"
            fill="#f8fafc" stroke="#0f172a" strokeWidth="3" paintOrder="stroke">
        {pct == null ? "—" : `${fmt(pct, 0)}%`}
      </text>
    </svg>
  );
}

const GLASS_FLAG = {
  defend: "border-rose-500/40 bg-rose-500/15 text-rose-300",
  expired: "border-slate-600 bg-slate-700/40 text-slate-300",
  expiring: "border-amber-500/40 bg-amber-500/15 text-amber-300",
  "roll now": "border-emerald-500/40 bg-emerald-500/15 text-emerald-300",
};

export default function ShortJuiceCard({ positions, nav }) {
  // One glass per open short call, ripest first.
  const shorts = React.useMemo(() => {
    const out = [];
    for (const p of positions || []) {
      for (const sc of p.short_calls || []) {
        out.push({ ticker: p.ticker, sc, ...juiceOf(sc) });
      }
    }
    out.sort((a, b) => (b.pct ?? -1) - (a.pct ?? -1));
    return out;
  }, [positions]);

  if (shorts.length === 0) {
    return (
      <Card title="Juice squeeze">
        <p className="text-sm text-slate-500">
          No shorts working — sell a call to start the pour. 🥤
        </p>
      </Card>
    );
  }

  const collected = shorts.reduce((s, x) => s + (x.captured ?? 0), 0);
  const onTable = shorts.reduce((s, x) => s + (x.total ?? 0), 0);
  const overallPct = onTable > 0 ? Math.max(0, Math.min(100, (collected / onTable) * 100)) : null;

  return (
    <Card
      title={`Juice squeeze — ${shorts.length} short${shorts.length === 1 ? "" : "s"} working`}
      right={
        <span className="text-xs text-slate-400">
          {"net "}
          <span className="font-semibold text-emerald-300">{money(collected)}</span>
          {" of "}{money(onTable)} collected
        </span>
      }
    >
      {/* Aggregate: all the juice on the table, one squeeze bar. */}
      {overallPct != null && (
        <div className="mb-4">
          <div className="h-2.5 w-full overflow-hidden rounded-full bg-slate-800">
            <div className="h-full rounded-full bg-gradient-to-r from-emerald-600 to-emerald-400"
                 style={{ width: `${overallPct}%` }} />
          </div>
          <div className="mt-1 text-right text-[11px] text-slate-500">
            {fmt(overallPct, 0)}% of open-short juice captured
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-end justify-center gap-x-5 gap-y-4 sm:justify-start">
        {shorts.map(({ ticker, sc, pct, captured, total }, i) => {
          const flags = [];
          if (sc.below_strike) flags.push("defend");
          if (sc.dte != null && sc.dte < 0) flags.push("expired");
          else if (sc.dte != null && sc.dte <= 2) flags.push("expiring");
          else if (sc.roll_now) flags.push("roll now");
          const label = `${ticker} ${fmt(sc.strike, 0)}C`;
          return (
            <button
              key={`${ticker}-${sc.strike}-${sc.expiration}-${i}`}
              onClick={() => nav?.focus?.(ticker)}
              className="group flex flex-col items-center rounded-lg px-1 pb-1 transition hover:bg-slate-800/50"
              title={`${label} — ${pct != null ? `${fmt(pct, 0)}% of juice captured` : "no mark yet"}${
                captured != null && total != null ? ` (${money(captured)} of ${money(total)})` : ""
              }${sc.dte != null ? ` · ${sc.dte} DTE` : ""}`}
            >
              <Glass uid={`${ticker}-${i}`} pct={pct} rollNow={!!sc.roll_now} />
              <div className="mt-1 text-xs font-semibold text-slate-100 group-hover:text-emerald-300">
                {label}
              </div>
              <div className="text-[10px] text-slate-500">
                {captured != null && captured < 0
                  ? `${money(Math.abs(captured))} given back`
                  : captured != null && total != null
                    ? `${money(captured)} of ${money(total)}`
                    : "no mark yet"}
                {sc.dte != null && sc.dte >= 0 ? ` · ${sc.dte}d` : ""}
              </div>
              {flags.length > 0 && (
                <div className="mt-1 flex gap-1">
                  {flags.map((f) => (
                    <span key={f}
                          className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${GLASS_FLAG[f]}`}>
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
        Fill = extrinsic captured since entry · dashed line = the 75% buyback rule — a glass
        past the line is ready to roll.
      </div>
    </Card>
  );
}
