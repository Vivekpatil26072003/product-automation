// Customer orders: field list and labels (same order and names as services/api/app/orders/fields.py).

export const ORDER_FIELDS = [
  "customer_name", "customer_number", "customer_email", "mobile", "order_number", "order_date", "delivery_date",
  "package", "size", "material", "quantity", "rate", "total", "advance", "remaining", "priority", "payment_status",
  "production_status", "delivery_status", "remarks", "employee",
] as const;
export type OrderField = (typeof ORDER_FIELDS)[number];

export const ORDER_LABEL: Record<OrderField, string> = {
  customer_name: "Customer name", customer_number: "Customer number", customer_email: "Customer email",
  mobile: "Mobile number", order_number: "Order number", order_date: "Order date", delivery_date: "Delivery date",
  package: "Package", size: "Size", material: "Material", quantity: "Quantity", rate: "Rate", total: "Total",
  advance: "Advance", remaining: "Remaining", priority: "Priority", payment_status: "Payment status",
  production_status: "Production status", delivery_status: "Delivery status", remarks: "Remarks", employee: "Employee",
};
// The columns of the review table (every field stays editable in the full form below it).
export const TABLE_FIELDS: OrderField[] = ["customer_name", "order_date", "delivery_date", "quantity", "rate", "total"];
export const ORDER_REQUIRED = new Set<OrderField>(["customer_name", "order_date", "quantity"]);
export const ORDER_DATES = new Set<OrderField>(["order_date", "delivery_date"]);
export const ORDER_NUMBERS = new Set<OrderField>(["quantity", "rate", "total", "advance", "remaining"]);

export type Evidence = { id: string; page: number | null; text: string; confidence: number | null };
export type OrderIssue = { field: OrderField | null; code: string; message: string; severity: "error" | "warning" };
export type DraftField = {
  value: string | null; display: string | null; raw: string | null; source: string; evidence: Evidence[];
  confidence: number | null; uncertain: boolean | null; note: string | null; corrected_from: string | null;
};
export type ExtraInfo = { label: string; value: string; evidence?: Evidence[] };
export type OrderMatch = {
  order_id: string; order_ref: string; customer_name: string; order_date: string | null; quantity: string | null; total: string | null;
};
export type OrderDraft = {
  id: string; batch_id: string; upload_id: string; file_name: string; page_no: number; source: "extracted" | "manual";
  state: "NEEDS_REVIEW" | "APPROVED" | "REJECTED" | "SUPERSEDED";
  fields: Record<OrderField, DraftField>;
  extra: ExtraInfo[];
  reading: { reader: string | null; model: string | null; languages: string[] | null; release: string | null; warnings: string[] | null };
  matches: OrderMatch[];
  decision: { mode: "new" | "update"; order_id?: string; order_ref?: string } | null;
  issues: OrderIssue[]; approvable: boolean; order_id: string | null; reject_reason: string | null; version: number;
};
export type OrderValues = Record<OrderField, string | null>;
export type OrderEmail = {
  id: string; to_email: string; revision: number; attachment: { name: string; bytes: number; sha256: string };
  state: "QUEUED" | "SENDING" | "ACCEPTED" | "FAILED" | "UNKNOWN"; http_status: number | null;
  error: { code: string; message: string | null } | null; by: string | null; at: string; finished_at: string | null;
};
export type OrderListItem = {
  id: string; order_ref: string; department: string; customer_id: string | null; revision: number; values: OrderValues;
  attention: number; pdf_name: string; last_email_state: OrderEmail["state"] | null; updated_at: string;
};
export type Order = Omit<OrderListItem, "attention"> & {
  state: string; source: { upload_id: string; batch_id: string; page_no: number };
  customer: { id: string; name: string } | null;
  extra: ExtraInfo[];
  attention: { field: OrderField | null; code: string; message: string }[];
  revisions: { number: number; reason: string | null; by: string | null; at: string; changed: OrderField[] }[];
  emails: OrderEmail[];
};

export const EMAIL_STATE: Record<OrderEmail["state"], { label: string; tone: string }> = {
  QUEUED: { label: "Waiting to send", tone: "progress" },
  SENDING: { label: "Sending", tone: "progress" },
  ACCEPTED: { label: "Sent", tone: "success" },
  FAILED: { label: "Not sent", tone: "error" },
  UNKNOWN: { label: "Unknown", tone: "warning" },
};

/** One address, the same rule as the server (no lists, no spaces). */
export function isEmail(value: string): boolean {
  return /^[^@\s,;]+@[^@\s,;]+\.[^@\s,;.]{2,}$/.test(value.trim());
}

export function formatDay(iso: string | null): string {
  if (!iso) return "";
  const [y = NaN, m = NaN, d = NaN] = iso.split("-").map(Number);
  if (![y, m, d].every(Number.isFinite)) return iso;
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

export function formatAmount(v: string | null): string {
  if (v === null || v === "") return "";
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : v;
}

export function formatCount(v: string | null): string {
  if (v === null || v === "") return "";
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString("en-IN", { maximumFractionDigits: 3 }) : v;
}

/** True while the worker still has to send it. */
export function emailPending(state: OrderEmail["state"]): boolean {
  return state === "QUEUED" || state === "SENDING";
}

/** How sure the reader was of a value, in words (never a made-up precision). */
export function readingNote(f: DraftField): string {
  if (f.source === "reviewer") return "Entered or confirmed by you.";
  if (f.source === "manual" || !f.raw) return "";
  const read = f.evidence.map((e) => e.text).join(" / ");
  const conf = f.confidence !== null && f.confidence !== undefined ? ` (${Math.round(f.confidence * 100)}% sure)` : "";
  const how = f.source === "ai" ? "Read by AI" : "Read from the page";
  return read ? `${how}${conf}: "${read}"` : `${how}${conf}.`;
}

export function pdfUrl(orderId: string, revision?: number, download = false): string {
  const q = new URLSearchParams();
  if (revision) q.set("revision", String(revision));
  if (download) q.set("download", "true");
  const s = q.toString();
  return `/api/v1/orders/${orderId}/pdf${s ? `?${s}` : ""}`;
}
