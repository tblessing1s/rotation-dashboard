import React from "react";
import { api } from "../api.js";
import { Card, Stat, Light, Pill, Meter, Loading, ErrorState, money, fmt, pct, useApi } from "./ui.jsx";
import JuiceStandCard from "./JuiceStand.jsx";

// The dashboard landing tab: one screen that answers "where does everything
// stand and what needs me today." It leans entirely on existing endpoints
// (regime · positions · theta-ledger · kill-switch · alerts) — the same
// position-derived signals the detail tabs render, gathered into a single
// glance with one-click routing into whichever tab owns the fix.

const REGIME_COPY = {
  green: "Market good — clear to hunt entries.",
  yellow: "Caution — tighten criteria, no fresh risk.",
  red: "Wait — regime risk-off, stand down.",
};

const SEV_RANK = { critical: 0, high: 1, medium: 2, low: 3 };
const SEV_TONE = {
  critical: "border-rose-500/50 bg-rose-500/10 text-rose-200",
  high: "border-amber-500/50 bg-amber-500/10 text-amber-200",
  medium: "border-sky-500/40 bg-sky-500/5 text-sky-200",
  low: "border-slate-700 bg-slate-800/40 text-slate-300",
};
const SEV_PILL = { critical: "red", high: "yellow", medium: "unknown", low: "unknown" };

// Fold the position/kill-switch/capital signals into one severity-ranked list of
// things to act on. Each item carries a go() that routes into the owning tab.
function buildActionItems({ positions, capital, killSwitch }, nav) {
  const items = [];
  const push = (severity, ticker, label, go) => items.push({ severity, ticker, label, go });

  if (capital && capital.reserve_ok === false) {
    push("high", null,
      `Reserve underfunded — ${money(capital.operating_cash)} cash vs ${money(capital.reserve_required)} required`,
      () => nav.tab("Positions"));
  }

  for (const p of positions) {
    const t = p.ticker;
    if (p.needs_review) {
      push("critical", t, `${t} — state diverged from the broker; resolve before trading`,
        () => nav.focus(t));
    }
    if (p.defend) {
      push("high", t, `${t} — stock below the short strike; stage a defensive roll`,
        () => nav.roll(t, "defend"));
    }
    if (p.earnings?.warning) {
      const d = p.earnings.days_until;
      push("high", t,
        `${t} — earnings ${p.earnings.date}${d != null ? ` (${d}d)` : ""}; roll deep-ITM or exit`,
        () => nav.focus(t));
    }
    for (const sc of p.short_calls || []) {
      if (sc.dte != null && sc.dte <= 2) {
        push("high", t, `${t} — short ${fmt(sc.strike, 0)}C expiring (${sc.dte} DTE); roll it`,
          () => nav.roll(t, "expiring"));
      } else if (sc.roll_now) {
        push("medium", t, `${t} — short ${fmt(sc.strike, 0)}C ≥75% decayed; roll to capture juice`,
          () => nav.roll(t, "75%-rule"));
      }
    }
    if (p.leap_health?.roll_due) {
      push("medium", t, `${t} — LEAP roll due${p.leap?.dte != null ? ` (${p.leap.dte} DTE)` : ""}`,
        () => nav.focus(t));
    }
  }

  for (const k of killSwitch || []) {
    if (!k.alert) continue;
    push(k.status === "red" ? "critical" : "high", k.ticker,
      `${k.ticker} — kill switch: ${k.suggested_action}`,
      () => nav.tab("Kill Switch"));
  }

  items.sort((a, b) => (SEV_RANK[a.severity] ?? 9) - (SEV_RANK[b.severity] ?? 9));
  return items;
}

