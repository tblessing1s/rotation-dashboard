import React from "react";
import { api } from "../api.js";
import { Card, ChartLink, Pill, Loading, ErrorState, Modal, useApi } from "./ui.jsx";
import { useToast } from "./Toast.jsx";
import { explainRec, explainResolution, ticketSummary } from "../recWhy.js";
import TrustScoreboard from "./TrustScoreboard.jsx";

// The recommendation engine's own page: everything open, why it fired, what
// acting on it does, and the trust instrumentation that grades the engine —
// gathered in one place instead of split across Overview's digest, a
// position card, and a Settings sub-section. Nothing here places an order:
// Execute still routes to the entry ticket (ENTER) or the position card
// (everything else), exactly as the Overview digest already does — this page
// adds the ability to see and act on the FULL open list, dismiss with a
// reason, and pre-approve a staged settle, none of which fit on the digest.

const REC_BADGE = {
  EXIT: "border-rose-500/40 bg-rose-500/15 text-rose-300",
  DEFEND: "border-amber-500/40 bg-amber-500/15 text-amber-300",
  ROLL_OUT: "border-sky-500/40 bg-sky-500/15 text-sky-300",
  ROLL_DOWN: "border-amber-500/40 bg-amber-500/15 text-amber-300",
  ENTER: "border-emerald-500/40 bg-emerald-500/15 text-emerald-300",
};

const OVERRIDE_REASONS = [
  ["DISAGREE_TIMING", "Disagree with the timing"],
  ["DISAGREE_STRIKE", "Disagree with the strike"],
  ["DISAGREE_ACTION", "Disagree with the action itself"],
  ["EXTERNAL_INFO", "Acting on external information"],
  ["DISCIPLINE_LAPSE", "Discipline lapse (logged honestly)"],
  ["OTHER", "Other — typed note required"],
];

const SETTLE_REASON_LABEL = {
  SETTLE_WINDOW: "post-open settle window",
  ENTRY_WINDOW: "entry window",
  CLOSE_BLACKOUT: "close blackout",
  MARKET_CLOSED: "market closed",
};

// Circuit-breaker auto-exit — the one place this app ever closes a position
// with nobody clicking anything. Off by default for every condition; turning
// one on requires an explicit confirmation (mirrors LiveTradingSwitch's
// enable flow), turning one off is immediate.
const AUTO_EXIT_CONDITIONS = [
  { id: "drawdown", label: "15% drop from the high since entry",
    detail: "The stock has fallen 15% from its highest close since this position was entered — a trailing floor that only ratchets up, never back down to the entry price." },
  { id: "ma_fast", label: "3 closes below the 50-day MA",
    detail: "3 consecutive daily closes below the 50-day moving average." },
  { id: "ma_slow", label: "A close below the 200-day MA",
    detail: "A single daily close below the 200-day moving average — the final backstop." },
];

