"""M8 hardening: retention holds, purge runs and the deletion manifest (FR25), and the ROI baseline (A10).

- retention_hold: prevents purging an upload, batch or report while active.
- retention_run / retention_event: every purge (or dry run) and what it deleted, per object, with the storage
  keys. PURGED events are the deletion manifest that is replayed after a backup restore so deleted content is
  never resurrected. Append-only.
- *_purged_at markers keep metadata (hashes, provenance, report facts) after the bytes are gone.
- roi_baseline: the manually measured "before" figures entered by an administrator (A10); pilot figures are
  computed from real data, never assumed.

Revision ID: 0008
"""

import os

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("APP_DB_ROLE", "prod_app")

UPGRADE = """
ALTER TABLE upload ADD COLUMN source_purged_at timestamptz;
ALTER TABLE report ADD COLUMN file_purged_at timestamptz;
ALTER TABLE export ADD COLUMN file_purged_at timestamptz;

CREATE TABLE retention_hold (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  object_type text NOT NULL CHECK (object_type IN ('upload', 'batch', 'report')),
  object_id uuid NOT NULL,
  reason text NOT NULL CHECK (char_length(reason) BETWEEN 3 AND 500),
  created_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  released_at timestamptz,
  released_by uuid,
  UNIQUE (tenant_id, id)
);
CREATE UNIQUE INDEX retention_hold_active ON retention_hold (tenant_id, object_type, object_id)
  WHERE released_at IS NULL;

CREATE TABLE retention_run (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  dry_run boolean NOT NULL,
  eligible jsonb NOT NULL DEFAULT '{}'::jsonb,
  purged integer NOT NULL DEFAULT 0,
  held integer NOT NULL DEFAULT 0,
  failed integer NOT NULL DEFAULT 0,
  requested_by uuid,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (tenant_id, id)
);
CREATE INDEX retention_run_latest ON retention_run (tenant_id, started_at DESC);

CREATE TABLE retention_event (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  action text NOT NULL CHECK (action IN ('PURGED', 'FAILED', 'SKIPPED_HOLD')),
  category text NOT NULL,
  object_type text NOT NULL,
  object_id uuid NOT NULL,
  object_keys jsonb NOT NULL DEFAULT '[]'::jsonb,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, run_id) REFERENCES retention_run (tenant_id, id)
);
CREATE INDEX retention_event_manifest ON retention_event (tenant_id, created_at) WHERE action = 'PURGED';

CREATE TABLE roi_baseline (
  tenant_id uuid PRIMARY KEY REFERENCES tenant(id),
  measured_from date,
  measured_to date,
  manual_minutes_per_report numeric(8,2),
  manual_minutes_per_entry numeric(8,2),
  manual_minutes_per_email numeric(8,2),
  manual_followups_per_week numeric(8,2),
  manual_correction_rate_pct numeric(5,2),
  notes text,
  updated_by uuid,
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1
);
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    for t in ("retention_hold", "retention_run", "retention_event", "roi_baseline"):
        op.execute(
            f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY; ALTER TABLE {t} FORCE ROW LEVEL SECURITY;"
            f"CREATE POLICY tenant_isolation ON {t} USING (tenant_id = app_current_tenant());"
        )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON retention_hold, retention_run, roi_baseline TO {APP_ROLE};")
    op.execute(f"GRANT SELECT, INSERT ON retention_event TO {APP_ROLE};")  # append-only deletion manifest


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS roi_baseline, retention_event, retention_run, retention_hold CASCADE;"
        "ALTER TABLE export DROP COLUMN IF EXISTS file_purged_at;"
        "ALTER TABLE report DROP COLUMN IF EXISTS file_purged_at;"
        "ALTER TABLE upload DROP COLUMN IF EXISTS source_purged_at;"
    )
