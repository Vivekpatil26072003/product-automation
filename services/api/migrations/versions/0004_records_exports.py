"""M4 records, dashboard and exports.

- export: an immutable snapshot of the rows (record id, revision id, canonical values) taken when the
  export is requested; the XLSX is rendered later from that snapshot only, so the file always matches
  the selection at request time (FR11, FR14; spec U6/§12 "snapshot" rules).
- Index for stable cursor paging over records (date desc, id desc) including archived rows.

Revision ID: 0004
"""

import os

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("APP_DB_ROLE", "prod_app")

UPGRADE = """
CREATE TABLE export (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  requested_by uuid NOT NULL,
  format text NOT NULL DEFAULT 'xlsx' CHECK (format IN ('xlsx')),
  filter_json jsonb NOT NULL,
  snapshot_json jsonb NOT NULL,
  metrics_json jsonb NOT NULL,
  data_version bigint NOT NULL,
  timezone text NOT NULL,
  row_count integer NOT NULL CHECK (row_count BETWEEN 0 AND 10000),
  state text NOT NULL DEFAULT 'QUEUED' CHECK (state IN ('QUEUED', 'GENERATING', 'READY', 'FAILED')),
  file_key text,
  sha256 text,
  bytes bigint,
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  FOREIGN KEY (tenant_id, requested_by) REFERENCES membership (tenant_id, id),
  UNIQUE (tenant_id, id),
  CHECK ((state = 'READY') = (file_key IS NOT NULL AND sha256 IS NOT NULL))
);
CREATE INDEX export_tenant_time ON export (tenant_id, created_at DESC);

-- The snapshot of an export never changes once written.
CREATE FUNCTION protect_export_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.filter_json, NEW.snapshot_json, NEW.metrics_json, NEW.data_version, NEW.row_count, NEW.requested_by)
     IS DISTINCT FROM
     (OLD.filter_json, OLD.snapshot_json, OLD.metrics_json, OLD.data_version, OLD.row_count, OLD.requested_by) THEN
    RAISE EXCEPTION 'export snapshots are immutable' USING ERRCODE = '42501';
  END IF;
  IF OLD.state = 'READY' THEN
    RAISE EXCEPTION 'a ready export cannot change' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER export_protect BEFORE UPDATE ON export FOR EACH ROW EXECUTE FUNCTION protect_export_snapshot();

CREATE INDEX production_record_paging ON production_record (tenant_id, production_date DESC, id DESC);
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(
        "ALTER TABLE export ENABLE ROW LEVEL SECURITY; ALTER TABLE export FORCE ROW LEVEL SECURITY;"
        "CREATE POLICY tenant_isolation ON export USING (tenant_id = app_current_tenant());"
        f"GRANT SELECT, INSERT, UPDATE ON export TO {APP_ROLE};"
    )


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS export CASCADE; DROP FUNCTION IF EXISTS protect_export_snapshot();"
        "DROP INDEX IF EXISTS production_record_paging;"
    )
