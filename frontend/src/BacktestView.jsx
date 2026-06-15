import React, { useState, useEffect, useMemo, useCallback } from "react";
import { C } from "./theme.js";

/* ============================================================================
   BACKTEST VIEW — configure a day-trading setup, run it against stored 5-minute
   bars, and audit the resulting trade log. Talks to the Flask backtest API:
     POST /api/backtest/run | /coverage | /backfill | /configs
   The engine reads the datastore only; "Backfill missing" is the explicit,
   user-triggered pull of intraday history from Schwab (Yahoo fallback).
   ============================================================================ */

const API = "";
const QUICK_TICKERS = ["AMD", "HOOD", "HIMS", "CVNA"];

const SETUP_TYPES = [
  { value: "support_resistance_break", label: "S/R breakout — close beyond yesterday's level (High→Long, Low→Short)" },
  { value: "support_resistance_bounce", label: "S/R bounce — fade yesterday's level (Low→Long, High→Short)" },
];
const ENTRY_TIMINGS = [
  { value: "candle_close", label: "Candle close" },
  { value: "immediate_touch", label: "Immediate touch" },
];
const STOP_LOGICS = [
  { value: "atr_divided_by_2", label: "ATR ÷ 2 beyond level" },
  { value: "fixed_distance", label: "Fixed distance from entry" },
  { value: "just_beyond_level", label: "Just beyond level (%)" },
];

const today = () => new Date().toISOString().slice(0, 10);
const daysAgo = (n) => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10);

const DEFAULT_FORM = {
  tickers: "AMD, HOOD, HIMS, CVNA",
  start: daysAgo(30),
  end: today(),
  setupType: "support_resistance_break",
  useYesterdayLevels: true,
  proximityPct: 0.3,
  volumeMultiplier: 2,
  volAvgLength: 50,
  entryTiming: "candle_close",
  skipFirstN: 0,
  skipIfSpyDown: false,
  skipIfSectorDown: false,
  riskReward: 2,
  stopLogic: "atr_divided_by_2",
  fixedDistance: 0.5,
  bufferPct: 0.1,
  atrPeriod: 14,
  atrTimeframe: "intraday",
  startTime: "08:30",
  endTime: "10:00",
  refineWith1m: true,
};

// Form state -> the JSON config shape the backend validates.
function buildConfig(f) {
  return {
    tickers: String(f.tickers || "").split(",").map((t) => t.trim().toUpperCase()).filter(Boolean),
    date_range: { start: f.start, end: f.end },
    setup_conditions: {
      type: f.setupType,
      use_yesterday_levels: !!f.useYesterdayLevels,
      proximity_pct: Number(f.proximityPct),
    },
    entry_rules: {
      volume_multiplier: Number(f.volumeMultiplier),
      vol_avg_length: Number(f.volAvgLength) || 50,
      entry_timing: f.entryTiming,
    },
    skip_conditions: {
      skip_first_n_candles: Number(f.skipFirstN) || 0,
      skip_if_spy_down: !!f.skipIfSpyDown,
      skip_if_sector_down: !!f.skipIfSectorDown,
    },
    risk_reward: Number(f.riskReward),
    stop_logic: f.stopLogic,
    stop_params: {
      fixed_distance: Number(f.fixedDistance),
      buffer_pct: Number(f.bufferPct),
      atr_period: Number(f.atrPeriod) || 14,
      atr_timeframe: f.atrTimeframe,
    },
    time_window: { start_time: f.startTime, end_time: f.endTime },
    refine_interval_min: f.refineWith1m ? 1 : 0,
  };
}

