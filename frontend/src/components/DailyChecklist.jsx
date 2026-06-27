import React from "react";
import { api } from "../api.js";
import { Card, Light, Loading, useApi } from "./ui.jsx";

export default function DailyChecklist() {
  const { data, error, loading } = useApi(api.dailyChecklist, [], null);
  // "Done" marks are local/manual per the spec (read-only computed status today).
  const [done, setDone] = React.useState({});

  if (loading && !data) return <Card title="Daily Checklist"><Loading /></Card>;
  if (error) return <Card title="Daily Checklist"><p className="text-sm text-rose-400">{error}</p></Card>;

  const items = data?.items || [];
  return (
    <Card title="Daily Checklist — 15-minute routine">
      <ul className="space-y-2">
        {items.map((it) => (
          <li key={it.id} className="flex items-center gap-3 rounded-lg border border-slate-800 px-3 py-2">
            <input
              type="checkbox"
              checked={!!done[it.id]}
              onChange={() => setDone({ ...done, [it.id]: !done[it.id] })}
              className="h-4 w-4 accent-emerald-500"
            />
            <Light status={it.ok ? "green" : "yellow"} />
            <span className={`text-sm ${done[it.id] ? "text-slate-500 line-through" : "text-slate-200"}`}>{it.label}</span>
          </li>
        ))}
        {items.length === 0 && <li className="text-sm text-slate-500">Nothing flagged.</li>}
      </ul>
    </Card>
  );
}