function AutoExitPermissions({ perms, onChanged }) {
  const toast = useToast();
  const [busy, setBusy] = React.useState(null); // condition id currently saving
  const [confirming, setConfirming] = React.useState(null); // condition object being enabled

  async function apply(condition, on) {
    setBusy(condition);
    try {
      await api.setCircuitBreakerAutoExit(condition, on);
      toast.show(`Circuit-breaker auto-exit ${on ? "enabled" : "disabled"} for ${condition}`,
        { type: on ? "success" : "info" });
      onChanged();
    } catch (e) {
      toast.show(String(e.message || e), { type: "error" });
    } finally {
      setBusy(null);
      setConfirming(null);
    }
  }

  const anyOn = AUTO_EXIT_CONDITIONS.some((c) => perms?.[c.id]);

  return (
    <Card
      title="Circuit-breaker auto-exit"
      right={
        <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-amber-300">
          {anyOn ? "armed" : "off"}
        </span>
      }
    >
      <p className="mb-3 text-xs text-slate-400">
        Off by default, per condition. Granting one lets the engine close the position —
        buy back the call, then sell the shares — the moment that specific condition trips,
        with nobody clicking anything. It still goes through the normal Paper/Live switch: it
        stays paper (logged, no order sent) until you separately enable Live Trading in Settings.
      </p>
      <div className="divide-y divide-slate-800">
        {AUTO_EXIT_CONDITIONS.map((c) => {
          const on = !!perms?.[c.id];
          return (
            <div key={c.id} className="flex items-center justify-between gap-4 py-2.5">
              <div className="min-w-0">
                <div className="text-sm font-medium text-slate-200">{c.label}</div>
                <div className="mt-0.5 text-xs text-slate-500">{c.detail}</div>
              </div>
              {on ? (
                <button
                  onClick={() => apply(c.id, false)}
                  disabled={busy === c.id}
                  className="shrink-0 rounded-lg border border-emerald-600/50 bg-emerald-500/15 px-3 py-1.5 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/25 disabled:opacity-50"
                >
                  {busy === c.id ? "Saving…" : "✓ Armed — disable"}
                </button>
              ) : (
                <button
                  onClick={() => setConfirming(c)}
                  disabled={busy === c.id}
                  className="shrink-0 rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-1.5 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50"
                >
                  Grant permission
                </button>
              )}
            </div>
          );
        })}
      </div>

      {confirming && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 p-4"
             role="dialog" aria-modal="true" onClick={() => setConfirming(null)}>
          <div className="w-full max-w-md rounded-xl border border-amber-700 bg-slate-900 p-5 shadow-2xl"
               onClick={(e) => e.stopPropagation()}>
            <h2 className="mb-2 text-base font-semibold text-slate-100">
              Auto-exit on "{confirming.label}"?
            </h2>
            <p className="text-sm text-amber-200">
              The next time this condition trips, the engine will close the position — buy back
              the call, then sell the shares — <span className="font-semibold">without you clicking
              anything</span>. Nothing else about the circuit breaker changes; every other
              condition still just recommends.
            </p>
            <p className="mt-2 rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-2 text-xs text-slate-300">
              This still respects Paper/Live: it stays paper (logged only, no order sent) until
              you separately enable Live Trading in Settings.
            </p>
            <div className="mt-4 flex items-center justify-end gap-2">
              <button onClick={() => setConfirming(null)} disabled={busy === confirming.id}
                      className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800 disabled:opacity-40">
                Cancel
              </button>
              <button onClick={() => apply(confirming.id, true)} disabled={busy === confirming.id}
                      className="rounded-lg bg-amber-500/20 px-4 py-2 text-sm font-semibold text-amber-300 hover:bg-amber-500/30 disabled:opacity-40">
                {busy === confirming.id ? "Enabling…" : "Grant permission"}
              </button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

// Friendly names for the armed-trigger codes autopilot.status() returns.
const GRANT_LABELS = {
  "exit:drawdown": "Exit — drawdown line",
  "exit:ma_fast": "Exit — 3 closes below the 50-day MA",
  "exit:ma_slow": "Exit — 200-day MA",
  "roll:ROLL_EXTRINSIC_CAPTURED": "Roll — extrinsic captured",
  "roll:DEFEND_APPROACHING_STRIKE": "Defend — stock getting close to the strike",
  "roll:DEFEND_BELOW_STRIKE": "Defend — closed below the strike",
  "roll:ROLL_75PCT": "Roll — 75% of the premium decayed",
  "roll:ROLL_SCHEDULED_WEEKLY": "Roll — weekly expiry",
};

// The pop-up shown when autopilot is switched on (and reopenable from "Settings"): every
// parameter it will watch, editable, with the default beside it. Saving stores them for
// this book; "Turn on" saves first, then flips the master switch.
function AutopilotSettingsModal({ enabling, onClose, onDone }) {
  const toast = useToast();
  const { data, error } = useApi(() => api.getAutopilotParams(), [], null);
  const [vals, setVals] = React.useState(null);
  const [posture, setPosture] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);

  React.useEffect(() => {
    if (data && vals === null) {
      setVals(Object.fromEntries(data.params.map((p) => [p.key, p.value])));
      setPosture(data.posture.value);
    }
  }, [data, vals]);

  async function save(turnOn) {
    setBusy(true); setErr(null);
    try {
      const changed = {};
      for (const p of data.params) {
        const v = p.kind === "bool" ? vals[p.key] : Number(vals[p.key]);
        if (p.kind !== "bool" && (vals[p.key] === "" || Number.isNaN(v))) {
          throw new Error(`${p.label} must be a number`);
        }
        if (v !== p.value) changed[p.key] = v;
      }
      const body = {};
      if (Object.keys(changed).length) body.values = changed;
      if (posture !== data.posture.value) body.posture = posture;
      if (Object.keys(body).length) await api.saveAutopilotParams(body);
      if (turnOn) await api.setAutopilot(true);
      window.dispatchEvent(new Event("cfm:autopilot-changed"));
      toast.show(turnOn ? "Autopilot ON — settings saved" : "Autopilot settings saved",
        { type: "success" });
      onDone();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  async function resetAll() {
    setBusy(true); setErr(null);
    try {
      const fresh = await api.saveAutopilotParams({ reset: true });
      setVals(Object.fromEntries(fresh.params.map((p) => [p.key, p.value])));
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  const groups = [];
  for (const p of data?.params || []) {
    let g = groups.find((x) => x.name === p.group);
    if (!g) { g = { name: p.group, items: [] }; groups.push(g); }
    g.items.push(p);
  }
  const inp = "w-20 rounded border border-slate-700 bg-slate-900/60 px-2 py-1 text-right text-sm text-slate-100";

  return (
    <Modal onClose={busy ? undefined : onClose} maxWidth="max-w-2xl">
      <Card title={enabling ? "Turn on autopilot — review what it will watch" : "Autopilot settings"}>
        {error && <p className="text-sm text-rose-400">{error}</p>}
        {!data && !error && <Loading />}
        {data && vals && (
          <div className="max-h-[70vh] space-y-4 overflow-y-auto pr-1">
            <p className="text-xs text-slate-400">
              Autopilot monitors your open positions against the values below. Adjust anything you
              want before it starts; the default is shown beside each value.
            </p>

            {data.granted.length === 0 ? (
              <p className="rounded-lg border border-amber-700/60 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
                No triggers are granted yet, so autopilot would not act on anything. Grant the ones
                you want in the lists on this tab.
              </p>
            ) : (
              <div>
                <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                  Armed triggers
                </div>
                <ul className="flex flex-wrap gap-1.5">
                  {data.granted.map((g) => (
                    <li key={g} className="rounded-full border border-emerald-700/50 bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-300">
                      {GRANT_LABELS[g] || g}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {groups.map((g) => (
              <div key={g.name}>
                <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">{g.name}</div>
                <div className="divide-y divide-slate-800">
                  {g.items.map((p) => (
                    <div key={p.key} className="flex items-center justify-between gap-4 py-2">
                      <div className="min-w-0">
                        <div className="text-sm text-slate-200">{p.label}</div>
                        <div className="text-[11px] text-slate-500">{p.help}</div>
                      </div>
                      <div className="flex shrink-0 items-center gap-2 text-xs text-slate-400">
                        {p.kind === "bool" ? (
                          <input type="checkbox" checked={!!vals[p.key]}
                                 onChange={(e) => setVals((v) => ({ ...v, [p.key]: e.target.checked }))} />
                        ) : (
                          <input type="number" step={p.kind === "int" ? 1 : 0.1} min={p.min} max={p.max}
                                 value={vals[p.key]} className={inp}
                                 onChange={(e) => setVals((v) => ({ ...v, [p.key]: e.target.value }))} />
                        )}
                        <span className="w-24 text-left">{p.kind === "bool" ? "" : p.unit}</span>
                        <span className="w-16 text-right text-[10px] text-slate-600">
                          default {p.kind === "bool" ? (p.default ? "on" : "off") : p.default}
                        </span>
                      </div>
                    </div>
                  ))}
                  {g.name === "New strike" && (
                    <div className="flex items-center justify-between gap-4 py-2">
                      <div>
                        <div className="text-sm text-slate-200">Strike posture</div>
                        <div className="text-[11px] text-slate-500">
                          How deep in the money the new call must be (with the market regime). Applies to the whole book.
                        </div>
                      </div>
                      <select value={posture} onChange={(e) => setPosture(e.target.value)}
                              className="rounded border border-slate-700 bg-slate-900/60 px-2 py-1 text-sm text-slate-100">
                        {data.posture.options.map((o) => <option key={o} value={o}>{o}</option>)}
                      </select>
                    </div>
                  )}
                </div>
              </div>
            ))}

            <div>
              <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">Fixed rules</div>
              <ul className="list-disc space-y-0.5 pl-5 text-[11px] text-slate-500">
                {data.fixed.map((f) => <li key={f}>{f}</li>)}
              </ul>
            </div>
            {err && <p className="text-sm text-rose-400">{err}</p>}
          </div>
        )}
        <div className="mt-4 flex items-center justify-between gap-2">
          <button onClick={resetAll} disabled={busy || !data}
                  className="rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-1.5 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50">
            Reset to defaults
          </button>
          <div className="flex gap-2">
            <button onClick={onClose} disabled={busy}
                    className="rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-1.5 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50">
              Cancel
            </button>
            <button onClick={() => save(enabling)} disabled={busy || !vals}
                    className="rounded-lg border border-emerald-600/50 bg-emerald-500/15 px-3 py-1.5 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/25 disabled:opacity-50">
              {busy ? "Saving…" : enabling ? "Save & turn autopilot ON" : "Save"}
            </button>
          </div>
        </div>
      </Card>
    </Modal>
  );
}

// "Why did / didn't autopilot trade?" — the standing preconditions (switch, grants,
// live vs paper, passes running, reconciliation freeze), what each open position is
// measuring against its thresholds, and the recent decision log. See backend/autopilot_log.py.
const CHECK_STYLE = {
  ok: ["✓", "text-emerald-400"], warn: ["!", "text-amber-300"],
  bad: ["✗", "text-rose-400"], info: ["•", "text-slate-500"],
};
const RESULT_STYLE = {
  placed: "border-emerald-700/50 bg-emerald-500/10 text-emerald-300",
  exited: "border-emerald-700/50 bg-emerald-500/10 text-emerald-300",
  skipped: "border-slate-700 bg-slate-800/60 text-slate-300",
  failed: "border-rose-700/50 bg-rose-500/10 text-rose-300",
};

function AutopilotStatus() {
  const { data, error, reload } = useApi(() => api.getAutopilotDiagnostics(), [], 60000);
  React.useEffect(() => {
    const h = () => reload();
    window.addEventListener("cfm:autopilot-changed", h);
    return () => window.removeEventListener("cfm:autopilot-changed", h);
  }, [reload]);
  const fmtTime = (t) => (t ? new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "—");
  const pct = (v) => (v == null ? "—" : `${Number(v).toFixed(1)}%`);
  return (
    <Card title="Autopilot status — why it did or didn't trade"
          right={<button onClick={reload}
                  className="rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-1.5 text-xs font-semibold text-slate-300 hover:bg-slate-800">Refresh</button>}>
      {error && <p className="text-sm text-rose-400">{error}</p>}
      {!data && !error && <Loading />}
      {data && (
        <div className="space-y-4">
          <ul className="space-y-1.5">
            {data.checks.map((c) => {
              const [icon, color] = CHECK_STYLE[c.level] || CHECK_STYLE.info;
              return (
                <li key={c.id} className="flex gap-2 text-sm">
                  <span className={`w-4 shrink-0 text-center font-bold ${color}`}>{icon}</span>
                  <span><span className="text-slate-200">{c.label}.</span>{" "}
                    <span className="text-slate-400">{c.detail}</span></span>
                </li>
              );
            })}
          </ul>

          {data.open.length > 0 && (
            <div>
              <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">Open recommendations</div>
              <table className="w-full text-xs">
                <tbody className="text-slate-300">
                  {data.open.map((o, i) => (
                    <tr key={i} className="border-t border-slate-800/60">
                      <td className="py-1 pr-3 font-mono">{o.ticker}</td>
                      <td className="py-1 pr-3">{o.trigger_rule}</td>
                      <td className={`py-1 pr-3 font-semibold ${o.autopilot_will_act ? "text-emerald-300" : "text-slate-400"}`}>
                        {o.autopilot_will_act ? "autopilot will act" : "autopilot will not act"}
                      </td>
                      <td className="py-1 text-slate-500">{o.why}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {data.watch.length > 0 && (
            <div>
              <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                What it's measuring (as of the last pass)
              </div>
              <div className="overflow-x-auto">
                <table className="w-full whitespace-nowrap text-xs">
                  <thead>
                    <tr className="text-left uppercase tracking-wide text-slate-500">
                      {["ticker", "price", "short", "dte", "extrinsic captured", "above strike", "as of"].map((h) => <th key={h} className="py-1 pr-3">{h}</th>)}
                    </tr>
                  </thead>
                  <tbody className="font-mono text-slate-300">
                    {data.watch.flatMap((w) => (w.shorts.length ? w.shorts : [null]).map((s, i) => (
                      <tr key={`${w.ticker}-${i}`} className="border-t border-slate-800/60">
                        <td className="py-1 pr-3">{w.ticker}</td>
                        <td className="py-1 pr-3">{w.price ?? "—"}</td>
                        <td className="py-1 pr-3">{s ? `${s.strike}C` : "no short open"}</td>
                        <td className="py-1 pr-3">{s?.dte ?? "—"}</td>
                        <td className="py-1 pr-3">{s ? `${pct(s.extrinsic_captured_pct)} of ${s.extrinsic_threshold_pct}%` : "—"}</td>
                        <td className="py-1 pr-3">{s ? `${pct(s.distance_pct)} (acts within ${s.band_pct}%)` : "—"}</td>
                        <td className="py-1 pr-3 font-sans text-slate-500">{fmtTime(w.as_of)}</td>
                      </tr>
                    )))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <div>
            <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">Recent decisions</div>
            {data.decisions.length === 0 ? (
              <p className="text-xs text-slate-500">
                Autopilot hasn't considered any recommendation yet. It only logs when a trigger
                has fired for a position.
              </p>
            ) : (
              <ul className="space-y-1">
                {data.decisions.map((d, i) => (
                  <li key={i} className="flex flex-wrap items-baseline gap-2 text-xs">
                    <span className="w-28 shrink-0 text-slate-500">{fmtTime(d.at)}</span>
                    <span className="font-mono text-slate-300">{d.ticker}</span>
                    <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase ${RESULT_STYLE[d.result] || RESULT_STYLE.skipped}`}>{d.result}</span>
                    <span className="text-slate-500">{d.trigger_rule}</span>
                    <span className="text-slate-400">{d.reason}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}

// Master autopilot switch — turns EVERY unattended action (both grant lists below)
// on or off without touching the individual grants. Turning it ON opens the settings
// pop-up first. See backend/autopilot.py and autopilot_params.py.
function AutopilotSwitch({ enabled, onChanged }) {
  const toast = useToast();
  const [busy, setBusy] = React.useState(false);
  const [modal, setModal] = React.useState(null); // null | "enable" | "edit"
  const on = enabled !== false; // never set == on (pre-existing grants keep working)
  async function turnOff() {
    setBusy(true);
    try {
      await api.setAutopilot(false);
      window.dispatchEvent(new Event("cfm:autopilot-changed"));
      toast.show("Autopilot OFF", { type: "info" });
      onChanged();
    } catch (e) {
      toast.show(String(e.message || e), { type: "error" });
    } finally {
      setBusy(false);
    }
  }
  const btn = "rounded-lg border px-3 py-1.5 text-xs font-semibold disabled:opacity-50";
  return (
    <Card
      title="Autopilot"
      right={
        <div className="flex items-center gap-2">
          <button onClick={() => setModal("edit")}
                  className={`${btn} border-slate-700 bg-slate-800/60 text-slate-300 hover:bg-slate-800`}>
            Settings
          </button>
          {on ? (
            <button onClick={turnOff} disabled={busy}
                    className={`${btn} border-emerald-600/50 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25`}>
              {busy ? "Saving…" : "● ON — turn off"}
            </button>
          ) : (
            <button onClick={() => setModal("enable")}
                    className={`${btn} border-slate-700 bg-slate-800/60 text-slate-300 hover:bg-slate-800`}>
              ○ OFF — turn on
            </button>
          )}
        </div>
      }
    >
      <p className="text-xs text-slate-400">
        Master switch over the two lists below. OFF: nothing acts unattended — recommendations
        still appear and can be executed by hand. ON: whichever triggers you've granted act on
        their own. Turning it on shows you every parameter it will watch first. Your individual
        grants are kept either way.
      </p>
      {modal && (
        <AutopilotSettingsModal
          enabling={modal === "enable"}
          onClose={() => setModal(null)}
          onDone={() => { setModal(null); onChanged(); }}
        />
      )}
    </Card>
  );
}

// Roll/defend auto-execute — the same "opt-in, per-trigger, default off,
// confirm-to-enable" pattern as AutoExitPermissions above, extended to the
// routine roll/defend triggers instead of EXIT. See
// backend/recommendation_auto_execute.py.
const AUTO_EXECUTE_TRIGGERS = [
  { id: "ROLL_SCHEDULED_WEEKLY", label: "Weekly roll due",
    detail: "The short is inside the expiry-warning window and hasn't been rolled yet — the scheduled weekly cadence." },
  { id: "ROLL_75PCT", label: "75% of the premium captured",
    detail: "The short has decayed ≥75% of its sale premium with more than a couple days left — the early-roll rule." },
  { id: "ROLL_EXTRINSIC_CAPTURED", label: "Extrinsic mostly banked",
    detail: "≥80% of the extrinsic sold at entry is captured — rolls up in place or up-and-out, whichever the current week's runway supports." },
  { id: "DEFEND_BELOW_STRIKE", label: "Stock closed below the strike",
    detail: "The underlying closed under the short strike — the defensive roll-down." },
  { id: "DEFEND_APPROACHING_STRIKE", label: "Stock getting close to the strike",
    detail: "Live price is within a few percent of the short strike (still above it) — rolls down in place with runway left this week, down-and-out to the next weekly when it's nearly over. New strike follows the regime/posture." },
];

function RollDefendAutoExecutePermissions({ perms, onChanged }) {
  const toast = useToast();
  const [busy, setBusy] = React.useState(null); // trigger id currently saving
  const [confirming, setConfirming] = React.useState(null); // trigger object being enabled

  async function apply(trigger, on) {
    setBusy(trigger);
    try {
      await api.setRollDefendAutoExecute(trigger, on);
      toast.show(`Roll/defend auto-execute ${on ? "enabled" : "disabled"} for ${trigger}`,
        { type: on ? "success" : "info" });
      onChanged();
    } catch (e) {
      toast.show(String(e.message || e), { type: "error" });
    } finally {
      setBusy(null);
      setConfirming(null);
    }
  }

  const anyOn = AUTO_EXECUTE_TRIGGERS.some((t) => perms?.[t.id]);

  return (
    <Card
      title="Roll / defend auto-execute"
      right={
        <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-amber-300">
          {anyOn ? "armed" : "off"}
        </span>
      }
    >
      <p className="mb-3 text-xs text-slate-400">
        Off by default, per trigger. Granting one lets the engine roll the short —
        buy back the current call, then sell the new one — the moment that specific
        trigger fires, with nobody clicking anything. It still goes through the normal
        Paper/Live switch: it stays paper (logged, no order sent) until you separately
        enable Live Trading in Settings.
      </p>
      <div className="divide-y divide-slate-800">
        {AUTO_EXECUTE_TRIGGERS.map((t) => {
          const on = !!perms?.[t.id];
          return (
            <div key={t.id} className="flex items-center justify-between gap-4 py-2.5">
              <div className="min-w-0">
                <div className="text-sm font-medium text-slate-200">{t.label}</div>
                <div className="mt-0.5 text-xs text-slate-500">{t.detail}</div>
              </div>
              {on ? (
                <button
                  onClick={() => apply(t.id, false)}
                  disabled={busy === t.id}
                  className="shrink-0 rounded-lg border border-emerald-600/50 bg-emerald-500/15 px-3 py-1.5 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/25 disabled:opacity-50"
                >
                  {busy === t.id ? "Saving…" : "✓ Armed — disable"}
                </button>
              ) : (
                <button
                  onClick={() => setConfirming(t)}
                  disabled={busy === t.id}
                  className="shrink-0 rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-1.5 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50"
                >
                  Grant permission
                </button>
              )}
            </div>
          );
        })}
      </div>

      {confirming && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 p-4"
             role="dialog" aria-modal="true" onClick={() => setConfirming(null)}>
          <div className="w-full max-w-md rounded-xl border border-amber-700 bg-slate-900 p-5 shadow-2xl"
               onClick={(e) => e.stopPropagation()}>
            <h2 className="mb-2 text-base font-semibold text-slate-100">
              Auto-roll on "{confirming.label}"?
            </h2>
            <p className="text-sm text-amber-200">
              The next time this trigger fires, the engine will roll the short — buy back
              the current call, then sell the new one — <span className="font-semibold">without
              you clicking anything</span>. Nothing else about the recommendation engine
              changes; every other trigger still just recommends.
            </p>
            <p className="mt-2 rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-2 text-xs text-slate-300">
              This still respects Paper/Live: it stays paper (logged only, no order sent) until
              you separately enable Live Trading in Settings.
            </p>
            <div className="mt-4 flex items-center justify-end gap-2">
              <button onClick={() => setConfirming(null)} disabled={busy === confirming.id}
                      className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800 disabled:opacity-40">
                Cancel
              </button>
              <button onClick={() => apply(confirming.id, true)} disabled={busy === confirming.id}
                      className="rounded-lg bg-amber-500/20 px-4 py-2 text-sm font-semibold text-amber-300 hover:bg-amber-500/30 disabled:opacity-40">
                {busy === confirming.id ? "Enabling…" : "Grant permission"}
              </button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

function useNow(intervalMs = 60000) {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

function validity(validUntil, now) {
  const t = Date.parse(validUntil || "");
  if (Number.isNaN(t)) return null;
  const hrs = (t - now) / 3600000;
  if (hrs <= 0) return { text: "expired", tone: "text-rose-400" };
  if (hrs < 1) return { text: `expires in ${Math.max(1, Math.round(hrs * 60))}m`, tone: "text-amber-300" };
  if (hrs <= 6) return { text: `expires in ${Math.round(hrs)}h`, tone: "text-amber-300" };
  return { text: `valid ${Math.round(hrs)}h`, tone: "text-slate-500" };
}

function settleInfo(settle, now) {
  if (!settle || !settle.status) return null;
  if (settle.status !== "PENDING_SETTLE") return { status: settle.status };
  const t = Date.parse(settle.executable_at || "");
  const etTime = Number.isNaN(t)
    ? null
    : new Intl.DateTimeFormat("en-US", {
        hour: "numeric", minute: "2-digit", timeZone: "America/New_York", hour12: false,
      }).format(t);
  const mins = Number.isNaN(t) ? null : Math.round((t - now) / 60000);
  return {
    status: "PENDING_SETTLE",
    etTime,
    countdown: mins == null ? "" : mins > 0 ? `in ${mins}m` : "now",
    preApproved: !!settle.pre_approved,
    reason: SETTLE_REASON_LABEL[settle.reason] || settle.reason,
  };
}

// "engine ran 12 min ago" — with the freeze called out, mirroring Overview's
// digest line so the two surfaces never disagree about the engine's state.
function EngineRunLine({ run }) {
  const now = useNow();
  if (!run?.at) {
    return <span className="text-sm text-slate-500">Engine hasn't run yet.</span>;
  }
  const t = Date.parse(run.at);
  const mins = Number.isNaN(t) ? null : Math.max(0, Math.round((now - t) / 60000));
  const ago = mins == null ? String(run.at).slice(0, 16).replace("T", " ") + "Z"
    : mins < 1 ? "just now"
    : mins < 60 ? `${mins} min ago`
    : mins < 48 * 60 ? `${Math.round(mins / 60)}h ago`
    : `${Math.round(mins / 1440)}d ago`;
  const frozen = !!run.reconcile_frozen;
  return (
    <span className={`text-sm ${frozen ? "text-amber-300" : "text-slate-400"}`}>
      Engine ran {ago}.
      {frozen && (
        <> <span className="font-semibold">Skipped</span> — reconciliation freeze
          {run.frozen_tickers?.length ? ` (${run.frozen_tickers.join(", ")})` : ""}.
          No new recommendations were evaluated on this pass.</>
      )}
    </span>
  );
}

// Dismissal modal — one coded reason is mandatory; OTHER additionally demands
// a typed note (the backend 400s without one).
function DismissModal({ rec, onClose, onDismissed }) {
  const toast = useToast();
  const [reason, setReason] = React.useState(null);
  const [note, setNote] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const noteRequired = reason === "OTHER";
  const canSubmit = !!reason && !(noteRequired && !note.trim()) && !busy;

  async function submit() {
    setBusy(true);
    try {
      await api.dismissRecommendation(rec.rec_id, reason, note.trim() || undefined);
      toast.show(`Dismissed ${rec.action_type} on ${rec.ticker} (${reason})`, { type: "success" });
      onDismissed?.();
    } catch (e) {
      toast.show(String(e.message || e), { type: "error" });
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()} className="w-full max-w-md">
        <Card title={`Dismiss recommendation — ${rec.ticker}`}>
          <p className="text-xs text-slate-500">
            {rec.action_type} · <span className="font-mono">{rec.trigger_rule}</span> ·{" "}
            <span className="font-mono">{rec.rec_id}</span>
          </p>
          <p className="mt-1 text-xs text-slate-400">
            A dismissal writes an immutable override record that feeds the trust scoreboard —
            pick the coded reason that honestly describes why you're not taking this action.
          </p>
          <div className="mt-3 space-y-1">
            {OVERRIDE_REASONS.map(([code, label]) => (
              <label
                key={code}
                className={`flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-sm ${
                  reason === code ? "bg-sky-500/10" : "hover:bg-slate-800/50"
                }`}
              >
                <input
                  type="radio" name={`dismiss-${rec.rec_id}`} checked={reason === code}
                  onChange={() => setReason(code)} className="accent-sky-400"
                />
                <span className="text-slate-200">{label}</span>
                <span className="ml-auto font-mono text-[10px] text-slate-500">{code}</span>
              </label>
            ))}
          </div>
          <label className="mt-3 block text-[10px] uppercase tracking-wide text-slate-500">
            note{noteRequired ? " — required for OTHER" : " (optional)"}
            <textarea
              value={note} onChange={(e) => setNote(e.target.value)} rows={2}
              placeholder={noteRequired ? "A typed note is required for OTHER." : "Optional context…"}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm normal-case tracking-normal text-slate-100 placeholder:text-slate-600"
            />
          </label>
          <div className="mt-3 flex items-center justify-end gap-2">
            <button
              onClick={onClose}
              className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800"
            >
              Cancel
            </button>
            <button
              onClick={submit} disabled={!canSubmit}
              className="rounded-lg border border-rose-700 bg-rose-500/10 px-3 py-1.5 text-sm font-semibold text-rose-300 hover:bg-rose-500/20 disabled:opacity-40"
            >
              {busy ? "Dismissing…" : "Dismiss"}
            </button>
          </div>
        </Card>
      </div>
    </div>
  );
}

// One open recommendation: badge + plain-English why (expand in place) + the
// proposed ticket + settle state, with Dismiss / Pre-approve / navigate.
function RecRow({ rec, now, expanded, onToggle, onGo, onDismiss, onPreapprove }) {
  const v = validity(rec.valid_until, now);
  const x = explainRec(rec);
  const badge = REC_BADGE[rec.action_type] || "border-slate-600 bg-slate-800/60 text-slate-300";
  const s = settleInfo(rec.settle, now);
  const pending = s?.status === "PENDING_SETTLE";

  return (
    <div className="rounded-lg border border-slate-700 bg-slate-950/60 px-3 py-2 text-sm">
      <div role="button" tabIndex={0} onClick={onToggle} onKeyDown={(e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); (onToggle)(e); } }} className="flex cursor-pointer w-full flex-wrap items-center gap-2 text-left">
        <span className="font-semibold text-slate-100">{rec.ticker}</span>
        <ChartLink ticker={rec.ticker} size="h-3.5 w-3.5" className="ml-1 align-middle" />
        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${badge}`}>
          {(rec.action_type || "").replaceAll("_", " ")}
        </span>
        <span className="text-xs font-medium text-slate-300" title={`Trigger rule: ${rec.trigger_rule}`}>
          {x?.label || rec.trigger_rule}
        </span>
        {v && (
          <span className={`ml-auto text-xs ${v.tone}`} title={`valid until ${rec.valid_until}`}>
            {v.text}
          </span>
        )}
        <span className="shrink-0 text-xs text-slate-500">{expanded ? "▲" : "▼"}</span>
      </div>

      {pending && (
        <div className="mt-1.5 flex flex-wrap items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-200">
          <span title="The market-settle gate deferred the order; the alert already fired.">
            ⏳ Staged — executable{s.etTime ? ` ${s.etTime} ET` : " next session"}
            {s.countdown ? ` (${s.countdown})` : ""}
          </span>
          {s.reason && <span className="text-amber-300/70">· {s.reason}</span>}
          {s.preApproved && <span className="font-semibold text-emerald-300">· pre-approved</span>}
        </div>
      )}

      {expanded && (
        <div className="mt-1.5 rounded-md border border-slate-800 bg-slate-900/50 px-2.5 py-1.5">
          {x?.why && <p className="text-xs leading-relaxed text-slate-200">{x.why}</p>}
          {x?.effect && (
            <p className="mt-1 text-[11px] text-emerald-300/90">
              <span className="font-semibold">{x.action}:</span> {x.effect}
            </p>
          )}
          {(x?.numbers?.length > 0 || x?.also?.length > 0) && (
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {x.numbers.map((n, i) => (
                <span key={i} className="rounded-full border border-slate-700 bg-slate-950/60 px-2 py-0.5 text-[10px] text-slate-300">
                  <span className="text-slate-500">{n.k} </span>
                  <span className="font-semibold text-slate-100">{n.v}</span>
                </span>
              ))}
              {x.also.length > 0 && (
                <span className="rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-200"
                      title="Other rules that fired on the same pass; the recommendation acts on the dominant one">
                  also: {x.also.join(", ")}
                </span>
              )}
            </div>
          )}
          <p className="mt-1.5 text-slate-400">
            <span className="uppercase tracking-wide text-slate-500">ticket</span> · {ticketSummary(rec.proposed_ticket)}
          </p>
        </div>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button
          onClick={onGo}
          className="rounded-full border border-emerald-600/50 bg-emerald-500/10 px-2.5 py-0.5 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20"
        >
          {rec.action_type === "ENTER" ? "Open entry ticket →" : "Go to position →"}
        </button>
        {pending && (
          <button
            onClick={() => onPreapprove(rec, !s.preApproved)}
            title="Auto-submit when the settle window opens — only if the trigger still holds then."
            className={`rounded-full border px-2.5 py-0.5 text-xs font-semibold ${
              s.preApproved
                ? "border-emerald-600/60 bg-emerald-500/20 text-emerald-200 hover:bg-emerald-500/30"
                : "border-amber-600/50 bg-amber-500/10 text-amber-200 hover:bg-amber-500/20"
            }`}
          >
            {s.preApproved ? "✓ Pre-approved" : "Pre-approve"}
          </button>
        )}
        <button
          onClick={onDismiss}
          className="rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-0.5 text-xs text-slate-300 hover:bg-slate-800"
        >
          Dismiss
        </button>
        <span className="ml-auto text-[11px] text-slate-500">Execute lives on the ticket / position card.</span>
      </div>
    </div>
  );
}

function OpenRecommendations({ recs, onGo, onChanged }) {
  const now = useNow();
  const [openId, setOpenId] = React.useState(null);
  const [dismissing, setDismissing] = React.useState(null);

  async function preapprove(rec, approve) {
    try {
      await api.preapproveRecommendation(rec.rec_id, approve);
      onChanged();
    } catch (e) {
      /* the row keeps its current state; the next poll re-syncs */
    }
  }

  // ENTER first (they have no position card, so this is the only place they
  // can be acted on or dismissed) — then everything else, most urgent first.
  const sevRank = { EXIT: 0, DEFEND: 1, ROLL_OUT: 2, ROLL_DOWN: 2 };
  const sorted = [...(recs || [])].sort((a, b) => {
    if (a.action_type === "ENTER" && b.action_type !== "ENTER") return -1;
    if (b.action_type === "ENTER" && a.action_type !== "ENTER") return 1;
    return (sevRank[a.action_type] ?? 9) - (sevRank[b.action_type] ?? 9);
  });

  if (!sorted.length) {
    return (
      <Card title="Open recommendations">
        <p className="text-sm text-emerald-300">All clear — nothing open right now.</p>
      </Card>
    );
  }

  return (
    <Card title={`Open recommendations — ${sorted.length}`}>
      <div className="space-y-2">
        {sorted.map((rec) => (
          <RecRow
            key={rec.rec_id}
            rec={rec}
            now={now}
            expanded={openId === rec.rec_id}
            onToggle={() => setOpenId((id) => (id === rec.rec_id ? null : rec.rec_id))}
            onGo={() => onGo(rec)}
            onDismiss={() => setDismissing(rec)}
            onPreapprove={preapprove}
          />
        ))}
      </div>
      {dismissing && (
        <DismissModal
          rec={dismissing}
          onClose={() => setDismissing(null)}
          onDismissed={() => { setDismissing(null); onChanged(); }}
        />
      )}
    </Card>
  );
}

function ResolutionRow({ res }) {
  const x = explainResolution(res);
  if (!x) return null;
  const at = (res.at || "").slice(0, 16).replace("T", " ");
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs text-slate-400">
      <span className="font-semibold text-slate-200">{res.ticker}</span><ChartLink ticker={res.ticker} size="h-3.5 w-3.5" className="ml-1 align-middle" />
      <span>engine called <span className="text-slate-300">{x.called}</span></span>
      {x.rule && <span className="text-slate-600">({x.rule.toLowerCase()})</span>}
      <span className={`font-medium ${x.tone}`}>{x.verdict}</span>
      {x.where && <span>{x.where}</span>}
      {x.detail && <span className="text-slate-600">· {x.detail}</span>}
      {at && <span className="text-slate-600">· {at}Z</span>}
    </li>
  );
}

function RecentResolutions({ items }) {
  if (!items?.length) {
    return (
      <Card title="Recent resolutions">
        <p className="text-sm text-slate-500">
          Nothing resolved in the last two weeks yet — this fills in as the engine's
          calls get matched (or overridden) by your moves.
        </p>
      </Card>
    );
  }
  return (
    <Card title={`Recent resolutions — last 2 weeks`}>
      <ul className="space-y-1.5">
        {items.map((res, i) => <ResolutionRow key={res.rec_id || i} res={res} />)}
      </ul>
    </Card>
  );
}

export default function RecommendationsTab({ onNavigate, onSelectStock, onAction }) {
  const { data, error, loading, reload } = useApi(api.recommendations, [], 5 * 60 * 1000);
  const toast = useToast();
  const [running, setRunning] = React.useState(false);

  const nav = React.useMemo(() => ({
    focus: (ticker) => onAction?.("focus", ticker),
    enter: (ticker, recId) => onSelectStock?.(ticker, recId),
  }), [onAction, onSelectStock]);

  const goTo = (rec) =>
    rec.action_type === "ENTER" ? nav.enter(rec.ticker, rec.rec_id) : nav.focus(rec.ticker);

  async function runNow() {
    setRunning(true);
    try {
      const r = await api.runRecommendations();
      toast.show(
        `Evaluation pass complete — ${r.emitted ?? 0} emitted, ` +
          `${r.positions_evaluated ?? 0} position(s) + ${r.entry_candidates ?? 0} entry candidate(s) evaluated`,
        { type: "success" },
      );
      await reload();
    } catch (e) {
      toast.show(String(e.message || e), { type: "error" });
    } finally {
      setRunning(false);
    }
  }

  if (loading && !data) return <Card title="Recommendations"><Loading label="Loading the engine's open calls…" /></Card>;
  if (error && !data) return <Card title="Recommendations"><ErrorState error={error} onRetry={reload} /></Card>;

  return (
    <div className="grid gap-4">
      <Card
        title="Recommendation engine"
        right={
          <div className="flex items-center gap-2">
            <Pill status={data?.gate_enforced ? "go" : "unknown"}>
              {data?.gate_enforced ? "settle gate enforced" : "settle gate off"}
            </Pill>
            <button
              onClick={runNow} disabled={running}
              className="rounded-full border border-emerald-600/50 bg-emerald-500/10 px-2.5 py-1 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-50"
            >
              {running ? "Running…" : "Run evaluation pass now"}
            </button>
          </div>
        }
      >
        <EngineRunLine run={data?.last_run} />
        <p className="mt-1 text-xs text-slate-500">
          {data?.open?.length ?? 0} open ({(data?.open_actionable || []).length} actionable) ·{" "}
          {(data?.pending_settle || []).length} staged for settle · {data?.total ?? 0} recommendations total.
        </p>
      </Card>

      <AutopilotSwitch enabled={data?.autopilot_enabled} onChanged={reload} />
      <AutopilotStatus />
      <AutoExitPermissions perms={data?.circuit_breaker_auto_exit} onChanged={reload} />

      <RollDefendAutoExecutePermissions perms={data?.roll_defend_auto_execute} onChanged={reload} />

      <OpenRecommendations
        recs={data?.open_actionable}
        onGo={goTo}
        onChanged={reload}
      />

      <RecentResolutions items={data?.recent_resolutions} />

      <TrustScoreboard />
    </div>
  );
}
