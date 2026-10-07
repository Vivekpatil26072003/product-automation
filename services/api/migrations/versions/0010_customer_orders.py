"""Customer orders: handwritten/typed order notes -> reviewed draft -> approved order with revisions -> PDF -> email.

Production records are unchanged. Orders live beside them:
- order_draft: the review form for one order read from an uploaded page (or entered manually for it). Editable
  while NEEDS_REVIEW; approving it creates exactly one customer_order (UNIQUE draft_id), so a double click or a
  replay can never create a duplicate order.
- customer_order: the order identity; current_revision_id points at the latest saved values.
- order_revision: append-only saved values (revision 1 on approval, one more per correction with a reason).
- order_email: every EmailJS send of an order PDF (recipient, revision, attachment checksum, outcome, time).

Revision ID: 0010
"""

import os

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("APP_DB_ROLE", "prod_app")

UPGRADE = """
CREATE TABLE order_draft (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  batch_id uuid NOT NULL REFERENCES batch(id),
  upload_id uuid NOT NULL REFERENCES upload(id),
  department_id uuid NOT NULL REFERENCES department(id),
  source text NOT NULL CHECK (source IN ('extracted', 'manual')),
  extraction_id uuid REFERENCES extraction(id),
  page_no integer NOT NULL DEFAULT 1 CHECK (page_no >= 1),
  fields jsonb NOT NULL,
  issues jsonb NOT NULL DEFAULT '[]'::jsonb,
  state text NOT NULL DEFAULT 'NEEDS_REVIEW' CHECK (state IN ('NEEDS_REVIEW', 'APPROVED', 'REJECTED', 'SUPERSEDED')),
  order_id uuid,
  reject_reason text CHECK (reject_reason IS NULL OR char_length(reject_reason) BETWEEN 5 AND 500),
  decided_by uuid,
  decided_at timestamptz,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  UNIQUE (tenant_id, id),
  CHECK ((state = 'APPROVED') = (order_id IS NOT NULL))
);
CREATE INDEX order_draft_batch ON order_draft (tenant_id, batch_id, state);
CREATE INDEX order_draft_upload ON order_draft (upload_id, state);

CREATE TABLE customer_order (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  department_id uuid NOT NULL REFERENCES department(id),
  draft_id uuid NOT NULL UNIQUE,
  order_ref text NOT NULL CHECK (char_length(order_ref) BETWEEN 1 AND 60),
  current_revision_id uuid NOT NULL,
  current_number integer NOT NULL DEFAULT 1 CHECK (current_number >= 1),
  state text NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE', 'ARCHIVED')),
  created_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, draft_id) REFERENCES order_draft (tenant_id, id)
);
CREATE INDEX customer_order_list ON customer_order (tenant_id, updated_at DESC, id DESC);

CREATE TABLE order_revision (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  order_id uuid NOT NULL,
  number integer NOT NULL CHECK (number >= 1),
  customer_name text NOT NULL CHECK (char_length(btrim(customer_name)) BETWEEN 1 AND 200),
  customer_number text CHECK (char_length(customer_number) <= 60),
  customer_email text CHECK (char_length(customer_email) <= 254),
  mobile text CHECK (char_length(mobile) <= 20),
  order_number text CHECK (char_length(order_number) <= 60),
  order_date date NOT NULL,
  delivery_date date,
  package text CHECK (char_length(package) <= 200),
  size text CHECK (char_length(size) <= 200),
  material text CHECK (char_length(material) <= 200),
  quantity numeric(18,3) NOT NULL CHECK (quantity >= 0),
  rate numeric(18,2) CHECK (rate >= 0),
  total numeric(18,2) CHECK (total >= 0),
  advance numeric(18,2) CHECK (advance >= 0),
  remaining numeric(18,2),
  priority text CHECK (char_length(priority) <= 60),
  remarks text NOT NULL DEFAULT '' CHECK (char_length(remarks) <= 2000),
  employee text CHECK (char_length(employee) <= 120),
  provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
  reason text CHECK (reason IS NULL OR char_length(reason) BETWEEN 5 AND 500),
  created_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (order_id, number),
  FOREIGN KEY (tenant_id, order_id) REFERENCES customer_order (tenant_id, id),
  CHECK (number = 1 OR reason IS NOT NULL)
);

CREATE TABLE order_email (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  order_id uuid NOT NULL,
  revision_number integer NOT NULL,
  to_email text NOT NULL CHECK (char_length(to_email) BETWEEN 3 AND 254),
  channel text NOT NULL DEFAULT 'emailjs' CHECK (channel IN ('emailjs')),
  attachment_name text NOT NULL,
  attachment_sha256 text NOT NULL,
  attachment_bytes integer NOT NULL CHECK (attachment_bytes > 0),
  state text NOT NULL DEFAULT 'SENDING' CHECK (state IN ('SENDING', 'ACCEPTED', 'FAILED', 'UNKNOWN')),
  http_status integer,
  error_code text,
  error_message text,
  created_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, order_id) REFERENCES customer_order (tenant_id, id),
  CHECK ((state = 'SENDING') = (finished_at IS NULL))
);
CREATE INDEX order_email_order ON order_email (order_id, created_at DESC);

-- A finished send is history: only SENDING may change, and only once.
CREATE FUNCTION order_email_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state <> 'SENDING' AND NOT (OLD.state = 'UNKNOWN' AND NEW.state IN ('ACCEPTED', 'FAILED')) THEN
    RAISE EXCEPTION 'order_email % is final', OLD.id USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.order_id, NEW.revision_number, NEW.to_email, NEW.attachment_sha256, NEW.created_by, NEW.created_at)
     IS DISTINCT FROM
     (OLD.order_id, OLD.revision_number, OLD.to_email, OLD.attachment_sha256, OLD.created_by, OLD.created_at) THEN
    RAISE EXCEPTION 'order_email identity is immutable' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER order_email_once BEFORE UPDATE ON order_email FOR EACH ROW EXECUTE FUNCTION order_email_once();
"""

TENANT_TABLES = ("order_draft", "customer_order", "order_revision", "order_email")


def upgrade() -> None:
    op.execute(UPGRADE)
    for name in ("order_draft", "customer_order"):
        op.execute(
            f"CREATE TRIGGER {name}_updated_at BEFORE UPDATE ON {name} FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
        )
    for t in TENANT_TABLES:
        op.execute(
            f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY; ALTER TABLE {t} FORCE ROW LEVEL SECURITY;"
            f"CREATE POLICY tenant_isolation ON {t} USING (tenant_id = app_current_tenant());"
        )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON order_draft, customer_order, order_email TO {APP_ROLE};")
    op.execute(f"GRANT SELECT, INSERT ON order_revision TO {APP_ROLE};")  # append-only saved values


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS order_email, order_revision, customer_order, order_draft CASCADE;"
        "DROP FUNCTION IF EXISTS order_email_once;"
    )
