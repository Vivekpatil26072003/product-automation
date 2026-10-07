// Typed client for /api/v1. Every mutation carries the CSRF token and a fresh Idempotency-Key.
// Errors always arrive as the stable envelope {error:{code,message,fields,request_id}}.

export type Issue = { field: string | null; code: string; message: string; severity: "error" | "warning" };

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public fields: Issue[] = [],
    public requestId?: string,
    /** Any additional fields of the error envelope, e.g. current_version or excluded. */
    public details: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

let csrfToken: string | null = null;
export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

async function parse<T>(res: Response): Promise<T> {
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const e = body?.error;
    throw new ApiError(
      res.status,
      e?.code ?? "HTTP_ERROR",
      e?.message ?? "The request could not be completed. Try again.",
      e?.fields ?? [],
      e?.request_id,
      e ?? {},
    );
  }
  return body as T;
}

export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`/api/v1${path}`, { credentials: "same-origin", signal, cache: "no-store" });
  return parse<T>(res);
}

export async function apiSend<T>(
  method: "POST" | "PATCH" | "PUT" | "DELETE",
  path: string,
  body?: unknown,
  opts: { idempotencyKey?: string; ifMatch?: string } = {},
): Promise<T> {
  const headers: Record<string, string> = {
    "Idempotency-Key": opts.idempotencyKey ?? newIdempotencyKey(),
  };
  if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  if (opts.ifMatch) headers["If-Match"] = opts.ifMatch;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`/api/v1${path}`, {
    method,
    credentials: "same-origin",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return parse<T>(res);
}

export async function sha256Hex(file: Blob): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

// --- contracts (mirror services/api; see packages/contracts/openapi.json) ---------------------

export type Session = {
  user_id: string;
  tenant_id: string;
  display_name: string | null;
  email: string | null;
  roles: string[];
  departments: { id: string; code: string; name: string }[];
  timezone: string;
  csrf_token: string;
};

export type Limits = {
  max_files: number;
  max_file_bytes: number;
  max_batch_bytes: number;
  max_pdf_pages: number;
  max_office_rows: number;
  accepted_extensions: string[];
};

export type Job = {
  id: string;
  kind: string;
  state: "QUEUED" | "RUNNING" | "RETRY_WAIT" | "SUCCEEDED" | "PARTIAL" | "FAILED" | "CANCELLED";
  processed: number;
  total: number | null;
  error: Issue | null;
  attempt: number;
  max_attempts: number;
  retryable: boolean;
  cancel_requested: boolean;
  next_attempt_at: string | null;
  generation: number;
};

export type UploadSlot = {
  id: string;
  slot_no: number;
  name: string;
  state: string;
  put_url?: string;
  headers?: Record<string, string>;
};

export type BatchFile = {
  id: string;
  slot_no: number;
  name: string;
  extension: string;
  bytes: number;
  state: "UPLOADING" | "QUARANTINED" | "READY" | "REJECTED" | "EXPIRED";
  scan_status: "PENDING" | "CLEAN" | "INFECTED" | "ERROR" | "SKIPPED_DEV";
  scanner: string | null;
  reject: { code: string; message: string } | null;
  duplicate_of: string | null;
  pages: { total: number | null; processed: number; succeeded: number[]; failed: number[] };
  jobs: { scan: Job | null; parse: Job | null; extract: Job | null };
  to_review: number;
};

export type Batch = {
  id: string;
  department: { id: string; code: string; name: string };
  owner_id: string;
  created_at: string;
  files: BatchFile[];
  summary: {
    files: number;
    rejected: number;
    waiting_for_upload: number;
    scanning: number;
    parsed: number;
    partial: number;
    pages_processed: number;
    pages_total: number;
    to_review: number;
    in_progress: boolean;
  };
};

// --- review (M3) -----------------------------------------------------------------------------

export const FIELD_NAMES = [
  "production_date", "department_id", "machine_id", "operator_name", "production_qty", "target_qty",
  "unit", "status", "stop_minutes", "remarks",
] as const;
export type FieldName = (typeof FIELD_NAMES)[number];

export type Evidence = {
  id: string;
  page: number | null;
  text: string;
  char_start: number | null;
  char_end: number | null;
  sheet: string | null;
  cell: string | null;
  polygon: number[][] | null;
  confidence: number | null;
};

export type CandidateField = {
  value: string | number | null;
  display: string | null;
  raw: string | null;
  source: "extracted" | "reviewer" | "upload_context" | "manual";
  evidence: Evidence[];
};

export type CandidateIssue = { field: FieldName | null; code: string; message: string; severity: "error" | "warning" };

export type Candidate = {
  id: string;
  batch_id: string;
  upload_id: string;
  source_record_key: string;
  state: "NEEDS_REVIEW" | "APPROVED" | "REJECTED" | "SUPERSEDED";
  confidence: "OK" | "ATTENTION" | "UNASSESSED";
  fields: Record<FieldName, CandidateField>;
  issues: CandidateIssue[];
  approvable: boolean;
  duplicate_decision: { action: "KEEP" | "SKIP"; reason: string | null } | null;
  duplicates: { kind: string; record_id: string | null; candidate_id: string | null; upload_id: string | null }[];
  previous_candidate_id: string | null;
  record_id: string | null;
  reject_reason: string | null;
  change_count: number;
  version: number;
};

export type ExtractionInfo = {
  upload_id: string;
  state: "SUCCEEDED" | "NO_RECORDS" | "FAILED";
  extractor: string;
  model: string | null;
  release_state: string;
  warnings: string[];
  error_code: string | null;
  error_message: string | null;
};

export type SourcePage = {
  upload_id: string;
  page_no: number;
  page_count: number | null;
  state: "SUCCEEDED" | "FAILED";
  parser: string;
  error: { code: string; message: string } | null;
  text: string | null;
  spans: { id: string; text: string; char_start: number; char_end: number }[];
  image_url: string | null;
  file_name: string;
};

export type Master = { id: string; code: string; name: string | null; department_id?: string; active: boolean };

export type RecordFields = {
  production_date: string; department_id: string; operator_name: string; machine_id: string;
  production_qty: string; target_qty: string; unit: string; status: string; stop_minutes: number; remarks: string;
};

export type RecordView = {
  id: string;
  state: "ACTIVE" | "ARCHIVED";
  version: number;
  archived_at: string | null;
  archive_reason: string | null;
  current_revision: number;
  fields: RecordFields;
  display: { department: string | null; machine: string | null };
  revisions: {
    id: string; number: number; state: "PENDING" | "APPROVED" | "REJECTED"; fields: RecordFields;
    reason: string | null; created_at: string; approved_at: string | null;
    provenance: Record<string, unknown> | null;
  }[];
};
