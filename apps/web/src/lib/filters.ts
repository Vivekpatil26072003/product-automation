// Shared record filter <-> URL (spec §5: filters persist in the URL; Back restores them).
// The same query string drives /records, /dashboard and exports, so every view shows the same scope.

export type RecordFilter = {
  date_from: string;
  date_to: string;
  department_id: string;
  status: string;
  unit: string;
  operator: string;
  q: string;
  include_archived: boolean;
};

const KEYS = ["date_from", "date_to", "department_id", "status", "unit", "operator", "q"] as const;

function iso(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Default: the last 7 local days including today (spec §5 shared list contract). */
export function defaultFilter(today = new Date()): RecordFilter {
  const from = new Date(today);
  from.setDate(today.getDate() - 6);
  return { date_from: iso(from), date_to: iso(today), department_id: "", status: "", unit: "", operator: "", q: "", include_archived: false };
}

export function readFilter(params: URLSearchParams, today = new Date()): RecordFilter {
  const f = defaultFilter(today);
  for (const k of KEYS) {
    const v = params.get(k);
    if (v) f[k] = v;
  }
  f.include_archived = params.get("include_archived") === "true";
  return f;
}

/** Query string for both the page URL and the API (only non-empty values). */
export function toQuery(f: RecordFilter, extra: Record<string, string> = {}): string {
  const p = new URLSearchParams();
  for (const k of KEYS) if (f[k]) p.set(k, f[k]);
  if (f.include_archived) p.set("include_archived", "true");
  for (const [k, v] of Object.entries(extra)) if (v) p.set(k, v);
  return p.toString();
}

/** Body for POST /exports (API names differ slightly from the URL names). */
export function toExportFilter(f: RecordFilter) {
  return {
    date_from: f.date_from, date_to: f.date_to,
    department_ids: f.department_id ? [f.department_id] : [],
    statuses: f.status ? [f.status] : [], units: f.unit ? [f.unit] : [],
    operator_query: f.operator || null, q: f.q || null, include_archived: f.include_archived,
  };
}

export function formatQty(value: string, unit?: string): string {
  const n = Number(value);
  const text = Number.isInteger(n) ? n.toLocaleString("en-IN") : n.toLocaleString("en-IN", { maximumFractionDigits: 3 });
  return unit ? `${text} ${unit}` : text;
}
