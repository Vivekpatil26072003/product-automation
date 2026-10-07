"use client";

import { useState } from "react";

import type { Master } from "@/lib/api";
import type { RecordFilter } from "@/lib/filters";

// One filter row above everything it scopes (dashboard, records, export). Applying writes the URL.

type Props = {
  value: RecordFilter;
  departments: Master[];
  onApply: (f: RecordFilter) => void;
  showSearch?: boolean;
};

export function FilterBar({ value, departments, onApply, showSearch }: Props) {
  const [draft, setDraft] = useState(value);
  const set = <K extends keyof RecordFilter>(k: K, v: RecordFilter[K]) => setDraft((d) => ({ ...d, [k]: v }));
  const invalid = draft.date_from > draft.date_to;
  return (
    <form
      className="card filterbar"
      aria-label="Filters"
      onSubmit={(e) => {
        e.preventDefault();
        if (!invalid) onApply(draft);
      }}
    >
      <div>
        <label htmlFor="f-from">From</label>
        <input id="f-from" type="date" value={draft.date_from} onChange={(e) => set("date_from", e.target.value)} />
      </div>
      <div>
        <label htmlFor="f-to">To</label>
        <input id="f-to" type="date" value={draft.date_to} onChange={(e) => set("date_to", e.target.value)} aria-invalid={invalid || undefined} aria-describedby={invalid ? "f-range" : undefined} />
      </div>
      <div>
        <label htmlFor="f-dept">Department</label>
        <select id="f-dept" value={draft.department_id} onChange={(e) => set("department_id", e.target.value)}>
          <option value="">All my departments</option>
          {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </select>
      </div>
      <div>
        <label htmlFor="f-unit">Unit</label>
        <select id="f-unit" value={draft.unit} onChange={(e) => set("unit", e.target.value)}>
          <option value="">All units</option>
          <option value="m">m</option>
          <option value="kg">kg</option>
          <option value="pcs">pcs</option>
        </select>
      </div>
      <div>
        <label htmlFor="f-status">Status</label>
        <select id="f-status" value={draft.status} onChange={(e) => set("status", e.target.value)}>
          <option value="">All statuses</option>
          {["RUNNING", "COMPLETED", "PENDING", "HOLD"].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>
      {showSearch && (
        <div>
          <label htmlFor="f-q">Search</label>
          <input id="f-q" type="text" placeholder="Operator, machine or remark" maxLength={120} value={draft.q} onChange={(e) => set("q", e.target.value)} />
        </div>
      )}
      <label className="check">
        <input type="checkbox" checked={draft.include_archived} onChange={(e) => set("include_archived", e.target.checked)} /> Include archived
      </label>
      <div className="filterbar-actions">
        <button className="primary" type="submit" disabled={invalid}>Apply</button>
      </div>
      {invalid && <p id="f-range" className="reason" role="alert">The start date must be on or before the end date.</p>}
    </form>
  );
}
