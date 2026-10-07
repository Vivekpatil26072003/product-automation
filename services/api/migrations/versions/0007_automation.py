"""M7 automation: schedules (FR24), exceptions (A1), reminders and notifications (A2, A3).

- schedule / schedule_version: a schedule's content is versioned; every edit adds an immutable version and
  revokes auto-send approval. approval_hash is the policy the Sender confirmed (scope, recipients, mailbox,
  time zone, cadence, template); it must still match at dispatch.
- schedule_run: one row per (schedule, version, period, run kind); the unique key is what guarantees a
  period is never run twice, even after a worker restart.
- exception_item / exception_event: explainable findings with severity, reason, object and status; one open
  item per condition (dedupe_key); every status change is an append-only event. Exceptions never change
  production data.
- notification: in-app reminders and escalations, unique per (day, department, stage, recipient).

Revision ID: 0007
"""

import os

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("APP_DB_ROLE", "prod_app")

UPGRADE = """
CREATE TABLE schedule (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  owner_id uuid NOT NULL,
  name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
  version integer NOT NULL DEFAULT 1,
  config jsonb NOT NULL,
  active boolean NOT NULL DEFAULT true,
  paused_reason text,
  approval_state text NOT NULL DEFAULT 'UNAPPROVED' CHECK (approval_state IN ('UNAPPROVED', 'APPROVED')),
  approval_hash text,
  approved_version integer,
  approved_by uuid,
  approved_at timestamptz,
  last_due_at timestamptz,
  next_due_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  row_version integer NOT NULL DEFAULT 1,
  UNIQUE (tenant_id, id),
  CHECK ((approval_state = 'APPROVED') = (approval_hash IS NOT NULL AND approved_version IS NOT NULL))
);
CREATE INDEX schedule_due ON schedule (tenant_id, next_due_at) WHERE active;

CREATE TABLE schedule_version (
  tenant_id uuid NOT NULL,
  schedule_id uuid NOT NULL,
  version integer NOT NULL,
  config jsonb NOT NULL,
  created_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (schedule_id, version),
  FOREIGN KEY (tenant_id, schedule_id) REFERENCES schedule (tenant_id, id)
);

CREATE TABLE schedule_run (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  schedule_id uuid NOT NULL,
  version integer NOT NULL,
  period_start date NOT NULL,
  period_end date NOT NULL,
  run_kind text NOT NULL CHECK (run_kind IN ('SCHEDULED', 'MANUAL')),
  mode text NOT NULL CHECK (mode IN ('DRAFT_ONLY', 'AUTO_SEND')),
  due_at timestamptz NOT NULL,
  state text NOT NULL DEFAULT 'QUEUED' CHECK (state IN ('QUEUED', 'WAITING', 'REPORTING', 'SENDING', 'DRAFTED',
    'SENT', 'SKIPPED_EMPTY', 'SKIPPED_MISSED', 'FAILED', 'CANCELLED', 'HALTED')),
  cancel_requested boolean NOT NULL DEFAULT false,
  report_id uuid,
  draft_id uuid,
  email_id uuid,
  excluded_pending integer,
  note text,
  error_code text,
  error_message text,
  requested_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (tenant_id, id),
  UNIQUE (schedule_id, version, period_start, period_end, run_kind),
  FOREIGN KEY (schedule_id, version) REFERENCES schedule_version (schedule_id, version),
  FOREIGN KEY (tenant_id, report_id) REFERENCES report (tenant_id, id),
  CHECK (period_start <= period_end)
);
CREATE INDEX schedule_run_list ON schedule_run (schedule_id, created_at DESC);

CREATE TABLE exception_item (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  kind text NOT NULL,
  severity text NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'CRITICAL')),
  audience text NOT NULL CHECK (audience IN ('DEPARTMENT', 'REPORTING', 'ADMIN')),
  reason text NOT NULL,
  object_type text NOT NULL,
  object_id uuid,
  department_id uuid,
  production_date date,
  dedupe_key text NOT NULL,
  status text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED', 'DISMISSED')),
  detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz,
  resolved_by uuid,
  resolution text,
  version integer NOT NULL DEFAULT 1,
  UNIQUE (tenant_id, id)
);
-- One live item per condition; a dismissed condition is remembered so it does not reopen.
CREATE UNIQUE INDEX exception_live ON exception_item (tenant_id, dedupe_key) WHERE status IN ('OPEN', 'ACKNOWLEDGED');
CREATE INDEX exception_list ON exception_item (tenant_id, status, last_seen_at DESC);

CREATE TABLE exception_event (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  exception_id uuid NOT NULL,
  action text NOT NULL CHECK (action IN ('OPENED', 'ACKNOWLEDGED', 'RESOLVED', 'DISMISSED', 'AUTO_RESOLVED')),
  actor_id uuid,
  note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, exception_id) REFERENCES exception_item (tenant_id, id)
);

CREATE TABLE notification (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  recipient_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('REMINDER_FIRST', 'REMINDER_SECOND', 'ESCALATION', 'EXCEPTION', 'SCHEDULE')),
  dedupe_key text NOT NULL,
  title text NOT NULL,
  body text NOT NULL,
  link text,
  department_id uuid,
  day date,
  read_at timestamptz,
  email_state text NOT NULL DEFAULT 'NOT_REQUESTED'
    CHECK (email_state IN ('NOT_REQUESTED', 'QUEUED', 'ACCEPTED', 'UNKNOWN', 'FAILED', 'SKIPPED')),
  email_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, dedupe_key)
);
CREATE INDEX notification_inbox ON notification (recipient_id, created_at DESC);
"""

TENANT_TABLES = ("schedule", "schedule_version", "schedule_run", "exception_item", "exception_event", "notification")


def upgrade() -> None:
    op.execute(UPGRADE)
    for t in ("schedule", "schedule_run"):
        op.execute(
            f"CREATE TRIGGER {t}_updated_at BEFORE UPDATE ON {t} FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
        )
    for t in TENANT_TABLES:
        op.execute(
            f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY; ALTER TABLE {t} FORCE ROW LEVEL SECURITY;"
            f"CREATE POLICY tenant_isolation ON {t} USING (tenant_id = app_current_tenant());"
        )
    # The worker's periodic tick lists companies (IDs only) from the dispatcher scope; all other work is per tenant.
    op.execute("CREATE POLICY dispatcher_list ON tenant FOR SELECT USING (app_scope() = 'dispatcher');")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON schedule, schedule_run, exception_item, notification TO {APP_ROLE};")
    op.execute(f"GRANT SELECT, INSERT ON schedule_version, exception_event TO {APP_ROLE};")  # append-only history


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS dispatcher_list ON tenant;"
        "DROP TABLE IF EXISTS notification, exception_event, exception_item, schedule_run, schedule_version, schedule"
        " CASCADE;"
    )
