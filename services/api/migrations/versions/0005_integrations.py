"""M5 integrations: connections (encrypted write-only secrets), per-record sync ledger, Power BI refreshes,
the read-only approved-production views for Power BI, and the ERP sync ledger.

- integration_connection: one row per configured destination. Secrets are stored only as AES-GCM ciphertext
  (app.core.crypto); the API never returns them. config_version increments on every change.
- record_sync: the projection state of each record in each downstream destination (Sheets, ERP). A newer
  revision always wins; an older one can never overwrite a newer one.
- approved_production_v / production_watermark_v: what Power BI imports. They show only rows of the
  companies mapped to the connecting database role in bi_access, so a BI login can never read another
  company's data even though the views bypass per-request RLS.

Revision ID: 0005
"""

import os

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("APP_DB_ROLE", "prod_app")

UPGRADE = """
CREATE TABLE integration_connection (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  provider text NOT NULL CHECK (provider IN ('google_sheets', 'power_bi', 'ms_graph_mail', 'erp')),
  name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
  state text NOT NULL DEFAULT 'NEEDS_TEST' CHECK (state IN
    ('NEEDS_TEST', 'CONNECTED', 'TEST_FAILED', 'RECONNECT_REQUIRED', 'CONFLICT', 'DISCONNECTED')),
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  secret_ciphertext bytea,
  secret_key_id text,
  config_version integer NOT NULL DEFAULT 1,
  mapping_version integer NOT NULL DEFAULT 1,
  last_test_at timestamptz,
  last_test_ok boolean,
  last_error_code text,
  last_error_message text,
  last_sync_at timestamptz,
  created_by uuid NOT NULL,
  disconnected_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  UNIQUE (tenant_id, id),
  CHECK ((secret_ciphertext IS NULL) = (secret_key_id IS NULL))
);
-- At most one live connection per provider per company.
CREATE UNIQUE INDEX integration_one_live ON integration_connection (tenant_id, provider) WHERE state <> 'DISCONNECTED';

CREATE TABLE record_sync (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  connection_id uuid NOT NULL,
  record_id uuid NOT NULL,
  target_revision integer NOT NULL,
  synced_revision integer,
  state text NOT NULL CHECK (state IN ('PENDING', 'SYNCED', 'FAILED', 'CONFLICT')),
  external_ref text,
  last_hash text,
  attempts integer NOT NULL DEFAULT 0,
  error_code text,
  error_message text,
  synced_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, connection_id) REFERENCES integration_connection (tenant_id, id),
  FOREIGN KEY (tenant_id, record_id) REFERENCES production_record (tenant_id, id),
  UNIQUE (connection_id, record_id),
  CHECK (synced_revision IS NULL OR synced_revision <= target_revision)
);
CREATE INDEX record_sync_pending ON record_sync (connection_id, state) WHERE state <> 'SYNCED';

CREATE TABLE powerbi_refresh (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  connection_id uuid NOT NULL,
  source_data_version bigint NOT NULL,
  state text NOT NULL CHECK (state IN ('REQUESTED', 'IN_PROGRESS', 'COMPLETED', 'FAILED')),
  provider_request_id text,
  requested_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  error_code text,
  error_message text,
  FOREIGN KEY (tenant_id, connection_id) REFERENCES integration_connection (tenant_id, id)
);
CREATE INDEX powerbi_refresh_latest ON powerbi_refresh (connection_id, requested_at DESC);

-- Power BI read access: which database role may see which company's approved data.
CREATE TABLE bi_access (
  role_name text NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  PRIMARY KEY (role_name, tenant_id)
);

CREATE VIEW approved_production_v WITH (security_barrier = true) AS
SELECT r.tenant_id, r.id AS record_id, rev.number AS revision, rev.production_date,
       rev.department_id, d.code AS department_code, d.name AS department_name,
       rev.machine_id, m.code AS machine_code, rev.operator_name,
       rev.production_qty, rev.target_qty, rev.unit, rev.status, rev.stop_minutes, rev.remarks,
       rev.approved_at, r.updated_at
  FROM production_record r
  JOIN record_revision rev ON rev.id = r.current_revision_id
  JOIN department d ON d.id = rev.department_id
  JOIN machine m ON m.id = rev.machine_id
 WHERE r.state = 'ACTIVE'
   AND r.tenant_id IN (SELECT tenant_id FROM bi_access WHERE role_name = current_user);

-- Imported in the same refresh as the data, so freshness is measured against what Power BI actually holds.
CREATE VIEW production_watermark_v WITH (security_barrier = true) AS
SELECT t.id AS tenant_id, t.data_version, now() AS read_at
  FROM tenant t
 WHERE t.id IN (SELECT tenant_id FROM bi_access WHERE role_name = current_user);

CREATE TABLE erp_sync_attempt (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  connection_id uuid NOT NULL,
  record_id uuid NOT NULL,
  revision integer NOT NULL,
  direction text NOT NULL CHECK (direction IN ('OUTBOUND', 'INBOUND')),
  mapping_version integer NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('SUCCEEDED', 'FAILED', 'RETRY')),
  external_ref text,
  error_code text,
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, connection_id) REFERENCES integration_connection (tenant_id, id)
);
CREATE INDEX erp_sync_attempt_record ON erp_sync_attempt (connection_id, record_id, created_at DESC);
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    for t in ("integration_connection", "record_sync"):
        op.execute(
            f"CREATE TRIGGER {t}_updated_at BEFORE UPDATE ON {t} FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
        )
    for t in ("integration_connection", "record_sync", "powerbi_refresh", "erp_sync_attempt"):
        op.execute(
            f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY; ALTER TABLE {t} FORCE ROW LEVEL SECURITY;"
            f"CREATE POLICY tenant_isolation ON {t} USING (tenant_id = app_current_tenant());"
        )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON integration_connection, record_sync, powerbi_refresh TO {APP_ROLE};")
    op.execute(f"GRANT SELECT, INSERT ON erp_sync_attempt TO {APP_ROLE};")  # append-only attempt ledger
    # bi_access and the views are managed by operations (owner); the app role cannot grant itself BI access.


def downgrade() -> None:
    op.execute(
        "DROP VIEW IF EXISTS production_watermark_v, approved_production_v;"
        "DROP TABLE IF EXISTS erp_sync_attempt, bi_access, powerbi_refresh, record_sync, integration_connection"
        " CASCADE;"
    )
