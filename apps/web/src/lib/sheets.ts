// Daily production sheet ("SULZER PROD. REPORT"): types and helpers shared by the sheet list and the sheet page.

export type Shift = "I" | "II" | "III" | "D";
export type SheetCell = {
  value: string | null; source: "read" | "ai" | "reviewer" | "manual" | null; uncertain: boolean; note: string | null;
  raw: string | null; evidence: { upload_id: string; span_id: string }[];
};
export type SheetRow = {
  metric: string; label: string; kind: "input" | "derived"; unit: string; agg: "sum" | "avg"; target: string | null;
  cells: Partial<Record<Shift, SheetCell>>; total: string | null; to_date: string | null;
};
export type SheetSection = {
  key: string; title: string; group: string; shifts: Shift[]; rows: SheetRow[]; params: Record<string, string | null>;
};
export type SheetEmail = {
  id: string; to_email: string; format: "xlsx" | "pdf" | "csv"; attachment: string;
  state: "QUEUED" | "SENDING" | "ACCEPTED" | "FAILED" | "UNKNOWN"; http_status: number | null;
  error: { code: string; message: string | null } | null; by: string | null; at: string; finished_at: string | null;
};
export type Sheet = {
  id: string; department: string; department_id: string; report_date: string; date_confirmed: boolean;
  state: "DRAFT" | "APPROVED"; shifts: Record<"I" | "II" | "III", { supervisor?: string }>;
  notes: { label: string; text: string }[]; sections: SheetSection[]; uncertain: number; missing: number;
  approvable: boolean; approved_version: number; approved_by: string | null; approved_at: string | null;
  sources: { upload_id: string; batch_id: string; file: string; page_no: number; reader: string; values_read: number; conflicts: number }[];
  changes: { section: string; metric: string; shift: string; old: string | null; new: string | null; reason: string | null; by: string | null; at: string }[];
  emails: SheetEmail[]; version: number;
};
export type SheetListItem = {
  id: string; report_date: string; department: string; state: "DRAFT" | "APPROVED";
  supervisors: Record<"I" | "II" | "III", string | null>;
  figures: Record<"production_m" | "production_kg" | "picks" | "total_eff_pct" | "running_looms" | "downtime" | "warping_m", string | null>;
  values: number; uncertain: number; last_email_state: SheetEmail["state"] | null; updated_at: string; version: number;
};

export const FORMATS = [
  { key: "xlsx", label: "Excel" },
  { key: "pdf", label: "PDF" },
  { key: "csv", label: "CSV" },
] as const;

export function fileUrl(id: string, format: "xlsx" | "pdf" | "csv", download = true): string {
  return `/api/v1/sheets/${id}/file?format=${format}${download ? "" : "&download=false"}`;
}

/** Numbers with grouping and up to 2 decimals; empty when unknown (never "0" for a missing value). */
export function fmt(v: string | null | undefined, places = 2): string {
  if (v === null || v === undefined || v === "") return "";
  const n = Number(v);
  if (!Number.isFinite(n)) return v;
  return n.toLocaleString("en-IN", { minimumFractionDigits: Number.isInteger(n) ? 0 : Math.min(places, 2), maximumFractionDigits: places });
}

export function dayLabel(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y ?? 1970, (m ?? 1) - 1, d ?? 1)).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

export const SOURCE_LABEL: Record<string, string> = {
  read: "Read from the page", ai: "Read by AI", reviewer: "Entered by a reviewer", manual: "Entered by hand",
};

export const EMAIL_STATE_LABEL: Record<SheetEmail["state"], { label: string; tone: string }> = {
  QUEUED: { label: "Waiting to send", tone: "progress" }, SENDING: { label: "Sending", tone: "progress" },
  ACCEPTED: { label: "Sent", tone: "success" }, FAILED: { label: "Not sent", tone: "error" },
  UNKNOWN: { label: "Unknown", tone: "warning" },
};

export function isEmail(text: string): boolean {
  return /^[^@\s,;]+@[^@\s,;]+\.[^@\s,;.]{2,}$/.test(text.trim());
}

/** Key for a cell in the edit buffer. */
export function cellKey(section: string, metric: string, shift: string): string {
  return `${section}|${metric}|${shift}`;
}

export function validNumber(text: string): boolean {
  const t = text.trim().replace(/,/g, "");
  return t === "" || /^-?\d+(\.\d{1,4})?$/.test(t);
}
