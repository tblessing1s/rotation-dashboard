import React from "react";
import { Card, money, fmt } from "./ui.jsx";

// "The juice stand" — the Overview's per-position squeeze report, one row per
// position telling the whole story left to right: the orange IS the LEAP (the
// deployed capital — pulp level = intrinsic vs cost basis, leaf = juice-vs-burn
// maintenance), and beside it sit that position's juice glasses — one per open
// short call, filling as its extrinsic decays into our pocket, each drawn with
// the 75% buyback rule as a dashed line. Two aggregate bars up top answer the
// book-level questions: how much of the juice on the table have I collected,
// and how much of my deployed capital is stock-backed. Everything reads off
// the enriched positions payload — no new endpoints.

// ---------------------------------------------------------------------------
// Per-short juice: prefer the honest extrinsic-capture numbers (intrinsic
// tracks the stock and isn't ours to collect); fall back to whole-premium
// decay when the entry extrinsic wasn't recorded, so old positions still pour.
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

// Per-position pulp: LEAP intrinsic (live-priced leap_health first, enriched
// leap split as fallback) vs the cost basis the orange must cover to be full.
function pulpOf(p) {
  const lh = p.leap_health || {};
  const leap = p.leap || {};
  const intrinsic = lh.leap_intrinsic ?? leap.intrinsic ?? null;
  const basis = leap.cost_basis != null ? Number(leap.cost_basis) : null;
  const pct = intrinsic != null && basis ? (intrinsic / basis) * 100 : null;
  return { intrinsic, basis, pct };
}

// ---------------------------------------------------------------------------
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
      className={`h-24 w-[4.3rem] ${rollNow ? "drop-shadow-[0_0_10px_rgba(52,211,153,0.4)]" : ""}`}
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

// ---------------------------------------------------------------------------
const LEAF = {
  // maintenance_status -> leaf color + droop. A self-funding position holds
  // its leaf up green; a burning one wilts amber; unknown hangs gray.
  self_funding: { fill: "#34d399", tilt: 0 },
  burning: { fill: "#f59e0b", tilt: 42 },
  unknown: { fill: "#64748b", tilt: 20 },
};

// One orange in an 80×100 viewBox. Pulp fills a clipped inner circle bottom-up
// (same pour/wave idiom as the glasses, in fruit hues); seeds drift in the
// pulp instead of bubbles.
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

