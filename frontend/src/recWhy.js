// Plain-English reads of an engine recommendation, for the operator deciding
// whether to take it. Everything here is DERIVED from the rec's own
// `input_snapshot.trigger_detail` (frozen at emission) — no new data, no new
// authority. A card that says "ROLL_75PCT" tells you which rule fired; this
// says what the rule saw and what taking the action does, in the operator's
// words, with the numbers the decision actually turns on.

const dollars = (n, d = 2) =>
  n == null || Number.isNaN(Number(n)) ? "—" : `$${Number(n).toFixed(d)}`;
const num = (n, d = 1) =>
  n == null || Number.isNaN(Number(n)) ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d });
const dteStr = (dte) => (dte == null ? "" : ` with ${dte} DTE left`);
const shortStr = (s) => (s?.strike != null ? `the ${num(s.strike, 2)} call` : "the short call");

const fmt = (n, d = 2) => num(n, d);
const money = (n) => (n == null ? "—" : "$" + Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 }));

// Human names for the trigger rules (the raw code stays as a tooltip).
export const RULE_LABELS = {
  KILL_RS_SPY_CONFIRMED: "Kill switch — losing to SPY",
  KILL_RS_SECTOR: "Kill switch — losing to sector (retired rule)",
  CIRCUIT_BREAKER: "Circuit breaker",
  WHIPSAW_GUARD: "Defend whipsaw",
  DELTA_COVERAGE_FLOOR: "Coverage floor breached",
  DEFEND_BELOW_STRIKE: "Stock closed below the strike",
  ROLL_75PCT: "75% of the premium captured",
  ROLL_SCHEDULED_WEEKLY: "Weekly roll due",
  ROLL_EXTRINSIC_CAPTURED: "Juice mostly banked",
  JUICE_HURDLE_FAIL: "Juice below target",
  DTE_PLANNED_EXIT: "Planned exit reached",
  EARNINGS_WINDOW: "Earnings inside the window",
  DIVIDEND_ASSIGNMENT_RISK: "Early-assignment risk",
  GATE_ALL_PASS: "Every entry gate passed",
  ALL_CLEAR: "All clear",
};

// What taking the action DOES, per action type — the same sentence for every
// rule so the operator learns the vocabulary once.
export const ACTION_LABELS = {
  ROLL_OUT: "Roll out",
  ROLL_DOWN: "Roll down",
  DEFEND: "Defensive roll",
  EXIT: "Exit",
  ENTER: "Enter",
  NO_ACTION: "No action",
};

const ACTION_EFFECT = {
  ROLL_OUT: "Buy back this week's call and sell next week's — banks what's captured and starts fresh juice.",
  ROLL_DOWN: "Buy back the call and sell a lower strike — more premium now, less upside if the stock recovers.",
  DEFEND: "Roll the call down/out below where the stock closed, so the position keeps earning while it's under pressure.",
  EXIT: "Close the short call and sell the shares — the discipline rule says stop, not defend again.",
  ENTER: "Buy 100 shares and sell the first weekly call against them.",
};

