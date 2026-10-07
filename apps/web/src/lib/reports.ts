// Reports and email (M6): contracts and plain-language status. Status is words first; tone only reinforces it.

import type { Job } from "./api";
import type { Tone } from "./status";

export type UnitMetric = {
  unit: string; production_qty: string; target_qty: string; achievement_pct: string | null; variance: string; record_count: number;
};
export type DeptMetric = UnitMetric & { department_id: string; department_name: string | null };
export type ReportMetrics = {
  record_count: number; metrics: UnitMetric[]; departments: DeptMetric[];
  status_counts: Record<string, number>; status_shares: Record<string, string | null>; stop_total_minutes: number;
};
export type Sentence = { text: string; facts: string[] };

export type Report = {
  id: string; code: string; series_id: string; version: number; supersedes_id: string | null; title: string;
  filter: { date_from: string; date_to: string; department_ids: string[]; units: string[] };
  timezone: string; data_version: number; record_count: number; include_detail: boolean; is_empty: boolean;
  state: "QUEUED" | "GENERATING" | "READY" | "FAILED"; outdated: boolean; outdated_at: string | null;
  metrics: ReportMetrics; facts: { excluded_pending: number; period: { label: string } };
  summary: Sentence[]; summary_source: "TEMPLATE" | "AI" | "TEMPLATE_FALLBACK" | null;
  file: { sha256: string; bytes: number } | null; error: { code: string; message: string } | null;
  created_at: string; ready_at: string | null; created_by: string | null; job: Job | null;
  versions: { id: string; version: number; state: string; outdated: boolean; created_at: string }[];
  attachment_name: string; emails: EmailListItem[]; excel_export: { id: string; state: string } | null;
};

export type ReportListItem = Pick<Report, "id" | "code" | "version" | "title" | "state" | "outdated" | "record_count" |
  "is_empty" | "created_at" | "ready_at" | "filter"> & { metrics: UnitMetric[] };

export type Recipient = { kind: "TO" | "CC" | "BCC"; address: string; name: string };

export type Draft = {
  id: string; report_id: string; state: "DRAFT" | "QUEUED"; version: number; content_hash: string;
  subject: string; body: string; to: Recipient[]; cc: Recipient[]; bcc: Recipient[];
  report: { id: string; code: string; version: number; title: string; state: string; current: boolean };
  attachment: { name: string; bytes: number | null; sha256: string | null };
  sender_mailbox: string | null; channel: "graph" | "emailjs"; external_domains: string[]; sendable: boolean;
  blocking: { code: string; message: string }[];
  resend_of_email_id: string | null; correction_of_email_id: string | null; email_id: string | null; updated_at: string;
};

export type EmailState = "QUEUED" | "SENDING" | "ACCEPTED" | "UNKNOWN" | "FAILED";
export type EmailListItem = {
  id: string; state: EmailState; state_text: string; subject: string; report_id: string; report_code: string;
  report_version: number; recipient_count: number; created_at: string;
};
export type Email = {
  id: string; state: EmailState; state_text: string; draft_id: string; report_id: string; report_code: string;
  report_version: number; subject: string; sender_mailbox: string | null; channel: "graph" | "emailjs";
  provider_id: string | null; attachment: { name: string | null; bytes: number | null; sha256: string | null };
  recipients: { kind: string; address: string; state: string; observation_source: string | null; observed_at: string | null }[];
  attempts: { attempt_no: number; outcome: string; http_status: number | null; error_code: string | null;
    error_message: string | null; started_at: string; finished_at: string | null }[];
  error: { code: string; message: string } | null; created_at: string; accepted_at: string | null;
  reconciliation: { outcome: string; evidence_ref: string; reason: string; at: string } | null;
};

export function reportStatus(r: { state: string; outdated: boolean }): { label: string; tone: Tone } {
  if (r.state === "FAILED") return { label: "PDF failed", tone: "error" };
  if (r.state !== "READY") return { label: "Creating PDF", tone: "progress" };
  if (r.outdated) return { label: "Outdated", tone: "warning" };
  return { label: "Ready", tone: "success" };
}

export function emailStatus(state: string): { label: string; tone: Tone } {
  switch (state) {
    case "QUEUED":
    case "SENDING":
      return { label: "Sending", tone: "progress" };
    case "ACCEPTED":
      return { label: "Accepted by provider", tone: "success" };
    case "UNKNOWN":
      return { label: "Outcome unknown", tone: "warning" };
    default:
      return { label: "Not sent", tone: "error" };
  }
}

/** "a@x.com, b@y.com" or one per line -> addresses. Empty entries are dropped; validation is the server's. */
export function parseRecipients(text: string): string[] {
  return text.split(/[\n,;]+/).map((s) => s.trim()).filter(Boolean);
}

export function recipientsText(list: Recipient[]): string {
  return list.map((r) => r.address).join(", ");
}

export function formatBytes(n: number | null | undefined): string {
  if (!n) return "0 KB";
  return n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function periodText(f: { date_from: string; date_to: string }): string {
  return f.date_from === f.date_to ? f.date_from : `${f.date_from} to ${f.date_to}`;
}