// ---------------------------------------------------------------------------
const FLAG_TONE = {
  "self-funding": "border-emerald-500/40 bg-emerald-500/15 text-emerald-300",
  "roll now": "border-emerald-500/40 bg-emerald-500/15 text-emerald-300",
  burning: "border-rose-500/40 bg-rose-500/15 text-rose-300",
  hollow: "border-rose-500/40 bg-rose-500/15 text-rose-300",
  defend: "border-rose-500/40 bg-rose-500/15 text-rose-300",
  "roll due": "border-amber-500/40 bg-amber-500/15 text-amber-300",
  expiring: "border-amber-500/40 bg-amber-500/15 text-amber-300",
  expired: "border-slate-600 bg-slate-700/40 text-slate-300",
};
function FlagRow({ flags }) {
  if (!flags.length) return null;
  return (
    <div className="mt-1 flex flex-wrap justify-center gap-1">
      {flags.map((f) => (
        <span key={f}
              className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${FLAG_TONE[f] || FLAG_TONE.expired}`}>
          {f}
        </span>
      ))}
    </div>
  );
}

// A slim aggregate bar with its caption — the same shape for both totals.
function SqueezeBar({ pct, gradient, caption }) {
  return (
    <div className="min-w-0">
      <div className="h-2.5 w-full overflow-hidden rounded-full bg-slate-800">
        <div className={`h-full rounded-full bg-gradient-to-r ${gradient}`}
             style={{ width: `${Math.max(0, Math.min(100, pct))}%` }} />
      </div>
      <div className="mt-1 text-[11px] text-slate-500">{caption}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
export default function JuiceStandCard({ positions, nav }) {
  const rows = React.useMemo(() => {
    const out = (positions || [])
      .map((p) => ({
        p,
        pulp: pulpOf(p),
        shorts: (p.short_calls || []).map((sc) => ({ sc, ...juiceOf(sc) })),
      }))
      .filter((r) => r.p.leap || r.shorts.length > 0);
    out.sort((a, b) => (b.pulp.pct ?? -1) - (a.pulp.pct ?? -1));
    return out;
  }, [positions]);

  if (rows.length === 0) {
    return (
      <Card title="The juice stand">
        <p className="text-sm text-slate-500">
          No positions working — plant an orange (enter a position) to open the stand. 🍊
        </p>
      </Card>
    );
  }

  const allShorts = rows.flatMap((r) => r.shorts);
  const collected = allShorts.reduce((s, x) => s + (x.captured ?? 0), 0);
  const onTable = allShorts.reduce((s, x) => s + (x.total ?? 0), 0);
  const juicePct = onTable > 0 ? (collected / onTable) * 100 : null;
  const backed = rows.reduce((s, r) => s + (r.pulp.intrinsic ?? 0), 0);
  const deployed = rows.reduce((s, r) => s + (r.pulp.basis ?? 0), 0);
  const backedPct = deployed > 0 ? (backed / deployed) * 100 : null;

  return (
    <Card
      title={`The juice stand — ${rows.length} position${rows.length === 1 ? "" : "s"}`}
      right={
        <span className="text-xs text-slate-400">
          {"net "}
          <span className="font-semibold text-emerald-300">{money(collected)}</span>
          {" of "}{money(onTable)} squeezed
        </span>
      }
    >
      {/* Book-level squeeze: juice collected + capital stock-backed. */}
      {(juicePct != null || backedPct != null) && (
        <div className="mb-2 grid gap-3 sm:grid-cols-2">
          {juicePct != null && (
            <SqueezeBar pct={juicePct} gradient="from-emerald-600 to-emerald-400"
                        caption={`${fmt(juicePct, 0)}% of open-short juice captured (net)`} />
          )}
          {backedPct != null && (
            <SqueezeBar pct={backedPct} gradient="from-orange-600 to-orange-400"
                        caption={`${fmt(backedPct, 0)}% of deployed capital covered by intrinsic
                          — ${money(backed)} on ${money(deployed)}`} />
          )}
        </div>
      )}

      <div className="divide-y divide-slate-800/60">
        {rows.map(({ p, pulp, shorts }) => {
          const lh = p.leap_health || {};
          const orangeFlags = [];
          // A hollow orange — no intrinsic left at all — outranks the calm
          // self-funding pill: the stock is at/below the LEAP strike and the
          // deployed capital is pure time value.
          if (pulp.pct != null && pulp.pct <= 0) orangeFlags.push("hollow");
          else if (lh.maintenance_status === "self_funding") orangeFlags.push("self-funding");
          if (lh.maintenance_status === "burning") orangeFlags.push("burning");
          if (lh.roll_due) orangeFlags.push("roll due");
          const juice = lh.trailing_avg_weekly_juice;
          const burn = lh.leap_weekly_burn;

          return (
            <div key={p.ticker} className="flex flex-wrap items-start gap-x-2 gap-y-3 py-3 first:pt-0 last:pb-0">
              {/* The fruit: this position's LEAP engine. */}
              <button
                onClick={() => nav?.focus?.(p.ticker)}
                className="group flex w-28 shrink-0 flex-col items-center rounded-lg px-1 pb-1 transition hover:bg-slate-800/50"
                title={`${p.ticker} — intrinsic ${money(pulp.intrinsic)} of ${money(pulp.basis)} LEAP basis${
                  pulp.pct != null ? ` (${fmt(pulp.pct, 0)}%)` : ""
                }${juice != null && burn != null
                  ? ` · juice ${money(juice)}/wk vs burn ${money(burn)}/wk` : ""
                }${lh.leap_dte != null ? ` · LEAP ${lh.leap_dte} DTE` : ""}`}
              >
                <Orange uid={p.ticker} pct={pulp.pct}
                        maintenance={lh.maintenance_status || "unknown"}
                        maintained={pulp.pct != null && pulp.pct >= 100} />
                <div className="mt-1 text-xs font-semibold text-slate-100 group-hover:text-orange-300">
                  {p.ticker}
                </div>
                <div className="text-center text-[10px] text-slate-500">
                  {pulp.intrinsic != null && pulp.basis != null
                    ? `${money(pulp.intrinsic)} of ${money(pulp.basis)}`
                    : "no mark yet"}
                </div>
                {juice != null && burn != null && (
                  <div className="text-center text-[10px] text-slate-500">
                    juice {money(juice)}/wk · burn {money(burn)}/wk
                  </div>
                )}
                <FlagRow flags={orangeFlags} />
              </button>

              <span className="hidden self-center text-slate-600 sm:block" aria-hidden="true">→</span>

              {/* The squeeze: this orange's glasses, one per open short. */}
              {shorts.length === 0 ? (
                <div className="flex min-h-[6rem] flex-1 items-center text-xs italic text-slate-500">
                  not being squeezed — no shorts working against this LEAP
                </div>
              ) : (
                <div className="flex min-w-0 flex-1 flex-wrap items-start justify-center gap-x-3 gap-y-2 sm:justify-start">
                  {shorts.map(({ sc, pct, captured, total }, i) => {
                    const flags = [];
                    if (sc.below_strike) flags.push("defend");
                    if (sc.dte != null && sc.dte < 0) flags.push("expired");
                    else if (sc.dte != null && sc.dte <= 2) flags.push("expiring");
                    else if (sc.roll_now) flags.push("roll now");
                    const label = `${fmt(sc.strike, 0)}C`;
                    return (
                      <button
                        key={`${sc.strike}-${sc.expiration}-${i}`}
                        onClick={() => nav?.focus?.(p.ticker)}
                        className="group flex flex-col items-center rounded-lg px-1 pb-1 transition hover:bg-slate-800/50"
                        title={`${p.ticker} ${label} — ${pct != null ? `${fmt(pct, 0)}% of juice captured` : "no mark yet"}${
                          captured != null && total != null ? ` (${money(captured)} of ${money(total)})` : ""
                        }${sc.dte != null ? ` · ${sc.dte} DTE` : ""}`}
                      >
                        <Glass uid={`${p.ticker}-${i}`} pct={pct} rollNow={!!sc.roll_now} />
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
                        <FlagRow flags={flags} />
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="mt-3 border-t border-slate-800 pt-2 text-[11px] text-slate-500">
        Orange = the LEAP: pulp is intrinsic vs cost basis (full = capital stock-backed), leaf
        is juice-vs-burn (green upright = self-funding, amber droop = burning). Glasses = its
        shorts: fill is extrinsic captured since entry; the dashed line is the 75% buyback rule
        — a glass past the line is ready to roll.
      </div>
    </Card>
  );
}
