"""M2 capture and ingestion: batches, uploads (quarantine lifecycle) and per-page parse results.

- An upload never leaves quarantine until its bytes are verified and scanned (FR02, spec §3).
- page_result is keyed by (upload, page, pipeline_version): a retry updates failed pages in place and
  never duplicates completed ones (FR04, TC08).
- Extracted text is document content, so it lives in object storage (derived/); the database holds
  only its key and counts.

Revision ID: 0002
"""

import os

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("APP_DB_ROLE", "prod_app")

TS = """
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1"""

UPGRADE = f"""
CREATE TABLE batch (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  department_id uuid NOT NULL,
  owner_id uuid NOT NULL,
  file_count integer NOT NULL CHECK (file_count BETWEEN 1 AND 20),
  total_bytes bigint NOT NULL CHECK (total_bytes BETWEEN 1 AND 104857600),{TS},
  FOREIGN KEY (tenant_id, department_id) REFERENCES department (tenant_id, id),
  FOREIGN KEY (tenant_id, owner_id) REFERENCES membership (tenant_id, id),
  UNIQUE (tenant_id, id)
);
CREATE INDEX batch_scope ON batch (tenant_id, department_id, created_at DESC);
CREATE INDEX batch_owner ON batch (tenant_id, owner_id, created_at DESC);

CREATE TABLE upload (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  batch_id uuid NOT NULL,
  slot_no integer NOT NULL CHECK (slot_no BETWEEN 1 AND 20),
  display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 255),
  extension text NOT NULL CHECK (extension IN ('jpg', 'jpeg', 'png', 'pdf', 'xlsx', 'docx', 'txt')),
  declared_mime text NOT NULL,
  declared_bytes bigint NOT NULL CHECK (declared_bytes BETWEEN 1 AND 20971520),
  declared_sha256 bytea NOT NULL CHECK (octet_length(declared_sha256) = 32),
  object_key text NOT NULL UNIQUE,
  state text NOT NULL DEFAULT 'UPLOADING'
    CHECK (state IN ('UPLOADING', 'QUARANTINED', 'READY', 'REJECTED', 'EXPIRED')),
  detected_type text,
  page_count integer CHECK (page_count IS NULL OR page_count BETWEEN 1 AND 50),
  scan_status text NOT NULL DEFAULT 'PENDING'
    CHECK (scan_status IN ('PENDING', 'CLEAN', 'INFECTED', 'ERROR', 'SKIPPED_DEV')),
  scanner text,
  reject_code text,
  reject_message text,
  duplicate_of uuid,
  expires_at timestamptz NOT NULL,
  completed_at timestamptz,
  ready_at timestamptz,{TS},
  FOREIGN KEY (tenant_id, batch_id) REFERENCES batch (tenant_id, id),
  UNIQUE (batch_id, slot_no),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, duplicate_of) REFERENCES upload (tenant_id, id),
  CHECK ((state = 'REJECTED') = (reject_code IS NOT NULL)),
  CHECK (state <> 'READY' OR scan_status IN ('CLEAN', 'SKIPPED_DEV'))
);
CREATE INDEX upload_sha ON upload (tenant_id, declared_sha256);
CREATE INDEX upload_expiry ON upload (expires_at) WHERE state = 'UPLOADING';

CREATE TABLE page_result (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  upload_id uuid NOT NULL,
  page_no integer NOT NULL CHECK (page_no >= 1),
  pipeline_version text NOT NULL,
  parser text NOT NULL,
  state text NOT NULL CHECK (state IN ('SUCCEEDED', 'FAILED')),
  text_key text,
  char_count integer,
  span_count integer,
  min_confidence numeric(5, 4),
  warnings text[] NOT NULL DEFAULT '{{}}',
  error_code text,
  error_message text,
  attempts integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, upload_id) REFERENCES upload (tenant_id, id),
  UNIQUE (upload_id, page_no, pipeline_version),
  CHECK ((state = 'SUCCEEDED') = (text_key IS NOT NULL AND error_code IS NULL))
);
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    for t in ("batch", "upload", "page_result"):
        op.execute(
            f"CREATE TRIGGER {t}_updated_at BEFORE UPDATE ON {t} FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
            f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY; ALTER TABLE {t} FORCE ROW LEVEL SECURITY;"
            f"CREATE POLICY tenant_isolation ON {t} USING (tenant_id = app_current_tenant());"
            f"GRANT SELECT, INSERT, UPDATE ON {t} TO {APP_ROLE};"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS page_result, upload, batch CASCADE;")
