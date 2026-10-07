# Runbook: operations (monitoring, incidents, retention, backup and restore)

## Monitoring

- Set `METRICS_TOKEN` and scrape `GET /api/v1/ops/metrics` with `Authorization: Bearer <token>`. Load `infra/monitoring/alerts.yml` into Prometheus/Alertmanager.
- Administrators see the same health, plus their company's figures, at **Operations** (`/settings/operations`).
- Health endpoints: `/api/v1/health/live` (process) and `/api/v1/health/ready` (database, queue, storage). Provider outages never make the service unready.

| Alert | First action |
|---|---|
| QueueStalled | Check the worker processes are running (`python -m workers`); look for the oldest job in History → Sync or the `job` table |
| JobFailureRate | Group failed jobs by `error_code`; fix configuration or credentials; retry eligible jobs |
| EmailOutcomeUnknown | Open the email, check Sent Items / message trace, reconcile with evidence; never resend blindly |
| SyncLagging / CredentialFailure | Settings → Integrations: reconnect or fix the destination, then Resend all records |
| OutboxBacklog | Workers are not dispatching; restart them (events are durable, nothing is lost) |

## Incident: stop outbound email

Set `SENDS_PAUSED=true` for the workers and restart them. Report and reminder emails stay QUEUED; nothing is dropped and no attempts are used up. Set it back to `false` once the incident is resolved.

## Retention

- The purge runs daily per company. Administrators can **Dry run** (count only) or **Purge now** at Operations.
- **Holds:** place a hold on a report, upload batch or file (for example for an investigation) before its data ages out. Release it when done.
- After a purge, source links answer "deleted under the retention policy". Records, figures and audit remain.
- Log retention (operator): `python -m app.cli purge-logs --days 30` deletes old security events, expired sessions, login states and idempotency keys.

## Backup and restore

- **Local drill:** `python infra/local/restore_drill.py`. It backs up, restores into `production_restore_drill`, replays the deletion manifest, verifies and writes `docs/evidence/restore-drill-*.json`.
- **Production** (managed database with point-in-time recovery; versioned, off-site object storage):
  1. Set `SENDS_PAUSED=true` and pause schedules (Automation → Pause).
  2. Restore the database to the chosen point into an isolated environment; restore object storage to the matching versions.
  3. Run `python -m app.cli retention-replay --url <restored owner URL>`.
  4. Run `python -m app.cli verify-restore --url <restored owner URL>`. It must report `ok: true`.
  5. Reconcile every intent in `sends_to_reconcile` with provider evidence (Email page → Reconcile).
  6. Switch traffic, set `SENDS_PAUSED=false`, and resume schedules.
- Targets from the specification (need business approval and staging measurement): RPO ≤ 15 minutes, RTO ≤ 4 hours. Logical dumps alone give an RPO equal to the dump interval, so production needs WAL archiving / PITR.

## Load test

`python infra/local/perf/load_test.py --records 100000 --users 20 --seconds 120` seeds an isolated `production_perf` database, starts its own API and worker, and writes `docs/evidence/load-test-*.json`. Repeat in staging for the 30-minute run required by the specification.

## Deployment

- Migrations are additive and backward compatible: deploy migrations first, then API and workers.
- Roll back application images, not the database.
- CI gates are in `.github/workflows/ci.yml`: lint, types, audits, all tests, OpenAPI drift, build, E2E and accessibility, and the restore drill.
