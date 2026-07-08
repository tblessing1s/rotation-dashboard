import React from "react";
import { api } from "../api.js";
import { Card, Light, StaleBadge, Loading, fmt, useApi } from "./ui.jsx";

// Data health: last-successful fetch per source + cache staleness, so a silent
// provider failure is visible instead of quietly serving stale frames.

const SOURCE_LABELS = {
  schwab_bars: "Schwab daily bars",
  schwab_quote: "Schwab quotes",
  alpha_vantage_bars: "Alpha Vantage bars (fallback)",
  alpha_vantage_quote: "Alpha Vantage quotes (fallback)",
};

// On-demand universe sweep: which tickers return no provider data (dead /
// renamed / typo'd) and, optionally, which lack weekly options (can't run CFM).
// Not fetched on mount — it hits every ticker, so it only runs on the button.
function UniverseCheck() {
  const [res, setRes] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [manage, setManage] = React.useState(false);
  const [sectors, setSectors] = React.useState(null);
  const [form, setForm] = React.useState({ ticker: "", sector: "" });
  const [msg, setMsg] = React.useState(null);

  const run = async (weeklies) => {
    setBusy(true);
    setRes(null);
    try {
      setRes(await api.universeHealth(weeklies));
    } catch (e) {
      setRes({ error: String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };

  const openManage = async () => {
    const next = !manage;
    setManage(next);
    if (next && !sectors) {
      try {
        const u = await api.universe();
        setSectors(u.sectors || []);
        setForm((f) => ({ ...f, sector: u.sectors?.[0]?.etf || "" }));
      } catch (e) { setMsg({ err: String(e.message || e) }); }
    }
  };

  const addTicker = async () => {
    setMsg(null);
    try {
      const r = await api.universeAdd(form.ticker, form.sector);
      setMsg({ ok: `Added ${r.added} to ${r.sector}` });
      setForm((f) => ({ ...f, ticker: "" }));
    } catch (e) { setMsg({ err: String(e.message || e) }); }
  };

  const removeTicker = async (ticker) => {
    setMsg(null);
    try {
      await api.universeRemove(ticker);
      setMsg({ ok: `Removed ${ticker}` });
      // Drop it from the currently displayed dead-list without a full re-check.
      setRes((r) => r && !r.error ? { ...r, no_data: r.no_data.filter((d) => d.ticker !== ticker) } : r);
    } catch (e) { setMsg({ err: String(e.message || e) }); }
  };

  const syncFromSeed = async () => {
    setMsg(null);
    try {
      const r = await api.universeSync();
      setMsg({ ok: r.count ? `Synced ${r.count} new: ${r.added.join(", ")}` : "Already up to date with the seed" });
      setSectors(null);  // force the add-form sector list to refresh
    } catch (e) { setMsg({ err: String(e.message || e) }); }
  };

  const removeAllDead = async () => {
    const dead = (res?.no_data || []).map((d) => d.ticker);
    if (!dead.length || !window.confirm(`Remove ${dead.length} dead ticker(s) from the universe?`)) return;
    setMsg(null);
    try {
      const r = await api.universeRemoveBulk(dead);
      setMsg({ ok: `Removed ${r.removed.length} dead ticker(s)` });
      setRes((prev) => prev && !prev.error ? { ...prev, no_data: [] } : prev);
    } catch (e) { setMsg({ err: String(e.message || e) }); }
  };

  const group = (rows) => {
    const by = {};
    (rows || []).forEach((r) => { (by[r.sector || "?"] ||= []).push(r.ticker); });
    return Object.entries(by).sort();
  };

  return (
    <div className="mt-4 border-t border-slate-800 pt-3">
      <div className="flex items-center justify-between">
        <span className="text-xs uppercase tracking-wide text-slate-500">Universe check</span>
        <div className="flex gap-2">
          <button onClick={() => run(false)} disabled={busy}
                  className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50">
            {busy ? "Checking…" : "Check universe"}
          </button>
          <button onClick={() => run(true)} disabled={busy}
                  title="Also probe weekly options for every ticker (slower)"
                  className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs font-semibold text-slate-400 hover:bg-slate-800 disabled:opacity-50">
            + weeklies
          </button>
          <button onClick={openManage}
                  className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs font-semibold text-slate-400 hover:bg-slate-800">
            {manage ? "Done" : "Manage"}
          </button>
        </div>
      </div>

      {manage && (
        <div className="mt-2 rounded-lg border border-slate-800 bg-slate-950/50 p-2">
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={form.ticker}
              onChange={(e) => setForm((f) => ({ ...f, ticker: e.target.value.toUpperCase() }))}
              placeholder="TICKER"
              className="w-28 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 placeholder:text-slate-600"
            />
            <select
              value={form.sector}
              onChange={(e) => setForm((f) => ({ ...f, sector: e.target.value }))}
              className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
            >
              {(sectors || []).map((s) => (
                <option key={s.etf} value={s.etf}>{s.etf} — {s.name} ({s.count})</option>
              ))}
            </select>
            <button onClick={addTicker} disabled={!form.ticker || !form.sector}
                    className="rounded border border-emerald-700 bg-emerald-500/10 px-2.5 py-1 text-sm font-semibold text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-50">
              Add
            </button>
            <button onClick={syncFromSeed}
                    title="Pull in any new names from the built-in seed list (e.g. newly added ETFs) — respects your removals"
                    className="rounded border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-sm font-semibold text-slate-300 hover:bg-slate-800">
              Sync from seed
            </button>
            <span className="text-xs text-slate-600">to fix a ticker, remove the old and add the new</span>
          </div>
          {msg && (
            <p className={`mt-1 text-xs ${msg.err ? "text-rose-400" : "text-emerald-300"}`}>{msg.err || msg.ok}</p>
          )}
          <VetCandidates />
        </div>
      )}
      {res?.error && <p className="mt-2 text-sm text-rose-400">{res.error}</p>}
      {res && !res.error && res.skipped && <p className="mt-2 text-sm text-slate-500">{res.skipped}</p>}
      {res && !res.error && !res.skipped && (
        <div className="mt-2 text-sm">
          <p className="text-slate-400">
            {res.total} tickers · <span className="text-emerald-300">{res.with_data} returned data</span>
            {res.no_data.length > 0
              ? <> · <span className="text-rose-300">{res.no_data.length} dead</span></>
              : <> · <span className="text-emerald-300">none dead</span></>}
            {res.checked_weeklies && <> · <span className="text-slate-300">{res.cfm_ready} CFM-ready</span></>}
          </p>
          {res.no_data.length > 0 && (
            <div className="mt-1">
              <div className="flex items-center justify-between">
                <span className="text-xs uppercase tracking-wide text-rose-400/80">
                  No data — {manage ? "click ✕ to remove one" : "dead / renamed / typo'd"}
                </span>
                <button onClick={removeAllDead}
                        title="Remove every dead ticker shown from the universe (they never scan and waste a fetch each sweep)"
                        className="rounded-full border border-rose-800 bg-rose-500/10 px-2.5 py-0.5 text-xs font-semibold text-rose-300 hover:bg-rose-500/20">
                  Remove all dead ({res.no_data.length})
                </button>
              </div>
              {group(res.no_data).map(([sector, ts]) => (
                <div key={sector} className="flex flex-wrap items-center gap-1.5 text-xs text-slate-400">
                  <span className="text-slate-500">{sector}</span>
                  {ts.map((t) => (
                    manage ? (
                      <button key={t} onClick={() => removeTicker(t)}
                              className="rounded border border-rose-800 bg-rose-500/10 px-1.5 py-0.5 text-rose-300 hover:bg-rose-500/20">
                        {t} ✕
                      </button>
                    ) : <span key={t}>{t}</span>
                  ))}
                </div>
              ))}
            </div>
          )}
          {res.checked_weeklies && res.no_weeklies?.length > 0 && (
            <div className="mt-2">
              <div className="text-xs uppercase tracking-wide text-amber-400/80">No weeklies — can't run CFM</div>
              {group(res.no_weeklies).map(([sector, ts]) => (
                <div key={sector} className="text-xs text-slate-400"><span className="text-slate-500">{sector}</span> {ts.join(", ")}</div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Vet arbitrary candidate symbols against the CFM criteria (data + weekly
// options + Scorecard verdict) and add the ones that fit — the repeatable way to
// grow the universe from any source (QQQ, a screener, a tip).
function VetCandidates() {
  const [input, setInput] = React.useState("");
  const [res, setRes] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [sel, setSel] = React.useState({});     // ticker -> chosen sector
  const [added, setAdded] = React.useState({}); // ticker -> sector | "ERR:.."

  const vet = async () => {
    setBusy(true); setRes(null); setAdded({});
    try {
      const r = await api.universeVet(input);
      setRes(r);
      const first = r.sectors?.[0] || "";
      const s = {};
      (r.candidates || []).forEach((c) => { if (c.fit) s[c.ticker] = first; });
      setSel(s);
    } catch (e) { setRes({ error: String(e.message || e) }); }
    finally { setBusy(false); }
  };

  const add = async (t) => {
    try { await api.universeAdd(t, sel[t]); setAdded((a) => ({ ...a, [t]: sel[t] })); }
    catch (e) { setAdded((a) => ({ ...a, [t]: `ERR:${e.message || e}` })); }
  };

  return (
    <div className="mt-2 rounded-lg border border-slate-800 bg-slate-950/50 p-2">
      <div className="text-xs uppercase tracking-wide text-slate-500">Vet candidates</div>
      <div className="mt-1 flex flex-wrap items-start gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value.toUpperCase())}
          placeholder="SMH, XBI, PLTR …"
          className="min-w-[16rem] flex-1 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 placeholder:text-slate-600"
        />
        <button onClick={vet} disabled={busy || !input.trim()}
                className="rounded border border-sky-700 bg-sky-500/10 px-2.5 py-1 text-sm font-semibold text-sky-300 hover:bg-sky-500/20 disabled:opacity-50">
          {busy ? "Vetting…" : "Vet"}
        </button>
      </div>
      <p className="mt-1 text-xs text-slate-600">Paste any symbols — checks data, weekly options, and the Scorecard verdict, then lets you add the ones that fit.</p>
      {res?.error && <p className="mt-1 text-xs text-rose-400">{res.error}</p>}
      {res && !res.error && res.skipped && <p className="mt-1 text-xs text-slate-500">{res.skipped}</p>}
      {res && !res.error && !res.skipped && (
        <div className="mt-2 space-y-1">
          <div className="text-xs text-slate-400">{res.fit_count} of {res.candidates.length} fit CFM (add-ready)</div>
          {res.candidates.map((c) => {
            const done = added[c.ticker];
            return (
              <div key={c.ticker} className="flex flex-wrap items-center gap-2 rounded bg-slate-900/60 px-2 py-1 text-sm">
                <span className="w-16 font-semibold text-slate-100">{c.ticker}</span>
                {c.fit ? (
                  <>
                    <span className="rounded-full border border-emerald-600/50 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-semibold text-emerald-300">FITS CFM</span>
                    <span className="text-xs text-slate-500">
                      {c.verdict ? `${c.verdict} · ` : ""}{c.juice_weekly_pct != null ? `${fmt(c.juice_weekly_pct, 2)}%/wk` : "juice —"}
                    </span>
                    {done ? (
                      <span className={`ml-auto text-xs ${String(done).startsWith("ERR") ? "text-rose-400" : "text-emerald-300"}`}>
                        {String(done).startsWith("ERR") ? done.slice(4) : `added to ${done}`}
                      </span>
                    ) : (
                      <span className="ml-auto flex items-center gap-1">
                        <select value={sel[c.ticker] || ""} onChange={(e) => setSel((s) => ({ ...s, [c.ticker]: e.target.value }))}
                                className="rounded border border-slate-700 bg-slate-900 px-1.5 py-0.5 text-xs text-slate-200">
                          {(res.sectors || []).map((etf) => <option key={etf} value={etf}>{etf}</option>)}
                        </select>
                        <button onClick={() => add(c.ticker)}
                                className="rounded border border-emerald-700 bg-emerald-500/10 px-2 py-0.5 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20">
                          Add
                        </button>
                      </span>
                    )}
                  </>
                ) : (
                  <span className="text-xs text-slate-500">{c.reason}</span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// Position reconciliation status (state.json vs Schwab): last run timestamp,
// CLEAN/DIRTY (or FAILED), the diff count, and a manual "Reconcile now" trigger.
function ReconcileStatus() {
  const { data, reload } = useApi(api.reconcile, [], null);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);

  const run = async () => {
    setBusy(true); setErr(null);
    try { await api.runReconcile(); await reload(); }
    catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  };

  const last = data?.last;
  const status = last?.broker_ok === false ? "FAILED" : last?.status || "never run";
  const light = status === "CLEAN" ? "green" : status === "DIRTY" ? "yellow" : status === "never run" ? "yellow" : "red";
  const diffs = (last?.diffs || []).filter((d) => !(d.resolution && d.resolution.status));
  const nonBenign = diffs.filter((d) => d.classification !== "EXPIRED_WORTHLESS_PENDING");

  return (
    <div className="mt-4 border-t border-slate-800 pt-3">
      <div className="flex items-center justify-between">
        <span className="text-xs uppercase tracking-wide text-slate-500">Position reconciliation (vs Schwab)</span>
        <button onClick={run} disabled={busy}
                className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50">
          {busy ? "Reconciling…" : "Reconcile now"}
        </button>
      </div>
      <div className="mt-2 flex items-center gap-2 text-sm">
        <Light status={light} size="h-2.5 w-2.5" />
        <span className="font-semibold text-slate-200">{status}</span>
        {last?.as_of && <span className="text-xs text-slate-500">· {last.as_of.replace("T", " ").replace("Z", "")}</span>}
        {last?.broker_ok !== false && diffs.length > 0 && (
          <span className="text-xs text-slate-400">
            · {nonBenign.length} to resolve{diffs.length - nonBenign.length > 0 ? `, ${diffs.length - nonBenign.length} benign` : ""}
          </span>
        )}
      </div>
      {last?.broker_ok === false && (
        <p className="mt-1 text-xs text-rose-400">Broker fetch failed: {last.error} — no diffs generated; the stale clock keeps running.</p>
      )}
      {last?.broker_ok !== false && status === "CLEAN" && (
        <p className="mt-1 text-xs text-slate-500">state.json matches the brokerage account.</p>
      )}
      {last?.broker_ok !== false && nonBenign.length > 0 && (
        <p className="mt-1 text-xs text-slate-400">Resolve each diff on the Positions tab (the affected position shows a NEEDS REVIEW badge).</p>
      )}
      {err && <p className="mt-1 text-xs text-rose-400">{err}</p>}
    </div>
  );
}

// Live-fill verification: re-fetch recent live orders from Schwab and diff the
// broker's fills against what we logged, plus a reconcile pass. On-demand — this
// is the "prove the live-order path is correct" check for after a real order.
function LiveFillVerify() {
  const [res, setRes] = React.useState(null);
  const [busy, setBusy] = React.useState(false);

  const run = async () => {
    setBusy(true); setRes(null);
    try { setRes(await api.verifyFills()); }
    catch (e) { setRes({ error: String(e.message || e) }); }
    finally { setBusy(false); }
  };

  const green = res && !res.error && res.all_ok === true;
  return (
    <div className="mt-4 border-t border-slate-800 pt-3">
      <div className="flex items-center justify-between">
        <span className="text-xs uppercase tracking-wide text-slate-500">Live-fill verification (vs Schwab)</span>
        <button onClick={run} disabled={busy}
                title="Re-fetch recent live orders from Schwab and diff their fills against what we logged"
                className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50">
          {busy ? "Verifying…" : "Verify live fills"}
        </button>
      </div>
      {res?.error && <p className="mt-2 text-sm text-rose-400">{res.error}</p>}
      {res && !res.error && (
        <div className="mt-2 text-sm">
          {res.checked === 0 ? (
            <p className="text-slate-500">No live fills recorded yet — this lights up after a real (non-paper) order fills.</p>
          ) : (
            <>
              <div className="flex items-center gap-2">
                <Light status={green ? "green" : res.all_ok === null ? "yellow" : "red"} size="h-2.5 w-2.5" />
                <span className="font-semibold text-slate-200">
                  {green ? `All ${res.checked} fill(s) match Schwab` :
                   res.all_ok === null ? "Broker check skipped" : "Discrepancies found"}
                </span>
                {res.reconcile?.status && (
                  <span className="text-xs text-slate-500">· reconcile: {res.reconcile.status}
                    {res.reconcile.open_diffs ? ` (${res.reconcile.open_diffs} open)` : ""}</span>
                )}
              </div>
              {!green && (
                <ul className="mt-1 space-y-1">
                  {res.orders.filter((o) => o.ok !== true).map((o) => (
                    <li key={o.order_id} className="rounded bg-slate-900/60 px-2 py-1 text-xs">
                      <span className="font-semibold text-slate-200">{o.ticker} · {o.kind}</span>
                      <span className="text-slate-500"> · order {o.order_id}</span>
                      {(o.issues || []).map((i, k) => (
                        <div key={k} className="text-rose-300">— {i}</div>
                      ))}
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

// Tiered market-data scheduler: today's API budget per provider, the shed level
// (Tier 3 -> Tier 2 -> Tier 1 cadence; Tier 0 never), staleness of the live-quote
// store, and any active escalations. Reads the blocks already on /api/data-health.
function TieredScheduler({ data }) {
  const budget = data?.data_budget;
  const staleness = data?.staleness;
  const poll = data?.tier_poll;
  if (!budget && !staleness && !poll) return null;

  const providers = budget?.providers || {};
  const shed = Object.entries(providers).filter(([, p]) => (p.shed_level || 0) > 0);
  const staleCount = staleness?.stale_count || 0;

  return (
    <div className="mt-4 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
      <div className="mb-2 flex items-center gap-2">
        <h4 className="text-sm font-semibold text-slate-200">Tiered scheduler</h4>
        <StaleBadge stale={staleCount > 0} label={`${staleCount} stale`} title="Live-quote records past their tier tolerance" />
        {poll?.market_escalation_active && (
          <span className="rounded-full border border-rose-500/40 bg-rose-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-rose-300">
            Market escalation
          </span>
        )}
      </div>

      {/* Per-provider daily budget + shed */}
      <ul className="space-y-1 text-sm">
        {Object.entries(providers).map(([name, p]) => (
          <li key={name} className="flex items-center gap-2">
            <Light status={p.usage_pct >= 100 ? "red" : p.usage_pct >= 80 ? "yellow" : "green"} size="h-2.5 w-2.5" />
            <span className="text-slate-300">{name}</span>
            <span className="text-xs text-slate-500">
              {p.used}{p.limit ? ` / ${p.limit}` : ""} calls · {fmt(p.usage_pct, 0)}%
            </span>
            {(p.shed_level || 0) > 0 && (
              <span className="ml-auto text-xs text-amber-300">
                shedding {p.shed.tier3 ? "T3" : ""}{p.shed.tier2 ? "+T2" : ""}
                {p.shed.tier1_cadence_mult > 1 ? " · T1 slowed" : ""}
              </span>
            )}
          </li>
        ))}
      </ul>

      <p className="mt-2 text-xs text-slate-500">
        {poll ? (
          <>
            {poll.polled_symbols || 0} name(s) polled · kill-switch RS3M {poll.killswitch_runs_today || 0}/{poll.killswitch_target} today
            {poll.escalated_symbols?.length ? ` · escalated: ${poll.escalated_symbols.join(", ")}` : ""}
          </>
        ) : "Scheduler idle"}
      </p>

      {shed.length > 0 && (
        <p className="mt-1 text-xs text-amber-400">
          Budget pressure — shedding cheap tiers first; Tier 0 (open positions) never shed.
        </p>
      )}
      {poll?.recent_alerts?.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-xs text-amber-300/90">
          {poll.recent_alerts.slice(-4).map((a, i) => (
            <li key={i}>⚠ {a.detail}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function DataHealth() {
  const { data, error, loading, reload } = useApi(api.dataHealth, [], 120000);
  const [refreshing, setRefreshing] = React.useState(false);
  const [hotBusy, setHotBusy] = React.useState(false);

  async function refresh() {
    setRefreshing(true);
    try {
      await api.maintenanceRefresh();
      await reload();
    } finally {
      setRefreshing(false);
    }
  }

  async function refreshHot() {
    setHotBusy(true);
    try {
      await api.refreshHot();
      await reload();
    } finally {
      setHotBusy(false);
    }
  }

  if (loading && !data) return <Card title="Data health"><Loading /></Card>;
  if (error) return <Card title="Data health"><p className="text-sm text-rose-400">{error}</p></Card>;

  const providers = data?.providers || {};
  const sources = providers.sources || {};
  const ages = data?.ohlcv_cache_age_hours || {};
  const tok = data?.schwab_token || {};

  return (
    <Card
      title="Data health"
      right={
        !data?.demo && (
          <button onClick={refresh} disabled={refreshing}
                  className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50">
            {refreshing ? "Refreshing…" : "Refresh earnings/dividends"}
          </button>
        )
      }
    >
      {data?.demo ? (
        <p className="text-sm text-slate-500">Demo mode — synthetic cache, providers unused.</p>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <div className="mb-1 text-xs uppercase tracking-wide text-slate-500">Last successful fetch</div>
            <ul className="space-y-1 text-sm">
              {Object.entries(SOURCE_LABELS).map(([key, label]) => (
                <li key={key} className="flex items-center gap-2">
                  <Light status={sources[key] ? "green" : "yellow"} size="h-2.5 w-2.5" />
                  <span className="text-slate-300">{label}</span>
                  <span className="ml-auto text-xs text-slate-500">
                    {sources[key] ? `${sources[key].at.replace("T", " ")} (${sources[key].symbol})` : "no success this session"}
                  </span>
                </li>
              ))}
            </ul>
            {providers.fallback_events > 0 && (
              <p className="mt-1 text-xs text-amber-300">
                Alpha Vantage covered for Schwab {providers.fallback_events} time(s) this session.
              </p>
            )}
            <p className="mt-2 text-xs text-slate-500">
              Schwab token: {tok.status || "missing"}
              {tok.daysLeft != null && ` · ${fmt(tok.daysLeft, 1)}d left`}
            </p>
          </div>
          <div>
            <div className="mb-1 flex items-center justify-between gap-2">
              <span className="text-xs uppercase tracking-wide text-slate-500">OHLCV cache age (hot set)</span>
              <button onClick={refreshHot} disabled={hotBusy}
                      title="Force-refresh the hot set now (open positions + live entry/earnings candidates)"
                      className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50">
                {hotBusy ? "Refreshing…" : "Refresh hot now"}
              </button>
            </div>
            {data?.hot_refresh && (
              <p className="mb-1 text-xs text-slate-500">
                {data.hot_refresh.count} hot ticker(s) · every {data.hot_refresh.cadence_minutes}m in market hours
                {data.hot_refresh.last_refresh
                  ? ` · last ${data.hot_refresh.last_refresh.slice(11, 16)}`
                  : " · not run yet"}
                {!data.hot_refresh.enabled && " · disabled"}
              </p>
            )}
            <ul className="space-y-1 text-sm">
              {Object.entries(ages).map(([sym, age]) => (
                <li key={sym} className="flex items-center gap-2">
                  <Light status={age == null ? "red" : age <= 30 ? "green" : "yellow"} size="h-2.5 w-2.5" />
                  <span className="text-slate-300">{sym}</span>
                  <span className="ml-auto text-xs text-slate-500">{age == null ? "no cache" : `${fmt(age, 1)}h`}</span>
                </li>
              ))}
            </ul>
            <p className="mt-2 text-xs text-slate-500">
              Earnings cache: {data?.earnings_cache?.entries ?? 0} entries
              {data?.earnings_cache?.oldest_fetched_at && ` · oldest ${data.earnings_cache.oldest_fetched_at.slice(0, 16).replace("T", " ")}`}
              <br />
              Dividends cache: {data?.dividends_cache?.entries ?? 0} entries
              {data?.dividends_cache?.oldest_fetched_at && ` · oldest ${data.dividends_cache.oldest_fetched_at.slice(0, 16).replace("T", " ")}`}
            </p>
          </div>
        </div>
      )}
      {!data?.demo && <TieredScheduler data={data} />}
      <ReconcileStatus />
      {!data?.demo && <LiveFillVerify />}
      {!data?.demo && <UniverseCheck />}
    </Card>
  );
}
