"""SQLAlchemy Core table definitions used for queries.

migrations/ is the authoritative DDL (constraints, RLS, triggers, grants). These definitions list
only columns; tests/integration/test_schema.py fails if they drift from the migrated database.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP, UUID

metadata = MetaData()


def _id() -> Column:
    return Column("id", UUID(as_uuid=True), primary_key=True)


def _tenant() -> Column:
    return Column("tenant_id", UUID(as_uuid=True), nullable=False)


def _ts() -> list[Column]:
    return [
        Column("created_at", TIMESTAMP(timezone=True)),
        Column("updated_at", TIMESTAMP(timezone=True)),
        Column("version", Integer),
    ]


tenant = Table(
    "tenant", metadata, _id(),
    Column("name", Text), Column("timezone", Text), Column("date_order", Text),
    Column("data_version", BigInteger), Column("settings", JSONB), *_ts(),
)  # fmt: skip

membership = Table(
    "membership", metadata, _id(), _tenant(),
    Column("subject", Text), Column("email", Text), Column("display_name", Text),
    Column("roles", ARRAY(Text)), Column("active", Boolean), *_ts(),
)  # fmt: skip

department = Table(
    "department", metadata, _id(), _tenant(),
    Column("code", Text), Column("name", Text), Column("active", Boolean),
    Column("expected_daily_submission", Boolean), Column("sort_order", Integer), *_ts(),
)  # fmt: skip

membership_department = Table(
    "membership_department", metadata, _tenant(),
    Column("membership_id", UUID(as_uuid=True), primary_key=True),
    Column("department_id", UUID(as_uuid=True), primary_key=True),
)  # fmt: skip

machine = Table(
    "machine", metadata, _id(), _tenant(),
    Column("department_id", UUID(as_uuid=True)), Column("code", Text), Column("name", Text),
    Column("active", Boolean), *_ts(),
)  # fmt: skip

operator = Table(
    "operator", metadata, _id(), _tenant(),
    Column("department_id", UUID(as_uuid=True)), Column("name", Text), Column("active", Boolean), *_ts(),
)  # fmt: skip

master_alias = Table(
    "master_alias", metadata, _id(), _tenant(),
    Column("alias", Text), Column("alias_key", Text),
    Column("department_id", UUID(as_uuid=True)), Column("machine_id", UUID(as_uuid=True)),
    Column("operator_id", UUID(as_uuid=True)), Column("kind", Text),
    Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

unit_alias = Table(
    "unit_alias", metadata, _id(), _tenant(),
    Column("alias", Text), Column("alias_key", Text), Column("unit", Text),
    Column("factor", Numeric(20, 10)), Column("active", Boolean), *_ts(),
)  # fmt: skip

auth_session = Table(
    "auth_session", metadata, _id(), _tenant(),
    Column("membership_id", UUID(as_uuid=True)), Column("token_hash", LargeBinary),
    Column("auth_method", Text), Column("created_at", TIMESTAMP(timezone=True)),
    Column("expires_at", TIMESTAMP(timezone=True)), Column("last_seen_at", TIMESTAMP(timezone=True)),
    Column("revoked_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

oidc_login_state = Table(
    "oidc_login_state", metadata,
    Column("state_hash", LargeBinary, primary_key=True), Column("nonce", Text),
    Column("code_verifier", Text), Column("return_to", Text),
    Column("created_at", TIMESTAMP(timezone=True)), Column("expires_at", TIMESTAMP(timezone=True)),
    Column("consumed_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

security_event = Table(
    "security_event", metadata, _id(),
    Column("event", Text), Column("detail", Text), Column("correlation_id", UUID(as_uuid=True)),
    Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

audit_event = Table(
    "audit_event", metadata, _id(), _tenant(),
    Column("actor_type", Text), Column("actor_id", UUID(as_uuid=True)), Column("action", Text),
    Column("object_type", Text), Column("object_id", UUID(as_uuid=True)),
    Column("object_revision", Integer), Column("reason", Text),
    Column("before", JSONB), Column("after", JSONB),
    Column("correlation_id", UUID(as_uuid=True)), Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

outbox = Table(
    "outbox", metadata, _id(), _tenant(),
    Column("event_key", Text), Column("event_type", Text), Column("payload", JSONB),
    Column("correlation_id", UUID(as_uuid=True)), Column("created_at", TIMESTAMP(timezone=True)),
    Column("dispatched_at", TIMESTAMP(timezone=True)), Column("dispatch_attempts", Integer),
    Column("last_error", Text),
)  # fmt: skip

job = Table(
    "job", metadata, _id(), _tenant(),
    Column("kind", Text), Column("object_id", UUID(as_uuid=True)), Column("generation", Integer),
    Column("state", Text), Column("attempts", Integer), Column("max_attempts", Integer),
    Column("next_attempt_at", TIMESTAMP(timezone=True)), Column("lease_token", UUID(as_uuid=True)),
    Column("lease_until", TIMESTAMP(timezone=True)), Column("worker_id", Text),
    Column("cancel_requested", Boolean), Column("processed", Integer), Column("total", Integer),
    Column("retryable", Boolean), Column("error_code", Text), Column("error_message", Text),
    Column("result", JSONB), Column("source_event_key", Text), Column("created_by", UUID(as_uuid=True)),
    Column("correlation_id", UUID(as_uuid=True)), Column("finished_at", TIMESTAMP(timezone=True)), *_ts(),
)  # fmt: skip

job_attempt = Table(
    "job_attempt", metadata, _id(), _tenant(),
    Column("job_id", UUID(as_uuid=True)), Column("attempt_no", Integer),
    Column("lease_token", UUID(as_uuid=True)), Column("worker_id", Text),
    Column("started_at", TIMESTAMP(timezone=True)), Column("finished_at", TIMESTAMP(timezone=True)),
    Column("outcome", Text), Column("error_code", Text), Column("error_message", Text),
)  # fmt: skip

idempotency_record = Table(
    "idempotency_record", metadata, _tenant(),
    Column("actor_id", UUID(as_uuid=True), primary_key=True), Column("route", Text, primary_key=True),
    Column("idem_key", Text, primary_key=True), Column("request_hash", LargeBinary),
    Column("status_code", Integer), Column("response_body", JSONB),
    Column("created_at", TIMESTAMP(timezone=True)), Column("expires_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

production_record = Table(
    "production_record", metadata, _id(), _tenant(),
    Column("department_id", UUID(as_uuid=True)), Column("current_revision_id", UUID(as_uuid=True)),
    Column("production_date", Date), Column("state", Text), Column("entry_key", UUID(as_uuid=True)),
    Column("entry_label", Text), Column("archived_at", TIMESTAMP(timezone=True)),
    Column("archived_by", UUID(as_uuid=True)), Column("archive_reason", Text),
    Column("created_by", UUID(as_uuid=True)), *_ts(),
)  # fmt: skip

record_revision = Table(
    "record_revision", metadata, _id(), _tenant(),
    Column("record_id", UUID(as_uuid=True)), Column("number", Integer),
    Column("production_date", Date), Column("department_id", UUID(as_uuid=True)),
    Column("machine_id", UUID(as_uuid=True)), Column("operator_name", Text),
    Column("production_qty", Numeric(18, 3)), Column("target_qty", Numeric(18, 3)),
    Column("unit", Text), Column("status", Text), Column("stop_minutes", Integer),
    Column("remarks", Text), Column("provenance", JSONB), Column("approval_state", Text),
    Column("reason", Text), Column("created_by", UUID(as_uuid=True)),
    Column("approved_by", UUID(as_uuid=True)), Column("approved_at", TIMESTAMP(timezone=True)),
    Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

# --- M2 ingestion (migration 0002) -----------------------------------------------------------

batch = Table(
    "batch", metadata, _id(), _tenant(),
    Column("department_id", UUID(as_uuid=True)), Column("owner_id", UUID(as_uuid=True)),
    Column("file_count", Integer), Column("total_bytes", BigInteger), *_ts(),
)  # fmt: skip

upload = Table(
    "upload", metadata, _id(), _tenant(),
    Column("batch_id", UUID(as_uuid=True)), Column("slot_no", Integer), Column("display_name", Text),
    Column("extension", Text), Column("declared_mime", Text), Column("declared_bytes", BigInteger),
    Column("declared_sha256", LargeBinary), Column("object_key", Text), Column("state", Text),
    Column("detected_type", Text), Column("page_count", Integer), Column("scan_status", Text),
    Column("scanner", Text), Column("reject_code", Text), Column("reject_message", Text),
    Column("duplicate_of", UUID(as_uuid=True)), Column("expires_at", TIMESTAMP(timezone=True)),
    Column("completed_at", TIMESTAMP(timezone=True)), Column("ready_at", TIMESTAMP(timezone=True)),
    Column("source_purged_at", TIMESTAMP(timezone=True)), *_ts(),
)  # fmt: skip

page_result = Table(
    "page_result", metadata, _id(), _tenant(),
    Column("upload_id", UUID(as_uuid=True)), Column("page_no", Integer), Column("pipeline_version", Text),
    Column("parser", Text), Column("state", Text), Column("text_key", Text), Column("char_count", Integer),
    Column("span_count", Integer), Column("min_confidence", Numeric(5, 4)), Column("warnings", ARRAY(Text)),
    Column("error_code", Text), Column("error_message", Text), Column("attempts", Integer),
    Column("created_at", TIMESTAMP(timezone=True)), Column("updated_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

# --- M3 extraction and review (migration 0003) -----------------------------------------------

extraction = Table(
    "extraction", metadata, _id(), _tenant(),
    Column("upload_id", UUID(as_uuid=True)), Column("job_id", UUID(as_uuid=True)), Column("extractor", Text),
    Column("model", Text), Column("prompt_version", Text), Column("prompt_hash", Text),
    Column("schema_version", Text), Column("release_state", Text), Column("state", Text),
    Column("candidate_count", Integer), Column("warnings", ARRAY(Text)), Column("error_code", Text),
    Column("error_message", Text), Column("input_tokens", Integer), Column("output_tokens", Integer),
    Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

evidence = Table(
    "evidence", metadata, _id(), _tenant(),
    Column("upload_id", UUID(as_uuid=True)), Column("pipeline_version", Text), Column("span_id", Text),
    Column("page", Integer), Column("raw_text", Text), Column("char_start", Integer), Column("char_end", Integer),
    Column("sheet", Text), Column("cell", Text), Column("polygon", JSONB), Column("confidence", Numeric(5, 4)),
    Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

candidate = Table(
    "candidate", metadata, _id(), _tenant(),
    Column("batch_id", UUID(as_uuid=True)), Column("upload_id", UUID(as_uuid=True)),
    Column("extraction_id", UUID(as_uuid=True)), Column("source_record_key", Text), Column("fields", JSONB),
    Column("issues", JSONB), Column("confidence", Text), Column("state", Text),
    Column("duplicate_decision", JSONB), Column("previous_candidate_id", UUID(as_uuid=True)),
    Column("record_id", UUID(as_uuid=True)), Column("reject_reason", Text), Column("decided_by", UUID(as_uuid=True)),
    Column("decided_at", TIMESTAMP(timezone=True)), *_ts(),
)  # fmt: skip

candidate_change = Table(
    "candidate_change", metadata, _id(), _tenant(),
    Column("candidate_id", UUID(as_uuid=True)), Column("version", Integer), Column("actor_id", UUID(as_uuid=True)),
    Column("changes", JSONB), Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

duplicate_link = Table(
    "duplicate_link", metadata, _id(), _tenant(),
    Column("candidate_id", UUID(as_uuid=True)), Column("kind", Text), Column("other_record_id", UUID(as_uuid=True)),
    Column("other_candidate_id", UUID(as_uuid=True)), Column("other_upload_id", UUID(as_uuid=True)),
    Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

model_release = Table(
    "model_release", metadata, _id(), _tenant(),
    Column("extractor", Text), Column("model", Text), Column("prompt_hash", Text), Column("schema_version", Text),
    Column("state", Text), Column("evaluation_run_id", UUID(as_uuid=True)), Column("approved_by", UUID(as_uuid=True)),
    Column("approved_at", TIMESTAMP(timezone=True)), Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

evaluation_run = Table(
    "evaluation_run", metadata, _id(), _tenant(),
    Column("extractor", Text), Column("model", Text), Column("prompt_hash", Text), Column("schema_version", Text),
    Column("dataset_hash", Text), Column("metrics", JSONB), Column("gates", JSONB), Column("passed", Boolean),
    Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

# --- M4 exports (migration 0004) ------------------------------------------------------------

export = Table(
    "export", metadata, _id(), _tenant(),
    Column("requested_by", UUID(as_uuid=True)), Column("format", Text), Column("filter_json", JSONB),
    Column("snapshot_json", JSONB), Column("metrics_json", JSONB), Column("data_version", BigInteger),
    Column("timezone", Text), Column("row_count", Integer), Column("state", Text), Column("file_key", Text),
    Column("sha256", Text), Column("bytes", BigInteger), Column("error_code", Text),
    Column("created_at", TIMESTAMP(timezone=True)), Column("finished_at", TIMESTAMP(timezone=True)),
    Column("report_id", UUID(as_uuid=True)), Column("file_purged_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

# --- M5 integrations (migration 0005) --------------------------------------------------------

integration_connection = Table(
    "integration_connection", metadata, _id(), _tenant(),
    Column("provider", Text), Column("name", Text), Column("state", Text), Column("config", JSONB),
    Column("secret_ciphertext", LargeBinary), Column("secret_key_id", Text), Column("config_version", Integer),
    Column("mapping_version", Integer), Column("last_test_at", TIMESTAMP(timezone=True)),
    Column("last_test_ok", Boolean), Column("last_error_code", Text), Column("last_error_message", Text),
    Column("last_sync_at", TIMESTAMP(timezone=True)), Column("created_by", UUID(as_uuid=True)),
    Column("disconnected_at", TIMESTAMP(timezone=True)), *_ts(),
)  # fmt: skip

record_sync = Table(
    "record_sync", metadata, _id(), _tenant(),
    Column("connection_id", UUID(as_uuid=True)), Column("record_id", UUID(as_uuid=True)),
    Column("target_revision", Integer), Column("synced_revision", Integer), Column("state", Text),
    Column("external_ref", Text), Column("last_hash", Text), Column("attempts", Integer), Column("error_code", Text),
    Column("error_message", Text), Column("synced_at", TIMESTAMP(timezone=True)),
    Column("created_at", TIMESTAMP(timezone=True)), Column("updated_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

powerbi_refresh = Table(
    "powerbi_refresh", metadata, _id(), _tenant(),
    Column("connection_id", UUID(as_uuid=True)), Column("source_data_version", BigInteger), Column("state", Text),
    Column("provider_request_id", Text), Column("requested_at", TIMESTAMP(timezone=True)),
    Column("completed_at", TIMESTAMP(timezone=True)), Column("error_code", Text), Column("error_message", Text),
)  # fmt: skip

erp_sync_attempt = Table(
    "erp_sync_attempt", metadata, _id(), _tenant(),
    Column("connection_id", UUID(as_uuid=True)), Column("record_id", UUID(as_uuid=True)),
    Column("revision", Integer), Column("direction", Text), Column("mapping_version", Integer),
    Column("outcome", Text), Column("external_ref", Text), Column("error_code", Text), Column("error_message", Text),
    Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

# --- M6 reports and email (migration 0006) ---------------------------------------------------

report = Table(
    "report", metadata, _id(), _tenant(),
    Column("series_id", UUID(as_uuid=True)), Column("version", Integer), Column("supersedes_id", UUID(as_uuid=True)),
    Column("title", Text), Column("filter_json", JSONB), Column("date_from", Date), Column("date_to", Date),
    Column("department_ids", ARRAY(UUID(as_uuid=True))), Column("timezone", Text), Column("data_version", BigInteger),
    Column("record_count", Integer), Column("include_detail", Boolean), Column("is_empty", Boolean),
    Column("metrics_json", JSONB), Column("facts_json", JSONB), Column("template_version", Text),
    Column("narrative_json", JSONB), Column("narrative_source", Text), Column("narrative_model", Text),
    Column("narrative_prompt_hash", Text), Column("narrative_fallback_reason", Text), Column("state", Text),
    Column("file_key", Text), Column("sha256", Text), Column("bytes", BigInteger), Column("error_code", Text),
    Column("error_message", Text), Column("outdated_at", TIMESTAMP(timezone=True)), Column("outdated_reason", Text),
    Column("created_by", UUID(as_uuid=True)), Column("created_at", TIMESTAMP(timezone=True)),
    Column("ready_at", TIMESTAMP(timezone=True)), Column("file_purged_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

report_item = Table(
    "report_item", metadata, _tenant(),
    Column("report_id", UUID(as_uuid=True), primary_key=True),
    Column("record_id", UUID(as_uuid=True), primary_key=True),
    Column("revision_id", UUID(as_uuid=True)), Column("revision_number", Integer), Column("position", Integer),
    Column("fields", JSONB),
)  # fmt: skip

email_draft = Table(
    "email_draft", metadata, _id(), _tenant(),
    Column("report_id", UUID(as_uuid=True)), Column("subject", Text), Column("body", Text), Column("recipients", JSONB),
    Column("state", Text), Column("content_hash", Text), Column("resend_of_email_id", UUID(as_uuid=True)),
    Column("resend_reason", Text), Column("correction_of_email_id", UUID(as_uuid=True)),
    Column("created_by", UUID(as_uuid=True)), *_ts(),
)  # fmt: skip

email_message = Table(
    "email_message", metadata, _id(), _tenant(),
    Column("draft_id", UUID(as_uuid=True)), Column("draft_version", Integer), Column("content_hash", Text),
    Column("report_id", UUID(as_uuid=True)), Column("intent_key", Text), Column("state", Text),
    Column("connection_id", UUID(as_uuid=True)), Column("sender_mailbox", Text), Column("subject", Text),
    Column("recipients", JSONB), Column("attachment_name", Text), Column("attachment_sha256", Text),
    Column("attachment_bytes", BigInteger), Column("provider_request_id", Text), Column("error_code", Text),
    Column("error_message", Text), Column("created_by", UUID(as_uuid=True)),
    Column("created_at", TIMESTAMP(timezone=True)), Column("accepted_at", TIMESTAMP(timezone=True)),
    Column("finished_at", TIMESTAMP(timezone=True)), Column("reconciled_by", UUID(as_uuid=True)),
    Column("reconciled_at", TIMESTAMP(timezone=True)), Column("reconcile_outcome", Text),
    Column("reconcile_evidence", Text), Column("reconcile_reason", Text), Column("channel", Text),
)  # fmt: skip

email_attempt = Table(
    "email_attempt", metadata, _id(), _tenant(),
    Column("email_id", UUID(as_uuid=True)), Column("attempt_no", Integer), Column("outcome", Text),
    Column("http_status", Integer), Column("provider_request_id", Text), Column("error_code", Text),
    Column("error_message", Text), Column("started_at", TIMESTAMP(timezone=True)),
    Column("finished_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

email_recipient_status = Table(
    "email_recipient_status", metadata, _id(), _tenant(),
    Column("email_id", UUID(as_uuid=True)), Column("kind", Text), Column("address", Text), Column("state", Text),
    Column("observation_source", Text), Column("observed_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

# --- M7 automation (migration 0007) ----------------------------------------------------------

schedule = Table(
    "schedule", metadata, _id(), _tenant(),
    Column("owner_id", UUID(as_uuid=True)), Column("name", Text), Column("version", Integer), Column("config", JSONB),
    Column("active", Boolean), Column("paused_reason", Text), Column("approval_state", Text),
    Column("approval_hash", Text), Column("approved_version", Integer), Column("approved_by", UUID(as_uuid=True)),
    Column("approved_at", TIMESTAMP(timezone=True)), Column("last_due_at", TIMESTAMP(timezone=True)),
    Column("next_due_at", TIMESTAMP(timezone=True)), Column("created_at", TIMESTAMP(timezone=True)),
    Column("updated_at", TIMESTAMP(timezone=True)), Column("row_version", Integer),
)  # fmt: skip

schedule_version = Table(
    "schedule_version", metadata, _tenant(),
    Column("schedule_id", UUID(as_uuid=True), primary_key=True), Column("version", Integer, primary_key=True),
    Column("config", JSONB), Column("created_by", UUID(as_uuid=True)), Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

schedule_run = Table(
    "schedule_run", metadata, _id(), _tenant(),
    Column("schedule_id", UUID(as_uuid=True)), Column("version", Integer), Column("period_start", Date),
    Column("period_end", Date), Column("run_kind", Text), Column("mode", Text),
    Column("due_at", TIMESTAMP(timezone=True)),
    Column("state", Text), Column("cancel_requested", Boolean), Column("report_id", UUID(as_uuid=True)),
    Column("draft_id", UUID(as_uuid=True)), Column("email_id", UUID(as_uuid=True)), Column("excluded_pending", Integer),
    Column("note", Text), Column("error_code", Text), Column("error_message", Text),
    Column("requested_by", UUID(as_uuid=True)), Column("created_at", TIMESTAMP(timezone=True)),
    Column("updated_at", TIMESTAMP(timezone=True)), Column("finished_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

exception_item = Table(
    "exception_item", metadata, _id(), _tenant(),
    Column("kind", Text), Column("severity", Text), Column("audience", Text), Column("reason", Text),
    Column("object_type", Text), Column("object_id", UUID(as_uuid=True)), Column("department_id", UUID(as_uuid=True)),
    Column("production_date", Date), Column("dedupe_key", Text), Column("status", Text), Column("detail", JSONB),
    Column("first_seen_at", TIMESTAMP(timezone=True)), Column("last_seen_at", TIMESTAMP(timezone=True)),
    Column("resolved_at", TIMESTAMP(timezone=True)), Column("resolved_by", UUID(as_uuid=True)),
    Column("resolution", Text), Column("version", Integer),
)  # fmt: skip

exception_event = Table(
    "exception_event", metadata, _id(), _tenant(),
    Column("exception_id", UUID(as_uuid=True)), Column("action", Text), Column("actor_id", UUID(as_uuid=True)),
    Column("note", Text), Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

notification = Table(
    "notification", metadata, _id(), _tenant(),
    Column("recipient_id", UUID(as_uuid=True)), Column("kind", Text), Column("dedupe_key", Text), Column("title", Text),
    Column("body", Text), Column("link", Text), Column("department_id", UUID(as_uuid=True)), Column("day", Date),
    Column("read_at", TIMESTAMP(timezone=True)), Column("email_state", Text), Column("email_error", Text),
    Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

# --- M8 hardening (migration 0008) -----------------------------------------------------------

retention_hold = Table(
    "retention_hold", metadata, _id(), _tenant(),
    Column("object_type", Text), Column("object_id", UUID(as_uuid=True)), Column("reason", Text),
    Column("created_by", UUID(as_uuid=True)), Column("created_at", TIMESTAMP(timezone=True)),
    Column("released_at", TIMESTAMP(timezone=True)), Column("released_by", UUID(as_uuid=True)),
)  # fmt: skip

retention_run = Table(
    "retention_run", metadata, _id(), _tenant(),
    Column("dry_run", Boolean), Column("eligible", JSONB), Column("purged", Integer), Column("held", Integer),
    Column("failed", Integer), Column("requested_by", UUID(as_uuid=True)),
    Column("started_at", TIMESTAMP(timezone=True)), Column("finished_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

retention_event = Table(
    "retention_event", metadata, _id(), _tenant(),
    Column("run_id", UUID(as_uuid=True)), Column("action", Text), Column("category", Text),
    Column("object_type", Text), Column("object_id", UUID(as_uuid=True)), Column("object_keys", JSONB),
    Column("error", Text), Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

roi_baseline = Table(
    "roi_baseline", metadata,
    Column("tenant_id", UUID(as_uuid=True), primary_key=True),
    Column("measured_from", Date), Column("measured_to", Date),
    Column("manual_minutes_per_report", Numeric(8, 2)), Column("manual_minutes_per_entry", Numeric(8, 2)),
    Column("manual_minutes_per_email", Numeric(8, 2)), Column("manual_followups_per_week", Numeric(8, 2)),
    Column("manual_correction_rate_pct", Numeric(5, 2)), Column("notes", Text),
    Column("updated_by", UUID(as_uuid=True)), Column("updated_at", TIMESTAMP(timezone=True)),
    Column("version", Integer),
)  # fmt: skip

# --- Customer orders (migration 0010) --------------------------------------------------------

order_draft = Table(
    "order_draft", metadata, _id(), _tenant(),
    Column("batch_id", UUID(as_uuid=True)), Column("upload_id", UUID(as_uuid=True)),
    Column("department_id", UUID(as_uuid=True)), Column("source", Text), Column("extraction_id", UUID(as_uuid=True)),
    Column("page_no", Integer), Column("fields", JSONB), Column("issues", JSONB), Column("state", Text),
    Column("order_id", UUID(as_uuid=True)), Column("reject_reason", Text), Column("decided_by", UUID(as_uuid=True)),
    Column("decided_at", TIMESTAMP(timezone=True)), Column("created_by", UUID(as_uuid=True)), *_ts(),
    Column("extra", JSONB), Column("reading", JSONB), Column("decision", JSONB),
)  # fmt: skip

customer_order = Table(
    "customer_order", metadata, _id(), _tenant(),
    Column("department_id", UUID(as_uuid=True)), Column("draft_id", UUID(as_uuid=True)), Column("order_ref", Text),
    Column("current_revision_id", UUID(as_uuid=True)), Column("current_number", Integer), Column("state", Text),
    Column("created_by", UUID(as_uuid=True)), *_ts(), Column("customer_id", UUID(as_uuid=True)),
)  # fmt: skip

order_revision = Table(
    "order_revision", metadata, _id(), _tenant(),
    Column("order_id", UUID(as_uuid=True)), Column("number", Integer), Column("customer_name", Text),
    Column("customer_number", Text), Column("customer_email", Text), Column("mobile", Text),
    Column("order_number", Text), Column("order_date", Date), Column("delivery_date", Date), Column("package", Text),
    Column("size", Text), Column("material", Text), Column("quantity", Numeric(18, 3)), Column("rate", Numeric(18, 2)),
    Column("total", Numeric(18, 2)), Column("advance", Numeric(18, 2)), Column("remaining", Numeric(18, 2)),
    Column("priority", Text), Column("remarks", Text), Column("employee", Text), Column("provenance", JSONB),
    Column("reason", Text), Column("created_by", UUID(as_uuid=True)), Column("created_at", TIMESTAMP(timezone=True)),
    Column("payment_status", Text), Column("production_status", Text), Column("delivery_status", Text),
    Column("extra", JSONB), Column("attention", JSONB),
)  # fmt: skip

order_email = Table(
    "order_email", metadata, _id(), _tenant(),
    Column("order_id", UUID(as_uuid=True)), Column("revision_number", Integer), Column("to_email", Text),
    Column("channel", Text), Column("attachment_name", Text), Column("attachment_sha256", Text),
    Column("attachment_bytes", Integer), Column("state", Text), Column("http_status", Integer),
    Column("error_code", Text), Column("error_message", Text), Column("created_by", UUID(as_uuid=True)),
    Column("created_at", TIMESTAMP(timezone=True)), Column("finished_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

# --- Diary automation (migration 0011) -------------------------------------------------------

customer = Table(
    "customer", metadata, _id(), _tenant(),
    Column("name", Text), Column("name_key", Text), Column("mobile", Text), Column("customer_number", Text), *_ts(),
)  # fmt: skip

owner_report_settings = Table(
    "owner_report_settings", metadata,
    Column("tenant_id", UUID(as_uuid=True), primary_key=True),
    Column("owner_email", Text), Column("auto_send", Boolean), Column("company_name", Text),
    Column("emailjs_service_id", Text), Column("emailjs_template_id", Text), Column("emailjs_public_key", Text),
    Column("emailjs_private_key", LargeBinary), Column("emailjs_key_id", Text), Column("max_request_kb", Integer),
    Column("updated_by", UUID(as_uuid=True)), Column("updated_at", TIMESTAMP(timezone=True)),
    Column("version", Integer),
)  # fmt: skip

batch_report = Table(
    "batch_report", metadata, _id(), _tenant(),
    Column("batch_id", UUID(as_uuid=True)), Column("version", Integer), Column("trigger", Text),
    Column("email_owner", Boolean), Column("state", Text), Column("order_ids", ARRAY(UUID(as_uuid=True))),
    Column("record_ids", ARRAY(UUID(as_uuid=True))), Column("revision_ids", ARRAY(UUID(as_uuid=True))),
    Column("summary", JSONB), Column("file_key", Text), Column("file_name", Text), Column("sha256", Text),
    Column("bytes", Integer), Column("error_code", Text), Column("error_message", Text),
    Column("created_by", UUID(as_uuid=True)), Column("created_at", TIMESTAMP(timezone=True)),
    Column("ready_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

report_delivery = Table(
    "report_delivery", metadata, _id(), _tenant(),
    Column("report_id", UUID(as_uuid=True)), Column("to_email", Text), Column("trigger", Text), Column("state", Text),
    Column("attachment_sha256", Text), Column("http_status", Integer), Column("error_code", Text),
    Column("error_message", Text), Column("reconcile_note", Text), Column("created_by", UUID(as_uuid=True)),
    Column("created_at", TIMESTAMP(timezone=True)), Column("finished_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

# --- Daily production sheets (migration 0012) ------------------------------------------------

shift_report = Table(
    "shift_report", metadata, _id(), _tenant(),
    Column("department_id", UUID(as_uuid=True)), Column("report_date", Date), Column("state", Text),
    Column("date_confirmed", Boolean), Column("shifts", JSONB), Column("notes", JSONB),
    Column("approved_version", Integer), Column("approved_by", UUID(as_uuid=True)),
    Column("approved_at", TIMESTAMP(timezone=True)), Column("created_by", UUID(as_uuid=True)), *_ts(),
)  # fmt: skip

shift_report_value = Table(
    "shift_report_value", metadata, _id(), _tenant(),
    Column("report_id", UUID(as_uuid=True)), Column("section", Text), Column("metric", Text), Column("shift", Text),
    Column("value", Numeric(18, 4)), Column("raw", Text), Column("source", Text), Column("confidence", Numeric(5, 4)),
    Column("uncertain", Boolean), Column("note", Text), Column("evidence", JSONB),
    Column("updated_by", UUID(as_uuid=True)), Column("updated_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

shift_report_source = Table(
    "shift_report_source", metadata, _id(), _tenant(),
    Column("report_id", UUID(as_uuid=True)), Column("upload_id", UUID(as_uuid=True)),
    Column("batch_id", UUID(as_uuid=True)), Column("page_no", Integer), Column("reader", Text),
    Column("values_read", Integer), Column("conflicts", Integer), Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

shift_report_change = Table(
    "shift_report_change", metadata, _id(), _tenant(),
    Column("report_id", UUID(as_uuid=True)), Column("section", Text), Column("metric", Text), Column("shift", Text),
    Column("old_value", Numeric(18, 4)), Column("new_value", Numeric(18, 4)), Column("reason", Text),
    Column("actor_id", UUID(as_uuid=True)), Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

shift_report_target = Table(
    "shift_report_target", metadata,
    Column("tenant_id", UUID(as_uuid=True), primary_key=True), Column("section", Text, primary_key=True),
    Column("metric", Text, primary_key=True), Column("target", Numeric(18, 4)),
    Column("updated_by", UUID(as_uuid=True)), Column("updated_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

sheet_email = Table(
    "sheet_email", metadata, _id(), _tenant(),
    Column("report_id", UUID(as_uuid=True)), Column("report_version", Integer), Column("to_email", Text),
    Column("format", Text), Column("attachment_name", Text), Column("attachment_sha256", Text),
    Column("attachment_bytes", Integer), Column("state", Text), Column("http_status", Integer),
    Column("error_code", Text), Column("error_message", Text), Column("created_by", UUID(as_uuid=True)),
    Column("created_at", TIMESTAMP(timezone=True)), Column("finished_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

# --- Hourly production reading registers (migration 0013) ------------------------------------

pick_register = Table(
    "pick_register", metadata, _id(), _tenant(),
    Column("department_id", UUID(as_uuid=True)), Column("register_date", Date), Column("state", Text),
    Column("date_confirmed", Boolean), Column("notes", JSONB), Column("approved_version", Integer),
    Column("approved_by", UUID(as_uuid=True)), Column("approved_at", TIMESTAMP(timezone=True)),
    Column("created_by", UUID(as_uuid=True)), *_ts(),
)  # fmt: skip

pick_register_value = Table(
    "pick_register_value", metadata, _id(), _tenant(),
    Column("register_id", UUID(as_uuid=True)), Column("shift", Text), Column("machine", Text), Column("slot", Integer),
    Column("reading", Numeric(18, 4)), Column("picks", Numeric(18, 4)), Column("status", Text), Column("raw", Text),
    Column("source", Text), Column("confidence", Numeric(5, 4)), Column("uncertain", Boolean), Column("note", Text),
    Column("accepted", Text), Column("evidence", JSONB), Column("updated_by", UUID(as_uuid=True)),
    Column("updated_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

pick_register_total = Table(
    "pick_register_total", metadata, _id(), _tenant(),
    Column("register_id", UUID(as_uuid=True)), Column("shift", Text), Column("slot", Integer),
    Column("written", Numeric(18, 4)), Column("raw", Text), Column("source", Text), Column("evidence", JSONB),
    Column("accepted", Text),
    Column("updated_by", UUID(as_uuid=True)), Column("updated_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

pick_register_source = Table(
    "pick_register_source", metadata, _id(), _tenant(),
    Column("register_id", UUID(as_uuid=True)), Column("upload_id", UUID(as_uuid=True)),
    Column("batch_id", UUID(as_uuid=True)), Column("page_no", Integer), Column("shift", Text), Column("reader", Text),
    Column("values_read", Integer), Column("conflicts", Integer), Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

pick_register_change = Table(
    "pick_register_change", metadata, _id(), _tenant(),
    Column("register_id", UUID(as_uuid=True)), Column("shift", Text), Column("machine", Text), Column("slot", Integer),
    Column("field", Text), Column("old_value", Text), Column("new_value", Text), Column("reason", Text),
    Column("actor_id", UUID(as_uuid=True)), Column("created_at", TIMESTAMP(timezone=True)),
)  # fmt: skip

register_email = Table(
    "register_email", metadata, _id(), _tenant(),
    Column("register_id", UUID(as_uuid=True)), Column("register_version", Integer), Column("to_email", Text),
    Column("format", Text), Column("attachment_name", Text), Column("attachment_sha256", Text),
    Column("attachment_bytes", Integer), Column("state", Text), Column("http_status", Integer),
    Column("error_code", Text), Column("error_message", Text), Column("created_by", UUID(as_uuid=True)),
    Column("created_at", TIMESTAMP(timezone=True)), Column("finished_at", TIMESTAMP(timezone=True)),
)  # fmt: skip