// The core: {label, why, effect, numbers:[{k, v}]} for one rec.
export function explainRec(rec) {
  if (!rec) return null;
  const rule = rec.trigger_rule;
  const snap = rec.input_snapshot || {};
  const d = snap.trigger_detail || {};
  const numbers = [];
  let why = "";

  switch (rule) {
    case "ROLL_75PCT":
      why = `${num(d.decay_pct, 0)}% of the premium you sold on ${shortStr(d.short)} has already decayed${dteStr(d.dte)}. The remaining juice is small and slow — the rule says roll early and sell fresh time value.`;
      numbers.push({ k: "captured", v: `${num(d.decay_pct, 0)}%` }, { k: "DTE", v: num(d.dte, 0) });
      break;
    case "ROLL_EXTRINSIC_CAPTURED":
      why = `${num(d.extrinsic_captured_pct, 0)}% of the extrinsic on ${shortStr(d.short)} is banked (threshold ${num(d.threshold_pct, 0)}%)${dteStr(d.dte)}. Sold at ${dollars(d.entry_extrinsic_per_share)}/sh, only ${dollars(d.current_extrinsic_per_share)}/sh is left to earn.`;
      numbers.push(
        { k: "banked", v: `${num(d.extrinsic_captured_pct, 0)}%` },
        { k: "juice left", v: `${dollars(d.current_extrinsic_per_share)}/sh` },
        { k: "DTE", v: num(d.dte, 0) },
      );
      break;
    case "ROLL_SCHEDULED_WEEKLY":
      why = `${shortStr(d.short)} expires in ${num(d.dte, 0)} day${Number(d.dte) === 1 ? "" : "s"}. Weekly shorts are always rolled, never left to expire unmanaged.`;
      numbers.push({ k: "DTE", v: num(d.dte, 0) }, { k: "strike", v: num(d.short?.strike, 2) });
      break;
    case "DEFEND_BELOW_STRIKE": {
      const gap = d.last_close != null && d.short?.strike != null
        ? ((Number(d.last_close) / Number(d.short.strike)) - 1) * 100 : null;
      why = `The stock closed at ${dollars(d.last_close)}, ${gap != null ? `${num(Math.abs(gap), 1)}% ` : ""}below the ${num(d.short?.strike, 2)} strike${d.price != null ? `, and is ${dollars(d.price)} now` : ""}. The call is no longer earning — roll it down so the position keeps paying.`;
      numbers.push(
        { k: "close", v: dollars(d.last_close) },
        { k: "strike", v: num(d.short?.strike, 2) },
        ...(gap != null ? [{ k: "below strike", v: `${num(Math.abs(gap), 1)}%` }] : []),
      );
      break;
    }
    case "EARNINGS_WINDOW": {
      const e = d.earnings || {};
      why = `Earnings ${e.date ? `on ${e.date}` : "are"}${e.days_until != null ? ` (${e.days_until}d away)` : ""} while ${shortStr(d.short)} is open. Roll deep-ITM through the report or exit — never hold a normal weekly across earnings.`;
      numbers.push({ k: "earnings", v: e.date || "—" }, ...(e.days_until != null ? [{ k: "days", v: num(e.days_until, 0) }] : []));
      break;
    }
    case "DIVIDEND_ASSIGNMENT_RISK": {
      const a = d.assignment_risk || {};
      why = a.trigger === "dividend"
        ? `Only ${dollars(a.extrinsic)}/sh of time value is left on ${shortStr(d.short)} against a ${dollars(a.dividend)} dividend${a.ex_date ? ` going ex ${a.ex_date}` : ""}. A holder can call the shares away early to collect it.`
        : `Time value on ${shortStr(d.short)} has collapsed to ${dollars(a.extrinsic)}/sh while deep in the money — it can be assigned any day now.`;
      numbers.push({ k: "extrinsic left", v: `${dollars(a.extrinsic)}/sh` });
      if (a.dividend != null) numbers.push({ k: "dividend", v: dollars(a.dividend) });
      if (a.ex_date) numbers.push({ k: "ex-date", v: a.ex_date });
      break;
    }
    case "KILL_RS_SPY_CONFIRMED": {
      const ks = d.kill_switch || {};
      why = `3-month relative strength vs SPY turned negative on a confirmed close${ks.rs3m_vs_spy != null ? ` (${num(ks.rs3m_vs_spy, 1)}%)` : ""}${d.condition_first_true_at ? `, first on ${String(d.condition_first_true_at).slice(0, 10)}` : ""}. The stock is losing to the index — exit within 1–2 days.`;
      if (ks.rs3m_vs_spy != null) numbers.push({ k: "RS3M vs SPY", v: `${num(ks.rs3m_vs_spy, 1)}%` });
      break;
    }
    case "CIRCUIT_BREAKER": {
      const cb = d.circuit_breaker || {};
      why = cb.headline || "The line-in-the-sand exit level set at entry has been hit.";
      if (cb.suggested_action) why += ` ${cb.suggested_action}`;
      for (const c of cb.tripped_conditions || []) numbers.push({ k: "tripped", v: String(c) });
      break;
    }
    case "WHIPSAW_GUARD": {
      const w = d.whipsaw || {};
      why = `This position has been defended ${num(w.defensive_rolls, 0)} time${Number(w.defensive_rolls) === 1 ? "" : "s"}${w.roll_drag != null ? `, giving back ${dollars(w.roll_drag)}/sh` : ""}${w.drag_pct != null ? ` (${num(w.drag_pct, 1)}% of capital)` : ""}. Another defend would be chasing — the rule is exit.`;
      numbers.push({ k: "defends", v: num(w.defensive_rolls, 0) });
      if (w.drag_pct != null) numbers.push({ k: "drag", v: `${num(w.drag_pct, 1)}%` });
      break;
    }
    case "DELTA_COVERAGE_FLOOR": {
      const c = d.delta_coverage || {};
      why = `The short side is no longer covered: short delta ${num(c.short_delta, 2)} vs long ${num(c.long_delta, 2)} (floor ${num(c.min_leg_delta, 2)}). Uncovered calls are outside the strategy — exit.`;
      break;
    }
    case "JUICE_HURDLE_FAIL":
      why = "Trailing weekly juice on this name is under the income target while the capital is intact. The strategy says redeploy that capital into a name that pays.";
      break;
    case "DTE_PLANNED_EXIT":
      why = `The long leg is at ${num(d.leap_dte, 0)} DTE, at or under the planned exit of ${num(d.planned_exit_dte, 0)}. Holding past this is drift, not strategy.`;
      numbers.push({ k: "DTE", v: num(d.leap_dte, 0) }, { k: "planned exit", v: num(d.planned_exit_dte, 0) });
      break;
    case "GATE_ALL_PASS": {
      const t = rec.proposed_ticket || {};
      const cs = t.covering_short || {};
      const est = t.estimates || {};
      const fit = snap.lot_cost != null && snap.deployable != null
        ? ` One lot costs ${money(snap.lot_cost)} against ${money(snap.deployable)} of dry powder${snap.dry_powder_after != null ? `, leaving ${money(snap.dry_powder_after)}` : ""}.`
        : "";
      const first = cs.expiration
        ? ` First call: exp ${cs.expiration} (${cs.dte} DTE, the earliest full week)${est.short_premium_per_share != null ? `, est ${dollars(est.short_premium_per_share)}/sh` : ""}${est.first_call_pct_to_expiry != null ? ` ≈ ${num(est.first_call_pct_to_expiry, 2)}% to expiry` : ""}.`
        : "";
      why = `Every entry gate passed${snap.regime ? ` in a ${String(snap.regime).toUpperCase()} regime` : ""}${snap.juice_weekly_pct != null ? `, with the weekly call paying about ${num(snap.juice_weekly_pct, 2)}% of the lot per full week` : ""}.${fit}${first}`;
      if (snap.juice_weekly_pct != null) numbers.push({ k: "juice / full wk", v: `${num(snap.juice_weekly_pct, 2)}%` });
      if (snap.lot_cost != null) numbers.push({ k: "lot", v: money(snap.lot_cost) });
      if (snap.deployable != null) numbers.push({ k: "dry powder", v: money(snap.deployable) });
      if (cs.expiration) numbers.push({ k: "first call", v: `${cs.expiration.slice(5)} · ${cs.dte} DTE` });
      if (snap.regime) numbers.push({ k: "regime", v: String(snap.regime) });
      break;
    }
    case "ALL_CLEAR":
      why = "Nothing on this position needs a move.";
      break;
    default:
      why = "";
  }

  const others = (snap.secondary_triggers || []).map((r) => RULE_LABELS[r] || r);
  return {
    label: RULE_LABELS[rule] || (rule || "").replaceAll("_", " "),
    action: ACTION_LABELS[rec.action_type] || (rec.action_type || "").replaceAll("_", " "),
    why,
    effect: ACTION_EFFECT[rec.action_type] || "",
    numbers,
    also: others,
  };
}

