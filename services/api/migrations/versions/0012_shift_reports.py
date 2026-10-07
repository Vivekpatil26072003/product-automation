"""Daily production sheets (the Sulzer report): one sheet per department and day, values per shift.

- shift_report: one row per (department, report date). DRAFT while values are being read and checked;
  APPROVED when a Reviewer saves it. Shift supervisors (I, II, III) and free-text notes (machine numbers, etc.).
- shift_report_value: one value per (section, metric, shift). Shift is I, II, III, or D for day-level rows.
  Only written values are stored: totals, averages, to-date figures and the efficiency formulas are calculated.
  Each value keeps how it was entered (read from a page, AI, reviewer) and the source line it came from.
- shift_report_source: which uploaded pages contributed to a sheet.
- shift_report_change: append-only history of every change made after a value was first saved.
- shift_report_target: targets per metric (administrators), defaults from the company's current sheet.
- sheet_email: every email of a sheet file (xlsx / pdf / csv), one in flight per sheet and address.

Revision ID: 0012
"""

import os

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("APP_DB_ROLE", "prod_app")

UPGRADE = """
CREATE TABLE shift_report (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  department_id uuid NOT NULL REFERENCES department(id),
  report_date date NOT NULL,
  state text NOT NULL DEFAULT 'DRAFT' CHECK (state IN ('DRAFT', 'APPROVED')),
  date_confirmed boolean NOT NULL DEFAULT false,
  shifts jsonb NOT NULL DEFAULT '{}'::jsonb,
  notes jsonb NOT NULL DEFAULT '[]'::jsonb,
  approved_version integer NOT NULL DEFAULT 0,
  approved_by uuid,
  approved_at timestamptz,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, department_id, report_date)
);
CREATE INDEX shift_report_list ON shift_report (tenant_id, report_date DESC);

CREATE TABLE shift_report_value (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  report_id uuid NOT NULL,
  section text NOT NULL CHECK (char_length(section) <= 60),
  metric text NOT NULL CHECK (char_length(metric) <= 60),
  shift text NOT NULL CHECK (shift IN ('I', 'II', 'III', 'D')),
  value numeric(18,4),
  raw text CHECK (char_length(raw) <= 200),
  source text NOT NULL CHECK (source IN ('read', 'ai', 'reviewer', 'manual')),
  confidence numeric(5,4),
  uncertain boolean NOT NULL DEFAULT false,
  note text CHECK (char_length(note) <= 300),
  evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
  updated_by uuid,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (report_id, section, metric, shift),
  FOREIGN KEY (tenant_id, report_id) REFERENCES shift_report (tenant_id, id)
);

CREATE TABLE shift_report_source (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  report_id uuid NOT NULL,
  upload_id uuid NOT NULL REFERENCES upload(id),
  batch_id uuid NOT NULL REFERENCES batch(id),
  page_no integer NOT NULL,
  reader text NOT NULL,
  values_read integer NOT NULL DEFAULT 0,
  conflicts integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (report_id, upload_id, page_no),
  FOREIGN KEY (tenant_id, report_id) REFERENCES shift_report (tenant_id, id)
);
CREATE INDEX shift_report_source_batch ON shift_report_source (batch_id);

CREATE TABLE shift_report_change (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  report_id uuid NOT NULL,
  section text NOT NULL,
  metric text NOT NULL,
  shift text NOT NULL,
  old_value numeric(18,4),
  new_value numeric(18,4),
  reason text CHECK (reason IS NULL OR char_length(reason) <= 500),
  actor_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, report_id) REFERENCES shift_report (tenant_id, id)
);
CREATE INDEX shift_report_change_report ON shift_report_change (report_id, created_at DESC);

CREATE TABLE shift_report_target (
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  section text NOT NULL,
  metric text NOT NULL,
  target numeric(18,4),
  updated_by uuid,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, section, metric)
);

CREATE TABLE sheet_email (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  report_id uuid NOT NULL,
  report_version integer NOT NULL,
  to_email text NOT NULL CHECK (char_length(to_email) BETWEEN 3 AND 254),
  format text NOT NULL CHECK (format IN ('xlsx', 'pdf', 'csv')),
  attachment_name text NOT NULL,
  attachment_sha256 text NOT NULL,
  attachment_bytes integer NOT NULL CHECK (attachment_bytes > 0),
  state text NOT NULL DEFAULT 'QUEUED' CHECK (state IN ('QUEUED', 'SENDING', 'ACCEPTED', 'FAILED', 'UNKNOWN')),
  http_status integer,
  error_code text,
  error_message text,
  created_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, report_id) REFERENCES shift_report (tenant_id, id),
  CHECK ((state IN ('QUEUED', 'SENDING')) = (finished_at IS NULL))
);
CREATE UNIQUE INDEX sheet_email_in_flight ON sheet_email (report_id, lower(to_email), format)
  WHERE state IN ('QUEUED', 'SENDING');

CREATE FUNCTION sheet_email_final() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state IN ('ACCEPTED', 'FAILED') OR (OLD.state = 'UNKNOWN' AND NEW.state NOT IN ('ACCEPTED', 'FAILED')) THEN
    RAISE EXCEPTION 'sheet_email % is final', OLD.id USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.report_id, NEW.to_email, NEW.attachment_sha256, NEW.created_at)
     IS DISTINCT FROM (OLD.report_id, OLD.to_email, OLD.attachment_sha256, OLD.created_at) THEN
    RAISE EXCEPTION 'sheet_email identity is immutable' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER sheet_email_final BEFORE UPDATE ON sheet_email FOR EACH ROW EXECUTE FUNCTION sheet_email_final();
"""

TENANT_TABLES = (
    "shift_report",
    "shift_report_value",
    "shift_report_source",
    "shift_report_change",
    "shift_report_target",
    "sheet_email",
)


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(
        "CREATE TRIGGER shift_report_updated_at BEFORE UPDATE ON shift_report "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
    )
    for t in TENANT_TABLES:
        op.execute(
            f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY; ALTER TABLE {t} FORCE ROW LEVEL SECURITY;"
            f"CREATE POLICY tenant_isolation ON {t} USING (tenant_id = app_current_tenant());"
        )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON shift_report, shift_report_value, shift_report_source, "
        f"shift_report_target, sheet_email TO {APP_ROLE};"
    )
    op.execute(f"GRANT SELECT, INSERT ON shift_report_change TO {APP_ROLE};")  # append-only history


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS sheet_email, shift_report_target, shift_report_change, shift_report_source, "
        "shift_report_value, shift_report CASCADE;"
        "DROP FUNCTION IF EXISTS sheet_email_final;"
    )
