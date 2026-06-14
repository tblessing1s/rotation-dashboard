// Shared color + font tokens for the dashboard. Kept in one place so views
// split across files (TradingDashboard, BacktestView, …) render identically.
export const C = {
  bg: "#0a0e14",
  panel: "#121821",
  panel2: "#0f141c",
  line: "#1f2935",
  lineSoft: "#19222e",
  ink: "#e6edf3",
  inkDim: "#8b97a7",
  inkFaint: "#5a6573",
  green: "#3fb950",
  greenDim: "#1f6f2e",
  yellow: "#d2a64a",
  red: "#f0506e",
  redDim: "#7a2438",
  blue: "#4493f8",
  amber: "#e3a008",
  mono: "'Roboto Mono', ui-monospace, 'SF Mono', Menlo, monospace",
  sans: "'Inter', -apple-system, system-ui, sans-serif",
};

export const SIG = { GREEN: C.green, YELLOW: C.yellow, RED: C.red };
