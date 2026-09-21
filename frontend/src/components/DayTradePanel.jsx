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

// Bar/event timestamps are already stamped in ET (market time — see
// schwab_api.get_intraday_bars), so slicing the string is correct; the " ET"
// suffix is explicit rather than assumed, since the header ticker strip right
// above this panel labels ITS times "Z" (UTC) — same-looking bare "HH:MM"
// values in two different zones side by side is exactly how this got
// misread as a wrong/frozen price rather than a correctly-labeled ET one.
const timeOf = (iso) => (iso ? `${iso.slice(11, 16)} ET` : "—");
// `computed_at` is stored in UTC (unlike the bar/event timestamps above) —
// this converts to the VIEWER's own local time instead of assuming an offset.
const localTime = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—");
const money = (n) =>
  n == null ? "—" : `${n < 0 ? "-" : ""}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const rMult = (n) => (n == null ? "—" : `${n >= 0 ? "+" : ""}${n.toFixed(2)}R`);
const toneFor = (n) => (n == null ? "text-slate-300" : n > 0 ? "text-emerald-300" : n < 0 ? "text-rose-300" : "text-slate-300");
const clamp01 = (x) => Math.max(0, Math.min(1, x));

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

// `prices` is the latest INGESTED 5-min bar per symbol (api.daytradePrices —
// backend/daytrade/store.py's latest_bars): discrete OHLC candles for the
// strategy's own breakout/stop rules, not a live quote — it only moves on
// the DAYTRADE_BAR_INTERVAL_MINUTES (5 min) cadence bars.ingest() runs on,
// during the 8:30-10:00 CT window, and freezes outside it. `quotes` is the
// TRUE current price (api.daytradeQuotes — backend data_handler.latest_
// quotes, the SAME centralized quote path api.tickerStrip reads from), so
// it can never disagree with the price shown anywhere else in the app and
// keeps moving all day. `price` (the screener's own column) is a THIRD,
// older number: the prior day's close, stamped once when the screener last
// ran (nightly, or a manual rescan) — it never moves intraday at all.
function UniverseTable({ picks, prices, quotes }) {
  if (!picks?.length) {
    return <p className="text-[11px] text-slate-500">No qualifying names for this date.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[840px] text-sm">
        <thead>
          <tr className="text-left text-[10px] uppercase tracking-wide text-slate-500">
            <th className="py-1 pr-3">Symbol</th>
            <th className="py-1 pr-3 text-right">Price</th>
            <th className="py-1 pr-3 text-right" title="The current quote (data_handler.latest_quotes) — same source as the header ticker strip, updates all session">Live Quote</th>
            <th className="py-1 pr-3 text-right" title="Latest ingested 5-min bar the strategy's own rules evaluate — updates every 5 min during the 8:30-10:00 CT window only, frozen outside it">Last Bar</th>
            <th className="py-1 pr-3 text-right">Avg Volume</th>
            <th className="py-1 pr-3 text-right">ATR14</th>
            <th className="py-1 pr-3 text-right">ATR%</th>
            <th className="py-1 pr-3 text-right">Prior High</th>
            <th className="py-1 pr-3 text-right">Prior Low</th>
          </tr>
        </thead>
        <tbody>
          {picks.map((p) => {
            const live = prices?.[p.symbol];
            const barTone = !live ? "text-slate-500"
              : live.close > p.price ? "text-emerald-300"
              : live.close < p.price ? "text-rose-300" : "text-slate-300";
            const quote = quotes?.[p.symbol];
            const quoteTone = !quote?.price ? "text-slate-500"
              : quote.price > p.price ? "text-emerald-300"
              : quote.price < p.price ? "text-rose-300" : "text-slate-300";
            return (
              <tr key={p.symbol} className="border-t border-slate-800 text-slate-200">
                <td className="py-1.5 pr-3 font-mono font-semibold">{p.symbol}</td>
                <td className="py-1.5 pr-3 text-right font-mono">{p.price}</td>
                <td className={`py-1.5 pr-3 text-right font-mono ${quoteTone}`}>
                  {quote?.price ?? "—"}
                </td>
                <td className={`py-1.5 pr-3 text-right font-mono ${barTone}`}>
                  {live
                    ? <>{live.close} <span className="text-[10px] text-slate-500">{timeOf(live.datetime)}</span></>
                    : "—"}
                </td>
                <td className="py-1.5 pr-3 text-right font-mono">{p.avg_volume?.toLocaleString()}</td>
                <td className="py-1.5 pr-3 text-right font-mono">{p.atr14}</td>
                <td className="py-1.5 pr-3 text-right font-mono">{p.atr_pct}%</td>
                <td className="py-1.5 pr-3 text-right font-mono text-slate-400">{p.prior_day_high}</td>
                <td className="py-1.5 pr-3 text-right font-mono text-slate-400">{p.prior_day_low}</td>
              </tr>
            );
          })}
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

// Every reason _evaluate() (daytrade/universe.py) can hand back for a
// disqualified ticker — matched by substring since a row can combine more
// than one ("price out of range; avg volume too low"). The two DATA rows
// (couldn't fetch anything at all) are a different kind of failure from the
// three CRITERIA rows (fetched fine, just didn't qualify) — see below.
const SCREEN_REASON_BUCKETS = [
  { key: "price", label: "price out of range", match: (r) => r.includes("price out of range") },
  { key: "volume", label: "avg volume too low", match: (r) => r.includes("avg volume too low") },
  { key: "atr", label: "ATR% out of range", match: (r) => r.includes("ATR% out of range") },
  { key: "no_data", label: "no data", match: (r) => r === "no data", isDataGap: true },
  { key: "unavailable", label: "data unavailable", match: (r) => r.startsWith("data unavailable"), isDataGap: true },
];

// Answers "was the WHOLE roster actually screened?" — the backend already
// guarantees this structurally (universe.screen()'s screened list has one
// row per ticker it was ASKED to evaluate, pass or fail, never silently
// dropped — see its own docstring), so the one way coverage can genuinely
// fall short is staleness: the roster grew (an add, or a CSV import) AFTER
// the last screen ran, and nobody's rescanned since. This compares the last
// screen's coverage against the roster's CURRENT size to catch exactly that.
// A Stat that doubles as a filter toggle for the per-ticker drill-down table
// below it — click a count to see exactly which tickers make it up, click
// again to close. Same visual weight as the plain Stat it replaces.
function ClickableStat({ label, value, sub, tone, active, onClick }) {
  return (
    <button
      onClick={onClick}
      className={`min-w-0 rounded-lg border px-2 py-1 text-left transition ${
        active ? "border-sky-600 bg-sky-500/10" : "border-transparent hover:border-slate-700 hover:bg-slate-800/40"
      }`}
    >
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`text-xl font-semibold leading-tight sm:text-2xl ${tone || "text-slate-100"}`}>{value}</div>
      {sub && <div className="text-xs text-slate-500">{sub}</div>}
    </button>
  );
}

// Every screened row, pass or fail, filtered down to whichever bucket the
// user clicked above — this is the "validate the actual tickers" layer: the
// bucket counts say HOW MANY failed on volume, this says WHICH ones and at
// what price/volume/ATR%, so a specific "why isn't XYZ trading" is answerable
// without touching a terminal.
function ScreenedTable({ rows }) {
  const [symbolFilter, setSymbolFilter] = React.useState("");
  const filtered = React.useMemo(() => {
    const f = symbolFilter.trim().toUpperCase();
    const matched = f ? rows.filter((r) => r.symbol.includes(f)) : rows;
    return [...matched].sort((a, b) => a.symbol.localeCompare(b.symbol));
  }, [rows, symbolFilter]);

  return (
    <div className="mt-3 space-y-2 rounded-lg border border-slate-800 bg-slate-950/40 p-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-[11px] text-slate-500">
          {filtered.length} of {rows.length} ticker{rows.length === 1 ? "" : "s"}
        </p>
        <input
          type="text"
          value={symbolFilter}
          onChange={(e) => setSymbolFilter(e.target.value)}
          placeholder="Filter symbol…"
          className="w-28 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-[11px] text-slate-200"
        />
      </div>
      <div className="max-h-64 overflow-y-auto">
        <table className="w-full min-w-[760px] text-sm">
          <thead>
            <tr className="text-left text-[10px] uppercase tracking-wide text-slate-500">
              <th className="py-1 pr-3">Symbol</th>
              <th className="py-1 pr-3 text-right">Price</th>
              <th className="py-1 pr-3 text-right">Avg Volume</th>
              <th className="py-1 pr-3 text-right">ATR%</th>
              <th className="py-1 pr-3 text-right">Prior High</th>
              <th className="py-1 pr-3 text-right">Prior Low</th>
              <th className="py-1 pr-3">Status</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r) => (
              <tr key={r.symbol} className="border-t border-slate-800 text-slate-200">
                <td className="py-1 pr-3 font-mono font-semibold">{r.symbol}</td>
                <td className="py-1 pr-3 text-right font-mono">{r.price ?? "—"}</td>
                <td className="py-1 pr-3 text-right font-mono">{r.avg_volume?.toLocaleString() ?? "—"}</td>
                <td className="py-1 pr-3 text-right font-mono">{r.atr_pct != null ? `${r.atr_pct}%` : "—"}</td>
                <td className="py-1 pr-3 text-right font-mono text-slate-400">{r.prior_day_high ?? "—"}</td>
                <td className="py-1 pr-3 text-right font-mono text-slate-400">{r.prior_day_low ?? "—"}</td>
                <td className="py-1 pr-3 text-[11px]">
                  {r.qualified
                    ? <Pill status="go">qualified</Pill>
                    : <span className="text-slate-400">{r.reason}</span>}
                </td>
              </tr>
            ))}
            {!filtered.length && (
              <tr><td colSpan={7} className="py-2 text-[11px] text-slate-500">No tickers match.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ScreenCoverage({ universe }) {
  const [open, setOpen] = React.useState(true);
  const [drill, setDrill] = React.useState(null); // null | "all" | "qualified" | bucket key
  const { data: roster } = useApi(() => api.daytradeTickers(), [], 60000);

  if (!universe?.ran) return null;

  const screened = universe.screened || [];
  const total = screened.length;
  const qualifiedRows = screened.filter((r) => r.qualified);
  const qualified = qualifiedRows.length;
  const failed = screened.filter((r) => !r.qualified);
  const buckets = SCREEN_REASON_BUCKETS.map((b) => ({
    ...b,
    rows: failed.filter((r) => b.match(r.reason || "")),
  }));
  const dataGaps = buckets.filter((b) => b.isDataGap).flatMap((b) => b.rows);

  const rosterTotal = roster?.total;
  const stale = rosterTotal != null && rosterTotal !== total;

  const drillRows =
    drill === "all" ? screened
    : drill === "qualified" ? qualifiedRows
    : drill ? buckets.find((b) => b.key === drill)?.rows || []
    : null;
  const toggleDrill = (key) => setDrill((d) => (d === key ? null : key));

  return (
    <section>
      <button
        onClick={() => setOpen((o) => !o)}
        className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-slate-300 hover:text-slate-100"
      >
        <span className="text-slate-500">{open ? "▾" : "▸"}</span>
        Screener coverage — {total} of {rosterTotal ?? "?"} tickers screened
        {stale && <Pill status="caution">roster changed since last rescan</Pill>}
      </button>
      {open && (
        <div className="space-y-3 rounded-lg border border-slate-800 bg-slate-900/40 p-3">
          {stale && (
            <p className="text-[11px] text-amber-300">
              The day-trade universe now has {rosterTotal} ticker{rosterTotal === 1 ? "" : "s"}, but
              the last screen (computed {localTime(universe.computed_at)}) only evaluated {total}.
              Click <span className="font-semibold">Rescan now</span> above to cover the current
              roster.
            </p>
          )}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            <ClickableStat label="Screened" value={total} active={drill === "all"} onClick={() => toggleDrill("all")} />
            <ClickableStat label="Qualified" value={qualified} tone="text-emerald-300"
                           active={drill === "qualified"} onClick={() => toggleDrill("qualified")} />
            {buckets.map((b) => (
              <ClickableStat key={b.key} label={b.label} value={b.rows.length}
                    tone={b.isDataGap && b.rows.length ? "text-amber-300" : "text-slate-100"}
                    active={drill === b.key} onClick={() => toggleDrill(b.key)} />
            ))}
          </div>
          <p className="mt-1.5 text-[11px] text-slate-500">Click any count above to see the actual tickers behind it.</p>
          {drillRows && <ScreenedTable rows={drillRows} />}
          {dataGaps.length > 0 && (
            <div>
              <p className="mb-1 text-[11px] text-slate-500">
                {dataGaps.length} ticker{dataGaps.length === 1 ? "" : "s"} couldn't be evaluated at
                all (no market data reached the screener) — worth checking for typos or delisted
                symbols:
              </p>
              <div className="max-h-32 overflow-y-auto">
                <div className="flex flex-wrap gap-1.5">
                  {dataGaps.map((r) => (
                    <span
                      key={r.symbol}
                      title={r.reason}
                      className="rounded border border-slate-700 bg-slate-800/60 px-1.5 py-0.5 text-[11px] font-mono text-amber-300"
                    >
                      {r.symbol}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
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

// Two-sided range meter for an OPEN trade: -1R (stop) on the left to the
// active target (+1R before half_taken, +2R after — computed from the
// actual target price rather than hardcoded, so it tracks
// DAYTRADE_HALF_TARGET_R/FULL_TARGET_R if those ever change) on the right,
// 0R (entry) marked as the red/green boundary, current price as the dot.
// `rNow` is the true, UNCLAMPED R-multiple (shown in the caption); only the
// dot's POSITION is clamped into the visible domain, since a gap-through
// can briefly put price past the stop or target before the engine's next
// replay resolves the trade.
function RMultiBar({ rNow, targetR }) {
  const domainMin = -1;
  const span = targetR - domainMin;
  const pctOf = (r) => clamp01((r - domainMin) / span) * 100;
  const entryPct = pctOf(0);
  const markerPct = pctOf(rNow);
  return (
    <div className="relative h-2.5 w-full rounded-full bg-slate-800">
      <div className="absolute inset-y-0 left-0 rounded-l-full bg-rose-500/30" style={{ width: `${entryPct}%` }} />
      <div className="absolute inset-y-0 rounded-r-full bg-emerald-500/30" style={{ left: `${entryPct}%`, right: 0 }} />
      <div
        className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-slate-100 bg-slate-950"
        style={{ left: `${markerPct}%` }}
        title={`${rMult(rNow)} now`}
      />
    </div>
  );
}

// How close an armed setup is to its breakout trigger, 0-100 (null if there's
// no live price to measure against yet) — the single source both the row's
// own meter and the panel's "closest to getting traded first" sort read from,
// so the two can never disagree.
function armedProgressPct(row) {
  if (row.current_price == null) return null;
  const range = Math.abs(row.setup_high - row.setup_low) || 1;
  return clamp01(row.direction === "long"
    ? (row.current_price - row.setup_low) / range
    : (row.setup_high - row.current_price) / range) * 100;
}

// One row per symbol currently armed (watching for a breakout) or in_trade
// (open, watching for stop/target) — backend/daytrade/signals.py's
// current_status, display-only and re-derived from the persisted signals
// log, so it can lag or go stale but can never contradict or drive an
// actual trading decision.
function LiveStatusRow({ row }) {
  const priceKnown = row.current_price != null;
  const dirTone = row.direction === "long" ? "text-emerald-300" : "text-rose-300";

  if (row.status === "armed") {
    const trigger = row.direction === "long" ? row.setup_high : row.setup_low;
    const pct = armedProgressPct(row) ?? 0;
    return (
      <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2">
        <div className="mb-1 flex items-center justify-between gap-2 text-xs">
          <span className="font-mono font-semibold text-slate-200">{row.symbol}</span>
          <span className={`uppercase ${dirTone}`}>{row.direction} setup</span>
        </div>
        <Meter pct={pct} tone="bg-sky-500" />
        <p className="mt-1 text-[11px] text-slate-500">
          {priceKnown ? `${Math.round(pct)}% to breakout` : "no live price yet"} — needs a close{" "}
          {row.direction === "long" ? "above" : "below"} {trigger}
          {priceKnown ? ` (bar ${row.current_price} as of ${timeOf(row.current_price_at)})` : ""}
        </p>
      </div>
    );
  }

  // in_trade
  const risk = Math.abs(row.entry - row.stop);
  const targetPrice = row.half_taken ? row.target2 : row.target1;
  const canRender = priceKnown && risk > 0;
  const rNow = canRender
    ? ((row.direction === "long" ? row.current_price - row.entry : row.entry - row.current_price) / risk)
    : null;
  const targetR = (row.direction === "long" ? targetPrice - row.entry : row.entry - targetPrice) / (risk || 1);
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2">
      <div className="mb-1 flex items-center justify-between gap-2 text-xs">
        <span className="font-mono font-semibold text-slate-200">{row.symbol}</span>
        <span className={`uppercase ${dirTone}`}>
          {row.direction} · {row.half_taken ? "half out, riding to target" : "open"}
        </span>
      </div>
      {canRender ? (
        <>
          <RMultiBar rNow={rNow} targetR={targetR} />
          <p className="mt-1 text-[11px] text-slate-500">
            {rMult(rNow)} as of bar {timeOf(row.current_price_at)} — stop at -1R ({row.stop}), target at {rMult(targetR)} ({targetPrice})
          </p>
        </>
      ) : (
        <p className="text-[11px] text-slate-500">no live price yet</p>
      )}
    </div>
  );
}

// in_trade rows first (already a real trade — the most time-sensitive to
// watch), then armed rows ranked by how close they are to actually
// triggering, closest first: "closest to getting traded" at the top.
const LIVE_STATUS_RANK = { in_trade: 0, armed: 1 };
function sortLiveStatusRows(rows) {
  return [...rows].sort((a, b) => {
    const rankDiff = (LIVE_STATUS_RANK[a.status] ?? 2) - (LIVE_STATUS_RANK[b.status] ?? 2);
    if (rankDiff !== 0) return rankDiff;
    if (a.status !== "armed") return 0;
    return (armedProgressPct(b) ?? -1) - (armedProgressPct(a) ?? -1);
  });
}

function LiveStatusPanel({ rows }) {
  if (!rows?.length) {
    return <p className="text-[11px] text-slate-500">Nothing armed or open right now.</p>;
  }
  return (
    <div className="space-y-2">
      {sortLiveStatusRows(rows).map((r) => <LiveStatusRow key={r.symbol} row={r} />)}
    </div>
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

  // Adaptive poll cadence: the baseline (60s) is fine while nothing's close
  // to happening, but once a setup is armed (watching for a breakout) or a
  // trade is actually open, stale-by-a-minute prices are the whole reason to
  // have this panel open at all. Ratchets down as soon as the picture
  // changes rather than waiting a full baseline cycle to notice — see the
  // effect below, which re-derives this from each fetch's own liveStatus.
  const IDLE_POLL_MS = 60000;
  const ARMED_POLL_MS = 30000;
  const IN_TRADE_POLL_MS = 15000;
  const [pollMs, setPollMs] = React.useState(IDLE_POLL_MS);

  const { data, error, loading, reload } = useApi(
    async () => {
      const [universe, signals, trades, prices, quotes, liveStatus] = await Promise.all([
        api.daytradeUniverse(date),
        api.daytradeSignals(date),
        api.daytradeTrades(date),
        api.daytradePrices(date),
        api.daytradeQuotes(date),
        api.daytradeLiveStatus(date),
      ]);
      return { universe, signals, trades, prices, quotes, liveStatus };
    },
    [date],
    pollMs,
  );

  React.useEffect(() => {
    if (!data) return;
    const rows = data.liveStatus?.rows || [];
    const next = date !== todayISO() ? IDLE_POLL_MS
      : rows.some((r) => r.status === "in_trade") ? IN_TRADE_POLL_MS
      : rows.some((r) => r.status === "armed") ? ARMED_POLL_MS
      : IDLE_POLL_MS;
    setPollMs((p) => (p === next ? p : next));
  }, [data, date]);

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
          <span
            className="text-[10px] text-slate-500"
            title="Refresh cadence ratchets down automatically: 60s idle, 30s once a setup is armed, 15s once a trade is open"
          >
            every {pollMs / 1000}s
          </span>
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
              <UniverseTable picks={data.universe.picks} prices={data.prices.prices} quotes={data.quotes.quotes} />
            </section>

            <ScreenCoverage universe={data.universe} />

            <TickerRoster />

            <section>
              <h4 className="mb-2 text-xs font-semibold text-slate-300">
                Live trade status
              </h4>
              <LiveStatusPanel rows={data.liveStatus.rows} />
            </section>

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
