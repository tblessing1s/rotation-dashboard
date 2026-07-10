import React from "react";
import { api } from "../api.js";
import { Card, Stat, Light, Pill, Meter, Modal, Loading, ErrorState, money, fmt, pct, useApi } from "./ui.jsx";
import JuiceStandCard from "./JuiceStand.jsx";
import ProcessRibbon from "./ProcessRibbon.jsx";
import ReadyToEnter from "./ReadyToEnter.jsx";

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

// The four Genius lights, in vote order, with a short label each.
const LIGHT_ORDER = ["close_vs_ma", "fast_vs_slow", "sar", "momentum"];
const LIGHT_LABELS = {
  close_vs_ma: "Close > MA",
  fast_vs_slow: "Fast > Slow",
  sar: "SAR",
  momentum: "Momentum",
};

// Read-only display of the four lights + the raw vote that produced the regime.
function FourLights({ lights, rawCondition, greenCount }) {
  if (!lights) return null;
  return (
    <div className="mt-4">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs uppercase tracking-wide text-slate-400">Four lights</span>
        {rawCondition != null && (
          <span className="text-xs text-slate-400">
            raw vote <span className="font-semibold text-slate-200">{rawCondition.toUpperCase()}</span>
            {greenCount != null ? ` (${greenCount}/4)` : ""}
          </span>
        )}
      </div>
      <div className="grid grid-cols-4 gap-2">
        {LIGHT_ORDER.map((k) => (
          <div key={k} className="flex flex-col items-center gap-1 rounded-lg border border-slate-700 bg-slate-800/40 px-2 py-2">
            <Light status={lights[k]?.signal || "unknown"} size="h-4 w-4" />
            <span className="text-center text-[10px] leading-tight text-slate-400">{LIGHT_LABELS[k]}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// The dwell countdown (why a yellow is being held) plus SECONDARY breadth/VIX
// context. Breadth/VIX do NOT set the light — they are shown here, neutrally, as
// extra confirmation the operator can weigh. Renders nothing when there's nothing
// noteworthy.
function RegimeDetail({ r }) {
  const d = r.dwell || {};
  const s = r.secondary || {};
  const dwellText =
    d.dwell_day > 0 && d.dwell_min
      ? `YELLOW — day ${d.dwell_day} of ${d.dwell_min} minimum${
          d.held_by_dwell ? ` (raw ${(d.raw_condition || "").toUpperCase()} held)` : ""
        }`
      : null;
  const notes = [];
  if (s.breadth?.diverging)
    notes.push(`breadth ${fmt(s.breadth.value, 0)}% below ${s.breadth.confirm_min}% (not confirming)`);
  if (s.vix?.elevated) notes.push(`VIX ${fmt(s.vix.value, 1)} elevated (> ${s.vix.elevated_above})`);
  if (!dwellText && notes.length === 0) return null;
  return (
    <div className="mt-3 space-y-1">
      {dwellText && <div className="text-xs text-amber-300">⏳ {dwellText}</div>}
      {notes.length > 0 && (
        <div className="text-xs text-slate-400">
          <span className="uppercase tracking-wide text-slate-500">secondary</span> · {notes.join(" · ")}
        </div>
      )}
    </div>
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
      <div className="mt-4 grid grid-cols-2 gap-3">
        <Stat label="Breadth" value={r.breadth != null ? `${fmt(r.breadth, 0)}%` : "—"} />
        <Stat label="VIX" value={fmt(r.vix, 1)} tone={r.vix == null ? "text-slate-500" : "text-slate-100"} />
      </div>
      <FourLights lights={r.lights} rawCondition={r.raw_condition} greenCount={r.vote?.green_count} />
      <RegimeDetail r={r} />
    </Card>
  );
}

// The Dry Powder detail — deployment capacity behind the barrel illustration.
function BookSummary({ capital }) {
  const cap = capital || {};
  return (
    <Card title="The book — capital">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Deployable now" value={money(cap.deployable)} tone="text-sky-300"
              sub={cap.slots_open != null ? `${cap.slots_open} slot${cap.slots_open === 1 ? "" : "s"} open` : undefined} />
        <Stat label="Deployed" value={money(cap.capital_deployed)}
              sub={cap.max_deployed != null ? `of ${money(cap.max_deployed)} cap` : undefined} />
        <Stat label="Operating cash" value={money(cap.operating_cash)}
              sub={cap.operating_cash_source === "schwab" ? "live from Schwab" : undefined} />
        <Stat label="Reserve req." value={money(cap.reserve_required)}
              tone={cap.reserve_ok ? "text-slate-100" : "text-rose-300"}
              sub={cap.reserve_ok ? "funded" : "underfunded"} />
      </div>
    </Card>
  );
}

// The Weekly Juice detail — income behind the glass illustration: the raw
// week/month/YTD figures, the net-per-week projection, the weekly target band,
// and the monthly milestones (all the numbers the ribbon deliberately hides).
function IncomeDetail({ theta, capital, burnDiv }) {
  const t = theta?.totals || {};
  const rollup = theta?.net_juice_rollup || {};
  const wt = theta?.weekly_target || {};
  const ms = (capital || {}).milestones || {};
  return (
    <Card title="Weekly juice & income">
      <div className="grid grid-cols-3 gap-4">
        <Stat label="This week" value={money(t.this_week)} tone="text-emerald-300" />
        <Stat label="This month" value={money(t.this_month)} />
        <Stat label="Juice · YTD" value={money(t.ytd)} />
      </div>
      <div className="mt-4 grid grid-cols-2 gap-4">
        <Stat label="Proj net / wk" value={money(rollup.net_juice_per_week)}
              tone={rollup.net_juice_per_week == null ? "text-slate-500"
                : rollup.net_juice_per_week >= 0 ? "text-emerald-300" : "text-rose-300"}
              sub="juice minus LEAP burn" />
        <Stat label="Weekly target"
              value={wt.target_low != null ? `${money(wt.target_low)}–${money(wt.target_high)}` : "—"}
              sub="1–2% of deployed / wk" />
      </div>
      {burnDiv?.warn && (
        <div className="mt-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          LEAP burn is drifting {fmt(burnDiv.mean_abs_divergence_pct, 0)}% from the model (trailing) —
          over the {burnDiv.threshold_pct}% warn threshold; the net figures may be off.
        </div>
      )}
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

// Monthly income figures want cents, unlike the whole-dollar `money` helper.
function cash(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  return Number(n).toLocaleString(undefined, {
    style: "currency", currency: "USD",
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
}

const PAYOUT_PILL = {
  in_progress: { status: "caution", label: "In progress" },
  finalizable: { status: "ready", label: "Ready to finalize" },
  finalized: { status: "yellow", label: "Finalized · unpaid" },
  paid: { status: "go", label: "Paid" },
  none: { status: "unknown", label: "—" },
};

function payoutSub(m, isCurrent) {
  if (!m) return undefined;
  if (m.paid) return "paid out";
  if (m.finalized) return "finalized · awaiting payout";
  if (m.finalizable) return "ready to finalize";
  if (isCurrent) return "still accruing";
  return "no income";
}

// The monthly payout at a glance: this month's estimated payout + last month's,
// on the landing so "what the payout is going to be" is visible without a click.
// Full finalize/mark-paid controls live on the Payouts tab.
function PayoutGlance({ payouts, onOpen }) {
  const cur = payouts?.current;
  const prev = payouts?.previous;
  if (!cur) return null;
  const curPill = PAYOUT_PILL[cur.status] || PAYOUT_PILL.none;
  return (
    <Card
      title="Monthly payout"
      right={
        <button onClick={onOpen} className="text-xs text-slate-400 hover:text-slate-200">
          Open Payouts →
        </button>
      }
    >
      <div className="grid grid-cols-2 gap-4">
        <div className="min-w-0">
          <div className="mb-1 flex items-center gap-2">
            <span className="text-xs uppercase tracking-wide text-slate-500">
              {cur.label} leftover{cur.estimated ? " · est." : ""}
            </span>
            <Pill status={curPill.status}>{curPill.label}</Pill>
          </div>
          <div className="text-2xl font-semibold leading-tight text-emerald-300">
            {cash(cur.payout_amount)}
          </div>
          <div className="text-xs text-slate-500"
               title="Juice collected minus the LEAP's weekly extrinsic burn">
            {cur.burn_tracked
              ? `${cash(cur.net_juice)} juice − ${cash(cur.leap_burn)} LEAP burn`
              : payoutSub(cur, true)}
          </div>
        </div>
        {prev && (
          <Stat label={`${prev.label} (last month)`} value={cash(prev.payout_amount)}
                sub={payoutSub(prev, false)} />
        )}
      </div>
    </Card>
  );
}

export default function Overview({ onNavigate, onSelectStock, onAction, onRegimeStatus }) {
  // One aggregate call (see /api/overview) instead of stitching regime +
  // positions + theta + kill-switch client-side. Sections are best-effort on
  // the server: a failed one carries {error} without blanking the rest.
  const ov = useApi(api.overview, [], 5 * 60 * 1000);
  const regimeData = ov.data?.regime?.error ? null : ov.data?.regime;

  // Which detail card is popped open over the ribbon (null = none). The ribbon's
  // illustrations carry the high-level read; the rich card is one click away.
  const [detail, setDetail] = React.useState(null);

  // Routing helpers passed down to every clickable signal.
  const nav = React.useMemo(() => ({
    tab: (t) => onNavigate?.(t),
    focus: (ticker) => onAction?.("focus", ticker),
    roll: (ticker, reason) => onAction?.("roll", ticker, reason),
    enter: (ticker) => onSelectStock?.(ticker),
    detail: (key) => setDetail(key),
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
  const payback = ov.data?.theta?.extrinsic_payback || {};
  const burnDiv = ov.data?.burn_divergence || {};

  // Close the modal, then run an action (navigate / focus / enter) — so a link
  // inside a detail card takes you to the full tab without leaving it open.
  const closeThen = (fn) => (...args) => { setDetail(null); fn?.(...args); };

  // Each ribbon illustration opens its rich card here. `tab` adds an
  // "Open <tab> →" link at the foot of the modal for the full view.
  const DETAIL = {
    regime: { node: <RegimeHero regime={regimeData} /> },
    book: { node: <BookSummary capital={cap} />, tab: "Positions" },
    ready: {
      node: <ReadyToEnter onSelectStock={closeThen(nav.enter)} />, tab: "Scan",
    },
    grove: {
      node: <JuiceStandCard positions={openPositions} payback={payback}
                            killByTicker={killByTicker}
                            nav={{ ...nav, focus: closeThen(nav.focus) }} />,
      tab: "Positions",
    },
    juice: { node: <IncomeDetail theta={ov.data?.theta} capital={cap} burnDiv={burnDiv} />, tab: "History" },
  };
  const active = detail ? DETAIL[detail] : null;

  return (
    <div className="grid gap-4">
      {/* The illustrated CFM process ribbon carries the high-level read; each
          stage opens its detailed card in a modal (weather → regime, barrel →
          capital, grove → juice stand, glass → income). */}
      <ProcessRibbon
        capital={capital}
        positions={openPositions}
        killByTicker={killByTicker}
        theta={ov.data?.theta}
        regime={regimeData}
        nav={nav}
      />

      {/* What needs me today — the one list the illustrations can't replace. */}
      {ov.data?.positions?.error ? (
        <Card title="Needs attention"><ErrorState error={ov.data.positions.error} onRetry={ov.reload} /></Card>
      ) : (
        <ActionItems items={actionItems} />
      )}

      {/* This month's estimated payout at a glance → full detail on Payouts. */}
      {!ov.data?.payouts?.error && (
        <PayoutGlance payouts={ov.data?.payouts} onOpen={() => nav.tab("Payouts")} />
      )}

      {active && (
        <Modal onClose={() => setDetail(null)} maxWidth={detail === "grove" ? "max-w-4xl" : "max-w-2xl"}>
          {active.node}
          {active.tab && (
            <div className="mt-3 flex justify-end">
              <button
                onClick={() => { const t = active.tab; setDetail(null); nav.tab(t); }}
                className="rounded-lg border border-slate-700 bg-slate-900/80 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800"
              >
                Open {active.tab} →
              </button>
            </div>
          )}
        </Modal>
      )}
    </div>
  );
}