async function postJson(path, body) {
  const r = await fetch(`${API}${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, data };
}

const OUTCOME_COLOR = { Win: C.green, Loss: C.red, Skip: C.yellow, Unresolved: C.amber };
const DIR_COLOR = { Up: C.green, Down: C.red, Unknown: C.inkFaint };

export default function BacktestView({ store }) {
  const [form, setForm] = useState(() => ({ ...DEFAULT_FORM, ...(store?.get("backtestForm", {}) || {}) }));
  const [running, setRunning] = useState(false);
  const [backfilling, setBackfilling] = useState(false);
  const [errors, setErrors] = useState([]);
  const [result, setResult] = useState(null);
  const [ranConfig, setRanConfig] = useState(null);
  const [coverage, setCoverage] = useState(null);
  const [savedConfigs, setSavedConfigs] = useState({});
  const [configName, setConfigName] = useState("");
  const [status, setStatus] = useState("");

  // Result filters
  const [fTicker, setFTicker] = useState("all");
  const [fOutcome, setFOutcome] = useState("all");
  const [fFrom, setFFrom] = useState("");
  const [fTo, setFTo] = useState("");

  const set = useCallback((k, v) => setForm((prev) => {
    const next = { ...prev, [k]: v };
    store?.set("backtestForm", next);
    return next;
  }), [store]);

  useEffect(() => {
    fetch(`${API}/api/backtest/configs`).then((r) => r.json()).then(setSavedConfigs).catch(() => {});
  }, []);

  const runWith = useCallback(async (config, autoBackfill = false, resetFilters = true) => {
    setRunning(true); setErrors([]); setStatus(autoBackfill ? "Pulling missing data, then running…" : "Running backtest…");
    const { ok, data } = await postJson("/api/backtest/run", { config, autoBackfill });
    setRunning(false);
    if (!ok || data.ok === false) {
      setErrors(data.errors || [data.error || "Backtest failed."]);
      setStatus("");
      return;
    }
    setResult(data.result);
    setRanConfig(data.result.config || config);
    setCoverage(data.coverage || null);
    if (resetFilters) { setFTicker("all"); setFOutcome("all"); setFFrom(""); setFTo(""); }
    const s = data.result.summary;
    setStatus(`Done — ${s.total_trades} trades${s.unresolved ? `, ${s.unresolved} need manual review` : ""}.`);
  }, []);

  const run = useCallback((autoBackfill = false) => runWith(buildConfig(form), autoBackfill), [form, runWith]);

  // Record which way an Unresolved trade actually went (from the chart), then
  // re-run with the same config so the resolution is applied to the stats.
  const resolveTrade = useCallback(async (t, outcome) => {
    await postJson("/api/backtest/resolve", { ticker: t.ticker, date: t.date, entry_time: t.entry_time, outcome });
    if (ranConfig) await runWith(ranConfig, false, false);
  }, [ranConfig, runWith]);

  const checkCoverage = useCallback(async () => {
    setStatus("Checking data coverage…"); setErrors([]);
    const { ok, data } = await postJson("/api/backtest/coverage", { config: buildConfig(form) });
    if (!ok || data.ok === false) { setErrors(data.errors || [data.error || "Coverage check failed."]); setStatus(""); return; }
    setCoverage(data.coverage);
    setStatus(data.coverage.complete ? "All sessions present in the datastore." : `${data.coverage.missing.length} session(s) missing.`);
  }, [form]);

  const backfill = useCallback(async () => {
    setBackfilling(true); setStatus("Backfilling daily + intraday history from Schwab…"); setErrors([]);
    const { ok, data } = await postJson("/api/backtest/backfill", { config: buildConfig(form) });
    setBackfilling(false);
    const bf = data.backfill || {};
    const perSym = bf.perSymbol || {};
    // Surface any per-symbol problem (intraday or daily), even on partial success.
    const msgs = Object.entries(perSym).flatMap(([k, v]) => {
      const out = [];
      if (v.error) out.push(`${k} intraday: ${v.error}`);
      if (v.daily && v.daily.error) out.push(`${k} daily: ${v.daily.error}`);
      return out;
    });
    setCoverage(data.coverage || null);
    if (!ok || data.ok === false) {
      setErrors(msgs.length ? msgs : [data.error || "Backfill failed — Schwab may need re-authorization."]);
      setStatus("");
      return;
    }
    if (msgs.length) setErrors(msgs);
    const wrote = (bf.rowsWritten ?? 0) + (bf.dailyWritten ?? 0);
    const via = bf.providers ? ` via ${bf.providers.join(" → ")}` : "";
    setStatus(wrote === 0 && !msgs.length
      ? `Already up to date — no new bars to pull${via}.`
      : `Backfilled ${bf.rowsWritten ?? 0} intraday candles and ${bf.dailyWritten ?? 0} daily bars${via}.`);
  }, [form]);

  const saveConfig = useCallback(async () => {
    const name = configName.trim();
    if (!name) return;
    const { ok, data } = await postJson("/api/backtest/configs", { name, config: buildConfig(form) });
    if (ok && data.configs) { setSavedConfigs(data.configs); setStatus(`Saved configuration "${name}".`); }
  }, [configName, form]);

  const loadConfig = useCallback((name) => {
    const c = savedConfigs[name];
    if (!c) return;
    setForm({
      tickers: (c.tickers || []).join(", "),
      start: c.date_range?.start || DEFAULT_FORM.start,
      end: c.date_range?.end || DEFAULT_FORM.end,
      setupType: c.setup_conditions?.type || DEFAULT_FORM.setupType,
      useYesterdayLevels: c.setup_conditions?.use_yesterday_levels ?? true,
      proximityPct: c.setup_conditions?.proximity_pct ?? DEFAULT_FORM.proximityPct,
      volumeMultiplier: c.entry_rules?.volume_multiplier ?? DEFAULT_FORM.volumeMultiplier,
      volAvgLength: c.entry_rules?.vol_avg_length ?? DEFAULT_FORM.volAvgLength,
      entryTiming: c.entry_rules?.entry_timing || DEFAULT_FORM.entryTiming,
      skipFirstN: c.skip_conditions?.skip_first_n_candles ?? 0,
      skipIfSpyDown: c.skip_conditions?.skip_if_spy_down ?? false,
      skipIfSectorDown: c.skip_conditions?.skip_if_sector_down ?? false,
      riskReward: c.risk_reward ?? DEFAULT_FORM.riskReward,
      stopLogic: c.stop_logic || DEFAULT_FORM.stopLogic,
      fixedDistance: c.stop_params?.fixed_distance ?? DEFAULT_FORM.fixedDistance,
      bufferPct: c.stop_params?.buffer_pct ?? DEFAULT_FORM.bufferPct,
      atrPeriod: c.stop_params?.atr_period ?? DEFAULT_FORM.atrPeriod,
      atrTimeframe: c.stop_params?.atr_timeframe ?? DEFAULT_FORM.atrTimeframe,
      startTime: c.time_window?.start_time || DEFAULT_FORM.startTime,
      endTime: c.time_window?.end_time || DEFAULT_FORM.endTime,
      refineWith1m: (c.refine_interval_min ?? 1) > 0,
    });
    setStatus(`Loaded configuration "${name}".`);
  }, [savedConfigs]);

  const trades = result?.trades || [];
  const tickerOptions = useMemo(() => Array.from(new Set(trades.map((t) => t.ticker))).sort(), [trades]);
  const filtered = useMemo(() => trades.filter((t) =>
    (fTicker === "all" || t.ticker === fTicker)
    && (fOutcome === "all" || t.outcome === fOutcome)
    && (!fFrom || t.date >= fFrom)
    && (!fTo || t.date <= fTo)
  ), [trades, fTicker, fOutcome, fFrom, fTo]);

  const exportCsv = useCallback(() => {
    const usesAtrStop = ranConfig?.stop_logic === "atr_divided_by_2";
    const cols = ["date", "ticker", "level_type", "volume_spike", "entry_volume", "avg_volume",
      "volume_ratio", "direction", "entry_time", "entry_price", "stop_price", "target_price",
      ...(usesAtrStop ? ["risk_amount", "reward_amount"] : []),
      "exit_time", "exit_price", "outcome", "r_result", "spy_direction", "sector_direction", "notes"];
    const esc = (v) => { const s = v == null ? "" : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
    const rows = [cols.join(","), ...filtered.map((t) => cols.map((c) => esc(t[c])).join(","))];
    const blob = new Blob([rows.join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `backtest_${form.start}_${form.end}.csv`; a.click();
    URL.revokeObjectURL(url);
  }, [filtered, form.start, form.end, ranConfig?.stop_logic]);

  const summary = result?.summary;
  const usesAtrStop = ranConfig?.stop_logic === "atr_divided_by_2";

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <SectionHeader title="Backtest" subtitle="Define a setup, replay it across 5-minute history, and audit every fill." />

      {/* ---- Configuration ---- */}
      <Card>
        <Grid>
          <Field label="Tickers (comma-separated)">
            <Input value={form.tickers} onChange={(e) => set("tickers", e.target.value)} placeholder="AMD, HOOD, HIMS, CVNA" />
            <div style={{ display: "flex", gap: 6, marginTop: 6, flexWrap: "wrap" }}>
              {QUICK_TICKERS.map((t) => (
                <Chip key={t} onClick={() => {
                  const cur = form.tickers.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean);
                  if (!cur.includes(t)) set("tickers", [...cur, t].join(", "));
                }}>+{t}</Chip>
              ))}
            </div>
          </Field>
          <Field label="Start date"><Input type="date" value={form.start} onChange={(e) => set("start", e.target.value)} /></Field>
          <Field label="End date"><Input type="date" value={form.end} onChange={(e) => set("end", e.target.value)} /></Field>

          <Field label="Setup type">
            <Select value={form.setupType} onChange={(e) => set("setupType", e.target.value)} options={SETUP_TYPES} />
          </Field>
          <Field label="Proximity to level (%)" hint="How close price must come to yesterday's level.">
            <Input type="number" step="0.05" value={form.proximityPct} onChange={(e) => set("proximityPct", e.target.value)} />
          </Field>
          <Field label="Use yesterday's levels">
            <Toggle checked={form.useYesterdayLevels} onChange={(v) => set("useYesterdayLevels", v)} label="Y-High / Y-Low" />
          </Field>

          <Field label="Volume spike (×average)" hint="Candle volume must exceed N× the volume average. 0 disables.">
            <Input type="number" step="0.1" value={form.volumeMultiplier} onChange={(e) => set("volumeMultiplier", e.target.value)} />
          </Field>
          <Field label="Volume avg length (bars)" hint="Bars in the volume MA. Matches TOS Average(volume, length); default 50.">
            <Input type="number" value={form.volAvgLength} onChange={(e) => set("volAvgLength", e.target.value)} />
          </Field>
          <Field label="Entry timing"><Select value={form.entryTiming} onChange={(e) => set("entryTiming", e.target.value)} options={ENTRY_TIMINGS} /></Field>
          <Field label="Risk : reward" hint="Target = entry + R×risk.">
            <Input type="number" step="0.5" value={form.riskReward} onChange={(e) => set("riskReward", e.target.value)} />
          </Field>

          <Field label="Stop placement"><Select value={form.stopLogic} onChange={(e) => set("stopLogic", e.target.value)} options={STOP_LOGICS} /></Field>
          {form.stopLogic === "atr_divided_by_2" && (
            <Field label="ATR period (bars)" hint="Number of bars in the ATR.">
              <Input type="number" value={form.atrPeriod} onChange={(e) => set("atrPeriod", e.target.value)} />
            </Field>
          )}
          {form.stopLogic === "atr_divided_by_2" && (
            <Field label="ATR timeframe" hint="Intraday = last N candles (proportional to the trade); Daily = N-day ATR.">
              <Select value={form.atrTimeframe} onChange={(e) => set("atrTimeframe", e.target.value)}
                options={[{ value: "intraday", label: "Intraday candles" }, { value: "daily", label: "Daily" }]} />
            </Field>
          )}
          {form.stopLogic === "fixed_distance" && (
            <Field label="Fixed distance ($)"><Input type="number" step="0.05" value={form.fixedDistance} onChange={(e) => set("fixedDistance", e.target.value)} /></Field>
          )}
          {form.stopLogic === "just_beyond_level" && (
            <Field label="Buffer beyond level (%)"><Input type="number" step="0.05" value={form.bufferPct} onChange={(e) => set("bufferPct", e.target.value)} /></Field>
          )}

          <Field label="Time window — start (CT)" hint="US Central time; CST/CDT is handled automatically."><Input type="time" value={form.startTime} onChange={(e) => set("startTime", e.target.value)} /></Field>
          <Field label="Time window — end (CT)" hint="US Central time; CST/CDT is handled automatically."><Input type="time" value={form.endTime} onChange={(e) => set("endTime", e.target.value)} /></Field>
          <Field label="Skip first N candles"><Input type="number" value={form.skipFirstN} onChange={(e) => set("skipFirstN", e.target.value)} /></Field>

          <Field label="Skip conditions">
            <Toggle checked={form.skipIfSpyDown} onChange={(v) => set("skipIfSpyDown", v)} label="Skip if SPY direction down" />
            <Toggle checked={form.skipIfSectorDown} onChange={(v) => set("skipIfSectorDown", v)} label="Skip if sector direction down" />
          </Field>
          <Field label="Exit resolution" hint="When a 5-min bar hits both stop and target, use 1-min bars to see which came first.">
            <Toggle checked={form.refineWith1m} onChange={(v) => set("refineWith1m", v)} label="Resolve ambiguous exits on 1-min data" />
          </Field>
        </Grid>

        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginTop: 16, paddingTop: 14, borderTop: `1px solid ${C.lineSoft}` }}>
          <Button primary disabled={running || backfilling} onClick={() => run(false)}>{running ? "Running…" : "Run Backtest"}</Button>
          <Button disabled={running || backfilling} onClick={() => run(true)} title="Pull any missing intraday history from Schwab first, then run.">Run + auto-backfill</Button>
          <Button disabled={running || backfilling} onClick={checkCoverage}>Check data coverage</Button>
          <Button disabled={running || backfilling} onClick={backfill}>{backfilling ? "Backfilling…" : "Backfill missing"}</Button>
          <span style={{ flex: 1 }} />
          <Input style={{ width: 150 }} placeholder="config name" value={configName} onChange={(e) => setConfigName(e.target.value)} />
          <Button disabled={!configName.trim()} onClick={saveConfig}>Save config</Button>
          {Object.keys(savedConfigs).length > 0 && (
            <select onChange={(e) => e.target.value && loadConfig(e.target.value)} value=""
              style={selectStyle}>
              <option value="">Load config…</option>
              {Object.keys(savedConfigs).map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          )}
        </div>

        {status && <div style={{ marginTop: 10, font: `400 12px ${C.mono}`, color: C.inkDim }}>{status}</div>}
        {errors.length > 0 && (
          <div style={{ marginTop: 10, padding: 10, border: `1px solid ${C.redDim}`, borderRadius: 8, background: "#1a0e14" }}>
            {errors.map((e, i) => <div key={i} style={{ font: `400 12px ${C.sans}`, color: C.red }}>• {e}</div>)}
          </div>
        )}
      </Card>

      {/* ---- Coverage alert ---- */}
      {coverage && !coverage.complete && (
        <div style={{ padding: 12, border: `1px solid ${C.yellow}`, borderRadius: 8, background: "#1a160c", display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <span style={{ font: `600 13px ${C.sans}`, color: C.yellow }}>⚠ Missing data</span>
          <span style={{ font: `400 12px ${C.mono}`, color: C.inkDim }}>
            {coverage.missingDaily?.length > 0 && (
              <>No <b>daily</b> history for {coverage.missingDaily.join(", ")} (needed for yesterday's level + ATR). </>
            )}
            {coverage.missing?.length > 0 && <>{coverage.missing.length} intraday session(s) absent. </>}
            Backfill to include them.
          </span>
          <span style={{ flex: 1 }} />
          <Button disabled={backfilling} onClick={backfill}>{backfilling ? "Backfilling…" : "Backfill from Schwab"}</Button>
        </div>
      )}

      {/* ---- Summary ---- */}
      {summary && (
        <Card>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 10 }}>
            <Stat label="Trades" value={summary.total_trades} />
            <Stat label="Wins" value={summary.wins} color={C.green} />
            <Stat label="Losses" value={summary.losses} color={C.red} />
            <Stat label="Skips" value={summary.skips} color={C.yellow} />
            {summary.unresolved > 0 && <Stat label="Review" value={summary.unresolved} color={C.amber} />}
            <Stat label="Win rate" value={`${summary.win_rate_percent}%`} color={summary.win_rate_percent >= 50 ? C.green : C.ink} />
            <Stat label="Avg win" value={`${summary.avg_win_r}R`} color={C.green} />
            <Stat label="Avg loss" value={`${summary.avg_loss_r}R`} color={C.red} />
            <Stat label="Expectancy" value={`${summary.expectancy_per_trade}R`} color={summary.expectancy_per_trade >= 0 ? C.green : C.red} />
          </div>
          {result.warnings?.length > 0 && (
            <details style={{ marginTop: 12 }}>
              <summary style={{ cursor: "pointer", font: `400 12px ${C.mono}`, color: C.yellow }}>{result.warnings.length} warning(s)</summary>
              <div style={{ marginTop: 6 }}>
                {result.warnings.map((w, i) => <div key={i} style={{ font: `400 11px ${C.mono}`, color: C.inkDim }}>• {w}</div>)}
              </div>
            </details>
          )}
          {result.diagnostics && <Diagnostics d={result.diagnostics} cov={result.coverage} />}
        </Card>
      )}

      {/* ---- Trade log ---- */}
      {summary && (
        <Card>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginBottom: 12 }}>
            <strong style={{ font: `600 13px ${C.sans}`, color: C.ink }}>Trade log</strong>
            <span style={{ font: `400 12px ${C.mono}`, color: C.inkFaint }}>{filtered.length} of {trades.length}</span>
            <span style={{ flex: 1 }} />
            <select value={fTicker} onChange={(e) => setFTicker(e.target.value)} style={selectStyle}>
              <option value="all">All tickers</option>
              {tickerOptions.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            <select value={fOutcome} onChange={(e) => setFOutcome(e.target.value)} style={selectStyle}>
              {["all", "Win", "Loss", "Skip", "Unresolved"].map((o) => <option key={o} value={o}>{o === "all" ? "All outcomes" : o}</option>)}
            </select>
            <Input type="date" style={{ width: 140 }} value={fFrom} onChange={(e) => setFFrom(e.target.value)} title="From date" />
            <Input type="date" style={{ width: 140 }} value={fTo} onChange={(e) => setFTo(e.target.value)} title="To date" />
            <Button disabled={!filtered.length} onClick={exportCsv}>Export CSV</Button>
          </div>

          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", font: `400 12px ${C.mono}` }}>
              <thead>
                <tr>
                  {["Date", "Ticker", "Level", "Vol↑", "Volume", "Avg vol", "RVOL", "Dir", "Entry time (CT)", "Entry", "Stop", "Target", ...(usesAtrStop ? ["Risk amt", "Reward amt"] : []), "Exit time (CT)", "Exit", "Outcome", "R", "SPY", "Sector", "Notes"].map((h) => (
                    <th key={h} style={thStyle}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 && (
                  <tr><td colSpan={usesAtrStop ? 21 : 19} style={{ ...tdStyle, color: C.inkFaint, textAlign: "center", padding: 18 }}>No trades match the filters.</td></tr>
                )}
                {filtered.map((t, i) => (
                  <tr key={i} style={{ borderTop: `1px solid ${C.lineSoft}` }}>
                    <td style={tdStyle}>{t.date}</td>
                    <td style={{ ...tdStyle, color: C.ink }}>{t.ticker}</td>
                    <td style={tdStyle}>{t.level_type}</td>
                    <td style={{ ...tdStyle, color: t.volume_spike ? C.green : C.inkFaint }}>{t.volume_spike ? "✓" : "·"}</td>
                    <td style={tdStyle}>{fmtVol(t.entry_volume)}</td>
                    <td style={tdStyle}>{fmtVol(t.avg_volume)}</td>
                    <td style={{ ...tdStyle, color: t.volume_ratio >= 2 ? C.green : C.inkDim }}>{t.volume_ratio == null ? "—" : `${t.volume_ratio}×`}</td>
                    <td style={tdStyle}>{t.direction}</td>
                    <td style={tdStyle}>{t.entry_time || "—"}</td>
                    <td style={tdStyle}>{fmt(t.entry_price)}</td>
                    <td style={tdStyle}>{fmt(t.stop_price)}</td>
                    <td style={tdStyle}>{fmt(t.target_price)}</td>
                    {usesAtrStop && <td style={tdStyle}>{fmt(t.risk_amount)}</td>}
                    {usesAtrStop && <td style={tdStyle}>{fmt(t.reward_amount)}</td>}
                    <td style={tdStyle}>{t.exit_time || "—"}</td>
                    <td style={tdStyle}>{fmt(t.exit_price)}</td>
                    <td style={{ ...tdStyle, color: OUTCOME_COLOR[t.outcome] || C.ink, fontWeight: 600 }}>
                      {t.outcome === "Unresolved" ? (
                        <span style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
                          <span title="Check the chart: stop and target printed inside one 1-minute bar.">review</span>
                          <ResolveBtn color={C.green} onClick={() => resolveTrade(t, "Win")}>W</ResolveBtn>
                          <ResolveBtn color={C.red} onClick={() => resolveTrade(t, "Loss")}>L</ResolveBtn>
                        </span>
                      ) : t.outcome}
                    </td>
                    <td style={{ ...tdStyle, color: t.r_result > 0 ? C.green : t.r_result < 0 ? C.red : C.inkDim }}>{t.r_result == null ? "—" : `${t.r_result > 0 ? "+" : ""}${t.r_result}`}</td>
                    <td style={{ ...tdStyle, color: DIR_COLOR[t.spy_direction] || C.inkFaint }}>{t.spy_direction}</td>
                    <td style={{ ...tdStyle, color: DIR_COLOR[t.sector_direction] || C.inkFaint }}>{t.sector_direction}</td>
                    <td style={{ ...tdStyle, color: C.inkFaint, maxWidth: 160, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }} title={t.notes}>{t.notes}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {!summary && (
        <div style={{ padding: 24, textAlign: "center", font: `400 13px ${C.sans}`, color: C.inkFaint, border: `1px dashed ${C.line}`, borderRadius: 10 }}>
          Configure a setup above and run a backtest. Results and a CSV-exportable trade log appear here.
        </div>
      )}
    </div>
  );
}

// ---- Small styled primitives (match the dashboard palette) -----------------
const fmt = (v) => (v == null ? "—" : Number(v).toFixed(2));
const fmtVol = (v) => {
  if (v == null) return "—";
  const n = Number(v);
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
};

const selectStyle = {
  background: C.panel2, color: C.ink, border: `1px solid ${C.line}`, borderRadius: 6,
  padding: "7px 9px", font: `400 12px ${C.sans}`, cursor: "pointer",
};
const thStyle = { textAlign: "left", padding: "7px 8px", font: `600 11px ${C.sans}`, color: C.inkFaint, borderBottom: `1px solid ${C.line}`, whiteSpace: "nowrap" };
const tdStyle = { padding: "6px 8px", color: C.inkDim, whiteSpace: "nowrap" };

function SectionHeader({ title, subtitle }) {
  return (
    <div>
      <h2 style={{ margin: 0, font: `700 18px ${C.sans}`, color: C.ink }}>{title}</h2>
      <div style={{ marginTop: 4, font: `400 13px ${C.sans}`, color: C.inkDim }}>{subtitle}</div>
    </div>
  );
}

function Card({ children }) {
  return <div style={{ background: C.panel, border: `1px solid ${C.line}`, borderRadius: 10, padding: 16 }}>{children}</div>;
}

function Grid({ children }) {
  return <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 14 }}>{children}</div>;
}

function Field({ label, hint, children }) {
  return (
    <label style={{ display: "block" }}>
      <div style={{ font: `600 11px ${C.sans}`, color: C.inkDim, marginBottom: 5, textTransform: "uppercase", letterSpacing: 0.3 }}>{label}</div>
      {children}
      {hint && <div style={{ marginTop: 4, font: `400 10px ${C.sans}`, color: C.inkFaint }}>{hint}</div>}
    </label>
  );
}

function Input({ style, ...props }) {
  return <input {...props} style={{
    width: "100%", boxSizing: "border-box", background: C.panel2, color: C.ink,
    border: `1px solid ${C.line}`, borderRadius: 6, padding: "8px 9px", font: `400 13px ${C.mono}`, ...style,
  }} />;
}

function Select({ value, onChange, options }) {
  return (
    <select value={value} onChange={onChange} style={{ ...selectStyle, width: "100%" }}>
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

function Toggle({ checked, onChange, label }) {
  return (
    <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", padding: "4px 0" }}>
      <input type="checkbox" checked={!!checked} onChange={(e) => onChange(e.target.checked)} />
      <span style={{ font: `400 12px ${C.sans}`, color: C.inkDim }}>{label}</span>
    </label>
  );
}

function Button({ children, primary, ...props }) {
  return (
    <button {...props} style={{
      background: primary ? C.blue : C.panel2, color: primary ? "#fff" : C.ink,
      border: `1px solid ${primary ? C.blue : C.line}`, borderRadius: 7, padding: "8px 14px",
      font: `600 12px ${C.sans}`, cursor: props.disabled ? "not-allowed" : "pointer", opacity: props.disabled ? 0.5 : 1,
    }}>{children}</button>
  );
}

function ResolveBtn({ children, color, onClick }) {
  return (
    <button onClick={onClick} title={children === "W" ? "Mark as Win" : "Mark as Loss"} style={{
      background: "transparent", color, border: `1px solid ${color}`, borderRadius: 4,
      width: 18, height: 18, lineHeight: "16px", padding: 0, cursor: "pointer",
      font: `700 10px ${C.mono}`,
    }}>{children}</button>
  );
}

function Chip({ children, onClick }) {
  return (
    <button onClick={onClick} style={{
      background: C.panel2, color: C.inkDim, border: `1px solid ${C.line}`, borderRadius: 20,
      padding: "3px 10px", font: `500 11px ${C.mono}`, cursor: "pointer",
    }}>{children}</button>
  );
}

// Explains why a run produced the trades it did — especially a 0-trade run.
function Diagnostics({ d, cov }) {
  const tip = (() => {
    if (d.setups_detected === 0 && d.level_touches === 0)
      return "Price never reached yesterday's level in the time window. Widen the time window or raise proximity %.";
    if (d.setups_detected === 0 && d.volume_spikes === 0)
      return "Levels were touched but no candle hit the volume multiplier. Lower the volume multiplier (try 1.5 or 1.0, or 0 to disable).";
    if (d.setups_detected === 0)
      return "Touches and volume spikes occurred, but never on the same candle. Loosen proximity % or the volume multiplier.";
    if (d.setups_skipped > 0)
      return `${d.setups_skipped} setup(s) were filtered out by skip conditions.`;
    return null;
  })();
  return (
    <div style={{ marginTop: 12, paddingTop: 10, borderTop: `1px solid ${C.lineSoft}` }}>
      <div style={{ font: `400 11px ${C.mono}`, color: C.inkFaint }}>
        Scanned {d.candles_evaluated} candles across {cov?.covered ?? "?"} session(s)
        {" · "}{d.level_touches} level touch{d.level_touches === 1 ? "" : "es"}
        {" · "}{d.volume_spikes} volume spike{d.volume_spikes === 1 ? "" : "s"}
        {" · "}{d.setups_detected} setup{d.setups_detected === 1 ? "" : "s"}
        {d.setups_skipped ? ` · ${d.setups_skipped} skipped` : ""}
        {d.ambiguous_bars ? ` · ${d.refined_bars}/${d.ambiguous_bars} ambiguous exits resolved on 1m` : ""}
      </div>
      {tip && <div style={{ marginTop: 5, font: `400 12px ${C.sans}`, color: C.yellow }}>💡 {tip}</div>}
    </div>
  );
}

function Stat({ label, value, color }) {
  return (
    <div style={{ background: C.panel2, border: `1px solid ${C.line}`, borderRadius: 8, padding: "10px 12px" }}>
      <div style={{ font: `600 10px ${C.sans}`, color: C.inkFaint, textTransform: "uppercase", letterSpacing: 0.4 }}>{label}</div>
      <div style={{ marginTop: 4, font: `700 20px ${C.mono}`, color: color || C.ink }}>{value}</div>
    </div>
  );
}
