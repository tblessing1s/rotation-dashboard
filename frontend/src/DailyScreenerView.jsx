import React, { useState, useCallback } from "react";
import { C } from "./theme.js";

/* ============================================================================
   DAILY SCREENER — scan the full US market via Finviz by day-trading params.
   Criteria: price $20–$100, avg daily volume ≥ 10M shares, ATR% 4–9%.
   Returns top 50 by ATR% descending so the most volatile bounded names appear
   first for setup selection.
   ============================================================================ */

const API = "";

const inputStyle = {
  width: "100%", boxSizing: "border-box", background: "#0f141c",
  border: `1px solid ${C.line}`, borderRadius: 6, color: C.ink,
  font: `500 13px/1.5 'Roboto Mono', ui-monospace, monospace`, padding: "9px 10px", outline: "none",
};

const numStyle = {
  ...inputStyle,
  width: 90, textAlign: "right",
};

function Panel({ title, eyebrow, children, accent }) {
  return (
    <section style={{ background: C.panel, border: `1px solid ${C.line}`, borderRadius: 10, overflow: "hidden" }}>
      <header style={{
        display: "flex", alignItems: "center", padding: "12px 16px",
        borderBottom: `1px solid #19222e`,
        borderLeft: accent ? `3px solid ${accent}` : "none",
      }}>
        <div>
          {eyebrow && <div style={{ font: `600 9px/1 'Roboto Mono', monospace`, letterSpacing: 2, color: C.inkFaint, textTransform: "uppercase", marginBottom: 5 }}>{eyebrow}</div>}
          <h2 style={{ margin: 0, font: `600 14px/1 'Inter', system-ui, sans-serif`, color: C.ink, letterSpacing: -0.2 }}>{title}</h2>
        </div>
      </header>
      <div style={{ padding: 16 }}>{children}</div>
    </section>
  );
}

