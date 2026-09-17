import React from "react";
import { api } from "../api.js";
import { Card, Meter, Pill, Spinner, ErrorState, Stat, useApi } from "./ui.jsx";

// Day-trade sleeve (backend/daytrade/) — a SEPARATE, rules-based intraday
// strategy for capital too small to fit a CFM position. The rotation regime
// gate does not feed into it, and paper mode never places a real order — see
// PaperAdapter in daytrade/adapters.py. PER ACCOUNT (daytrade/settings.py):
// one book can run the trial while another sits out entirely, so App.jsx
// keys this component on the active account (remount on switch — the same
// convention Overview/PositionTracker/HistoryTab use) and every fetch here
// rides the X-CFM-Account header like the rest of the app.

const todayISO = () => new Date().toISOString().slice(0, 10);
const addDays = (iso, n) => {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
};

const timeOf = (iso) => (iso ? iso.slice(11, 16) : "—");
const money = (n) =>
  n == null ? "—" : `${n < 0 ? "-" : ""}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const rMult = (n) => (n == null ? "—" : `${n >= 0 ? "+" : ""}${n.toFixed(2)}R`);
const toneFor = (n) => (n == null ? "text-slate-300" : n > 0 ? "text-emerald-300" : n < 0 ? "text-rose-300" : "text-slate-300");

// Every event this panel can see is the engine following its own rules
// exactly (paper mode has no manual-override path yet) — so each maps to a
// Pill status that reads as "expected", never as a warning, including the
// skips: a guardrail skip IS the rule working, not a miss.
const EVENT_META = {
  setup: { label: "setup", status: "watch" },
  setup_skipped: { label: "setup · skipped", status: "unknown" },
  entry: { label: "entry", status: "go" },
  entry_skipped: { label: "entry · skipped", status: "unknown" },
  expired: { label: "expired", status: "unknown" },
  half_target: { label: "half target", status: "go" },
  breakeven_exit: { label: "breakeven exit", status: "caution" },
  final_target: { label: "final target", status: "go" },
  stop_out: { label: "stop out", status: "avoid" },
  time_cutoff: { label: "time cutoff", status: "caution" },
};

function detailFor(e) {
  switch (e.event) {
    case "setup":
      return `${e.high}/${e.low} on ${e.volume?.toLocaleString()} vol (avg ${e.avg_volume?.toLocaleString()})`;
    case "setup_skipped":
    case "entry_skipped":
      return e.reason || "—";
    case "expired":
      return "no break within the entry window";
    case "entry":
      return `@${e.entry} · ${e.size} sh · stop ${e.stop} · targets ${e.target1}/${e.target2}`;
    default:
      return e.price != null ? `@${e.price} · ${rMult(e.r)}` : rMult(e.r);
  }
}

function UniverseTable({ picks }) {
  if (!picks?.length) {
    return <p className="text-[11px] text-slate-500">No qualifying names for this date.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-sm">
        <thead>
          <tr className="text-left text-[10px] uppercase tracking-wide text-slate-500">
            <th className="py-1 pr-3">Symbol</th>
            <th className="py-1 pr-3 text-right">Price</th>
            <th className="py-1 pr-3 text-right">Avg Volume</th>
            <th className="py-1 pr-3 text-right">ATR14</th>
            <th className="py-1 pr-3 text-right">ATR%</th>
            <th className="py-1 pr-3 text-right">Prior High</th>
            <th className="py-1 pr-3 text-right">Prior Low</th>
          </tr>
        </thead>
        <tbody>
          {picks.map((p) => (
            <tr key={p.symbol} className="border-t border-slate-800 text-slate-200">
              <td className="py-1.5 pr-3 font-mono font-semibold">{p.symbol}</td>
              <td className="py-1.5 pr-3 text-right font-mono">{p.price}</td>
              <td className="py-1.5 pr-3 text-right font-mono">{p.avg_volume?.toLocaleString()}</td>
              <td className="py-1.5 pr-3 text-right font-mono">{p.atr14}</td>
              <td className="py-1.5 pr-3 text-right font-mono">{p.atr_pct}%</td>
              <td className="py-1.5 pr-3 text-right font-mono text-slate-400">{p.prior_day_high}</td>
              <td className="py-1.5 pr-3 text-right font-mono text-slate-400">{p.prior_day_low}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// On-demand rescan of the day-trade screener — a detached server-side job
// (daytrade/universe.py's start_background_screen), same shape as CFM's own
// ScanProgress: POST kicks it off, a short poll follows it to completion.
// Only meaningful for TODAY (the screener always (re)writes today's picks
// file), so the caller only renders this when the panel's selected date is
// today.
function RescanButton({ onComplete }) {
  const [st, setSt] = React.useState(null);
  const pollRef = React.useRef(null);

  const poll = React.useCallback(async () => {
    let s;
    try { s = await api.daytradeUniverseRescanStatus(); } catch { return; } // transient — next tick retries
    setSt(s);
    if (!s.running) {
      clearInterval(pollRef.current);
      pollRef.current = null;
      if (s.status === "done") onComplete?.();
    }
  }, [onComplete]);

  const rescan = async () => {
    try {
      const s = await api.daytradeUniverseRescan();
      setSt(s);
      if (s.running && !pollRef.current) pollRef.current = setInterval(poll, 2500);
    } catch (e) {
      setSt({ status: "error", error: String(e.message || e) });
    }
  };

  React.useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const busy = !!st?.running;
  return (
    <div className="flex items-center gap-2">
      {busy && <span className="text-[11px] text-amber-300">Rescanning…</span>}
      {!busy && st?.status === "error" && (
        <span className="text-[11px] text-rose-300" title={st.error}>Rescan failed</span>
      )}
      <button
        onClick={rescan}
        disabled={busy}
        title="Force the screener to run again right now against the current day-trade universe"
        className="rounded border border-slate-700 px-2 py-0.5 text-[11px] text-slate-400 hover:text-slate-200 disabled:opacity-40"
      >
        {busy ? <Spinner size="h-3 w-3" /> : "Rescan now"}
      </button>
    </div>
  );
}

// Pulls ticker symbols out of a pasted/uploaded CSV: takes each line's first
// cell, keeps only ones shaped like a ticker (letters, optional ".B"-style
// share class), drops a leading header cell ("Symbol"/"Ticker"/…), and
// dedupes. Deliberately simple (no quoted-comma handling) — good enough for
// a plain watchlist export, which is what this is for.
const CSV_HEADER_CELLS = new Set(["SYMBOL", "TICKER", "STOCK", "STOCKS", "NAME"]);
function parseCsvTickers(text) {
  const tokens = [];
  for (const line of text.split(/\r?\n/)) {
    const cell = line.split(",")[0].trim().replace(/^"|"$/g, "");
    if (!cell) continue;
    const t = cell.toUpperCase();
    if (/^[A-Z]{1,6}(\.[A-Z]{1,3})?$/.test(t)) tokens.push(t);
  }
  if (tokens.length && CSV_HEADER_CELLS.has(tokens[0])) tokens.shift();
  return Array.from(new Set(tokens));
}

// The day-trade sleeve's OWN ticker roster (backend/daytrade/tickers.py) —
// separate from CFM's universe: seeded from it once, independent from then
// on. Collapsed by default (the roster can run into the hundreds) with a
// client-side filter so managing it doesn't mean scrolling a wall of chips.
function TickerRoster() {
  const [open, setOpen] = React.useState(false);
  const { data, error, loading, reload } = useApi(() => api.daytradeTickers(), [], null);
  const [filter, setFilter] = React.useState("");
  const [newTicker, setNewTicker] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);

  const addTicker = async () => {
    if (!newTicker.trim()) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.daytradeTickersAdd(newTicker.trim());
      setMsg({ ok: `Added ${r.added}` });
      setNewTicker("");
      await reload();
    } catch (e) {
      setMsg({ err: String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };

  const removeTicker = async (ticker) => {
    setMsg(null);
    try {
      await api.daytradeTickersRemove(ticker);
      await reload();
    } catch (e) {
      setMsg({ err: String(e.message || e) });
    }
  };

  const fileInputRef = React.useRef(null);
  const importCsv = async (file) => {
    if (!file) return;
    setBusy(true);
    setMsg(null);
    try {
      const text = await file.text();
      const parsed = parseCsvTickers(text);
      if (!parsed.length) {
        setMsg({ err: "No ticker-looking symbols found in that file" });
        return;
      }
      const r = await api.daytradeTickersAddBulk(parsed);
      setMsg({
        ok: `Imported ${r.added.length} of ${parsed.length}` +
          (r.skipped.length ? ` (${r.skipped.length} already in the universe)` : ""),
      });
      await reload();
    } catch (e) {
      setMsg({ err: String(e.message || e) });
    } finally {
      setBusy(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const shown = (data?.tickers || []).filter((t) => t.includes(filter.trim().toUpperCase()));

  return (
    <section>
      <button
        onClick={() => setOpen((o) => !o)}
        className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-slate-300 hover:text-slate-100"
      >
        <span className="text-slate-500">{open ? "▾" : "▸"}</span>
        Day-trade universe{data ? ` (${data.total})` : ""}
      </button>
      {open && (
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-3">
          <p className="mb-2 text-[11px] text-slate-500">
            The stocks the nightly screener draws from — separate from CFM's own
            universe, seeded from it once and managed independently from here on.
          </p>
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <input
              type="text"
              value={newTicker}
              onChange={(e) => setNewTicker(e.target.value.toUpperCase())}
              onKeyDown={(e) => e.key === "Enter" && addTicker()}
              placeholder="Add ticker…"
              className="w-28 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-200"
            />
            <button
              onClick={addTicker}
              disabled={busy || !newTicker.trim()}
              className="rounded border border-emerald-700 bg-emerald-500/10 px-2.5 py-1 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-50"
            >
              Add
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept=".csv,text/csv"
              onChange={(e) => importCsv(e.target.files?.[0])}
              className="hidden"
            />
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={busy}
              title="Import a CSV — the first column of each row is read as a ticker symbol"
              className="rounded border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50"
            >
              Import CSV
            </button>
            <input
              type="text"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter…"
              className="w-28 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-200"
            />
            {msg?.ok && <span className="text-[11px] text-emerald-300">{msg.ok}</span>}
            {msg?.err && <span className="text-[11px] text-rose-300">{msg.err}</span>}
          </div>
          {loading && <Spinner />}
          {error && <ErrorState error={error} onRetry={reload} />}
          {data && (
            <div className="max-h-48 overflow-y-auto">
              <div className="flex flex-wrap gap-1.5">
                {shown.map((t) => (
                  <button
                    key={t}
                    onClick={() => removeTicker(t)}
                    title={`Remove ${t}`}
                    className="rounded border border-slate-700 bg-slate-800/60 px-1.5 py-0.5 text-[11px] font-mono text-slate-300 hover:border-rose-800 hover:bg-rose-500/10 hover:text-rose-300"
                  >
                    {t} ✕
                  </button>
                ))}
                {!shown.length && (
                  <p className="text-[11px] text-slate-500">No tickers match.</p>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function SignalFeed({ events }) {
  if (!events?.length) {
    return <p className="text-[11px] text-slate-500">No signals yet for this date.</p>;
  }
  const rows = [...events].sort((a, b) => (a.at < b.at ? 1 : -1)); // newest first
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[720px] text-sm">
        <thead>
          <tr className="text-left text-[10px] uppercase tracking-wide text-slate-500">
            <th className="py-1 pr-3">Time</th>
            <th className="py-1 pr-3">Symbol</th>
            <th className="py-1 pr-3">Event</th>
            <th className="py-1 pr-3">Dir</th>
            <th className="py-1 pr-3">Detail</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((e) => {
            const meta = EVENT_META[e.event] || { label: e.event, status: "unknown" };
            return (
              <tr key={e.id || `${e.symbol}-${e.event}-${e.at}`} className="border-t border-slate-800 text-slate-200">
                <td className="py-1.5 pr-3 font-mono text-[11px] text-slate-400">{timeOf(e.at)}</td>
                <td className="py-1.5 pr-3 font-mono font-semibold">{e.symbol}</td>
                <td className="py-1.5 pr-3"><Pill status={meta.status}>{meta.label}</Pill></td>
                <td className={`py-1.5 pr-3 font-mono text-[11px] uppercase ${e.direction === "long" ? "text-emerald-300" : e.direction === "short" ? "text-rose-300" : "text-slate-500"}`}>
                  {e.direction || "—"}
                </td>
                <td className="py-1.5 pr-3 text-[11px] text-slate-400">{detailFor(e)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function TradeLog({ trades }) {
  const rows = Object.values(trades || {}).sort((a, b) => (a.entry?.at < b.entry?.at ? 1 : -1));
  if (!rows.length) {
    return <p className="text-[11px] text-slate-500">No trades taken for this date.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[760px] text-sm">
        <thead>
          <tr className="text-left text-[10px] uppercase tracking-wide text-slate-500">
            <th className="py-1 pr-3">Symbol</th>
            <th className="py-1 pr-3">Dir</th>
            <th className="py-1 pr-3">Entry</th>
            <th className="py-1 pr-3">Exits</th>
            <th className="py-1 pr-3">Status</th>
            <th className="py-1 pr-3 text-right">R</th>
            <th className="py-1 pr-3 text-right">P&amp;L</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((t) => (
            <tr key={t.trade_id} className="border-t border-slate-800 text-slate-200">
              <td className="py-1.5 pr-3 font-mono font-semibold">{t.symbol}</td>
              <td className={`py-1.5 pr-3 font-mono text-[11px] uppercase ${t.direction === "long" ? "text-emerald-300" : "text-rose-300"}`}>
                {t.direction}
              </td>
              <td className="py-1.5 pr-3 font-mono text-[11px]">
                {t.entry.price} · {t.entry.size}sh · {timeOf(t.entry.at)}
              </td>
              <td className="py-1.5 pr-3 text-[11px] text-slate-400">
                {t.exits.length
                  ? t.exits.map((x) => `${EVENT_META[x.kind]?.label || x.kind} @${x.price} (${x.size}sh)`).join(", ")
                  : "open"}
              </td>
              <td className="py-1.5 pr-3">
                <Pill status={t.status === "closed" ? "unknown" : "watch"}>{t.status}</Pill>
              </td>
              <td className={`py-1.5 pr-3 text-right font-mono ${toneFor(t.realized_r)}`}>{rMult(t.realized_r)}</td>
              <td className={`py-1.5 pr-3 text-right font-mono ${toneFor(t.realized_pnl)}`}>{money(t.realized_pnl)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const VERDICT_TONE = { win: "go", loss: "avoid", flat: "unknown" };

function TrialBanner({ trial }) {
  if (!trial) return null;
  const pct = trial.target_trades ? (trial.completed_trades / trial.target_trades) * 100 : 0;
  return (
    <div className="mb-4 rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-3">
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-xs font-semibold text-slate-300">
          Paper trial: {trial.completed_trades}/{trial.target_trades} trades
          {trial.status === "complete" && (
            <Pill status={VERDICT_TONE[trial.verdict] || "unknown"}>{trial.verdict}</Pill>
          )}
        </div>
        <div className={`text-xs font-mono ${toneFor(trial.net_pnl)}`}>
          {rMult(trial.net_r)} · {money(trial.net_pnl)}
        </div>
      </div>
      <Meter pct={pct} tone={trial.status === "complete" ? (trial.verdict === "loss" ? "bg-rose-500" : "bg-emerald-500") : "bg-sky-500"} />
      {trial.status === "complete" ? (
        <p className="mt-1.5 text-[11px] text-slate-500">
          Trial complete — no new paper entries will be taken. Going live is
          your call; nothing here flips MODE automatically.
        </p>
      ) : (
        <p className="mt-1.5 text-[11px] text-slate-500">
          Running automatically until {trial.target_trades} trades close, then the
          sleeve pauses new entries and this becomes a WIN/LOSS/FLAT verdict.
        </p>
      )}
    </div>
  );
}

function EnabledToggle({ enabled, busy, onToggle }) {
  if (!enabled) return null;
  return (
    <button
      onClick={() => onToggle(!enabled.enabled)}
      disabled={busy}
      className={`rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide disabled:opacity-40 ${
        enabled.enabled
          ? "bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25"
          : "bg-slate-700/40 text-slate-400 hover:bg-slate-700/60"
      }`}
      title={enabled.enabled ? "Click to turn OFF for this account" : "Click to turn ON for this account"}
    >
      day trading: {enabled.enabled ? "on" : "off"}
    </button>
  );
}

export default function DayTradePanel() {
  const [date, setDate] = React.useState(todayISO);

  const { data: enabled, reload: reloadEnabled } = useApi(() => api.daytradeEnabled(), [], null);
  const [toggleBusy, setToggleBusy] = React.useState(false);
  async function toggle(on) {
    setToggleBusy(true);
    try { await api.daytradeSetEnabled(on); await reloadEnabled(); }
    finally { setToggleBusy(false); }
  }

  // Live sizing budget and trial progress — independent of `date` (both are
  // always "right now"/"overall"), so they get their own poll rather than
  // riding the per-date fetch below.
  const { data: budget } = useApi(() => api.daytradeBudget(), [], 60000);
  const { data: trial } = useApi(() => api.daytradeTrial(), [], 60000);

  const { data, error, loading, reload } = useApi(
    async () => {
      const [universe, signals, trades] = await Promise.all([
        api.daytradeUniverse(date),
        api.daytradeSignals(date),
        api.daytradeTrades(date),
      ]);
      return { universe, signals, trades };
    },
    [date],
    60000, // the strategy's own scheduler ticks every 30s and bars land every 5 min
  );

  const trades = React.useMemo(() => Object.values(data?.trades?.trades || {}), [data]);
  const closed = trades.filter((t) => t.status === "closed");
  const wins = closed.filter((t) => (t.realized_r || 0) > 0);
  const cumulativeR = closed.reduce((s, t) => s + (t.realized_r || 0), 0);
  const realizedPnl = trades.reduce((s, t) => s + (t.realized_pnl || 0), 0);

  return (
    <Card
      title="Day Trade"
      right={
        <div className="flex items-center gap-2">
          <span className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-sky-300">
            paper mode
          </span>
          <EnabledToggle enabled={enabled} busy={toggleBusy} onToggle={toggle} />
          <button onClick={() => setDate((d) => addDays(d, -1))} className="rounded border border-slate-700 px-2 py-0.5 text-[11px] text-slate-400 hover:text-slate-200">
            ←
          </button>
          <input
            type="date"
            value={date}
            max={todayISO()}
            onChange={(e) => e.target.value && setDate(e.target.value)}
            className="rounded border border-slate-700 bg-slate-900 px-1.5 py-0.5 text-[11px] text-slate-200"
          />
          <button
            onClick={() => setDate((d) => addDays(d, 1))}
            disabled={date >= todayISO()}
            className="rounded border border-slate-700 px-2 py-0.5 text-[11px] text-slate-400 hover:text-slate-200 disabled:opacity-30"
          >
            →
          </button>
          <button onClick={reload} className="text-[11px] text-slate-400 hover:text-slate-200">↻</button>
        </div>
      }
    >
      <p className="mb-3 text-xs text-slate-400">
        A separate, rules-based intraday strategy for capital that doesn't fit
        a CFM position — 8:30-10:00 AM CT breakout setups on the nightly
        screener's picks, ATR-based stops, half-out at 1R. Paper mode only:
        every fill here is simulated against real market data; nothing is
        ever sent to the broker.
      </p>

      {enabled && !enabled.enabled && (
        <p className="mb-3 rounded border border-slate-700 bg-slate-800/40 px-3 py-2 text-[11px] text-slate-400">
          Day trading is <span className="font-semibold text-slate-300">OFF</span> for this
          account — the scheduler won't screen or take new entries here. History below (if
          any) is read-only. Flip the switch above to turn it on.
        </p>
      )}

      <TrialBanner trial={trial} />

      {loading && <Spinner />}
      {error && <ErrorState error={error} onRetry={reload} />}

      {data && (
        <>
          <div className="mb-4 grid grid-cols-2 gap-4 rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-3 sm:grid-cols-3 lg:grid-cols-6">
            <Stat
              label="Budget"
              value={budget ? money(budget.amount) : "—"}
              sub={budget ? (budget.source === "dry_powder" ? budget.detail : `fallback — ${budget.detail}`) : "loading…"}
              tone={budget?.source === "fallback" ? "text-amber-300" : "text-slate-100"}
            />
            <Stat label="Trades taken" value={trades.length} sub={`max 2/day`} />
            <Stat label="Cumulative R" value={rMult(cumulativeR)} tone={toneFor(cumulativeR)} />
            <Stat label="Realized P&L" value={money(realizedPnl)} tone={toneFor(realizedPnl)} />
            <Stat
              label="Win rate"
              value={closed.length ? `${Math.round((wins.length / closed.length) * 100)}%` : "—"}
              sub={closed.length ? `${wins.length}/${closed.length} closed` : "no closed trades yet"}
            />
            <Stat label="Rule adherence" value="100%" sub="paper mode enforces the rules exactly" tone="text-emerald-300" />
          </div>

          <div className="space-y-6">
            <section>
              <div className="mb-2 flex items-center justify-between gap-2">
                <h4 className="text-xs font-semibold text-slate-300">
                  {data.universe.ran ? "Screener picks" : "Screener hasn't run for this date"}
                </h4>
                {date === todayISO() && <RescanButton onComplete={reload} />}
              </div>
              <UniverseTable picks={data.universe.picks} />
            </section>

            <TickerRoster />

            <section>
              <h4 className="mb-2 text-xs font-semibold text-slate-300">Signal feed</h4>
              <SignalFeed events={data.signals.events} />
            </section>

            <section>
              <h4 className="mb-2 text-xs font-semibold text-slate-300">Trade log</h4>
              <TradeLog trades={data.trades.trades} />
            </section>
          </div>
        </>
      )}
    </Card>
  );
}
