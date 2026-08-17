// Canonical per-share <-> whole-position unit conversion for option legs — the ONE
// named home for the CFM units convention on the frontend, mirroring the backend
// `units.py`, so the x100 factor can't drift across the components that build
// order payloads and render fills.
//
// Convention (see CLAUDE.md "Units"):
//   * A short leg stores dollars PER SHARE (premium_per_share / close_price_per_share).
//   * Display and order limits are per-share. One contract = 100 shares.
//
// These are pure conversions and do NOT round: a site that stores cents rounds
// explicitly, exactly as the hand-written code did — so routing a call site
// through here is a de-duplication, not a behavior change.

export const SHARES_PER_CONTRACT = 100;

// Per-share price/premium -> total position dollars across `contracts`
// (per-share x shares/contract x contracts). Used for "total extrinsic" /
// "est. proceeds" style readouts on either leg.
export function totalDollars(perShare, contracts) {
  return perShare * SHARES_PER_CONTRACT * contracts;
}