export default function DailyScreenerView() {
  const [filters, setFilters] = useState({ priceMin: 20, priceMax: 100, volMin: 10, atrMin: 4, atrMax: 9 });
  const [status, setStatus] = useState("idle"); // idle | loading | done | error
  const [results, setResults] = useState(null);
  const [volApplied, setVolApplied] = useState("");
  const [errorMsg, setErrorMsg] = useState("");

  const runScreen = useCallback(async () => {
    setStatus("loading");
    setErrorMsg("");
    setResults(null);
    setVolApplied("");
    try {
      const params = new URLSearchParams({
        price_min: filters.priceMin,
        price_max: filters.priceMax,
        vol_min: filters.volMin * 1_000_000,
        atr_min: filters.atrMin,
        atr_max: filters.atrMax,
      });
      const res = await fetch(`${API}/api/daily-screener?${params}`);
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        throw new Error(j.error || `HTTP ${res.status}`);
      }
      const data = await res.json();
      setResults(data.results || []);
      setVolApplied(data.volFilterApplied || "");
      setStatus("done");
    } catch (e) {
      setErrorMsg(e.message || "Fetch failed");
      setStatus("error");
    }
  }, [filters]);

  const setF = (k, v) => setFilters((prev) => ({ ...prev, [k]: v }));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <Panel title="Daily stock screener" eyebrow="Finviz · full US market">
        <div style={{ font: `400 12px 'Inter', sans-serif`, color: C.inkDim, marginBottom: 14 }}>
          Set your parameters and run — Finviz scans the full US market and returns the top 50 by ATR%.
        </div>

        <div style={{ display: "flex", flexWrap: "wrap", gap: 14, alignItems: "flex-end" }}>
          <FilterNum label="Price min ($)" value={filters.priceMin} onChange={(v) => setF("priceMin", v)} />
          <FilterNum label="Price max ($)" value={filters.priceMax} onChange={(v) => setF("priceMax", v)} />
          <FilterNum label="Avg vol min (M)" value={filters.volMin} onChange={(v) => setF("volMin", v)} step={0.5} />
          <FilterNum label="ATR% min" value={filters.atrMin} onChange={(v) => setF("atrMin", v)} step={0.5} />
          <FilterNum label="ATR% max" value={filters.atrMax} onChange={(v) => setF("atrMax", v)} step={0.5} />
          <button
            onClick={runScreen}
            disabled={status === "loading"}
            style={{
              background: C.blue, border: "none", borderRadius: 6, color: "#fff",
              font: `600 13px 'Inter', sans-serif`, padding: "9px 20px", cursor: "pointer",
              opacity: status === "loading" ? 0.6 : 1, alignSelf: "flex-end",
            }}
          >
            {status === "loading" ? "Screening…" : "Run screen"}
          </button>
        </div>

        {errorMsg && (
          <div style={{ font: `500 12px 'Inter', sans-serif`, color: C.red, marginTop: 10 }}>{errorMsg}</div>
        )}
      </Panel>

      {status === "done" && results !== null && (
        <Panel
          title={results.length ? `Top ${results.length} match${results.length !== 1 ? "es" : ""}` : "No matches"}
          eyebrow="Finviz · sorted by ATR% · highest volatility first"
          accent={results.length ? C.green : C.inkFaint}
        >
          {results.length === 0 ? (
            <div style={{ color: C.inkDim, font: `400 13px 'Inter', sans-serif`, padding: "8px 0" }}>
              No symbols matched all filters. Try widening price, volume, or ATR% ranges.
            </div>
          ) : (
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 400 }}>
                <thead>
                  <tr style={{ borderBottom: `1px solid ${C.line}` }}>
                    {["Symbol", "Price", "ATR%"].map((h) => (
                      <th key={h} style={{ textAlign: "left", padding: "6px 10px", font: `600 11px 'Roboto Mono', monospace`, color: C.inkFaint, letterSpacing: 0.5 }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {results.map((r, i) => (
                    <tr key={r.symbol} style={{ background: i % 2 === 0 ? "transparent" : "#0f141c", borderBottom: `1px solid #19222e` }}>
                      <td style={{ padding: "9px 10px" }}>
                        <span style={{ font: `700 13px 'Roboto Mono', monospace`, color: C.ink }}>{r.symbol}</span>
                      </td>
                      <td style={{ padding: "9px 10px" }}>
                        <span style={{ font: `500 13px 'Roboto Mono', monospace`, color: C.ink }}>${r.price.toFixed(2)}</span>
                      </td>
                      <td style={{ padding: "9px 10px" }}>
                        <AtrBadge value={r.atrPct} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div style={{ marginTop: 12, font: `400 11px 'Roboto Mono', monospace`, color: C.inkFaint }}>
            Filters: price ${filters.priceMin}–${filters.priceMax} · avg vol ≥ {filters.volMin}M · ATR% {filters.atrMin}–{filters.atrMax}%
          </div>
          {volApplied && (
            <div style={{ marginTop: 6, font: `400 11px 'Roboto Mono', monospace`, color: C.inkFaint }}>
              Finviz avg-vol floor applied: <span style={{ color: C.inkDim }}>{volApplied}</span>
              {filters.volMin > 2 && volApplied === "Over 2M" && (
                <span style={{ color: C.yellow }}>
                  {" "}— Finviz caps its server-side floor at 2M, so names down to ~2M avg vol may appear.
                </span>
              )}
            </div>
          )}
        </Panel>
      )}
    </div>
  );
}

function FilterNum({ label, value, onChange, step = 1 }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      <span style={{ font: `500 11px/1 'Inter', sans-serif`, color: C.inkDim }}>{label}</span>
      <input
        type="number"
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={numStyle}
      />
    </label>
  );
}

function AtrBadge({ value }) {
  const color = value >= 6 ? C.yellow : C.green;
  return (
    <span style={{
      display: "inline-block", font: `600 12px 'Roboto Mono', monospace`,
      color, background: `${color}18`, borderRadius: 4, padding: "2px 7px",
    }}>
      {value.toFixed(2)}%
    </span>
  );
}
