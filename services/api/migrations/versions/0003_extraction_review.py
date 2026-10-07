"""M3 extraction and review: extraction runs, evidence, candidates, change log, duplicate links,
model releases and evaluation runs.

- evidence rows are immutable copies of source-span metadata so a candidate's evidence IDs can be
  validated and shown without re-reading storage.
- candidate_change is append-only (the reviewer change history of FR08).
- An approved candidate is immutable and always linked to the production_record it created.
- model_release/evaluation_run implement the FR28 promotion gate: an AI extractor version is only
  used in staging/production when an evaluation run for exactly that model + prompt + schema passed.

Revision ID: 0003
"""

import os

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("APP_DB_ROLE", "prod_app")

UPGRADE = """
CREATE TABLE extraction (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  upload_id uuid NOT NULL,
  job_id uuid,
  extractor text NOT NULL,
  model text,
  prompt_version text,
  prompt_hash text,
  schema_version text NOT NULL DEFAULT '1',
  release_state text NOT NULL DEFAULT 'NOT_APPLICABLE'
    CHECK (release_state IN ('NOT_APPLICABLE', 'APPROVED', 'UNEVALUATED')),
  state text NOT NULL CHECK (state IN ('SUCCEEDED', 'NO_RECORDS', 'FAILED')),
  candidate_count integer NOT NULL DEFAULT 0,
  warnings text[] NOT NULL DEFAULT '{}',
  error_code text,
  error_message text,
  input_tokens integer,
  output_tokens integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, upload_id) REFERENCES upload (tenant_id, id),
  UNIQUE (tenant_id, id)
);
CREATE INDEX extraction_upload ON extraction (upload_id, created_at DESC);

CREATE TABLE evidence (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  upload_id uuid NOT NULL,
  pipeline_version text NOT NULL,
  span_id text NOT NULL,
  page integer,
  raw_text text NOT NULL,
  char_start integer,
  char_end integer,
  sheet text,
  cell text,
  polygon jsonb,
  confidence numeric(5, 4),
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, upload_id) REFERENCES upload (tenant_id, id),
  UNIQUE (upload_id, pipeline_version, span_id)
);

CREATE TABLE candidate (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  batch_id uuid NOT NULL,
  upload_id uuid NOT NULL,
  extraction_id uuid NOT NULL,
  source_record_key text NOT NULL CHECK (char_length(source_record_key) BETWEEN 1 AND 200),
  fields jsonb NOT NULL,
  issues jsonb NOT NULL DEFAULT '[]'::jsonb,
  confidence text NOT NULL CHECK (confidence IN ('OK', 'ATTENTION', 'UNASSESSED')),
  state text NOT NULL DEFAULT 'NEEDS_REVIEW'
    CHECK (state IN ('NEEDS_REVIEW', 'APPROVED', 'REJECTED', 'SUPERSEDED')),
  duplicate_decision jsonb,
  previous_candidate_id uuid,
  record_id uuid,
  reject_reason text CHECK (reject_reason IS NULL OR char_length(reject_reason) BETWEEN 5 AND 500),
  decided_by uuid,
  decided_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  FOREIGN KEY (tenant_id, batch_id) REFERENCES batch (tenant_id, id),
  FOREIGN KEY (tenant_id, upload_id) REFERENCES upload (tenant_id, id),
  FOREIGN KEY (tenant_id, extraction_id) REFERENCES extraction (tenant_id, id),
  FOREIGN KEY (tenant_id, record_id) REFERENCES production_record (tenant_id, id),
  UNIQUE (extraction_id, source_record_key),
  UNIQUE (tenant_id, id),
  CHECK ((state = 'APPROVED') = (record_id IS NOT NULL)),
  CHECK ((state = 'REJECTED') = (reject_reason IS NOT NULL))
);
CREATE INDEX candidate_batch ON candidate (batch_id, state, created_at);
ALTER TABLE candidate ADD CONSTRAINT candidate_previous_fk
  FOREIGN KEY (tenant_id, previous_candidate_id) REFERENCES candidate (tenant_id, id);

-- Decided candidates never change again (the record they produced carries the history).
CREATE FUNCTION protect_decided_candidate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'candidates cannot be deleted' USING ERRCODE = '42501';
  END IF;
  IF OLD.state IN ('APPROVED', 'REJECTED') THEN
    RAISE EXCEPTION 'candidate % is % and immutable', OLD.id, OLD.state USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER candidate_protect BEFORE UPDATE OR DELETE ON candidate
  FOR EACH ROW EXECUTE FUNCTION protect_decided_candidate();

CREATE TABLE candidate_change (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  candidate_id uuid NOT NULL,
  version integer NOT NULL,
  actor_id uuid NOT NULL,
  changes jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, candidate_id) REFERENCES candidate (tenant_id, id)
);
CREATE INDEX candidate_change_candidate ON candidate_change (candidate_id, created_at);

CREATE TABLE duplicate_link (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  candidate_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('EXACT_FILE', 'SAME_RECORD', 'NEAR_RECORD', 'PENDING_CANDIDATE')),
  other_record_id uuid,
  other_candidate_id uuid,
  other_upload_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, candidate_id) REFERENCES candidate (tenant_id, id),
  CHECK (num_nonnulls(other_record_id, other_candidate_id, other_upload_id) = 1)
);
CREATE INDEX duplicate_link_candidate ON duplicate_link (candidate_id);

CREATE TABLE model_release (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  extractor text NOT NULL,
  model text NOT NULL,
  prompt_hash text NOT NULL,
  schema_version text NOT NULL,
  state text NOT NULL DEFAULT 'CANDIDATE' CHECK (state IN ('CANDIDATE', 'APPROVED', 'RETIRED')),
  evaluation_run_id uuid,
  approved_by uuid,
  approved_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, extractor, model, prompt_hash, schema_version)
);

CREATE TABLE evaluation_run (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  extractor text NOT NULL,
  model text,
  prompt_hash text,
  schema_version text NOT NULL,
  dataset_hash text NOT NULL,
  metrics jsonb NOT NULL,
  gates jsonb NOT NULL,
  passed boolean NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE model_release ADD CONSTRAINT model_release_eval_fk
  FOREIGN KEY (evaluation_run_id) REFERENCES evaluation_run (id);
"""

TABLES = ["extraction", "evidence", "candidate", "candidate_change", "duplicate_link", "model_release",
          "evaluation_run"]  # fmt: skip


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute("CREATE TRIGGER candidate_updated_at BEFORE UPDATE ON candidate "
               "FOR EACH ROW EXECUTE FUNCTION set_updated_at();")  # fmt: skip
    for t in TABLES:
        op.execute(
            f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY; ALTER TABLE {t} FORCE ROW LEVEL SECURITY;"
            f"CREATE POLICY tenant_isolation ON {t} USING (tenant_id = app_current_tenant());"
        )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON extraction, candidate, model_release TO {APP_ROLE};")
    # Append-only for the runtime role.
    op.execute(f"GRANT SELECT, INSERT ON evidence, candidate_change, duplicate_link, evaluation_run TO {APP_ROLE};")


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS model_release, evaluation_run, duplicate_link, candidate_change, candidate, "
        "evidence, extraction CASCADE; DROP FUNCTION IF EXISTS protect_decided_candidate();"
    )
