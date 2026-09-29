import React from "react";

// Strike-ladder paging for the CC / CSP tickets. The backend returns a wide
// ladder per expiration; the UI opens on the INITIAL nearest the suggested
// strike and reveals STEP more per click, growing outward from it.
export const INITIAL_STRIKES = 5;
export const STRIKE_STEP = 5;

// The `n` strikes closest to the suggested one (index fallback: the middle),
// returned in the ladder's original ascending order.
export function pageStrikes(strikes, n) {
  const list = strikes || [];
  if (list.length <= n) return list;
  let anchor = list.findIndex((s) => s.suggested);
  if (anchor < 0) anchor = Math.floor(list.length / 2);
  const keep = new Set(
    list.map((_, i) => i).sort((a, b) => Math.abs(a - anchor) - Math.abs(b - anchor) || a - b).slice(0, n),
  );
  return list.filter((_, i) => keep.has(i));
}

// Per-key visible counts (key = expiration) so each week pages independently.
export function useStrikePaging() {
  const [counts, setCounts] = React.useState({});
  const visible = (key) => counts[key] ?? INITIAL_STRIKES;
  const more = (key) => setCounts((c) => ({ ...c, [key]: (c[key] ?? INITIAL_STRIKES) + STRIKE_STEP }));
  return { visible, more };
}
