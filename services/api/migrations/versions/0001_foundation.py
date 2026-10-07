"""M1 foundation: tenancy, identity, master data, audit, outbox, jobs, records and revisions.

Invariants enforced in the database (a second barrier behind application checks):
- every business foreign key includes tenant_id, so cross-tenant references are impossible;
- a machine referenced by a revision must belong to that revision's department;
- approved record revisions are immutable and no revision can be deleted;
- audit_event is append-only for the runtime role;
- an ACTIVE/ARCHIVED production_record always points at an APPROVED revision of itself;
- row-level security scopes the runtime role to the transaction's tenant (app.tenant_id).

Revision ID: 0001
"""

import os

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("APP_DB_ROLE", "prod_app")

TS = """
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1"""

UPGRADE = f"""
-- Session-scoped context helpers. Unset settings resolve to NULL / '' and therefore match nothing.
CREATE FUNCTION app_current_tenant() RETURNS uuid LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$;
CREATE FUNCTION app_scope() RETURNS text LANGUAGE sql STABLE AS
  $$ SELECT COALESCE(current_setting('app.scope', true), '') $$;

CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

CREATE TABLE tenant (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
  timezone text NOT NULL DEFAULT 'Asia/Kolkata',
  date_order text NOT NULL DEFAULT 'DMY' CHECK (date_order IN ('DMY', 'MDY')),
  data_version bigint NOT NULL DEFAULT 0,
  settings jsonb NOT NULL DEFAULT '{{}}'::jsonb,{TS}
);

CREATE TABLE membership (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  subject text NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 255),
  email text CHECK (email IS NULL OR char_length(email) <= 320),
  display_name text CHECK (display_name IS NULL OR char_length(display_name) <= 120),
  roles text[] NOT NULL DEFAULT '{{}}'
    CHECK (roles <@ ARRAY['UPLOADER', 'REVIEWER', 'SENDER', 'ADMIN', 'VIEWER']::text[]),
  active boolean NOT NULL DEFAULT true,{TS},
  UNIQUE (tenant_id, subject),
  UNIQUE (tenant_id, id)
);
CREATE INDEX membership_subject_active ON membership (subject) WHERE active;

CREATE TABLE department (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  code text NOT NULL CHECK (code ~ '^[A-Z0-9][A-Z0-9_-]{{0,39}}$'),
  name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
  active boolean NOT NULL DEFAULT true,
  expected_daily_submission boolean NOT NULL DEFAULT true,
  sort_order integer NOT NULL DEFAULT 0,{TS},
  UNIQUE (tenant_id, code),
  UNIQUE (tenant_id, id)
);

CREATE TABLE membership_department (
  tenant_id uuid NOT NULL,
  membership_id uuid NOT NULL,
  department_id uuid NOT NULL,
  PRIMARY KEY (membership_id, department_id),
  FOREIGN KEY (tenant_id, membership_id) REFERENCES membership (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, department_id) REFERENCES department (tenant_id, id)
);

CREATE TABLE machine (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  department_id uuid NOT NULL,
  code text NOT NULL CHECK (char_length(code) BETWEEN 1 AND 40),
  name text CHECK (name IS NULL OR char_length(name) <= 120),
  active boolean NOT NULL DEFAULT true,{TS},
  FOREIGN KEY (tenant_id, department_id) REFERENCES department (tenant_id, id),
  UNIQUE (tenant_id, code),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, department_id, id)
);

CREATE TABLE operator (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  department_id uuid,
  name text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 120),
  active boolean NOT NULL DEFAULT true,{TS},
  FOREIGN KEY (tenant_id, department_id) REFERENCES department (tenant_id, id),
  UNIQUE (tenant_id, id)
);

CREATE TABLE master_alias (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  alias text NOT NULL CHECK (char_length(alias) BETWEEN 1 AND 120),
  alias_key text NOT NULL CHECK (char_length(alias_key) BETWEEN 1 AND 120),
  department_id uuid,
  machine_id uuid,
  operator_id uuid,
  kind text GENERATED ALWAYS AS (
    CASE WHEN department_id IS NOT NULL THEN 'department'
         WHEN machine_id IS NOT NULL THEN 'machine' ELSE 'operator' END) STORED,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (num_nonnulls(department_id, machine_id, operator_id) = 1),
  FOREIGN KEY (tenant_id, department_id) REFERENCES department (tenant_id, id),
  FOREIGN KEY (tenant_id, machine_id) REFERENCES machine (tenant_id, id),
  FOREIGN KEY (tenant_id, operator_id) REFERENCES operator (tenant_id, id),
  UNIQUE (tenant_id, kind, alias_key)
);

CREATE TABLE unit_alias (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  alias text NOT NULL CHECK (char_length(alias) BETWEEN 1 AND 40),
  alias_key text NOT NULL CHECK (char_length(alias_key) BETWEEN 1 AND 40),
  unit text NOT NULL CHECK (unit IN ('m', 'kg', 'pcs')),
  factor numeric(20, 10) NOT NULL CHECK (factor > 0),
  active boolean NOT NULL DEFAULT true,{TS},
  UNIQUE (tenant_id, alias_key)
);

CREATE TABLE auth_session (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  membership_id uuid NOT NULL,
  token_hash bytea NOT NULL UNIQUE,
  auth_method text NOT NULL CHECK (auth_method IN ('oidc', 'dev')),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  FOREIGN KEY (tenant_id, membership_id) REFERENCES membership (tenant_id, id)
);

-- One-time OAuth state (not tenant data; tenant is unknown until the callback resolves).
CREATE TABLE oidc_login_state (
  state_hash bytea PRIMARY KEY,
  nonce text NOT NULL,
  code_verifier text NOT NULL,
  return_to text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz
);

-- Pre-tenant security events such as rejected sign-ins. Append-only; no document content.
CREATE TABLE security_event (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event text NOT NULL,
  detail text,
  correlation_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_event (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  actor_type text NOT NULL CHECK (actor_type IN ('user', 'service', 'system')),
  actor_id uuid,
  action text NOT NULL,
  object_type text NOT NULL,
  object_id uuid,
  object_revision integer,
  reason text CHECK (reason IS NULL OR char_length(reason) <= 2000),
  before jsonb,
  after jsonb,
  correlation_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_event_tenant_time ON audit_event (tenant_id, created_at DESC);
CREATE INDEX audit_event_object ON audit_event (tenant_id, object_type, object_id, created_at);

CREATE TABLE outbox (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  event_key text NOT NULL UNIQUE CHECK (char_length(event_key) BETWEEN 1 AND 300),
  event_type text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  correlation_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  dispatched_at timestamptz,
  dispatch_attempts integer NOT NULL DEFAULT 0,
  last_error text
);
CREATE INDEX outbox_pending ON outbox (created_at) WHERE dispatched_at IS NULL;

CREATE TABLE job (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  kind text NOT NULL,
  object_id uuid NOT NULL,
  generation integer NOT NULL DEFAULT 1 CHECK (generation >= 1),
  state text NOT NULL DEFAULT 'QUEUED'
    CHECK (state IN ('QUEUED', 'RUNNING', 'RETRY_WAIT', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED')),
  attempts integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 20),
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  lease_token uuid,
  lease_until timestamptz,
  worker_id text,
  cancel_requested boolean NOT NULL DEFAULT false,
  processed integer,
  total integer,
  retryable boolean NOT NULL DEFAULT true,
  error_code text,
  error_message text,
  result jsonb,
  source_event_key text,
  created_by uuid,
  correlation_id uuid,
  finished_at timestamptz,{TS},
  UNIQUE (tenant_id, kind, object_id, generation),
  UNIQUE (tenant_id, id),
  CHECK ((state = 'RUNNING') = (lease_token IS NOT NULL AND lease_until IS NOT NULL))
);
CREATE INDEX job_claimable ON job (next_attempt_at) WHERE state IN ('QUEUED', 'RETRY_WAIT');
CREATE INDEX job_running_lease ON job (lease_until) WHERE state = 'RUNNING';

CREATE TABLE job_attempt (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  job_id uuid NOT NULL,
  attempt_no integer NOT NULL,
  lease_token uuid NOT NULL,
  worker_id text,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  outcome text NOT NULL DEFAULT 'RUNNING' CHECK (outcome IN
    ('RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'RETRY', 'LEASE_EXPIRED', 'CANCELLED')),
  error_code text,
  error_message text,
  FOREIGN KEY (tenant_id, job_id) REFERENCES job (tenant_id, id),
  UNIQUE (job_id, attempt_no)
);

CREATE TABLE idempotency_record (
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  actor_id uuid NOT NULL,
  route text NOT NULL,
  idem_key text NOT NULL CHECK (char_length(idem_key) BETWEEN 1 AND 200),
  request_hash bytea NOT NULL,
  status_code integer,
  response_body jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, actor_id, route, idem_key)
);

CREATE TABLE production_record (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  department_id uuid NOT NULL,
  current_revision_id uuid,
  production_date date NOT NULL,
  state text NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE', 'ARCHIVED')),
  entry_key uuid NOT NULL DEFAULT gen_random_uuid(),
  entry_label text CHECK (entry_label IS NULL OR char_length(entry_label) <= 80),
  archived_at timestamptz,
  archived_by uuid,
  archive_reason text CHECK (archive_reason IS NULL OR char_length(archive_reason) BETWEEN 5 AND 500),
  created_by uuid NOT NULL,{TS},
  CHECK ((state = 'ARCHIVED') = (archived_at IS NOT NULL AND archive_reason IS NOT NULL)),
  FOREIGN KEY (tenant_id, department_id) REFERENCES department (tenant_id, id),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, entry_key)
);
-- No uniqueness on date/machine: several legitimate events may exist after duplicate review.
CREATE INDEX production_record_scope ON production_record (tenant_id, production_date, department_id)
  WHERE state = 'ACTIVE';

CREATE TABLE record_revision (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  record_id uuid NOT NULL,
  number integer NOT NULL CHECK (number >= 1),
  production_date date NOT NULL,
  department_id uuid NOT NULL,
  machine_id uuid NOT NULL,
  operator_name text NOT NULL CHECK (char_length(btrim(operator_name)) BETWEEN 1 AND 120),
  production_qty numeric(18, 3) NOT NULL CHECK (production_qty >= 0),
  target_qty numeric(18, 3) NOT NULL CHECK (target_qty >= 0),
  unit text NOT NULL CHECK (unit IN ('m', 'kg', 'pcs')),
  status text NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'PENDING', 'HOLD')),
  stop_minutes integer NOT NULL CHECK (stop_minutes BETWEEN 0 AND 1440),
  remarks text NOT NULL DEFAULT '' CHECK (char_length(remarks) <= 2000),
  provenance jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  approval_state text NOT NULL DEFAULT 'PENDING' CHECK (approval_state IN ('PENDING', 'APPROVED', 'REJECTED')),
  reason text CHECK (reason IS NULL OR char_length(reason) BETWEEN 5 AND 500),
  created_by uuid NOT NULL,
  approved_by uuid,
  approved_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (unit <> 'pcs' OR (production_qty = trunc(production_qty) AND target_qty = trunc(target_qty))),
  CHECK ((approval_state = 'APPROVED') = (approved_by IS NOT NULL AND approved_at IS NOT NULL)),
  FOREIGN KEY (tenant_id, record_id) REFERENCES production_record (tenant_id, id),
  FOREIGN KEY (tenant_id, department_id, machine_id) REFERENCES machine (tenant_id, department_id, id),
  UNIQUE (record_id, number),
  UNIQUE (tenant_id, id)
);

ALTER TABLE production_record ADD CONSTRAINT production_record_current_revision_fk
  FOREIGN KEY (tenant_id, current_revision_id) REFERENCES record_revision (tenant_id, id)
  DEFERRABLE INITIALLY DEFERRED;

CREATE FUNCTION check_current_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.current_revision_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM record_revision r
     WHERE r.id = NEW.current_revision_id AND r.record_id = NEW.id AND r.approval_state = 'APPROVED'
       AND r.production_date = NEW.production_date AND r.department_id = NEW.department_id
  ) THEN
    RAISE EXCEPTION 'production_record % must point at an approved revision of itself', NEW.id
      USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER production_record_current_revision
  AFTER INSERT OR UPDATE ON production_record DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION check_current_revision();

CREATE FUNCTION protect_record_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'record revisions cannot be deleted' USING ERRCODE = '42501';
  END IF;
  IF OLD.approval_state <> 'PENDING' THEN
    RAISE EXCEPTION 'revision % is % and immutable', OLD.id, OLD.approval_state USING ERRCODE = '42501';
  END IF;
  IF (NEW.tenant_id, NEW.record_id, NEW.number, NEW.production_date, NEW.department_id, NEW.machine_id,
      NEW.operator_name, NEW.production_qty, NEW.target_qty, NEW.unit, NEW.status, NEW.stop_minutes,
      NEW.remarks, NEW.provenance, NEW.created_by, NEW.created_at)
     IS DISTINCT FROM
     (OLD.tenant_id, OLD.record_id, OLD.number, OLD.production_date, OLD.department_id, OLD.machine_id,
      OLD.operator_name, OLD.production_qty, OLD.target_qty, OLD.unit, OLD.status, OLD.stop_minutes,
      OLD.remarks, OLD.provenance, OLD.created_by, OLD.created_at) THEN
    RAISE EXCEPTION 'revision content is immutable; create a new revision' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER record_revision_protect BEFORE UPDATE OR DELETE ON record_revision
  FOR EACH ROW EXECUTE FUNCTION protect_record_revision();
"""

