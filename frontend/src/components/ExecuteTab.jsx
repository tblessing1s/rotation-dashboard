import React from "react";
import { api } from "../api.js";
import { Card, Pill, Light, Loading, GENIUS_LIGHT_ORDER, GENIUS_LIGHT_LABELS, fmt } from "./ui.jsx";
import OptionChainModal from "./OptionChainModal.jsx";
import { useToast } from "./Toast.jsx";
import { submitOrder } from "../orderFlow.js";
import { useTradeMode, TradeModeBadge } from "../tradeMode.jsx";

function checkValue(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "string") return v;
  return fmt(v, 1);
}

// The per-name Genius four-light row — mirrors the market regime's FourLights UI
// (Overview.jsx), applied to a single stock. One indicator system, fractal across
// market and stock. Uses the shared light order/labels from ui.jsx.
function StockFourLights({ lights, greens, verdict }) {
  if (!lights) return null;
  return (
    <div className="mt-2">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs uppercase tracking-wide text-slate-400">Stock lights</span>
        <span className="flex items-center gap-2 text-xs text-slate-400">
          {greens != null ? `${greens}/4 green` : ""}
          {verdict ? <Pill status={verdict}>{verdict}</Pill> : null}
        </span>
      </div>
      <div className="grid grid-cols-4 gap-2">
        {GENIUS_LIGHT_ORDER.map((k) => (
          <div key={k} className="flex flex-col items-center gap-1 rounded-lg border border-slate-700 bg-slate-800/40 px-2 py-2">
            <Light status={lights[k]?.signal || "unknown"} size="h-4 w-4" />
            <span className="text-center text-[10px] leading-tight text-slate-400">{GENIUS_LIGHT_LABELS[k]}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// Veto banners — any tripped veto forces the stock verdict to RED, independent of
// the lights. Shows every veto that fired, worst-signal-wins.
const VETO_LABELS = {
  rs3m_vs_sector: "RS3M vs Sector negative (weaker than its own sector)",
  atr_expanding_high_ivr: "ATR expanding into rich IV (IVR ≥ threshold)",
  close_below_ma200: "Close below MA200 (trend broken)",
};

function VetoBanners({ vetoes }) {
  const tripped = (vetoes || []).filter((v) => v.tripped);
  if (!tripped.length) return null;
  return (
    <div className="mt-2 space-y-1">
      {tripped.map((v) => (
        <div key={v.id} className="flex items-center gap-2 rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-xs text-rose-300">
          <span className="font-semibold uppercase tracking-wide">Veto</span>
          <span>{VETO_LABELS[v.id] || v.id}</span>
        </div>
      ))}
    </div>
  );
}

// The separate "Right Spot" card — the consolidation gate that runs AFTER the
// lights (not a light). Blocking; identical for stocks and ETFs.
function RightSpotCard({ spot }) {
  if (!spot) return null;
  return (
    <div className="mt-2 rounded-lg border border-slate-700 bg-slate-800/30 px-3 py-2">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs uppercase tracking-wide text-slate-400">Right Spot</span>
        <Pill status={spot.pass ? "ready" : "no"}>{spot.pass ? "IN SPOT" : "BLOCKED"}</Pill>
      </div>
      <div className="space-y-0.5">
        {(spot.checks || []).map((c) => (
          <div key={c.id} className="flex items-center gap-2 text-xs">
            <span className={c.pass ? "text-emerald-400" : "text-rose-400"}>{c.pass ? "✓" : "✗"}</span>
            <span className="text-slate-400">{c.id}</span>
            <span className="text-slate-500">({checkValue(c.value)})</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function GateLevel({ lv }) {
  const d = lv.detail || {};
  const isStockLights = lv.level === 3;
  const isRightSpot = lv.level === 4;
  return (
    <div className="flex items-start gap-3 border-t border-slate-800 py-2">
      <Light status={lv.pass ? "green" : "red"} />
      <div className="flex-1">
        <div className="text-sm font-medium text-slate-200">
          Level {lv.level}: {lv.name}
        </div>
        {/* Per-condition sub-checks: each leg is flagged on its own so a level
            FAIL is never ambiguous about which condition missed. */}
        {lv.checks?.length ? (
          <div className="mt-1 space-y-0.5">
            {lv.checks.map((c, i) => (
              <div key={i} className="flex items-center gap-2 text-xs">
                <span className={c.pass ? "text-emerald-400" : "text-rose-400"}>{c.pass ? "✓" : "✗"}</span>
                <span className="text-slate-400">{c.label}</span>
                <span className="text-slate-500">({checkValue(c.value)})</span>
              </div>
            ))}
          </div>
        ) : null}
        {/* Level 3 = the per-name Genius lights + verdict pill + veto banners. */}
        {isStockLights && (
          <>
            <StockFourLights lights={d.lights} greens={d.greens} verdict={d.verdict} />
            <VetoBanners vetoes={d.vetoes} />
          </>
        )}
        {/* Level 4 = the separate Right Spot gate card. */}
        {isRightSpot && <RightSpotCard spot={d.right_spot} />}
      </div>
      <Pill status={lv.pass ? "ready" : "no"}>{lv.pass ? "PASS" : "FAIL"}</Pill>
    </div>
  );
}

// Level 5 — Account & Juice: is the ACCOUNT ready and does the TRADE pay.
// Blocking failures stop the entry server-side (override requires a typed,
// logged reason inside the order ticket).
function AccountGate({ gate }) {
  if (!gate) return null;
  return (
    <div className="flex items-start gap-3 border-t border-slate-800 py-2">
      <Light status={gate.pass ? "green" : "red"} />
      <div className="flex-1">
        <div className="text-sm font-medium text-slate-200">Level 5: Account &amp; Juice</div>
        <div className="mt-1 space-y-0.5">
          {gate.checks?.map((c) => (
            <div key={c.id} className="flex items-center gap-2 text-xs">
              <span className={c.pass ? "text-emerald-400" : c.blocking ? "text-rose-400" : "text-amber-400"}>
                {c.pass ? "✓" : c.blocking ? "✗" : "!"}
              </span>
              <span className="text-slate-400">{c.label}</span>
              {c.id === "cash_reserve" && c.detail?.free_cash_after != null && (
                <span className="text-slate-500">
                  (free after: ${fmt(c.detail.free_cash_after, 0)} vs reserve ${fmt(c.detail.reserve_required, 0)}
                  {c.detail.operating_cash_source && `, ${c.detail.operating_cash_source} cash`})
                </span>
              )}
              {c.id === "juice_adequacy" && c.detail?.weekly_yield_pct != null && (
                <span className="text-slate-500">
                  ({fmt(c.detail.weekly_yield_pct, 2)}%/wk, {c.detail.source})
                </span>
              )}
            </div>
          ))}
        </div>
        {gate.suggested_circuit_breaker?.price != null && (
          <p className="mt-1 text-xs text-slate-500">
            Suggested circuit breaker (line in the sand): <span className="font-semibold text-slate-300">
            {fmt(gate.suggested_circuit_breaker.price, 2)}</span> = max(MA50 {fmt(gate.suggested_circuit_breaker.ma50, 2)},
            price − 2×ATR {fmt(gate.suggested_circuit_breaker.atr_stop, 2)})
          </p>
        )}
      </div>
      <Pill status={gate.pass ? "ready" : "no"}>{gate.pass ? "PASS" : "BLOCKED"}</Pill>
    </div>
  );
}

export default function ExecuteTab({ initialTicker, onExecuted, onBack }) {
  const toast = useToast();
  const [ticker, setTicker] = React.useState(initialTicker || "");
  const [gate, setGate] = React.useState(null);
  const [acctGate, setAcctGate] = React.useState(null);
  const [roll, setRoll] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [chainOpen, setChainOpen] = React.useState(false);
  const [gateLoading, setGateLoading] = React.useState(false);
  const tradeMode = useTradeMode(); // "paper" | "live" | null — where do executed orders go?

  React.useEffect(() => { if (initialTicker) setTicker(initialTicker); }, [initialTicker]);

  const loadGate = React.useCallback(async (t) => {
    if (!t) return;
    setError(null); setGate(null); setAcctGate(null); setRoll(null); setChainOpen(false);
    setGateLoading(true);
    try {
      const [g, a, r] = await Promise.all([
        api.entryGate(t),
        api.accountGate(t).catch(() => null),
        api.rollSuggestion(t).catch(() => null),
      ]);
      setGate(g); setAcctGate(a); setRoll(r);
    } catch (e) { setError(e.message); }
    finally { setGateLoading(false); }
  }, []);

  React.useEffect(() => { if (ticker) loadGate(ticker); }, [ticker, loadGate]);

  const ready = gate?.verdict === "READY TO ENTER";
  // Show the chain button once the gate has run. The modal enforces the regime:
  // GREEN 1.5× / YELLOW 2.0× for entries; RED blocks new entries but still opens
  // in management-only mode so an existing position can be closed/rolled to exit.
  const regimeStatus = gate?.levels?.[0]?.detail?.status;
  const canViewChain = !!gate;
  const chainBtnLabel = regimeStatus === "red" ? "Manage Positions (market RED)" : "View Option Chain";

  // All execution flows through the option chain modal (it builds + sends the
  // order ticket); submitOrder drives the toast lifecycle (submit → fill/cancel)
  // and we refresh the dependent tabs on success.
  async function runExecute(payload) {
    const res = await submitOrder(api, toast, payload);
    onExecuted?.();
    return res;
  }

  return (
    <div className="grid gap-4">
      {onBack && (
        <div>
          <button
            onClick={onBack}
            className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-1.5 text-sm text-slate-400 hover:bg-slate-900/70 hover:text-slate-200"
          >
            ← Back
          </button>
        </div>
      )}
      <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Entry Gate" right={gate ? <Pill status={ready ? "ready" : "wait"}>{gate.verdict}</Pill> : null}>
        <div className="mb-3 flex gap-2">
          <input
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            placeholder="Ticker (e.g. ON)"
            className="w-40 rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-slate-100"
          />
          <button onClick={() => loadGate(ticker)} className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800">
            Run gate
          </button>
        </div>
        {error && <p className="text-sm text-rose-400">{error}</p>}
        {gateLoading && <Loading label="Running gate…" />}
        {gate?.levels?.map((lv) => <GateLevel key={lv.level} lv={lv} />)}
        {gate && <AccountGate gate={acctGate} />}
        {gate && (
          <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950 p-3 text-sm">
            Cleared <span className="font-semibold text-emerald-300">{gate.cleared_level}/4</span> levels
            {acctGate ? (
              acctGate.pass
                ? <> · Level 5 <span className="font-semibold text-emerald-300">PASS</span></>
                : <> · Level 5 <span className="font-semibold text-rose-300">BLOCKED</span></>
            ) : null}
            . {ready ? "READY TO ENTER." : "Gate not cleared — wait."}
          </div>
        )}
      </Card>

      <Card title="Execute" right={<TradeModeBadge mode={tradeMode} />}>
        <p className="mb-3 text-sm text-slate-400">
          Send trades from the live option chain — it auto-detects the next action
          (buy LEAP · sell / close / roll the short · sell the LEAP to exit) from
          your current position and prices the order ticket for you.
        </p>
        {tradeMode === "paper" && (
          <p className="mb-3 rounded-lg border border-amber-700 bg-amber-500/5 px-3 py-2 text-xs text-amber-300">
            <span className="font-semibold">Paper mode.</span> Trades are captured to your
            ledger at live prices but <span className="font-semibold">no order is sent to Schwab</span>.
            Enable <code className="text-amber-200">CFM_LIVE_TRADING</code> and connect Schwab to trade live.
          </p>
        )}
        {canViewChain ? (
          <button
            onClick={() => setChainOpen(true)}
            className={`w-full rounded-lg border py-2 text-sm font-semibold ${
              regimeStatus === "red"
                ? "border-rose-700 bg-rose-500/10 text-rose-300 hover:bg-rose-500/20"
                : "border-sky-700 bg-sky-500/10 text-sky-300 hover:bg-sky-500/20"
            }`}
          >
            {chainBtnLabel}
          </button>
        ) : (
          <p className="text-sm text-slate-500">Run the entry gate for a ticker above to load its option chain.</p>
        )}
        {roll && !roll.error && (
          <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950 p-3 text-xs text-slate-400">
            Suggested weekly short strike for {ticker}: <span className="font-semibold text-slate-100">{fmt(roll.suggested_strike, 1)}</span>{" "}
            ({roll.regime ? `${roll.regime.toUpperCase()} / ` : ""}{roll.posture}: {roll.atr_mult}×ATR {fmt(roll.atr, 2)}
            {roll.itm_pct != null ? ` / ${(roll.itm_pct * 100).toFixed(0)}% ITM floor` : ""})
          </div>
        )}
      </Card>

      {chainOpen && (
        <OptionChainModal
          ticker={ticker}
          accountGate={acctGate}
          onExecute={runExecute}
          onClose={() => setChainOpen(false)}
        />
      )}
      </div>
    </div>
  );
}