function ActionItems({ items }) {
  if (items.length === 0) {
    return (
      <Card title="Needs attention">
        <p className="text-sm text-emerald-300">All clear — nothing needs action right now.</p>
      </Card>
    );
  }
  return (
    <Card title={`Needs attention — ${items.length}`}>
      <ul className="space-y-2">
        {items.map((it, i) => (
          <li key={i}>
            <button
              onClick={it.go}
              className={`flex w-full items-center gap-3 rounded-lg border px-3 py-2 text-left text-sm transition hover:brightness-125 ${
                SEV_TONE[it.severity] || SEV_TONE.low
              }`}
            >
              <Pill status={SEV_PILL[it.severity]}>{it.severity}</Pill>
              <span className="min-w-0 flex-1 text-slate-100">{it.label}</span>
              <span className="shrink-0 text-xs opacity-70">→</span>
            </button>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function RegimeHero({ regime }) {
  const r = regime || {};
  return (
    <Card title="Market Regime" right={<Pill status={r.status}>{r.status || "—"}</Pill>}>
      <div className="flex items-center gap-4">
        <Light status={r.status} size="h-9 w-9" />
        <div className="min-w-0">
          <div className="text-base font-semibold text-slate-100">{(r.status || "—").toUpperCase()}</div>
          <div className="text-xs text-slate-400">{REGIME_COPY[r.status] || ""}</div>
        </div>
      </div>
      <div className="mt-4 grid grid-cols-3 gap-3">
        <Stat label="Breadth" value={r.breadth != null ? `${fmt(r.breadth, 0)}%` : "—"} />
        <Stat label="VIX" value={fmt(r.vix, 1)} tone={r.vix == null ? "text-slate-500" : "text-slate-100"} />
        <Stat label="SPY" value={(r.spy_trend || "—").toUpperCase()} sub={`MA21 ${fmt(r.spy_dist_ma21, 1)}%`} />
      </div>
    </Card>
  );
}

function BookSummary({ capital }) {
  const cap = capital || {};
  const ms = cap.milestones || {};
  return (
    <Card title="The book">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        <Stat label="Deployed" value={money(cap.capital_deployed)} />
        <Stat label="Operating cash" value={money(cap.operating_cash)}
              sub={cap.operating_cash_source === "schwab" ? "live from Schwab" : undefined} />
        <Stat label="Reserve req." value={money(cap.reserve_required)}
              tone={cap.reserve_ok ? "text-slate-100" : "text-rose-300"}
              sub={cap.reserve_ok ? "funded" : "underfunded"} />
      </div>
      {(ms.half_nut || ms.quit_safe) && (
        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          {["half_nut", "quit_safe"].map((k) => (
            ms[k] && (
              <div key={k}>
                <div className="mb-1 flex justify-between text-xs">
                  <span className="text-slate-300">{k === "half_nut" ? "Half-nut ($/mo)" : "Quit-safe ($/mo)"}</span>
                  <span className="text-slate-400">{money(ms[k].current)} / {money(ms[k].target)}</span>
                </div>
                <Meter pct={ms[k].pct} tone="bg-emerald-500" />
              </div>
            )
          ))}
        </div>
      )}
    </Card>
  );
}

export default function Overview({ onNavigate, onSelectStock, onAction, onRegimeStatus }) {
  // One aggregate call (see /api/overview) instead of stitching regime +
  // positions + theta + kill-switch client-side. Sections are best-effort on
  // the server: a failed one carries {error} without blanking the rest.
  const ov = useApi(api.overview, [], 5 * 60 * 1000);
  const regimeData = ov.data?.regime?.error ? null : ov.data?.regime;

  // Routing helpers passed down to every clickable signal.
  const nav = React.useMemo(() => ({
    tab: (t) => onNavigate?.(t),
    focus: (ticker) => onAction?.("focus", ticker),
    roll: (ticker, reason) => onAction?.("roll", ticker, reason),
    enter: (ticker) => onSelectStock?.(ticker),
  }), [onNavigate, onAction, onSelectStock]);

  // Feed the navbar's regime light (Overview is the landing tab, so the light
  // is lit from first paint).
  React.useEffect(() => {
    if (regimeData?.status) onRegimeStatus?.(regimeData.status);
  }, [regimeData, onRegimeStatus]);

  const allPositions = ov.data?.positions;
  const openPositions = React.useMemo(
    () => (Array.isArray(allPositions) ? allPositions : []).filter((p) => p.status !== "closed"),
    [allPositions],
  );
  const killPositions = Array.isArray(ov.data?.kill_switch) ? ov.data.kill_switch : [];
  const killByTicker = React.useMemo(() => {
    const out = {};
    for (const k of killPositions) out[k.ticker] = k;
    return out;
  }, [killPositions]);

  const capital = ov.data?.capital?.error ? null : ov.data?.capital;
  const actionItems = React.useMemo(
    () => buildActionItems({
      positions: openPositions,
      capital,
      killSwitch: killPositions,
    }, nav),
    [openPositions, capital, killPositions, nav],
  );

  if (ov.loading && !ov.data) {
    return <Card title="Overview"><Loading label="Gathering your dashboard…" /></Card>;
  }
  if (ov.error && !ov.data) {
    return <Card title="Overview"><ErrorState error={ov.error} onRetry={ov.reload} /></Card>;
  }

  const cap = capital || {};
  const juice = ov.data?.theta?.totals || {};
  const payback = ov.data?.theta?.extrinsic_payback || {};

  return (
    <div className="grid gap-4">
      {/* Top KPI band */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <div className="flex items-center justify-between">
            <div className="text-xs uppercase tracking-wide text-slate-500">Regime</div>
            <Light status={regimeData?.status} />
          </div>
          <div className="mt-1 text-2xl font-semibold text-slate-100">
            {(regimeData?.status || "—").toUpperCase()}
          </div>
        </Card>
        <Card>
          <div className="text-xs uppercase tracking-wide text-slate-500">Open positions</div>
          <div className="mt-1 text-2xl font-semibold text-slate-100">{openPositions.length}</div>
        </Card>
        <Card>
          <div className="text-xs uppercase tracking-wide text-slate-500">Net juice · week</div>
          <div className="mt-1 text-2xl font-semibold text-emerald-300">{money(juice.this_week)}</div>
          <div className="text-xs text-slate-500">month {money(juice.this_month)}</div>
        </Card>
        <Card>
          <div className="text-xs uppercase tracking-wide text-slate-500">Juice · YTD</div>
          <div className="mt-1 text-2xl font-semibold text-slate-100">
            {money(cap.juice_ytd ?? juice.ytd)}
          </div>
        </Card>
      </div>

      {ov.data?.positions?.error ? (
        <Card title="Needs attention"><ErrorState error={ov.data.positions.error} onRetry={ov.reload} /></Card>
      ) : (
        <>
          <ActionItems items={actionItems} />
          <JuiceStandCard positions={openPositions} payback={payback}
                          killByTicker={killByTicker} nav={nav} />
        </>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2"><BookSummary capital={cap} /></div>
        <RegimeHero regime={regimeData} />
      </div>
    </div>
  );
}
