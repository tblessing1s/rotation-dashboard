import { describe, expect, it } from "vitest";
import { SHARES_PER_CONTRACT, totalDollars } from "./units.js";

describe("units", () => {
  it("SHARES_PER_CONTRACT is 100 — one contract = 100 shares", () => {
    expect(SHARES_PER_CONTRACT).toBe(100);
  });

  it("totalDollars scales a per-share price by shares/contract and contract count", () => {
    // $1.25/sh premium, 3 contracts -> 1.25 * 100 * 3
    expect(totalDollars(1.25, 3)).toBe(375);
  });

  it("totalDollars with a single contract is just per-share x 100", () => {
    expect(totalDollars(2.5, 1)).toBe(250);
  });

  it("totalDollars is 0 with 0 contracts", () => {
    expect(totalDollars(4.2, 0)).toBe(0);
  });

  it("totalDollars carries the sign through for a debit (negative per-share)", () => {
    expect(totalDollars(-0.75, 2)).toBe(-150);
  });

  it("totalDollars does not round fractional cents", () => {
    // A caller that wants cents rounds explicitly; the conversion itself must not.
    expect(totalDollars(0.333, 1)).toBeCloseTo(33.3, 10);
  });
});
