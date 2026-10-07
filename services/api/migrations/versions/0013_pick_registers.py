"""Hourly production reading registers (WGS-02, pick reading): one register per department and day.

Each shift page has a column of machine numbers and five time columns (shift I 08-16, II 16-24, III 00-08):
the first column is the meter reading at the start of the shift (carried from the previous shift), each later
column has the meter reading and, written under it, the picks of those two hours (reading minus previous reading).
The bottom row has the worker's column totals (sum of the picks); a figure under the first column is the shift /
day total.

- pick_register: one row per (department, register date). DRAFT while values are read and checked; APPROVED when a
  Reviewer saves it. Free-text notes (checks that do not point at one cell).
- pick_register_value: one row per (shift, machine, time slot); slot 0 = start reading, 1..4 = the two-hour columns.
  reading, picks (the small written number) and status (B.fall, S/C, ... as written) are stored as written;
  machine totals, column totals, stopped machines, shift and day totals are calculated. The arithmetic checks
  (picks = reading - previous reading, column totals, shift-to-shift readings) are calculated when the register
  is shown; "accepted" keeps the exact checks a person looked at and accepted for this cell.
- pick_register_total: the totals the worker wrote (slot 1..4 = column total, slot 0 = shift / day total).
- pick_register_source: which uploaded pages went into a register.
- pick_register_change: append-only history of every change after a value was first saved.
- register_email: every email of a register file (xlsx / pdf / csv / sql), one in flight per register, address, format.

Revision ID: 0013
"""

import os

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("APP_DB_ROLE", "prod_app")

UPGRADE = """
CREATE TABLE pick_register (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  department_id uuid NOT NULL REFERENCES department(id),
  register_date date NOT NULL,
  state text NOT NULL DEFAULT 'DRAFT' CHECK (state IN ('DRAFT', 'APPROVED')),
  date_confirmed boolean NOT NULL DEFAULT false,
  notes jsonb NOT NULL DEFAULT '[]'::jsonb,
  approved_version integer NOT NULL DEFAULT 0,
  approved_by uuid,
  approved_at timestamptz,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, department_id, register_date)
);
CREATE INDEX pick_register_list ON pick_register (tenant_id, register_date DESC);

CREATE TABLE pick_register_value (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  register_id uuid NOT NULL,
  shift text NOT NULL CHECK (shift IN ('I', 'II', 'III')),
  machine text NOT NULL CHECK (char_length(machine) BETWEEN 1 AND 20),
  slot smallint NOT NULL CHECK (slot BETWEEN 0 AND 4),
  reading numeric(18,4),
  picks numeric(18,4),
  status text CHECK (char_length(status) <= 40),
  raw text CHECK (char_length(raw) <= 200),
  source text NOT NULL CHECK (source IN ('read', 'ai', 'reviewer', 'manual')),
  confidence numeric(5,4),
  uncertain boolean NOT NULL DEFAULT false,
  note text CHECK (char_length(note) <= 300),
  accepted text CHECK (char_length(accepted) <= 600),
  evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
  updated_by uuid,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (register_id, shift, machine, slot),
  FOREIGN KEY (tenant_id, register_id) REFERENCES pick_register (tenant_id, id),
  CHECK (slot > 0 OR picks IS NULL)
);

CREATE TABLE pick_register_total (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  register_id uuid NOT NULL,
  shift text NOT NULL CHECK (shift IN ('I', 'II', 'III')),
  slot smallint NOT NULL CHECK (slot BETWEEN 0 AND 4),
  written numeric(18,4),
  raw text CHECK (char_length(raw) <= 200),
  source text NOT NULL CHECK (source IN ('read', 'ai', 'reviewer', 'manual')),
  evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
  updated_by uuid,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (register_id, shift, slot),
  FOREIGN KEY (tenant_id, register_id) REFERENCES pick_register (tenant_id, id)
);

CREATE TABLE pick_register_source (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  register_id uuid NOT NULL,
  upload_id uuid NOT NULL REFERENCES upload(id),
  batch_id uuid NOT NULL REFERENCES batch(id),
  page_no integer NOT NULL,
  shift text NOT NULL CHECK (shift IN ('I', 'II', 'III')),
  reader text NOT NULL,
  values_read integer NOT NULL DEFAULT 0,
  conflicts integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (register_id, upload_id, page_no),
  FOREIGN KEY (tenant_id, register_id) REFERENCES pick_register (tenant_id, id)
);
CREATE INDEX pick_register_source_batch ON pick_register_source (batch_id);

CREATE TABLE pick_register_change (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  register_id uuid NOT NULL,
  shift text NOT NULL,
  machine text NOT NULL,
  slot smallint NOT NULL,
  field text NOT NULL CHECK (field IN ('reading', 'picks', 'status', 'total')),
  old_value text CHECK (char_length(old_value) <= 60),
  new_value text CHECK (char_length(new_value) <= 60),
  reason text CHECK (reason IS NULL OR char_length(reason) <= 500),
  actor_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, register_id) REFERENCES pick_register (tenant_id, id)
);
CREATE INDEX pick_register_change_register ON pick_register_change (register_id, created_at DESC);

CREATE TABLE register_email (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  register_id uuid NOT NULL,
  register_version integer NOT NULL,
  to_email text NOT NULL CHECK (char_length(to_email) BETWEEN 3 AND 254),
  format text NOT NULL CHECK (format IN ('xlsx', 'pdf', 'csv', 'sql')),
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
  FOREIGN KEY (tenant_id, register_id) REFERENCES pick_register (tenant_id, id),
  CHECK ((state IN ('QUEUED', 'SENDING')) = (finished_at IS NULL))
);
CREATE UNIQUE INDEX register_email_in_flight ON register_email (register_id, lower(to_email), format)
  WHERE state IN ('QUEUED', 'SENDING');

CREATE FUNCTION register_email_final() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state IN ('ACCEPTED', 'FAILED') OR (OLD.state = 'UNKNOWN' AND NEW.state NOT IN ('ACCEPTED', 'FAILED')) THEN
    RAISE EXCEPTION 'register_email % is final', OLD.id USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.register_id, NEW.to_email, NEW.attachment_sha256, NEW.created_at)
     IS DISTINCT FROM (OLD.register_id, OLD.to_email, OLD.attachment_sha256, OLD.created_at) THEN
    RAISE EXCEPTION 'register_email identity is immutable' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER register_email_final BEFORE UPDATE ON register_email
  FOR EACH ROW EXECUTE FUNCTION register_email_final();
"""

TENANT_TABLES = (
    "pick_register",
    "pick_register_value",
    "pick_register_total",
    "pick_register_source",
    "pick_register_change",
    "register_email",
)


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(
        "CREATE TRIGGER pick_register_updated_at BEFORE UPDATE ON pick_register "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
    )
    for t in TENANT_TABLES:
        op.execute(
            f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY; ALTER TABLE {t} FORCE ROW LEVEL SECURITY;"
            f"CREATE POLICY tenant_isolation ON {t} USING (tenant_id = app_current_tenant());"
        )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON pick_register, pick_register_value, pick_register_total, "
        f"pick_register_source, register_email TO {APP_ROLE};"
    )
    op.execute(f"GRANT SELECT, INSERT ON pick_register_change TO {APP_ROLE};")  # append-only history


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS register_email, pick_register_change, pick_register_source, pick_register_total, "
        "pick_register_value, pick_register CASCADE;"
        "DROP FUNCTION IF EXISTS register_email_final;"
    )
