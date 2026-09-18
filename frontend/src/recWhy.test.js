import { describe, expect, it } from "vitest";
import {
  ACTION_LABELS,
  RULE_LABELS,
  explainRec,
  explainResolution,
  recHeadline,
  ticketSummary,
} from "./recWhy.js";

describe("explainRec", () => {
  it("returns null for no rec", () => {
    expect(explainRec(null)).toBeNull();
    expect(explainRec(undefined)).toBeNull();
  });

  it("ROLL_75PCT: reads decay% and DTE out of trigger_detail", () => {
    const rec = {
      trigger_rule: "ROLL_75PCT",
      action_type: "ROLL_OUT",
      input_snapshot: {
        trigger_detail: { decay_pct: 78, dte: 3, short: { strike: 95 } },
      },
    };
    const x = explainRec(rec);
    expect(x.label).toBe(RULE_LABELS.ROLL_75PCT);
    expect(x.action).toBe(ACTION_LABELS.ROLL_OUT);
    expect(x.why).toContain("78%");
    expect(x.why).toContain("95");
    expect(x.numbers).toEqual([
      { k: "captured", v: "78%" },
      { k: "DTE", v: "3" },
    ]);
  });

  it("ROLL_EXTRINSIC_CAPTURED: adds a roll-direction clause and number only when a direction is proposed", () => {
    const base = {
      trigger_rule: "ROLL_EXTRINSIC_CAPTURED",
      action_type: "ROLL_UP",
      input_snapshot: {
        trigger_detail: {
          extrinsic_captured_pct: 80,
          threshold_pct: 75,
          dte: 4,
          entry_extrinsic_per_share: 2,
          current_extrinsic_per_share: 0.4,
          short: { strike: 100 },
          roll_up_same_week_min_dte: 3,
        },
      },
    };
    const withDir = explainRec({ ...base, proposed_ticket: { roll_direction: "ROLL_UP" } });
    expect(withDir.why).toContain("roll UP in place");
    expect(withDir.numbers).toContainEqual({ k: "roll", v: "up, same exp" });

    const withoutDir = explainRec({ ...base, proposed_ticket: {} });
    expect(withoutDir.why).not.toContain("roll UP");
    expect(withoutDir.numbers.find((n) => n.k === "roll")).toBeUndefined();
  });

  it("DEFEND_BELOW_STRIKE: computes the %-below-strike gap from last_close vs strike", () => {
    const rec = {
      trigger_rule: "DEFEND_BELOW_STRIKE",
      action_type: "DEFEND",
      input_snapshot: {
        trigger_detail: { last_close: 90, short: { strike: 100 }, price: 91 },
      },
    };
    const x = explainRec(rec);
    // (90/100 - 1) * 100 = -10 -> abs 10%
    expect(x.why).toContain("10% below the 100 strike");
    expect(x.numbers).toContainEqual({ k: "below strike", v: "10%" });
  });

  it("GATE_ALL_PASS: assembles regime, juice, lot fit and first-call clauses", () => {
    const rec = {
      trigger_rule: "GATE_ALL_PASS",
      action_type: "ENTER",
      input_snapshot: {
        regime: "green",
        juice_weekly_pct: 1.5,
        lot_cost: 10000,
        deployable: 15000,
        dry_powder_after: 5000,
      },
      proposed_ticket: {
        covering_short: { expiration: "2026-10-02", dte: 5 },
        estimates: { short_premium_per_share: 1.1, first_call_pct_to_expiry: 0.8 },
      },
    };
    const x = explainRec(rec);
    expect(x.why).toContain("GREEN regime");
    expect(x.why).toContain("$10,000");
    expect(x.why).toContain("2026-10-02");
    expect(x.numbers).toContainEqual({ k: "regime", v: "green" });
  });

  it("falls back to a readable label/blank why for an unrecognized rule", () => {
    const x = explainRec({ trigger_rule: "SOME_NEW_RULE", action_type: "NO_ACTION" });
    expect(x.label).toBe("SOME NEW RULE");
    expect(x.why).toBe("");
    expect(x.action).toBe(ACTION_LABELS.NO_ACTION);
  });

  it("collects secondary_triggers into `also` using the human labels", () => {
    const rec = {
      trigger_rule: "ALL_CLEAR",
      action_type: "NO_ACTION",
      input_snapshot: { trigger_detail: {}, secondary_triggers: ["ROLL_75PCT", "UNKNOWN_X"] },
    };
    const x = explainRec(rec);
    expect(x.also).toEqual([RULE_LABELS.ROLL_75PCT, "UNKNOWN_X"]);
  });
});