VERSIONED = ["tenant", "membership", "department", "machine", "operator", "unit_alias", "job", "production_record"]

TENANT_ONLY = [
    "department", "membership_department", "machine", "operator", "master_alias", "unit_alias",
    "audit_event", "idempotency_record", "production_record", "record_revision",
]  # fmt: skip
AUTH_VISIBLE = {"tenant": "id", "membership": "tenant_id", "auth_session": "tenant_id"}
DISPATCHER_VISIBLE = ["outbox", "job", "job_attempt"]


def _rls_sql() -> str:
    parts = []
    for t in TENANT_ONLY:
        parts.append(
            f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY; ALTER TABLE {t} FORCE ROW LEVEL SECURITY;"
            f" CREATE POLICY tenant_isolation ON {t} USING (tenant_id = app_current_tenant());"
        )
    for t, col in AUTH_VISIBLE.items():
        parts.append(
            f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY; ALTER TABLE {t} FORCE ROW LEVEL SECURITY;"
            f" CREATE POLICY tenant_isolation ON {t}"
            f" USING ({col} = app_current_tenant() OR app_scope() = 'auth');"
        )
    for t in DISPATCHER_VISIBLE:
        parts.append(
            f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY; ALTER TABLE {t} FORCE ROW LEVEL SECURITY;"
            f" CREATE POLICY tenant_isolation ON {t}"
            f" USING (tenant_id = app_current_tenant() OR app_scope() = 'dispatcher');"
        )
    return "\n".join(parts)


