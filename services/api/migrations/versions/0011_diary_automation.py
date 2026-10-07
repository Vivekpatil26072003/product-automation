"""Diary automation: customers, richer orders, owner report settings, batch reports and their email deliveries.

- customer: one row per real customer; orders link to it (matched by customer number, then mobile, then name).
- order_revision: payment / production / delivery status, `extra` (other information written on the page, kept
  rather than dropped) and `attention` (what was missing or uncertain when the values were saved).
- order_draft: `extra`, `reading` (which reader produced it: deterministic, OCR, AI model + prompt hash) and
  `decision` (save as a new order, or update an existing order: never a silent duplicate).
- owner_report_settings: owner email, automatic report on/off, company name, EmailJS identifiers; the EmailJS
  private key is stored encrypted (INTEGRATION_KEYS) and never returned by the API.
- batch_report: a consolidated PDF of the orders and production entries approved from one upload batch.
  Its content list is fixed when it is created; a later approval creates a new version.
- report_delivery: every email of a batch report (to the owner), with at most one send in flight per report.
- order_email: now also sent by the server (EmailJS REST API from the worker): QUEUED state, channel emailjs_api,
  at most one send in flight per order and recipient.
- notification: kind REPORT (report emailed / failed).

Revision ID: 0011
"""

import os

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("APP_DB_ROLE", "prod_app")

UPGRADE = """
CREATE TABLE customer (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  name text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 200),
  name_key text NOT NULL,
  mobile text CHECK (char_length(mobile) <= 20),
  customer_number text CHECK (char_length(customer_number) <= 60),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  UNIQUE (tenant_id, id)
);
CREATE INDEX customer_name ON customer (tenant_id, name_key);
CREATE INDEX customer_mobile ON customer (tenant_id, mobile) WHERE mobile IS NOT NULL;
CREATE INDEX customer_number_idx ON customer (tenant_id, customer_number) WHERE customer_number IS NOT NULL;

ALTER TABLE customer_order ADD COLUMN customer_id uuid;
ALTER TABLE customer_order ADD CONSTRAINT customer_order_customer
  FOREIGN KEY (tenant_id, customer_id) REFERENCES customer (tenant_id, id);
CREATE INDEX customer_order_customer_idx ON customer_order (tenant_id, customer_id);

ALTER TABLE order_revision
  ADD COLUMN payment_status text CHECK (char_length(payment_status) <= 60),
  ADD COLUMN production_status text CHECK (char_length(production_status) <= 60),
  ADD COLUMN delivery_status text CHECK (char_length(delivery_status) <= 60),
  ADD COLUMN extra jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN attention jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE order_draft
  ADD COLUMN extra jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN reading jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN decision jsonb;

CREATE TABLE owner_report_settings (
  tenant_id uuid PRIMARY KEY REFERENCES tenant(id),
  owner_email text CHECK (owner_email IS NULL OR char_length(owner_email) BETWEEN 3 AND 254),
  auto_send boolean NOT NULL DEFAULT false,
  company_name text CHECK (company_name IS NULL OR char_length(company_name) BETWEEN 1 AND 200),
  emailjs_service_id text CHECK (char_length(emailjs_service_id) <= 100),
  emailjs_template_id text CHECK (char_length(emailjs_template_id) <= 100),
  emailjs_public_key text CHECK (char_length(emailjs_public_key) <= 200),
  emailjs_private_key bytea,
  emailjs_key_id text,
  max_request_kb integer NOT NULL DEFAULT 50 CHECK (max_request_kb BETWEEN 10 AND 30000),
  updated_by uuid,
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  CHECK ((emailjs_private_key IS NULL) = (emailjs_key_id IS NULL))
);

CREATE TABLE batch_report (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  batch_id uuid NOT NULL REFERENCES batch(id),
  version integer NOT NULL CHECK (version >= 1),
  trigger text NOT NULL CHECK (trigger IN ('AUTO', 'MANUAL')),
  email_owner boolean NOT NULL DEFAULT false,
  state text NOT NULL DEFAULT 'QUEUED' CHECK (state IN ('QUEUED', 'GENERATING', 'READY', 'FAILED')),
  order_ids uuid[] NOT NULL DEFAULT '{}',
  record_ids uuid[] NOT NULL DEFAULT '{}',
  revision_ids uuid[] NOT NULL DEFAULT '{}',
  summary jsonb NOT NULL DEFAULT '{}'::jsonb,
  file_key text,
  file_name text,
  sha256 text,
  bytes integer,
  error_code text,
  error_message text,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  ready_at timestamptz,
  UNIQUE (tenant_id, id),
  UNIQUE (batch_id, version),
  CHECK (cardinality(order_ids) + cardinality(record_ids) >= 1),
  CHECK (state <> 'READY' OR (file_key IS NOT NULL AND sha256 IS NOT NULL))
);
CREATE INDEX batch_report_batch ON batch_report (tenant_id, batch_id, version DESC);

CREATE TABLE report_delivery (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  report_id uuid NOT NULL,
  to_email text NOT NULL CHECK (char_length(to_email) BETWEEN 3 AND 254),
  trigger text NOT NULL CHECK (trigger IN ('AUTO', 'MANUAL', 'RESEND')),
  state text NOT NULL DEFAULT 'QUEUED' CHECK (state IN ('QUEUED', 'SENDING', 'ACCEPTED', 'FAILED', 'UNKNOWN')),
  attachment_sha256 text,
  http_status integer,
  error_code text,
  error_message text,
  reconcile_note text,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, report_id) REFERENCES batch_report (tenant_id, id),
  CHECK ((state IN ('QUEUED', 'SENDING')) = (finished_at IS NULL))
);
-- A double click, a replay or an automatic and a manual request at once can never send the same report twice.
CREATE UNIQUE INDEX report_delivery_in_flight ON report_delivery (report_id) WHERE state IN ('QUEUED', 'SENDING');

CREATE FUNCTION report_delivery_final() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state IN ('ACCEPTED', 'FAILED') OR (OLD.state = 'UNKNOWN' AND NEW.state NOT IN ('ACCEPTED', 'FAILED')) THEN
    RAISE EXCEPTION 'report_delivery % is final', OLD.id USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.report_id, NEW.to_email, NEW.created_at) IS DISTINCT FROM (OLD.report_id, OLD.to_email, OLD.created_at) THEN
    RAISE EXCEPTION 'report_delivery identity is immutable' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER report_delivery_final BEFORE UPDATE ON report_delivery
  FOR EACH ROW EXECUTE FUNCTION report_delivery_final();

-- Order emails are sent by the worker now: QUEUED -> SENDING -> ACCEPTED / FAILED / UNKNOWN.
ALTER TABLE order_email DROP CONSTRAINT order_email_channel_check;
ALTER TABLE order_email ADD CONSTRAINT order_email_channel_check CHECK (channel IN ('emailjs', 'emailjs_api'));
ALTER TABLE order_email DROP CONSTRAINT order_email_state_check;
ALTER TABLE order_email ADD CONSTRAINT order_email_state_check
  CHECK (state IN ('QUEUED', 'SENDING', 'ACCEPTED', 'FAILED', 'UNKNOWN'));
ALTER TABLE order_email DROP CONSTRAINT order_email_check;
ALTER TABLE order_email ADD CONSTRAINT order_email_check
  CHECK ((state IN ('QUEUED', 'SENDING')) = (finished_at IS NULL));
CREATE UNIQUE INDEX order_email_in_flight ON order_email (order_id, lower(to_email))
  WHERE state IN ('QUEUED', 'SENDING');
CREATE OR REPLACE FUNCTION order_email_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state NOT IN ('QUEUED', 'SENDING') AND NOT (OLD.state = 'UNKNOWN' AND NEW.state IN ('ACCEPTED', 'FAILED')) THEN
    RAISE EXCEPTION 'order_email % is final', OLD.id USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.order_id, NEW.revision_number, NEW.to_email, NEW.attachment_sha256, NEW.created_by, NEW.created_at)
     IS DISTINCT FROM
     (OLD.order_id, OLD.revision_number, OLD.to_email, OLD.attachment_sha256, OLD.created_by, OLD.created_at) THEN
    RAISE EXCEPTION 'order_email identity is immutable' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

ALTER TABLE notification DROP CONSTRAINT notification_kind_check;
ALTER TABLE notification ADD CONSTRAINT notification_kind_check
  CHECK (kind IN ('REMINDER_FIRST', 'REMINDER_SECOND', 'ESCALATION', 'EXCEPTION', 'SCHEDULE', 'REPORT'));
"""

