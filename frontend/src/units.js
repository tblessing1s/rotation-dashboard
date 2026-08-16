// Canonical per-contract <-> per-share unit conversion for option legs — the ONE
// named home for the CFM units convention on the frontend, mirroring the backend
// `units.py`, so the x100 / /100 factor can't drift across the components that
// build order payloads and render fills.
//
// Convention (see CLAUDE.md "Units"):
//   * A LEAP leg stores dollars PER CONTRACT (execution_price / close_price).
//   * A short leg stores dollars PER SHARE (premium_per_share / close_price_per_share).
//   * Display and order limits are per-share. One contract = 100 shares.
//
// These are pure conversions and do NOT round: a site that stores cents rounds
// explicitly (`Math.round(leapPerContract(mark) * 100) / 100`), exactly as the
// hand-written code did — so routing a call site through here is a de-duplication,
// not a behavior change.

export const SHARES_PER_CONTRACT = 100;

// Per-share LEAP price/mark -> per-contract dollars (x shares/contract).
export function leapPerContract(perShare) {
  return perShare * SHARES_PER_CONTRACT;
}

// Per-contract LEAP dollars -> per-share (/ shares/contract), for display.
export function leapPerShare(perContract) {
  return perContract / SHARES_PER_CONTRACT;
}

// Per-contract *total* extrinsic (across `contracts`) -> per-share. Divides by
// both shares/contract and the contract count; `contracts` falls back to 1.
export function leapExtrinsicPerShare(perContractTotal, contracts) {
  return perContractTotal / (SHARES_PER_CONTRACT * (contracts || 1));
}

// Per-share price/premium -> total position dollars across `contracts`
// (per-share x shares/contract x contracts). Used for "total extrinsic" /
// "est. proceeds" style readouts on either leg.
export function totalDollars(perShare, contracts) {
  return perShare * SHARES_PER_CONTRACT * contracts;
}