def _grants_sql() -> str:
    r = APP_ROLE
    rw = [
        "tenant", "membership", "department", "machine", "operator", "unit_alias", "auth_session",
        "outbox", "job", "job_attempt", "production_record", "record_revision",
    ]  # fmt: skip
    return "\n".join(
        [
            f"GRANT USAGE ON SCHEMA public TO {r};",
            f"GRANT SELECT, INSERT, UPDATE ON {', '.join(rw)} TO {r};",
            f"GRANT SELECT, INSERT, DELETE ON membership_department, master_alias TO {r};",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON idempotency_record, oidc_login_state TO {r};",
            # Append-only for the runtime role.
            f"GRANT SELECT, INSERT ON audit_event, security_event TO {r};",
            f"GRANT EXECUTE ON FUNCTION app_current_tenant(), app_scope() TO {r};",
        ]
    )


def upgrade() -> None:
    op.execute(UPGRADE)
    for t in VERSIONED:
        op.execute(
            f"CREATE TRIGGER {t}_updated_at BEFORE UPDATE ON {t} FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
        )
    op.execute(_rls_sql())
    op.execute(_grants_sql())


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS record_revision, production_record, idempotency_record, job_attempt, job, outbox,
          audit_event, security_event, oidc_login_state, auth_session, unit_alias, master_alias, operator,
          machine, membership_department, department, membership, tenant CASCADE;
        DROP FUNCTION IF EXISTS protect_record_revision(), check_current_revision(), set_updated_at(),
          app_scope(), app_current_tenant();
        """
    )
