import React from "react";
import { api } from "../api.js";
import { Card, Pill, Light, Loading, fmt } from "./ui.jsx";
import OptionChainModal from "./OptionChainModal.jsx";
import { useToast } from "./Toast.jsx";
import { submitOrder } from "../orderFlow.js";

function checkValue(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "string") return v;
  return fmt(v, 1);
}

function GateLevel({ lv }) {
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
      </div>
      <Pill status={lv.pass ? "ready" : "no"}>{lv.pass ? "PASS" : "FAIL"}</Pill>
    </div>
  );
}

export default function ExecuteTab({ initialTicker, onExecuted }) {
  const toast = useToast();
  const [ticker, setTicker] = React.useState(initialTicker || "");
  const [gate, setGate] = React.useState(null);
  const [roll, setRoll] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [chainOpen, setChainOpen] = React.useState(false);
  const [gateLoading, setGateLoading] = React.useState(false);

  React.useEffect(() => { if (initialTicker) setTicker(initialTicker); }, [initialTicker]);

  const loadGate = React.useCallback(async (t) => {
    if (!t) return;
    setError(null); setGate(null); setRoll(null); setChainOpen(false);
    setGateLoading(true);
    try {
      const [g, r] = await Promise.all([api.entryGate(t), api.rollSuggestion(t).catch(() => null)]);
      setGate(g); setRoll(r);
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
        {gate && (
          <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950 p-3 text-sm">
            Cleared <span className="font-semibold text-emerald-300">{gate.cleared_level}/4</span> levels.{" "}
            {ready ? "READY TO ENTER." : "Gate not cleared — wait."}
          </div>
        )}
      </Card>

      <Card title="Execute">
        <p className="mb-3 text-sm text-slate-400">
          Send trades from the live option chain — it auto-detects the next action
          (buy LEAP · sell / close / roll the short · sell the LEAP to exit) from
          your current position and prices the order ticket for you.
        </p>
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
            (price {fmt(roll.stock_price, 2)} − {roll.atr_mult}×ATR {fmt(roll.atr, 2)})
          </div>
        )}
      </Card>

      {chainOpen && (
        <OptionChainModal
          ticker={ticker}
          onExecute={runExecute}
          onClose={() => setChainOpen(false)}
        />
      )}
    </div>
  );
}
