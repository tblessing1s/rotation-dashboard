import React from "react";
import { api } from "../api.js";
import { Card, Pill, Light, fmt } from "./ui.jsx";
import OptionChainModal from "./OptionChainModal.jsx";

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

const BLANK = { action: "buy_leap", strike: "", contracts: 5, execution_price: "", premium_per_share: "", close_price_per_share: "", stock_price: "" };

export default function ExecuteTab({ initialTicker, onExecuted }) {
  const [ticker, setTicker] = React.useState(initialTicker || "");
  const [gate, setGate] = React.useState(null);
  const [roll, setRoll] = React.useState(null);
  const [form, setForm] = React.useState(BLANK);
  const [busy, setBusy] = React.useState(false);
  const [result, setResult] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [chainOpen, setChainOpen] = React.useState(false);
  const [locked, setLocked] = React.useState(null); // confirmed chain pick

  React.useEffect(() => { if (initialTicker) setTicker(initialTicker); }, [initialTicker]);

  const loadGate = React.useCallback(async (t) => {
    if (!t) return;
    setError(null); setGate(null); setRoll(null); setLocked(null); setChainOpen(false);
    try {
      const [g, r] = await Promise.all([api.entryGate(t), api.rollSuggestion(t).catch(() => null)]);
      setGate(g); setRoll(r);
    } catch (e) { setError(e.message); }
  }, []);

  React.useEffect(() => { if (ticker) loadGate(ticker); }, [ticker, loadGate]);

  const ready = gate?.verdict === "READY TO ENTER";
  // Show the chain button once the gate has run. The modal enforces the regime:
  // GREEN 1.5× / YELLOW 2.0× for entries; RED blocks new entries but still opens
  // in management-only mode so an existing position can be closed/rolled to exit.
  const regimeStatus = gate?.levels?.[0]?.detail?.status;
  const canViewChain = !!gate;
  const chainBtnLabel = regimeStatus === "red" ? "Manage Positions (market RED)" : "View Option Chain";

  // Pre-fill the form for a given action from a confirmed chain pick. LEAP fills
  // the buy_leap fields; the chosen weekly strike fills the sell/close fields.
  function applyPick(pick, action) {
    const next = { ...BLANK, action, contracts: form.contracts };
    if (pick.underlying_price != null) next.stock_price = String(pick.underlying_price);
    if (action === "buy_leap" && pick.leap) {
      next.strike = String(pick.leap.strike);
      next.contracts = pick.leap.contracts ?? form.contracts;
      if (pick.leap.mark != null) next.execution_price = String(Math.round(pick.leap.mark * 100 * 100) / 100);
    } else if (pick.weekly) {
      next.strike = String(pick.weekly.strike);
      if (action === "sell_short" && pick.weekly.mark != null) next.premium_per_share = String(pick.weekly.mark);
      if (action === "close_short" && pick.weekly.mark != null) next.close_price_per_share = String(pick.weekly.mark);
    }
    setForm(next);
  }

  function onChainConfirm(pick) {
    setLocked(pick);
    applyPick(pick, form.action);
  }

  function changeAction(action) {
    if (locked) applyPick(locked, action);
    else setForm({ ...form, action });
  }

  // Shared by the form's Execute button and the option-chain modal's one-click
  // execute. Throws on failure so the caller (e.g. the modal) can react.
  async function runExecute(payload) {
    setBusy(true); setError(null); setResult(null);
    try {
      const res = await api.execute(payload);
      setResult(res);
      onExecuted?.();
      return res;
    } catch (e) { setError(e.message); throw e; }
    finally { setBusy(false); }
  }

  async function submit() {
    const payload = { action: form.action, ticker, contracts: Number(form.contracts) || 0 };
    if (form.strike !== "") payload.strike = Number(form.strike);
    if (form.stock_price !== "") payload.stock_price = Number(form.stock_price);
    if (form.action === "buy_leap" && form.execution_price !== "") payload.execution_price = Number(form.execution_price);
    if (form.action === "sell_short" && form.premium_per_share !== "") payload.premium_per_share = Number(form.premium_per_share);
    if (form.action === "close_short" && form.close_price_per_share !== "") payload.close_price_per_share = Number(form.close_price_per_share);
    try { await runExecute(payload); } catch { /* surfaced via error state */ }
  }

  const field = (k) => ({ value: form[k], onChange: (e) => setForm({ ...form, [k]: e.target.value }) });

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
        {gate?.levels?.map((lv) => <GateLevel key={lv.level} lv={lv} />)}
        {gate && (
          <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950 p-3 text-sm">
            Cleared <span className="font-semibold text-emerald-300">{gate.cleared_level}/4</span> levels.{" "}
            {ready ? "READY TO ENTER." : "Gate not cleared — wait."}
          </div>
        )}
      </Card>

      <Card title="Execute">
        {canViewChain && (
          <button
            onClick={() => setChainOpen(true)}
            className={`mb-3 w-full rounded-lg border py-2 text-sm font-semibold ${
              regimeStatus === "red"
                ? "border-rose-700 bg-rose-500/10 text-rose-300 hover:bg-rose-500/20"
                : "border-sky-700 bg-sky-500/10 text-sky-300 hover:bg-sky-500/20"
            }`}
          >
            {chainBtnLabel}
          </button>
        )}
        {locked && (
          <div className="mb-3 flex items-center justify-between rounded-lg border border-emerald-800 bg-emerald-500/5 p-3 text-xs text-emerald-200">
            <span>
              Strikes locked from chain · LEAP{" "}
              <span className="font-semibold">{locked.leap ? fmt(locked.leap.strike, 2) : "—"}</span> · Weekly{" "}
              <span className="font-semibold">{locked.weekly ? fmt(locked.weekly.strike, 2) : "—"}</span>
            </span>
            <button
              onClick={() => { setLocked(null); setForm({ ...BLANK, action: form.action, contracts: form.contracts }); }}
              className="rounded border border-emerald-700 px-2 py-0.5 text-emerald-200 hover:bg-emerald-500/10"
            >
              Reset
            </button>
          </div>
        )}
        {roll && !roll.error && (
          <div className="mb-3 rounded-lg border border-slate-800 bg-slate-950 p-3 text-xs text-slate-400">
            Suggested weekly short strike for {ticker}: <span className="font-semibold text-slate-100">{fmt(roll.suggested_strike, 1)}</span>{" "}
            (price {fmt(roll.stock_price, 2)} − {roll.atr_mult}×ATR {fmt(roll.atr, 2)})
          </div>
        )}
        <div className="grid grid-cols-2 gap-3 text-sm">
          <label className="col-span-2 text-slate-400">Action
            <select value={form.action} onChange={(e) => changeAction(e.target.value)} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-slate-100">
              <option value="buy_leap">Buy LEAP (deep ITM)</option>
              <option value="sell_short">Sell weekly short call</option>
              <option value="close_short">Close / roll short call</option>
            </select>
          </label>
          <label className="text-slate-400">Strike
            <input {...field("strike")} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-slate-100" />
          </label>
          <label className="text-slate-400">Contracts
            <input {...field("contracts")} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-slate-100" />
          </label>
          {form.action === "buy_leap" && (
            <label className="text-slate-400">Price / contract ($)
              <input {...field("execution_price")} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-slate-100" />
            </label>
          )}
          {form.action === "sell_short" && (
            <label className="text-slate-400">Premium / share ($)
              <input {...field("premium_per_share")} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-slate-100" />
            </label>
          )}
          {form.action === "close_short" && (
            <label className="text-slate-400">Close / share ($)
              <input {...field("close_price_per_share")} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-slate-100" />
            </label>
          )}
          <label className="text-slate-400">Stock price (optional)
            <input {...field("stock_price")} placeholder="auto-captured" className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-slate-100" />
          </label>
        </div>
        <button
          onClick={submit}
          disabled={busy || !ticker}
          className="mt-4 w-full rounded-lg bg-emerald-500/20 py-2 text-sm font-semibold text-emerald-300 hover:bg-emerald-500/30 disabled:opacity-40"
        >
          {busy ? "Executing…" : "Execute & log"}
        </button>
        {result && (
          <div className="mt-3 rounded-lg border border-emerald-800 bg-emerald-500/5 p-3 text-xs text-emerald-200">
            Logged {result.execution_id} ({result.mode}) · captured price {fmt(result.captured_price, 2)} · {result.timestamp}
          </div>
        )}
      </Card>

      {chainOpen && (
        <OptionChainModal
          ticker={ticker}
          onConfirm={onChainConfirm}
          onExecute={runExecute}
          onClose={() => setChainOpen(false)}
        />
      )}
    </div>
  );
}
