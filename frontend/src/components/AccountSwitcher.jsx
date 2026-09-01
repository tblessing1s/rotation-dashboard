import React from "react";

// The book selector in the navbar. Every tab reads ONE account at a time
// (backend/accounts.py keeps a separate state.json per account), so this is the
// highest-leverage control in the chrome — and the one place the operator can
// confirm, at a glance, which book they are about to trade.
//
// It renders nothing on a single-account install: no dropdown, no clutter, no
// behaviour change until the operator actually adds a second account.
export default function AccountSwitcher({ accounts = [], activeId, busy, onSelect, onManage }) {
  const [open, setOpen] = React.useState(false);
  const ref = React.useRef(null);

  React.useEffect(() => {
    if (!open) return undefined;
    const onDocClick = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const onEsc = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onEsc);
    };
  }, [open]);

  const live = accounts.filter((a) => !a.archived);
  if (live.length <= 1) return null;

  const active = live.find((a) => a.id === activeId) || live[0];

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        disabled={busy}
        title="Switch account — every tab reads the selected book"
        className="flex max-w-[9rem] items-center gap-1.5 rounded-full border border-sky-500/40 bg-sky-500/10 px-2.5 py-1.5 text-xs font-semibold text-sky-200 transition hover:bg-sky-500/20 disabled:opacity-50 sm:max-w-[14rem]"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-4 w-4 shrink-0">
          <path strokeLinecap="round" strokeLinejoin="round"
                d="M3 7h18M3 12h18M3 17h18" />
        </svg>
        <span className="truncate">{busy ? "Switching…" : active?.label || "Account"}</span>
        <span className="text-sky-400/70">▾</span>
      </button>

      {open && (
        <div className="absolute right-0 z-30 mt-1 w-64 overflow-hidden rounded-lg border border-slate-700 bg-slate-900 shadow-xl">
          <div className="border-b border-slate-800 px-3 py-2 text-[11px] uppercase tracking-wide text-slate-500">
            Accounts
          </div>
          {live.map((a) => (
            <button
              key={a.id}
              onClick={() => { setOpen(false); if (a.id !== activeId) onSelect(a.id); }}
              className={`flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm transition ${
                a.id === activeId
                  ? "bg-sky-500/10 text-sky-200"
                  : "text-slate-300 hover:bg-slate-800"
              }`}
            >
              <span className="min-w-0">
                <span className="block truncate">{a.label}</span>
                <span className="block truncate text-[11px] text-slate-500">
                  {a.broker_account_number
                    ? `Schwab …${String(a.broker_account_number).slice(-4)}`
                    : "no brokerage account linked"}
                </span>
              </span>
              {a.id === activeId && <span className="shrink-0 text-xs">✓</span>}
            </button>
          ))}
          {onManage && (
            <button
              onClick={() => { setOpen(false); onManage(); }}
              className="w-full border-t border-slate-800 px-3 py-2 text-left text-xs text-slate-400 transition hover:bg-slate-800 hover:text-slate-200"
            >
              Manage accounts →
            </button>
          )}
        </div>
      )}
    </div>
  );
}
