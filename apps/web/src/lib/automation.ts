// Automation (M7): schedules, exceptions and notifications. Words first; tone only reinforces them.

import type { Tone } from "./status";

export type Occurrence = { due_at: string; local_date: string; period_start: string; period_end: string };
export type ScheduleConfig = {
  cadence: "DAILY" | "WEEKLY" | "MONTHLY"; local_time: string; weekday: number | null; monthday: number | null;
  timezone: string; department_ids: string[]; units: string[]; title: string; include_detail: boolean;
  subject: string | null; mode: "DRAFT_ONLY" | "AUTO_SEND"; empty_policy: "SKIP" | "DRAFT";
};
export type Run = {
  id: string; version: number; period_start: string; period_end: string; run_kind: "SCHEDULED" | "MANUAL";
  mode: string; state: string; due_at: string; report_id: string | null; draft_id: string | null; email_id: string | null;
  excluded_pending: number | null; note: string | null; error: { code: string; message: string } | null;
};
export type Schedule = {
  id: string; name: string; version: number; row_version: number; active: boolean; paused_reason: string | null;
  config: ScheduleConfig; to: string[]; cc: string[]; bcc: string[];
  approval_state: "APPROVED" | "UNAPPROVED"; auto_send_active: boolean; auto_send_blocked_reason: string | null;
  policy: { hash: string; sender_mailbox: string | null }; next_runs: Occurrence[]; runs?: Run[];
};
export type ExceptionItem = {
  id: string; kind: string; severity: "INFO" | "WARNING" | "CRITICAL"; reason: string; object_type: string;
  link: string | null; production_date: string | null; status: string; first_seen_at: string; last_seen_at: string;
  resolution: string | null;
};
export type Notification = {
  id: string; kind: string; title: string; body: string; link: string | null; read: boolean; email_state: string; created_at: string;
};

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export function cadenceText(c: Pick<ScheduleConfig, "cadence" | "local_time" | "weekday" | "monthday" | "timezone">): string {
  if (c.cadence === "DAILY") return `Every day at ${c.local_time} (${c.timezone}), reporting the previous day`;
  if (c.cadence === "WEEKLY") return `Every ${WEEKDAYS[(c.weekday ?? 1) - 1]} at ${c.local_time} (${c.timezone}), reporting the previous Monday–Sunday`;
  const day = c.monthday ?? 1;
  return `Monthly on day ${day}${day > 28 ? " (or the month's last day)" : ""} at ${c.local_time} (${c.timezone}), reporting the previous month`;
}

export function runStatus(state: string): { label: string; tone: Tone } {
  const map: Record<string, { label: string; tone: Tone }> = {
    QUEUED: { label: "Queued", tone: "progress" }, WAITING: { label: "Waiting for an earlier run", tone: "warning" },
    REPORTING: { label: "Creating report", tone: "progress" }, SENDING: { label: "Sending", tone: "progress" },
    DRAFTED: { label: "Draft ready", tone: "success" }, SENT: { label: "Accepted by provider", tone: "success" },
    SKIPPED_EMPTY: { label: "Skipped: no approved records", tone: "neutral" },
    SKIPPED_MISSED: { label: "Missed (not run)", tone: "warning" }, FAILED: { label: "Failed", tone: "error" },
    CANCELLED: { label: "Cancelled", tone: "neutral" }, HALTED: { label: "Stopped: email outcome unknown", tone: "error" },
  };
  return map[state] ?? { label: state, tone: "neutral" };
}

export const BLOCKED_REASON: Record<string, string> = {
  AUTO_SEND_DISABLED: "Automatic sending is turned off for the company.",
  DRAFT_ONLY: "This schedule prepares drafts only.",
  AUTO_SEND_NOT_APPROVED: "Automatic sending has not been approved for this version.",
  POLICY_CHANGED: "Something in the approved policy changed (for example the sender mailbox). Approve again.",
};

export function severityTone(s: string): Tone {
  return s === "CRITICAL" ? "error" : s === "WARNING" ? "warning" : "neutral";
}

export const EXCEPTION_KIND: Record<string, string> = {
  ENTRY_MISSING_FIELDS: "Entry missing values", MACHINE_DEPARTMENT_MISMATCH: "Machine not in department",
  DUPLICATE_UNRESOLVED: "Possible duplicate", UNUSUAL_PRODUCTION: "Unusual production vs target",
  UNUSUAL_STOP: "Unusual stop minutes", MISSING_SUBMISSION: "Missing daily submission", SYNC_FAILURE: "Sync problem",
  POWERBI_STALE: "Power BI out of date", REPORT_FAILED: "Report failed", EMAIL_UNKNOWN: "Email outcome unknown",
  SCHEDULE_RUN_FAILED: "Scheduled report problem",
};