describe("explainResolution", () => {
  it("returns null for no resolution or an unrecognized status", () => {
    expect(explainResolution(null)).toBeNull();
    expect(explainResolution({ status: "PENDING" })).toBeNull();
  });

  it("EXECUTED_MATCHED at the proposed strike reads as a clean take", () => {
    const res = {
      status: "EXECUTED_MATCHED",
      action_type: "ROLL_OUT",
      trigger_rule: "ROLL_75PCT",
      executed_action_type: "ROLL_OUT",
      source: "engine_card",
      deltas: { action_delta: false, strike_delta: 0, credit_delta_vs_min: 0.1, hours_from_emission: 2.4 },
    };
    const x = explainResolution(res);
    expect(x.verdict).toBe("you took it");
    expect(x.tone).toBe("text-emerald-300");
    expect(x.detail).toContain("at the proposed strike");
    expect(x.detail).toContain("2h after the call");
  });

  it("OVERRIDDEN + ACTED_DIFFERENTLY reads as an override, exit vs roll worded correctly", () => {
    const exitRes = {
      status: "OVERRIDDEN",
      reason: "ACTED_DIFFERENTLY",
      action_type: "ROLL_OUT",
      executed_action_type: "EXIT",
      trigger_rule: "ROLL_75PCT",
    };
    expect(explainResolution(exitRes).verdict).toBe("you exited instead");

    const rollRes = { ...exitRes, executed_action_type: "ROLL_DOWN" };
    expect(explainResolution(rollRes).verdict).toBe("you rolled instead");
  });

  it("OVERRIDDEN alone reads as a dismissal with the reason lowercased", () => {
    const res = { status: "OVERRIDDEN", reason: "STALE_PRICE", action_type: "ROLL_OUT", trigger_rule: "ROLL_75PCT" };
    const x = explainResolution(res);
    expect(x.verdict).toBe("you dismissed it");
    expect(x.detail).toBe("stale price");
  });
});

describe("recHeadline", () => {
  it("formats ticker — action: lowercased label", () => {
    const rec = {
      ticker: "NVDA",
      trigger_rule: "ROLL_75PCT",
      action_type: "ROLL_OUT",
      input_snapshot: { trigger_detail: { decay_pct: 78, dte: 3 } },
    };
    expect(recHeadline(rec)).toBe("NVDA — Roll out: 75% of the premium captured");
  });

  it("is empty for no rec", () => {
    expect(recHeadline(null)).toBe("");
  });
});

describe("ticketSummary", () => {
  it("reports no ticket attached when there is none", () => {
    expect(ticketSummary(null)).toBe("no ticket attached");
  });

  it("formats a shares entry leg as a debit (net cost less premium)", () => {
    const t = {
      legs: [{ instruction: "buy_to_open", role: "shares", quantity: 100 }],
      order_type: "market",
      estimates: { net_debit_per_share: 48.5, shares_notional: 4850 },
    };
    const s = ticketSummary(t);
    expect(s).toContain("100 shares");
    expect(s).toContain("$4,850 lot");
    expect(s).toContain("$48.50/sh debit");
  });

  it("formats a short-call roll leg as a credit and includes strike + expiry", () => {
    const t = {
      legs: [{ instruction: "sell_to_open", strike: 102.5, expiration: "2026-10-02" }],
      order_type: "limit",
      estimates: { net_credit_per_share: 1.35 },
    };
    const s = ticketSummary(t);
    expect(s).toContain("102.5 exp 2026-10-02");
    expect(s).toContain("$1.35/sh credit");
  });

  it("reads unpriced when no net figure is present", () => {
    const t = { legs: [{ instruction: "buy_to_close", strike: 100 }], order_type: "limit", estimates: {} };
    expect(ticketSummary(t)).toContain("unpriced");
  });
});