// How a past engine call was closed out by the operator's own move — the line
// a position card shows under "engine called X": who did it, what they did,
// and how far it landed from the proposal. Reads a trust_derive resolution
// (EXECUTED_MATCHED / OVERRIDDEN) joined with its rec (recent_resolutions).
const SOURCE_LABEL = {
  engine_card: "from the card",
  app_manual: "by hand in the app",
  broker_manual: "by hand at Schwab",
};

export function explainResolution(res) {
  if (!res) return null;
  const called = ACTION_LABELS[res.action_type] || res.action_type;
  const rule = RULE_LABELS[res.trigger_rule] || res.trigger_rule || "";
  const did = ACTION_LABELS[res.executed_action_type] || res.executed_action_type || "";
  const where = SOURCE_LABEL[res.source] || "";
  const d = res.deltas || {};
  const parts = [];
  let verdict, tone;
  if (res.status === "EXECUTED_MATCHED") {
    verdict = d.action_delta ? `you took it as a ${did.toLowerCase()}` : "you took it";
    tone = "text-emerald-300";
    if (d.strike_delta != null && d.strike_delta !== 0) {
      parts.push(`strike ${d.strike_delta > 0 ? "+" : ""}${Number(d.strike_delta).toFixed(2)} vs proposed`);
    } else if (d.strike_delta === 0) {
      parts.push("at the proposed strike");
    }
    if (d.credit_delta_vs_min != null) {
      parts.push(`credit ${d.credit_delta_vs_min >= 0 ? "+" : "−"}$${Math.abs(Number(d.credit_delta_vs_min)).toFixed(2)} vs floor`);
    }
    if (d.hours_from_emission != null) parts.push(`${Math.round(d.hours_from_emission)}h after the call`);
  } else if (res.status === "OVERRIDDEN" && res.reason === "ACTED_DIFFERENTLY") {
    verdict = `you ${did.toLowerCase() === "exit" ? "exited" : "rolled"} instead`;
    tone = "text-amber-300";
    parts.push("logged as an override");
  } else if (res.status === "OVERRIDDEN") {
    verdict = "you dismissed it";
    tone = "text-slate-400";
    if (res.reason) parts.push(String(res.reason).replaceAll("_", " ").toLowerCase());
  } else {
    return null;
  }
  return { called, rule, verdict, where, tone, detail: parts.join(" · "), at: res.at };
}

