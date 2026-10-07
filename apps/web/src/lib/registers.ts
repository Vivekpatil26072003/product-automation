// Hourly production reading register (WGS-02, pick reading): types and helpers for the register list and page.

import type { SheetEmail } from "@/lib/sheets";

export type RegShift = "I" | "II" | "III";
export type RegCell = {
  slot: number; reading: string | null; picks: string | null; status: string | null; status_label: string;
  source: "read" | "ai" | "reviewer" | "manual" | null; uncertain: boolean; note: string | null; raw: string | null;
  evidence: { upload_id: string; span_id: string }[]; checks: { code: string; text: string }[]; accepted: string[];
};
export type RegMachine = { machine: string; cells: RegCell[]; total: string | null };
export type RegColumn = {
  slot: number; time: string; calculated: string | null; written: string | null; match: boolean | null;
  check: string | null; accepted: boolean; stopped: number;
};
export type RegShiftView = {
  shift: RegShift; hours: string; times: string[]; machines: RegMachine[]; columns: RegColumn[];
  total: string | null; written_total: string | null;
};
export type Register = {
  id: string; department: string; department_id: string; register_date: string; date_confirmed: boolean;
  state: "DRAFT" | "APPROVED"; notes: { label: string; text: string }[]; findings: { label: string; text: string }[];
  shifts: RegShiftView[]; day_total: string | null; uncertain: number; checks: number; approvable: boolean;
  approved_version: number; approved_by: string | null; approved_at: string | null;
  sources: { upload_id: string; batch_id: string; file: string; page_no: number; shift: RegShift; reader: string; values_read: number; conflicts: number }[];
  changes: { shift: string; machine: string; slot: number; time: string; field: string; old: string | null; new: string | null; reason: string | null; by: string | null; at: string }[];
  emails: (Omit<SheetEmail, "format"> & { format: RegFormat })[]; version: number;
};
export type RegisterListItem = {
  id: string; register_date: string; department: string; state: "DRAFT" | "APPROVED";
  shifts: Record<RegShift, string | null>; present: RegShift[]; machines: number; day_total: string | null;
  uncertain: number; checks: number; last_email_state: SheetEmail["state"] | null; updated_at: string; version: number;
};
export type RegFormat = "xlsx" | "pdf" | "csv" | "sql";

export const REG_FORMATS: { key: RegFormat; label: string }[] = [
  { key: "xlsx", label: "Excel" },
  { key: "pdf", label: "PDF" },
  { key: "csv", label: "CSV" },
  { key: "sql", label: "SQL" },
];

export function registerFileUrl(id: string, format: RegFormat): string {
  return `/api/v1/registers/${id}/file?format=${format}`;
}

/** How a cell is shown and typed: "reading picks mark", e.g. "2282 24", "1815 S/C", "B.FALL". */
export function cellText(c: Pick<RegCell, "reading" | "picks" | "status"> | undefined): string {
  if (!c) return "";
  return [c.reading, c.picks, c.status].filter((x) => x !== null && x !== "").join(" ");
}

export type ParsedCell = { reading: string | null; picks: string | null; status: string | null };

/** Parses what a person typed in a cell. The start column (slot 0) has a reading only, no picks. */
export function parseCell(text: string, slot: number): { ok: true; value: ParsedCell } | { ok: false; error: string } {
  const tokens = text.trim().replace(/,/g, "").split(/\s+/).filter(Boolean);
  const nums: string[] = [];
  const words: string[] = [];
  for (const t of tokens) {
    if (/^\d+(\.\d{1,4})?$/.test(t)) {
      if (words.length) return { ok: false, error: "Write the numbers first, then the mark (e.g. 1815 S/C)." };
      nums.push(t);
    } else if (/\d/.test(t)) {
      return { ok: false, error: `"${t}" is not a number (up to 4 decimals).` };
    } else words.push(t);
  }
  if (nums.length > (slot === 0 ? 1 : 2)) {
    return { ok: false, error: slot === 0 ? "The start column has one reading only." : "At most a reading and its picks." };
  }
  const status = words.join(" ").toUpperCase().slice(0, 40) || null;
  return { ok: true, value: { reading: nums[0] ?? null, picks: nums[1] ?? null, status } };
}

export function regKey(shift: string, machine: string, slot: number): string {
  return `${shift}|${machine}|${slot}`;
}

export function compareMachines(a: string, b: string): number {
  const na = parseInt(a, 10), nb = parseInt(b, 10);
  if (Number.isFinite(na) && Number.isFinite(nb) && na !== nb) return na - nb;
  return a.localeCompare(b);
}
