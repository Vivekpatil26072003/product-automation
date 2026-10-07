"""M6 reports and email: immutable report snapshots, versioned PDFs, email drafts and the send-intent ledger.

- report: one version of a report series. The snapshot (filter, scope, data version, metrics, facts) is written
  in the request transaction and can never change; the PDF identity (file key, checksum) is set once.
  A later correction never edits a report: it marks it outdated and a new version is generated.
- report_item: the exact approved revision of every record in the snapshot (append-only).
- email_draft: editable until confirmed; once queued for sending it is immutable.
- email_message: the send intent, one per confirmed draft (UNIQUE draft_id and intent_key), so a double click
  or a replayed request can never send twice. Provider acceptance is ACCEPTED, never "delivered".
- email_attempt: append-only attempt history (one allowed IN_FLIGHT -> outcome transition).
- email_recipient_status: per-recipient state with the source and time of each observation.

Revision ID: 0006
"""

import os

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("APP_DB_ROLE", "prod_app")

UPGRADE = """
CREATE TABLE report (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  series_id uuid NOT NULL,
  version integer NOT NULL CHECK (version >= 1),
  supersedes_id uuid,
  title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
  filter_json jsonb NOT NULL,
  date_from date NOT NULL,
  date_to date NOT NULL,
  department_ids uuid[] NOT NULL,
  timezone text NOT NULL,
  data_version bigint NOT NULL,
  record_count integer NOT NULL CHECK (record_count >= 0),
  include_detail boolean NOT NULL,
  is_empty boolean NOT NULL,
  metrics_json jsonb NOT NULL,
  facts_json jsonb NOT NULL,
  template_version text NOT NULL,
  narrative_json jsonb,
  narrative_source text CHECK (narrative_source IN ('TEMPLATE', 'AI', 'TEMPLATE_FALLBACK')),
  narrative_model text,
  narrative_prompt_hash text,
  narrative_fallback_reason text,
  state text NOT NULL DEFAULT 'QUEUED' CHECK (state IN ('QUEUED', 'GENERATING', 'READY', 'FAILED')),
  file_key text,
  sha256 text,
  bytes bigint,
  error_code text,
  error_message text,
  outdated_at timestamptz,
  outdated_reason text,
  created_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  ready_at timestamptz,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, series_id, version),
  FOREIGN KEY (tenant_id, supersedes_id) REFERENCES report (tenant_id, id),
  CHECK (date_from <= date_to),
  CHECK (state <> 'READY' OR (file_key IS NOT NULL AND sha256 IS NOT NULL))
);
CREATE INDEX report_list ON report (tenant_id, created_at DESC);
CREATE INDEX report_current ON report (tenant_id, date_from, date_to) WHERE outdated_at IS NULL;

CREATE FUNCTION report_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.series_id, NEW.version, NEW.supersedes_id, NEW.title, NEW.filter_json, NEW.date_from, NEW.date_to,
      NEW.department_ids, NEW.timezone, NEW.data_version, NEW.record_count, NEW.include_detail, NEW.is_empty,
      NEW.metrics_json, NEW.facts_json, NEW.template_version, NEW.created_by, NEW.created_at)
     IS DISTINCT FROM
     (OLD.series_id, OLD.version, OLD.supersedes_id, OLD.title, OLD.filter_json, OLD.date_from, OLD.date_to,
      OLD.department_ids, OLD.timezone, OLD.data_version, OLD.record_count, OLD.include_detail, OLD.is_empty,
      OLD.metrics_json, OLD.facts_json, OLD.template_version, OLD.created_by, OLD.created_at) THEN
    RAISE EXCEPTION 'report snapshot is immutable' USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.sha256 IS NOT NULL AND (NEW.sha256, NEW.file_key, NEW.bytes, NEW.narrative_json)
                                IS DISTINCT FROM (OLD.sha256, OLD.file_key, OLD.bytes, OLD.narrative_json) THEN
    RAISE EXCEPTION 'report file is immutable once rendered' USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.outdated_at IS NOT NULL AND NEW.outdated_at IS DISTINCT FROM OLD.outdated_at THEN
    RAISE EXCEPTION 'an outdated report cannot become current again' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER report_immutable BEFORE UPDATE ON report FOR EACH ROW EXECUTE FUNCTION report_immutable();

CREATE TABLE report_item (
  tenant_id uuid NOT NULL,
  report_id uuid NOT NULL,
  record_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  revision_number integer NOT NULL,
  position integer NOT NULL,
  fields jsonb NOT NULL,
  PRIMARY KEY (report_id, record_id),
  FOREIGN KEY (tenant_id, report_id) REFERENCES report (tenant_id, id),
  FOREIGN KEY (tenant_id, record_id) REFERENCES production_record (tenant_id, id)
);
CREATE INDEX report_item_record ON report_item (record_id);

ALTER TABLE export ADD COLUMN report_id uuid;
ALTER TABLE export ADD FOREIGN KEY (tenant_id, report_id) REFERENCES report (tenant_id, id);

CREATE TABLE email_draft (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  report_id uuid NOT NULL,
  subject text NOT NULL CHECK (char_length(subject) <= 200 AND subject !~ '[\\r\\n]'),
  body text NOT NULL CHECK (char_length(body) <= 20000),
  recipients jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_array_length(recipients) <= 50),
  state text NOT NULL DEFAULT 'DRAFT' CHECK (state IN ('DRAFT', 'QUEUED')),
  content_hash text NOT NULL,
  resend_of_email_id uuid,
  resend_reason text,
  correction_of_email_id uuid,
  created_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, report_id) REFERENCES report (tenant_id, id)
);
CREATE FUNCTION email_draft_locked() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state = 'QUEUED' THEN
    RAISE EXCEPTION 'a confirmed email draft is immutable' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER email_draft_locked BEFORE UPDATE ON email_draft FOR EACH ROW EXECUTE FUNCTION email_draft_locked();

CREATE TABLE email_message (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  draft_id uuid NOT NULL UNIQUE,
  draft_version integer NOT NULL,
  content_hash text NOT NULL,
  report_id uuid NOT NULL,
  intent_key text NOT NULL,
  state text NOT NULL DEFAULT 'QUEUED' CHECK (state IN ('QUEUED', 'SENDING', 'ACCEPTED', 'UNKNOWN', 'FAILED')),
  connection_id uuid NOT NULL,
  sender_mailbox text NOT NULL,
  subject text NOT NULL,
  recipients jsonb NOT NULL,
  attachment_name text NOT NULL,
  attachment_sha256 text NOT NULL,
  attachment_bytes bigint NOT NULL,
  provider_request_id text,
  error_code text,
  error_message text,
  created_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  accepted_at timestamptz,
  finished_at timestamptz,
  reconciled_by uuid,
  reconciled_at timestamptz,
  reconcile_outcome text CHECK (reconcile_outcome IN ('ACCEPTED', 'NOT_ACCEPTED')),
  reconcile_evidence text,
  reconcile_reason text,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, intent_key),
  FOREIGN KEY (tenant_id, draft_id) REFERENCES email_draft (tenant_id, id),
  FOREIGN KEY (tenant_id, report_id) REFERENCES report (tenant_id, id),
  FOREIGN KEY (tenant_id, connection_id) REFERENCES integration_connection (tenant_id, id)
);
CREATE INDEX email_message_list ON email_message (tenant_id, created_at DESC);
CREATE INDEX email_message_provider ON email_message (provider_request_id);

-- Allowed state changes. ACCEPTED is terminal; UNKNOWN leaves only through a recorded reconciliation.
CREATE FUNCTION email_message_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.draft_id, NEW.draft_version, NEW.content_hash, NEW.report_id, NEW.intent_key, NEW.recipients,
      NEW.subject, NEW.attachment_sha256, NEW.created_by)
     IS DISTINCT FROM (OLD.draft_id, OLD.draft_version, OLD.content_hash, OLD.report_id, OLD.intent_key,
      OLD.recipients, OLD.subject, OLD.attachment_sha256, OLD.created_by) THEN
    RAISE EXCEPTION 'send intent is immutable' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.state = OLD.state THEN
    RETURN NEW;
  END IF;
  IF NOT (
       (OLD.state = 'QUEUED'  AND NEW.state IN ('SENDING', 'FAILED'))
    OR (OLD.state = 'SENDING' AND NEW.state IN ('QUEUED', 'ACCEPTED', 'UNKNOWN', 'FAILED'))
    OR (OLD.state = 'UNKNOWN' AND NEW.state IN ('ACCEPTED', 'FAILED') AND NEW.reconciled_at IS NOT NULL)
  ) THEN
    RAISE EXCEPTION 'email state % -> % is not allowed', OLD.state, NEW.state USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER email_message_transition BEFORE UPDATE ON email_message
  FOR EACH ROW EXECUTE FUNCTION email_message_transition();

CREATE TABLE email_attempt (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  email_id uuid NOT NULL,
  attempt_no integer NOT NULL,
  outcome text NOT NULL DEFAULT 'IN_FLIGHT'
    CHECK (outcome IN ('IN_FLIGHT', 'ACCEPTED', 'RETRY', 'UNKNOWN', 'FAILED')),
  http_status integer,
  provider_request_id text,
  error_code text,
  error_message text,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (email_id, attempt_no),
  FOREIGN KEY (tenant_id, email_id) REFERENCES email_message (tenant_id, id)
);
CREATE FUNCTION email_attempt_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.outcome <> 'IN_FLIGHT' OR (NEW.email_id, NEW.attempt_no, NEW.started_at)
     IS DISTINCT FROM (OLD.email_id, OLD.attempt_no, OLD.started_at) THEN
    RAISE EXCEPTION 'email attempts are append-only' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER email_attempt_once BEFORE UPDATE ON email_attempt FOR EACH ROW EXECUTE FUNCTION email_attempt_once();

CREATE TABLE email_recipient_status (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  email_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('TO', 'CC', 'BCC')),
  address text NOT NULL,
  state text NOT NULL DEFAULT 'PENDING'
    CHECK (state IN ('PENDING', 'ACCEPTED', 'UNKNOWN', 'FAILED', 'DELIVERED', 'BOUNCED')),
  observation_source text,
  observed_at timestamptz,
  UNIQUE (email_id, address),
  FOREIGN KEY (tenant_id, email_id) REFERENCES email_message (tenant_id, id),
  -- Delivery and bounce need evidence from a delivery source; the send response alone never supplies it.
  CHECK (state NOT IN ('DELIVERED', 'BOUNCED') OR observation_source IN ('delivery_report', 'bounce_report'))
);
"""

TENANT_TABLES = ("report", "report_item", "email_draft", "email_message", "email_attempt", "email_recipient_status")


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(
        "CREATE TRIGGER email_draft_updated_at BEFORE UPDATE ON email_draft "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
    )
    for t in TENANT_TABLES:
        op.execute(
            f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY; ALTER TABLE {t} FORCE ROW LEVEL SECURITY;"
            f"CREATE POLICY tenant_isolation ON {t} USING (tenant_id = app_current_tenant());"
        )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON report, email_draft, email_message, email_recipient_status TO {APP_ROLE};"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON email_attempt TO {APP_ROLE};")  # trigger: one outcome update
    op.execute(f"GRANT SELECT, INSERT ON report_item TO {APP_ROLE};")  # append-only snapshot rows


def downgrade() -> None:
    op.execute(
        "ALTER TABLE export DROP COLUMN IF EXISTS report_id;"
        "DROP TABLE IF EXISTS email_recipient_status, email_attempt, email_message, email_draft, report_item, report"
        " CASCADE;"
        "DROP FUNCTION IF EXISTS email_attempt_once, email_message_transition, email_draft_locked, report_immutable;"
    )