// One line for a digest row: "NVDA — Roll out: 78% of the premium captured".
export function recHeadline(rec) {
  const x = explainRec(rec);
  if (!x) return "";
  return `${rec.ticker} — ${x.action}: ${x.label.toLowerCase()}`;
}

// One-line ticket read: legs (instruction + strike + expiry) · order type · est net.
export function ticketSummary(t) {
  if (!t) return "no ticket attached";
  const legs = (t.legs || [])
    .map((l) => {
      const when = l.expiration ? ` exp ${l.expiration}` : l.dte != null ? ` ${l.dte} DTE` : "";
      // A shares leg has no strike — its size IS the leg (100 shares, delta 1.0).
      const what = l.role === "shares" ? `${fmt(l.quantity, 0)} shares` : fmt(l.strike, 2);
      return `${(l.instruction || "").replaceAll("_", " ")} ${what}${when}`;
    })
    .join(" / ");
  // Each ticket shape nets under its own key: the LEAP exit/roll per share, the
  // shares exit as equity proceeds less the option buyback, the shares ENTRY as
  // a debit (share cost less the premium collected) — negated here so the
  // credit/debit wording below reads off one signed number.
  const est = t.estimates || {};
  const net = est.net_per_share != null ? est.net_per_share
    : est.net_credit_per_share != null ? est.net_credit_per_share
    : est.net_debit_per_share != null ? -Math.abs(est.net_debit_per_share)
    : null;
  const netStr = net != null
    ? `est ${net < 0 ? "−" : ""}$${Math.abs(Number(net)).toFixed(2)}/sh ${net >= 0 ? "credit" : "debit"}`
    : "unpriced";
  // What the lot actually COSTS — the number an entry decision turns on, and the
  // one thing a per-share figure hides (a $220/sh name and a $960/sh name read
  // alike until you see $22k next to $96k).
  const lot = t.estimates?.shares_notional;
  const lotStr = lot != null ? `${money(lot)} lot` : null;
  return [legs, (t.order_type || "").replaceAll("_", " "), lotStr, netStr]
    .filter(Boolean).join(" · ");
}