TENANT_TABLES = ("customer", "owner_report_settings", "batch_report", "report_delivery")


def upgrade() -> None:
    op.execute(UPGRADE)
    for name in ("customer", "owner_report_settings"):
        op.execute(
            f"CREATE TRIGGER {name}_updated_at BEFORE UPDATE ON {name} FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
        )
    for t in TENANT_TABLES:
        op.execute(
            f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY; ALTER TABLE {t} FORCE ROW LEVEL SECURITY;"
            f"CREATE POLICY tenant_isolation ON {t} USING (tenant_id = app_current_tenant());"
        )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON customer, owner_report_settings, batch_report, report_delivery TO {APP_ROLE};"
    )


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS report_delivery, batch_report, owner_report_settings CASCADE;"
        "DROP FUNCTION IF EXISTS report_delivery_final;"
        "DROP INDEX IF EXISTS order_email_in_flight;"
        "ALTER TABLE order_draft DROP COLUMN IF EXISTS extra, DROP COLUMN IF EXISTS reading,"
        " DROP COLUMN IF EXISTS decision;"
        "ALTER TABLE order_revision DROP COLUMN IF EXISTS payment_status, DROP COLUMN IF EXISTS production_status,"
        " DROP COLUMN IF EXISTS delivery_status, DROP COLUMN IF EXISTS extra, DROP COLUMN IF EXISTS attention;"
        "ALTER TABLE customer_order DROP COLUMN IF EXISTS customer_id;"
        "DROP TABLE IF EXISTS customer CASCADE;"
    )
