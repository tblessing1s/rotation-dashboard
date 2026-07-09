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
export function juiceOf(sc) {
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

// The other half of an ITM short's premium: intrinsic captured in cash. We sold
// intrinsic + extrinsic; the extrinsic is the juice above, and this is the
// intrinsic banked at entry that has since melted back to us as the stock fell
// toward/under the strike. Signed — a climbing stock hands it back, but that lift
// shows up as the orange's intrinsic gaining to match, so it's a hedge, not a
// leak. null when the entry extrinsic wasn't recorded (entry intrinsic unknowable).
function intrinsicCashOf(sc) {
  return sc.intrinsic_captured_total != null ? Number(sc.intrinsic_captured_total) : null;
}

// The all-in verdict for one row: is this position actually making money?
// Three legs, summed from what the payload already carries:
//   banked  — realized net juice this LEAP cycle (extrinsic_payback meter's
//             collected_to_date: close_short executions, carried across rolls)
//   leapPL  — the orange's value change: LEAP mark now vs cost basis
//   shortPL — open shorts marked-to-market: premium sold x decay (whole-premium,
//             not just extrinsic — an ITM short's intrinsic swing is real P/L here)
// Null only when no leg is knowable; missing legs otherwise contribute zero.
function rowNetOf(p, shorts, payback) {
  const t = p.leap_totals || p.leap || {};
  const banked = payback?.[p.ticker]?.collected_to_date ?? null;
  const value = t.current_value ?? t.current_bid;
  const leapPL = value != null && t.cost_basis != null
    ? Number(value) - Number(t.cost_basis)
    : null;
  let shortPL = null;
  for (const { sc } of shorts) {
    if (sc.entry_premium_total != null && sc.decay_pct != null) {
      shortPL = (shortPL || 0) + (Number(sc.entry_premium_total) * sc.decay_pct) / 100;
    }
  }
  if (banked == null && leapPL == null && shortPL == null) return null;
  return { net: (banked || 0) + (leapPL || 0) + (shortPL || 0), banked, leapPL, shortPL };
}

// money() with an explicit sign, for P/L readouts.
function signedMoney(n) {
  if (n == null) return "—";
  return `${n < 0 ? "−" : "+"}$${Math.abs(Number(n)).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

// Per-position pulp: total LEAP intrinsic across every leg vs the total cost
// basis the orange must cover to be full. leap_totals aggregates the legs
// server-side; the single-leg fallbacks keep older payloads rendering.
export function pulpOf(p) {
  const t = p.leap_totals;
  if (t) {
    const pct = t.intrinsic != null && t.cost_basis ? (t.intrinsic / t.cost_basis) * 100 : null;
    return { intrinsic: t.intrinsic, basis: t.cost_basis, pct };
  }
  const lh = p.leap_health || {};
  const leap = p.leap || {};
  const intrinsic = lh.leap_intrinsic ?? leap.intrinsic ?? null;
  const basis = leap.cost_basis != null ? Number(leap.cost_basis) : null;
  const pct = intrinsic != null && basis ? (intrinsic / basis) * 100 : null;
  return { intrinsic, basis, pct };
}

// The LEAP's live extrinsic (time value) — the burn side of the extrinsic
// ledger, paired with pulpOf's intrinsic. leap_totals sums the legs; the
// single-leg field is the fallback for older payloads.
function leapExtrinsicOf(p) {
  const t = p.leap_totals;
  if (t && t.extrinsic != null) return Number(t.extrinsic);
  const leap = p.leap || {};
  return leap.extrinsic != null ? Number(leap.extrinsic) : null;
}

// The PMCC hedge check for one position: does the LEAP's intrinsic (the asset)
// cover its short calls' intrinsic (the liability)? When it does, a stock move
// changes both legs together and washes out — so the position's real edge is the
// extrinsic left over: juice still on the shorts vs the LEAP's time-value burn.
// Returns null intrinsics when unknown so the caller can skip a position cleanly.
function balanceOf(p, shorts) {
  const longIntrinsic = pulpOf(p).intrinsic;
  const longExtrinsic = leapExtrinsicOf(p);
  const known = shorts.filter((s) => s.sc.current_intrinsic_total != null);
  const shortIntrinsic = known.length
    ? known.reduce((s, x) => s + Number(x.sc.current_intrinsic_total), 0) : null;
  const shortExtrinsic = shorts.reduce(
    (s, x) => (x.sc.extrinsic_remaining_total != null
      ? (s ?? 0) + Number(x.sc.extrinsic_remaining_total) : s), null);
  const net = longIntrinsic != null && shortIntrinsic != null
    ? longIntrinsic - shortIntrinsic : null;
  // Covered until the short's intrinsic outruns the LEAP's (oversold shorts or a
  // too-shallow LEAP). A small epsilon keeps rounding noise from tripping it.
  const covered = net == null ? null : net >= -1;
  return { longIntrinsic, longExtrinsic, shortIntrinsic, shortExtrinsic, net, covered };
}

// ---------------------------------------------------------------------------
// One tapered tumbler in an 80×112 viewBox. Inner (clip) region: y 13→101,
// so liquid height maps pct onto those 88 units. Pure SVG — no chart lib.
export function Glass({ uid, pct, rollNow }) {
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
// pulp instead of bubbles. ``mini`` renders a thumbnail (per-leg rows of a
// multi-tranche engine): no leaf/seeds/label — the row text carries those.
export function Orange({ uid, pct, maintenance, maintained, mini = false }) {
  const fill = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  const innerTop = 31;
  const innerBottom = 85;
  const surfaceY = innerBottom - ((innerBottom - innerTop) * fill) / 100;
  const leaf = LEAF[maintenance] || LEAF.unknown;

  return (
    <svg
      viewBox="0 0 80 100"
      className={mini
        ? "h-8 w-7 shrink-0"
        : `h-24 w-20 ${maintained ? "drop-shadow-[0_0_10px_rgba(52,211,153,0.35)]" : ""}`}
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
      {!mini && (
        <>
          <rect x="38.6" y="21" width="2.8" height="9" rx="1.4" fill="#78716c" />
          <g transform={`rotate(${leaf.tilt} 41 24)`}>
            <path d="M41 24 Q49 13 60 16 Q53 27 41 24 Z" fill={leaf.fill} opacity="0.9" />
          </g>
        </>
      )}

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
          {fill >= 20 && !mini && (
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
      {!mini && (
        <text x="40" y="63" textAnchor="middle" fontSize="15" fontWeight="700"
              fill="#f8fafc" stroke="#0f172a" strokeWidth="3" paintOrder="stroke">
          {pct == null ? "—" : `${fmt(pct, 0)}%`}
        </text>
      )}
    </svg>
  );
}

// ---------------------------------------------------------------------------
const FLAG_TONE = {
  "self-funding": "border-emerald-500/40 bg-emerald-500/15 text-emerald-300",
  "roll now": "border-emerald-500/40 bg-emerald-500/15 text-emerald-300",
  burning: "border-rose-500/40 bg-rose-500/15 text-rose-300",
  hollow: "border-rose-500/40 bg-rose-500/15 text-rose-300",
  uncovered: "border-rose-500/40 bg-rose-500/15 text-rose-300",
  defend: "border-rose-500/40 bg-rose-500/15 text-rose-300",
  review: "border-rose-500/40 bg-rose-500/15 text-rose-300",
  "kill switch": "border-rose-500/40 bg-rose-500/15 text-rose-300",
  "roll due": "border-amber-500/40 bg-amber-500/15 text-amber-300",
  earnings: "border-amber-500/40 bg-amber-500/15 text-amber-300",
  "wash-sale": "border-amber-500/40 bg-amber-500/15 text-amber-300",
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
export default function JuiceStandCard({ positions, payback, killByTicker, nav }) {
  const rows = React.useMemo(() => {
    const out = (positions || [])
      .map((p) => ({
        p,
        pulp: pulpOf(p),
        shorts: (p.short_calls || []).map((sc) => ({ sc, ...juiceOf(sc), intrinsicCash: intrinsicCashOf(sc) })),
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
  // Intrinsic banked in cash on the short side — the ITM half, separate from the
  // juice. Only surfaced when some short actually sold intrinsic that has moved.
  const intrinsicCash = allShorts.reduce((s, x) => s + (x.intrinsicCash ?? 0), 0);
  const hasIntrinsicCash = allShorts.some(
    (x) => x.intrinsicCash != null && Math.round(x.intrinsicCash) !== 0);
  const backed = rows.reduce((s, r) => s + (r.pulp.intrinsic ?? 0), 0);
  const deployed = rows.reduce((s, r) => s + (r.pulp.basis ?? 0), 0);
  const backedPct = deployed > 0 ? (backed / deployed) * 100 : null;

  // Book-level intrinsic balance: LEAP intrinsic (asset) vs short intrinsic
  // (liability), and the extrinsic left once they net out — juice on the shorts
  // against the LEAPs' burn. Summed only over positions that actually report the
  // pieces, so a partial payload degrades to a smaller-but-honest total.
  const bals = rows.map((r) => balanceOf(r.p, r.shorts));
  const longIntrinsic = bals.reduce((s, b) => s + (b.longIntrinsic ?? 0), 0);
  const shortIntrinsic = bals.reduce((s, b) => s + (b.shortIntrinsic ?? 0), 0);
  const longExtrinsic = bals.reduce((s, b) => s + (b.longExtrinsic ?? 0), 0);
  const shortExtrinsic = bals.reduce((s, b) => s + (b.shortExtrinsic ?? 0), 0);
  const netIntrinsic = longIntrinsic - shortIntrinsic;
  const intrinsicCovered = shortIntrinsic <= longIntrinsic + 1;
  const coverPct = longIntrinsic > 0
    ? (shortIntrinsic / longIntrinsic) * 100
    : (shortIntrinsic > 0 ? 100 : 0);
  const hasBalance = bals.some((b) => b.shortIntrinsic != null && (b.longIntrinsic ?? 0) > 0);

  return (
    <Card
      title={`The juice stand — ${rows.length} position${rows.length === 1 ? "" : "s"}`}
      right={
        <span className="text-xs text-slate-400">
          {"net "}
          <span className="font-semibold text-emerald-300">{money(collected)}</span>
          {" of "}{money(onTable)} squeezed
          {hasIntrinsicCash && (
            <>
              {" · "}
              <span className={`font-semibold ${intrinsicCash >= 0 ? "text-orange-300" : "text-rose-300"}`}>
                {signedMoney(intrinsicCash)}
              </span>
              {" intrinsic"}
            </>
          )}
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

      {/* The short side's other half: intrinsic banked in cash, separate from the
          juice. ITM premium we sold that's melted back as the stock fell (or been
          handed back on a climb — offset by the orange's matching intrinsic gain). */}
      {hasIntrinsicCash && (
        <div className="mb-2 text-[11px] text-slate-500">
          <span className={intrinsicCash >= 0 ? "text-orange-300" : "text-rose-300"}>
            {signedMoney(intrinsicCash)}
          </span>{" "}
          intrinsic captured in cash from shorts — the ITM premium sold, apart from the juice
          {intrinsicCash < 0 && " (net handed back as stock climbed — offset by the orange's intrinsic gain)"}
        </div>
      )}

      {/* Intrinsic balance: does the LEAP intrinsic (asset) cover the short-call
          intrinsic (liability)? When it does, stock moves wash out and the edge is
          the leftover extrinsic — juice on the shorts vs the LEAPs' burn. */}
      {hasBalance && (
        <div className="mb-2 rounded-lg border border-slate-800/60 bg-slate-900/40 p-2">
          <div className="mb-1 flex items-baseline justify-between gap-2 text-[11px]">
            <span className="text-slate-300">Intrinsic balance — LEAPs cover shorts</span>
            <span className={intrinsicCovered ? "text-emerald-300" : "text-rose-300"}>
              {intrinsicCovered ? "covered" : "uncovered"} · net {signedMoney(netIntrinsic)}
            </span>
          </div>
          {/* Short intrinsic as a fraction of long intrinsic — full/overflow = the
              short's intrinsic is catching up to (or outrunning) the LEAP's. */}
          <div className="h-2.5 w-full overflow-hidden rounded-full bg-slate-800">
            <div className={`h-full rounded-full ${intrinsicCovered
                ? "bg-gradient-to-r from-emerald-600 to-emerald-400"
                : "bg-gradient-to-r from-rose-600 to-rose-400"}`}
                 style={{ width: `${Math.max(0, Math.min(100, coverPct))}%` }} />
          </div>
          <div className="mt-1 text-[11px] text-slate-500">
            LEAP intrinsic {money(longIntrinsic)} vs short intrinsic {money(shortIntrinsic)}
            {" — with intrinsic netted out, the edge is extrinsic: "}
            <span className="text-emerald-300">{money(shortExtrinsic)}</span> juice left on the shorts
            {" vs "}
            <span className="text-orange-300">{money(longExtrinsic)}</span> LEAP burn.
          </div>
        </div>
      )}

      {/* One compact tile per position, flowing left to right — the shared
          tile border is what says "this orange feeds these glasses." A ticker
          can only ever hold one tile: the book keeps a single position slot
          per ticker (a re-entry reuses it and starts a fresh cycle). */}
      <div className="flex flex-wrap justify-center gap-3 sm:justify-start">
        {rows.map(({ p, pulp, shorts }) => {
          // Multi-tranche engines carry an aggregated health verdict (burn and
          // extrinsic summed across legs); single-leg falls back to leap_health.
          const lh = p.leap_health_agg || p.leap_health || {};
          const legs = p.leap_legs || [];
          const ks = killByTicker?.[p.ticker];
          const orangeFlags = [];
          // A hollow orange — no intrinsic left at all — outranks the calm
          // self-funding pill: the stock is at/below the LEAP strike and the
          // deployed capital is pure time value.
          if (pulp.pct != null && pulp.pct <= 0) orangeFlags.push("hollow");
          else if (lh.maintenance_status === "self_funding") orangeFlags.push("self-funding");
          const bal = balanceOf(p, shorts);
          // The short's intrinsic has outrun the LEAP's — the hedge no longer
          // covers, and a further rally now costs more on the shorts than the
          // orange makes back (oversold shorts or a too-shallow LEAP).
          if (bal.covered === false) orangeFlags.push("uncovered");
          if (lh.maintenance_status === "burning") orangeFlags.push("burning");
          if (p.needs_review) orangeFlags.push("review");
          if (ks?.alert) orangeFlags.push("kill switch");
          if (p.earnings?.warning) orangeFlags.push("earnings");
          if (lh.roll_due) orangeFlags.push("roll due");
          if (p.wash_sale_flag) orangeFlags.push("wash-sale");
          const juice = lh.trailing_avg_weekly_juice;
          const burn = lh.leap_weekly_burn;
          const rowNet = rowNetOf(p, shorts, payback);
          const pb = payback?.[p.ticker];
          const leap = p.leap || {};

          return (
            <div key={p.ticker}
                 className="flex items-start gap-1 rounded-lg border border-slate-800/60 bg-slate-900/40 p-2">
              {/* The fruit: this position's LEAP engine. */}
              <button
                onClick={() => nav?.focus?.(p.ticker)}
                className="group flex w-28 shrink-0 flex-col items-center rounded-lg px-1 pb-1 transition hover:bg-slate-800/50"
                title={`${p.ticker}${p.sector ? ` (${p.sector})` : ""} — intrinsic ${money(pulp.intrinsic)} of ${money(pulp.basis)} LEAP basis${
                  pulp.pct != null ? ` (${fmt(pulp.pct, 0)}%)` : ""
                }${juice != null && burn != null
                  ? ` · juice ${money(juice)}/wk vs burn ${money(burn)}/wk` : ""
                }${lh.leap_dte != null ? ` · LEAP ${lh.leap_dte} DTE` : ""}${
                  bal.shortIntrinsic != null && bal.longIntrinsic != null
                    ? ` · intrinsic ${bal.covered ? "covered" : "UNCOVERED"}: LEAP ${money(bal.longIntrinsic)} vs short ${money(bal.shortIntrinsic)} (net ${signedMoney(bal.net)})`
                    : ""
                }${bal.shortExtrinsic != null || bal.longExtrinsic != null
                    ? ` · extrinsic: ${money(bal.shortExtrinsic)} juice vs ${money(bal.longExtrinsic)} burn` : ""
                }${
                  rowNet
                    ? ` · all-in ${signedMoney(rowNet.net)} (juice banked ${signedMoney(rowNet.banked)}, LEAP ${signedMoney(rowNet.leapPL)}, open shorts ${signedMoney(rowNet.shortPL)})`
                    : ""
                }`}
              >
                <Orange uid={p.ticker} pct={pulp.pct}
                        maintenance={lh.maintenance_status || "unknown"}
                        maintained={pulp.pct != null && pulp.pct >= 100} />
                <div className="mt-1 text-xs font-semibold text-slate-100 group-hover:text-orange-300">
                  {p.ticker}
                  {p.stock_price != null && (
                    <span className="ml-1 font-normal text-slate-500">{fmt(p.stock_price, 2)}</span>
                  )}
                </div>
                {legs.length > 1 ? (
                  /* The lower level: each tranche is its own mini orange —
                     fill = that leg's intrinsic vs its own cost basis. */
                  <div className="mt-0.5 flex flex-col gap-0.5">
                    {legs.map((leg, i) => {
                      const hp = p.leap_health_legs?.[i] || {};
                      const lpct = leg.intrinsic != null && leg.cost_basis
                        ? (leg.intrinsic / leg.cost_basis) * 100 : null;
                      return (
                        <div key={`${leg.strike}-${leg.expiration}-${i}`}
                             className="flex items-center gap-1 text-[10px] text-slate-400">
                          <Orange uid={`${p.ticker}-leg${i}`} pct={lpct} mini />
                          <span className="whitespace-nowrap">
                            {leg.contracts || 0}×{fmt(leg.strike, 0)}C · {hp.leap_dte ?? leg.dte ?? "—"}d
                            · {lpct == null ? "—" : `${fmt(lpct, 0)}%`}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <div className="text-center text-[10px] text-slate-500">
                    {leap.contracts || 0}×{fmt(leap.strike, 0)}C · {leap.dte ?? "—"}d
                  </div>
                )}
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
                {pb != null && pb.leap_extrinsic_at_entry > 0 && (
                  <div className={`text-[10px] ${pb.pct_complete >= 100 ? "text-emerald-300" : "text-slate-500"}`}>
                    paid back {fmt(pb.pct_complete, 0)}%
                  </div>
                )}
                {rowNet && (
                  <div className={`text-[11px] font-semibold ${rowNet.net >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
                    all-in {signedMoney(rowNet.net)}
                  </div>
                )}
                <FlagRow flags={orangeFlags} />
              </button>

              {/* The squeeze: this orange's glasses, one per open short. */}
              {shorts.length === 0 ? (
                <div className="flex h-24 w-16 items-center text-center text-[10px] italic leading-snug text-slate-500">
                  not being squeezed
                </div>
              ) : (
                <div className="flex max-w-[14.5rem] flex-wrap items-start justify-center gap-x-1 gap-y-2">
                  {shorts.map(({ sc, pct, captured, total, intrinsicCash }, i) => {
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
                        }${
                          intrinsicCash != null && Math.round(intrinsicCash) !== 0
                            ? ` · intrinsic in cash ${signedMoney(intrinsicCash)}` : ""
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
                        {intrinsicCash != null && Math.round(intrinsicCash) !== 0 && (
                          <div className={`text-[10px] ${intrinsicCash >= 0 ? "text-orange-300/80" : "text-rose-300/80"}`}>
                            {signedMoney(intrinsicCash)} intrinsic
                          </div>
                        )}
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
        Orange = the LEAP engine: pulp is intrinsic vs cost basis (full = capital stock-backed),
        leaf is juice-vs-burn (green upright = self-funding, amber droop = burning). An engine
        holding several LEAPs lists each tranche as a mini orange — the big orange is all of
        them together. Glasses = its
        shorts: fill is extrinsic captured since entry; the dashed line is the 75% buyback rule
        — a glass past the line is ready to roll. An ITM short also shows its intrinsic captured in
        cash — the intrinsic half of the premium sold, melting back as the stock falls (or handed
        back on a climb, offset by the orange's matching intrinsic gain). Paid back = how much of
        the LEAP's entry extrinsic this cycle's juice has recovered. All-in = juice banked this
        cycle + LEAP value change + open shorts marked-to-market: is the row making money, glasses
        and orange together? Intrinsic balance = the hedge behind it all: the LEAPs' intrinsic must
        cover the shorts' intrinsic, so stock moves wash out and what's left to play for is the
        extrinsic — juice on the shorts against the LEAPs' burn. An orange flags "uncovered" when its
        short intrinsic outruns its LEAP's.
      </div>
    </Card>
  );
}
