# ADR 0001: Stack and foundation choices (M1)

**Status:** accepted for M1 · 2026-09-28

## Context
The repository was empty. A sibling project (`../docproc`) uses Next.js + FastAPI, but its data model is dynamic per-note fields. The spec requires fixed canonical fields, immutable revisions, snapshots and an outbox, so the owner chose a fresh build here, reusing docproc pieces later where they fit (OCR/AI adapters, PDF, Sheets).

## Decisions
1. **Stack (spec default):** Python FastAPI API + workers, PostgreSQL 16, Redis, S3-compatible storage (MinIO locally). Next.js + TypeScript web starts in M2 with the upload screen. Local infra runs via `infra/local/docker-compose.yml`.
2. **The migration is the authoritative DDL** (raw SQL in Alembic). `app/db/tables.py` lists columns for queries only; a test fails on drift.
3. **The database enforces invariants as a second barrier:** composite `(tenant_id, …)` foreign keys; machine ∈ department FK on revisions; immutable approved revisions (trigger); append-only audit (grants); deferred constraint "active record ⇒ approved revision of itself".
4. **Row-level security with a non-privileged runtime role** (`prod_app`: NOSUPERUSER, NOBYPASSRLS). Each transaction sets `app.tenant_id` locally. Two narrow cross-tenant scopes exist: `auth` (tenant/membership/session lookup at sign-in) and `dispatcher` (outbox/job tables only). Application authorization remains mandatory; RLS guards against application bugs, not against a compromised app process.
5. **Job ledger lives in PostgreSQL, Redis only wakes workers.** Spec §9 requires DB leases with fencing tokens, so the durable queue is the `job` table (`FOR UPDATE SKIP LOCKED`). Redis `BRPOP` reduces latency; if Redis is down, workers poll. This departs from "Redis-backed Celery" (spec §9 proposal) because Celery's broker acknowledgement cannot provide the fencing and exactly-once effect guarantees the spec asks for without a second ledger anyway.
6. **SSO:** OIDC authorization code + PKCE + nonce, one-time hashed state, ID token validated with `joserfc`. There is no public registration: a subject must already be mapped to a membership. Release 1 refuses sign-in when a subject has memberships in more than one tenant.
7. **Dev sign-in** (`POST /auth/dev-login`) exists only when `DEV_AUTH_ENABLED=true` and `APP_ENV` is development/test. Settings validation refuses it elsewhere. It still requires an existing membership.
8. **Idempotency:** the key row is inserted in the same transaction as the effect, so concurrent duplicates serialize on the primary key and replay the stored response.
9. **`INSERT … ON CONFLICT` results are read via `RETURNING`,** not `rowcount`, which proved unreliable with this driver during testing.

## Consequences
- Tests need a real PostgreSQL. They skip (not pass) when it is unreachable.
- Adding a tenant-scoped table requires adding it to the RLS lists in its migration. The RLS test enumerates the core tables.
