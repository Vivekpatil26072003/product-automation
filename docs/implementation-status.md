# Implementation status

_Last updated 2026-09-29 · Milestones M1–M8 complete in local scope · Next: staging with real accounts (see [release-evidence.md](release-evidence.md) for go-live blockers)._

## Verified on the Docker Compose stack

| Check | Result |
|---|---|
| Compose stack | PostgreSQL 16, Redis 7, SeaweedFS 4.47, ClamAV 1.4 healthy; dev and test databases at migration 0008 |
| `pytest` (backend) | **378 passed**, 0 failed, 0 skipped |
| Web: `tsc`, `eslint`, `vitest`, `next build` | clean · 19 passed · build OK |
| Playwright E2E (desktop + mobile) | **37 passed** (+1 desktop-only skip) on 2 consecutive runs; also the full suite against the production build with CSP |
| Accessibility (TC50) | axe WCAG 2.2 A/AA: 0 critical/serious on all primary screens; 360 px and 200% zoom without sideways scrolling; keyboard dialog |
| Retention and leakage (TC48, TC49) | holds, dry run, purge, retry after storage failure, manifest replay after restore; canary secrets, note text and signed URLs absent from logs and errors |
| Restore drill (TC54) | PASS in 5.2 s for the dev dataset; checksums of all 73 report PDFs verified ([evidence](evidence/restore-drill-20260929T163351Z.json)) |
| Load (TC55) | 100k records, 20 users: list p95 388 ms, dashboard p95 789 ms, 1,095-record PDF 7.2 s, 0 errors ([evidence](evidence/load-test-20260929T163736Z.json)) |
| End to end (TC56, TC57) | DB, sheet, dashboard, PDF, email and history agree for F1 and after the Tapeline correction |
| Dependency audit | pip-audit and npm audit: no known vulnerabilities |

## M8 scope delivered

- **Retention**: daily purge, dry run, holds, deletion manifest and replay, 410 responses for purged files, log-retention CLI.
- **Recovery**: `SENDS_PAUSED`, `verify-restore`, a timed local restore drill, and a runbook for production PITR.
- **Monitoring**: token-gated Prometheus metrics, alert rules for the spec thresholds, Operations page for administrators.
- **Security**: API security headers, production CSP, leak canary test, dependency audits.
- **Accessibility**: automated WCAG scan and viewport checks in E2E; responsive layout fix for wide tables.
- **ROI**: measured pilot figures against an entered baseline, with guard rails against premature claims.
- **CI** workflow covering every gate (written, not yet executed on a runner).

## Not yet verified (M8)

See [release-evidence.md](release-evidence.md): live SSO, real notes with OCR/AI, real providers, managed PITR, CI execution, a staging load run, a manual screen-reader pass, and business approvals.

## M7 scope delivered (unchanged)

- **Automation** (Senders): schedules with a live next-three-runs preview, versions, pause/resume, approval of automatic sending per version, Run now, cancel, run history linking the report, draft and email.
- **Exceptions**: a list across roles with acknowledge, resolve and dismiss; the control tower shows open counts.
- **Notifications**: header link with unread count; reminders and escalations inbox.
- **Automation settings** (administrators): module switches, cutoff and working days, reminder stages, exception thresholds, company email domains.
- **Control tower**: rejected entries and not-synced records per department, open exceptions and reminder activity.

## Not yet verified (M7)

- Automatic sending and reminder emails have only been exercised against the imitation Microsoft Graph (as in M6).
- Scheduling was verified with explicit instants rather than a long-running clock; the worker's one-minute tick loop itself ran during E2E but no multi-day soak test was done.
- The optional "wait for late approvals before auto-send" cutoff from spec §12 is not implemented: unapproved entries are excluded and counted on the run, and auto-send does not wait.
- Exceptions come from rules only; the "AI exception detection" mentioned in the spec overview is not implemented.

## M6 scope delivered (unchanged)

