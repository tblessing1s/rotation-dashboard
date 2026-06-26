import React from "react";
import { api } from "../api.js";
import { Card, Pill, Light, fmt, pct } from "./ui.jsx";

function GateLevel({ lv }) {
  return (
    <div className="flex items-start gap-3 border-t border-slate-800 py-2">
      <Light status={lv.pass ? "green" : "red"} />
      <div className="flex-1">
        <div className="text-sm font-medium text-slate-200">
          Level {lv.level}: {lv.name}
        </div>
        <div className="text-xs text-slate-500">
          {lv.level === 1 && lv.detail && `breadth ${fmt(lv.detail.breadth, 0)}% · VIX ${fmt(lv.detail.vix, 1)} · SPY ${lv.detail.spy_trend}`}
          {lv.level === 2 && lv.detail && `${lv.detail.sector || ""} RS3M ${pct(lv.detail.rs3m)} · breadth ${fmt(lv.detail.breadth, 0)}%`}
          {lv.level === 3 && lv.detail && `vs SPY ${pct(lv.detail.rs3m_vs_spy)} · vs Sector ${pct(lv.detail.rs3m_vs_sector)}`}
          {lv.level === 4 && lv.detail && `ATR ${fmt(lv.detail.atr_pct, 1)}% · ${lv.detail.consolidating ? "consolidating" : "extended"}`}
        </div>
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

  React.useEffect(() => { if (initialTicker) setTicker(initialTicker); }, [initialTicker]);

  const loadGate = React.useCallback(async (t) => {
    if (!t) return;
    setError(null); setGate(null); setRoll(null);
    try {
      const [g, r] = await Promise.all([api.entryGate(t), api.rollSuggestion(t).catch(() => null)]);
      setGate(g); setRoll(r);
    } catch (e) { setError(e.message); }
  }, []);

  React.useEffect(() => { if (ticker) loadGate(ticker); }, [ticker, loadGate]);

  const ready = gate?.verdict === "READY TO ENTER";

  async function submit() {
    setBusy(true); setError(null); setResult(null);
    try {
      const payload = { action: form.action, ticker, contracts: Number(form.contracts) || 0 };
      if (form.strike !== "") payload.strike = Number(form.strike);
      if (form.stock_price !== "") payload.stock_price = Number(form.stock_price);
      if (form.action === "buy_leap" && form.execution_price !== "") payload.execution_price = Number(form.execution_price);
      if (form.action === "sell_short" && form.premium_per_share !== "") payload.premium_per_share = Number(form.premium_per_share);
      if (form.action === "close_short" && form.close_price_per_share !== "") payload.close_price_per_share = Number(form.close_price_per_share);
      const res = await api.execute(payload);
      setResult(res);
      onExecuted?.();
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
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
        {roll && !roll.error && (
          <div className="mb-3 rounded-lg border border-slate-800 bg-slate-950 p-3 text-xs text-slate-400">
            Suggested weekly short strike for {ticker}: <span className="font-semibold text-slate-100">{fmt(roll.suggested_strike, 1)}</span>{" "}
            (price {fmt(roll.stock_price, 2)} − {roll.atr_mult}×ATR {fmt(roll.atr, 2)})
          </div>
        )}
        <div className="grid grid-cols-2 gap-3 text-sm">
          <label className="col-span-2 text-slate-400">Action
            <select {...field("action")} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-slate-100">
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
    </div>
  );
}