- **Reports** (Reviewer, Sender): create from a period and scope, snapshot records and metrics, PDF with summary, versions, outdated badge, Excel snapshot, retry and regenerate.
- **Email** (Sender): plain-text composer with autosave, confirmation of recipients (including Bcc), report version and attachment, one send per confirmed draft, status page with per-recipient observations, reconcile unknown outcomes, explicit resend and correction drafts.
- **History**: Uploads, Reports, Emails and Sync tabs, scoped by role.
- **Control tower**: real report and email panels.

## Not yet verified (M6)

- **Microsoft Graph sendMail has not been called live.** It is tested against an imitation server with the documented request and response shapes. A staged test with a real mailbox (and an application access policy) is required before use.
- Delivery and bounce evidence: no delivery-report source is connected, so emails never go beyond "accepted by provider". This is intended.
- The in-page PDF preview relies on the browser's PDF viewer (it is hidden on phones, where Open PDF is used). Headless test browsers cannot display it, so the preview was verified by checksum, not visually.
- AI summary wording (`NARRATIVE_PROVIDER=claude`) is tested with a scripted writer only; no live model call has been made.
- The embedded font covers Latin scripts only. Devanagari or other scripts in names and remarks print as "?" and are counted on the report.
- Attachments above 3 MiB are blocked (Graph inline limit). Upload sessions for larger files are not built.

## M5 scope delivered (unchanged)

- **Integrations page** (administrators): Google Sheets, Power BI, Microsoft 365 email and ERP cards with plain-language status, write-only credential forms, test, resend all, retry failed, Power BI refresh, disconnect.
- **Google Sheets** one-way projection keyed by record_id; per-record state in the records list.
- **Power BI**: read-only views plus a `bi-access` CLI for per-company logins; coalesced refresh and freshness on Overview and the control tower.
- **Microsoft 365 email**: connection and permission test only (sending is M6).
- **ERP**: adapter contract, attempt ledger and a development-only mock behind the feature flag.

## Not yet verified (M5)

- **No live provider has been called.** Google Sheets, Power BI and Microsoft Graph are verified against imitation servers that follow the documented REST calls. A staged test with real accounts is required before use (spec definition of done). In particular: the Power BI refresh request ID match (falls back to the latest refresh), and service-principal access to the workspace.
- Power BI Desktop import of the views through a gateway has not been exercised.
- There is no daily scheduled reconciliation yet. Reconciliation runs on every (re)connect and on demand ("Resend all records").
- After repeated Power BI refresh failures, a refresh is retried at the minimum interval while data is stale. There is no failure cap yet.

## M4 scope delivered (unchanged)

- **Production records:** filters in the URL, four sorts, stable paging, search that treats `%` literally, archived records only on request, links to record detail.
- **Overview:** tiles per unit, department production-vs-target and status charts (each with a table view and drill-down), record downtime, recent records, "as of" time and data version.
- **Excel export:** snapshot at request time, typed and inert XLSX, 5-minute signed download link, every download audited.
- **Control tower:** each department's status for a day, the review queue with blocked counts, processing and failed files, and today's totals. Not-yet-built connections are shown as such.

## Not yet verified

- Carried from M3: Claude live (no API key yet), real-handwriting accuracy (no held-out gold set), SSO round trip, real phone camera, production parser isolation.
- Load targets (p95 ≤ 500 ms list, ≤ 1 s dashboard at 100k records) are not measured yet (M8, TC55). The queries use indexes, but no load test has been run.

## Known gaps carried forward

| Gap | Planned |
|---|---|
| Live staged tests of Google Sheets, Power BI and Microsoft Graph with real accounts; a real ERP adapter once a vendor is chosen | Before go-live / when named |
| Daily scheduled Sheets reconciliation; Power BI failure cap | M7 (scheduling) |
| Live staged send through a real Microsoft 365 mailbox; delivery/bounce evidence source; Graph upload sessions for >3 MiB PDFs; non-Latin PDF font | Before go-live |
| AI cost ledger and spend ceiling enforcement | After pilot sizing |
| Multi-department selection in the filter UI (the API already accepts several departments) | UI follow-up |
