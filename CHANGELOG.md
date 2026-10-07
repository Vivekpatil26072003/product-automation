# Changelog

## [0.12.1] - 2026-10-07: Faster reading, several photos at once, one-click corrections

### Added
- **Several photos at once:** on *Diary photos*, "Take photo" collects pages in a list (preview, remove), and
  "Send N photos" uploads them together, three at a time.
- **One-click corrections:** a check gets a "Use 2234" button when two independent numbers on the page agree on the
  right value. For example, 2210 + 24 = 2234 and 2259 - 25 = 2234. The worker's own register mistakes get none.
  The reviewer still decides.
- **Same photo sent again:** an identical file for the same page and shift is noted, not read again. Reading it
  twice only added "Another page shows ..." differences.

### Changed
- Gemini `gemini-3.1-flash-lite` first: it answers in 10-25 s with the same accuracy, while larger free models
  were mostly busy. When every model is busy, the app retries once after 5 s; calls time out after 45 s.
- `start-office.ps1` runs 3 workers, so photos sent together are read at the same time: two register pages in
  about 20 s (before: 2 min and more).
- Batch steps: for a batch with only sheets or registers, "PDF report" and "Emailed to owner" are marked not
  needed (they have their own files and email).

## [0.12.0] - 2026-10-06: Pick reading registers (WGS-02)

### Added
- **Migration 0013:** `pick_register` (one per department and day), `pick_register_value` (one row per shift,
  machine and time: meter reading, picks, mark, source line, accepted checks), `pick_register_total` (totals the
  worker wrote), `pick_register_source`, `pick_register_change` (append-only history), `register_email`.
- Hourly production reading register (pick reading, F/WGS/201): recognised by its heading or column times, shift
  from the times (I 08-16, II 16-24, III 00-08), line reader for typed / OCR rows, Claude reader for handwritten
  grids. Register pages are taken before every other reader.
- Calculated machine, column, shift and day totals and stopped machines. Checks: picks = reading − previous
  reading, written column totals, shift start = previous shift end (also across days), meters counting in other
  units. OK accepts the exact check; a later change brings it back.
- *Pick registers* list and register page (grid like the page, OK, add machine row, save, approve, history);
  Excel (with formulas) / PDF / CSV / **SQL** (standard SQL, loads into PostgreSQL / MySQL / SQLite) downloads;
  email by the worker. Register links on Diary photos and the upload screens; registers count in the batch steps.
- Tested with the two real pages of 2 Oct 2026 (`docs/evidence/pick-register-test-2026-10-06.md`).

- **OCR provider `gemini`** (`OCR_PROVIDER=gemini`, `GEMINI_API_KEY`, `GEMINI_MODEL`): Google Gemini transcribes
  photos into lines, tables as `a | b | c`, unclear digits marked `?`; read by the existing line readers. Errors map
  to the existing OCR codes (rate limit and timeouts retried). Without a provider, photos are still not read.

- **Migration 0014:** `pick_register_total.accepted`. A written column total that does not match the picks blocks
  approval until a person checks it; a reading without picks on a running machine is flagged.
- Gemini falls back to `GEMINI_FALLBACK_MODELS` when the main model is busy (503 / 429) or retired (404).
  Real photos: 255 / 260 values right, every wrong one highlighted.
- Tests never use a developer's OCR / AI account (`OCR_PROVIDER`, `AI_PROVIDER`, `GEMINI_API_KEY` are reset).

### Changed
- `GET /batches/{id}/sheets` also lists the batch's pick registers (`kind: "register"`; daily sheets
  `kind: "sheet"`). The Send Email dialog serves both.

## [0.11.0] - 2026-10-04: Daily production sheets

### Added
- **Migration 0012:** `shift_report` (one per department and day, shift supervisors, notes), `shift_report_value`
  (one row per written value with its source line), `shift_report_source`, `shift_report_change` (append-only
  history), `shift_report_target` (targets and table parameters), `sheet_email`.
- The company's "SULZER PROD. REPORT" as a catalog (15 tables, Weaving / Wastage & manpower / Downtime / Warping);
  formulas for totals, to date, theoretical picks, loss of pick, efficiencies, picks/hour and warping metres/min,
  checked against `SULZER PROD 02.10.2026.xls`.
- Reading: line reader for typed / OCR text (targets skipped, written totals used as a check, ambiguous lines
  highlighted) and a Claude reader for handwritten pages; both feed the day's sheet without overwriting.
- *Daily sheets* list and sheet page (check, OK, save, approve, history), Excel (with formulas) / PDF / CSV
  downloads, email by the worker; sheet links on Diary photos and the upload screens.

### Fixed
- `test_tick_runs_everything_for_each_company` used a fixed date that fell outside the 7-day scan window.

## [0.10.1] - 2026-10-03: Diary data preview and data in emails

### Added
- Diary data table right after an upload (batch screen and Diary photos): saved orders with their latest values,
  orders still to review marked as such; **Download PDF** of that table (titled DRAFT until everything is reviewed).
- Every order and owner-report email carries the data itself: order template variables (`order_number`,
  `customer_name`, `order_date`, `delivery_date`, `quantity`, `rate`, `total`, `additional_details`, `title`,
  `name`, `email`), the whole table (`orders_text`, `orders_html`, `order_count`) and the table in `message`.

### Fixed
- Batch reports recorded order IDs instead of revision IDs as their exact content list.

## [0.10.0] - 2026-10-03: Diary automation

### Fixed
- With `AI_PROVIDER=claude` but no working key, an AI failure no longer stops a page from being read: it falls
  back to the label reader, the production extractors or manual entry.
- Order review on phones: the review table pushed the page sideways (hidden labels escaped the scroll box).

### Added
- **Migration 0011:** `customer`; order status fields, `extra` and `attention` on revisions; `owner_report_settings`
  (owner email, automatic report, company name, EmailJS with an encrypted private key); `batch_report` and
  `report_delivery` (one send in flight per report); order emails sent by the worker (QUEUED state).
- AI order reading with Claude (`app/orders/ai.py`, model `claude-opus-5-5` by default): several orders per page,
  tables without borders, Gujarati / Hindi / English, crossed-out values, other information kept; every value
  tied to its source line; uncertain customer, date, quantity and money values must be confirmed.
- Label reader: several orders per page, Gujarati / Hindi labels and digits.
- Review table of all orders in a batch, confirm buttons, "update existing order or new" decision, customers.
- Owner report: consolidated PDF of a batch (customer-wise orders, totals where valid, items needing attention,
  production entries), emailed automatically by the worker through the EmailJS REST API; retry, send again,
  unknown-result reconciliation; notifications; History -> Owner reports.
- Worker screen **Diary photos** (`/diary`), batch status steps, admin **Owner report & email** settings with a
  test email. Runbook `docs/runbooks/diary-automation.md`.

### Changed
- Order emails are sent by the worker (no browser needed) with the shared EmailJS template; the browser-only
  order template setting was removed. Report emails from M6 are unchanged.
- Default Claude model `claude-opus-5-5`.

## [0.9.0] - 2026-09-30: Customer orders

### Fixed
- A photo whose pages all failed to read (for example handwriting with `OCR_PROVIDER=none`) was stored but led
  nowhere: no extraction ran, so the review screen never listed it. It is now listed with the reason and with
  *Enter customer order* / *Enter production entry*, and the processing screen links to review.

### Added
- **Migration 0010:** `order_draft` (review form), `customer_order`, `order_revision` (append-only), `order_email`
  (send log; finished sends are final). Row-level tenant isolation as for all tables.
- Order notes are recognised during extraction (`app/orders/fields.py`, "Label : value" lines from text or OCR) and
  become an order form with per-field evidence; other pages keep the production extractors.
- Order review page beside the source page, autosave, approve/reject; *Customer orders* list and detail with
  corrections as revisions, per-revision PDF (`app/orders/pdf.py`), *Send Email* through EmailJS with the latest
  PDF as a variable attachment; History tabs *Orders* and *Order emails*; Overview card.
- Runbook `docs/runbooks/customer-orders.md` (Azure free-tier OCR, EmailJS order template, field mapping).

## [0.8.1] - 2026-09-30: EmailJS email channel

### Added
- **Migration 0009:** `email_message.channel` (`graph` | `emailjs`); Graph-only columns nullable for EmailJS, still required for Graph by a check constraint.
- `EMAIL_PROVIDER=emailjs`: the report **Send email** flow sends through EmailJS (`@emailjs/browser`, public key only) from the Sender's browser. The server claims each email once, re-checks the report against the saved records, builds the template variables, and records the EmailJS answer in the existing email status, attempts, audit and History. Unreported sends become UNKNOWN after 15 minutes.
- `POST /emails/{id}/client-send/claim` and `/client-send/result`; runbook `docs/runbooks/emailjs.md` with the template and variable mapping.

### Changed
- Composer and email page show the channel, progress, and success/failure/unknown notices. The production CSP allows `https://api.emailjs.com`.
- Automatic schedule sending is refused on the EmailJS channel (`BROWSER_CHANNEL`). The Graph channel is unchanged.

## [0.8.0] - 2026-09-29: M8 pilot hardening

### Added
- **Migration 0008:** `retention_hold`, `retention_run`, `retention_event` (append-only deletion manifest), `roi_baseline`; `*_purged_at` markers on uploads, reports and exports.
- **Retention (FR25):** daily purge per company, dry run, holds, failure retry, manifest replay after restore; purged sources and PDFs answer 410; `app.cli purge-logs`.
- **Recovery (FR29):** `SENDS_PAUSED`, `app.cli verify-restore`, `app.cli retention-replay --url`, `infra/local/restore_drill.py` with JSON evidence.
- **Monitoring:** `GET /api/v1/ops/metrics` (token), `GET /api/v1/ops/status` (administrators), `infra/monitoring/alerts.yml`.
- **ROI (A10):** `GET /roi`, `PUT /roi/baseline`; savings only with a measured baseline and enough pilot data.
- **Security:** API security headers; production CSP, X-Frame-Options and HSTS for the web app.
- **Web:** Operations page (health, retention, holds, ROI).
- **Tests:** backend 378 (+9: retention, restore, pause, metrics, leakage canaries, ROI, full end-to-end consistency), E2E 37 (+accessibility and operations specs); load test script; CI workflow.
- Docs: ADR 0008, runbook `docs/runbooks/operations.md`, `docs/release-evidence.md`, `docs/evidence/`.

### Fixed
- Pages with wide tables (records list) scrolled sideways on phones and at 200% zoom.
- History tabs contained a plain link inside the tab list; scrollable tables were not keyboard focusable.

## [0.7.0] - 2026-09-29: M7 automation

### Added
- **Migration 0007:** `schedule`, `schedule_version` (immutable), `schedule_run` (unique per schedule/version/period/kind), `exception_item` (one live item per condition), `exception_event` (append-only), `notification` (unique per day/department/stage/recipient); a SELECT-only dispatcher policy on `tenant` for the per-company tick.
- **Scheduling (FR24, optional):** DST-safe occurrences, previous-period reporting, missed/overlap/empty rules, Run now, cancel, policy-hash approval for auto-send rechecked at every dispatch together with the owner's role and grants.
- **Exception engine (A1)** with explainable rules, auto-resolution, acknowledge/resolve/dismiss with notes, audiences by role.
- **Reminders and escalation (A2, A3)**: staged, working-day and cutoff aware, deduplicated, audited, optional email copies.
- **API:** `GET/POST /schedules`, `GET /schedules/preview`, `GET/PATCH /schedules/{id}`, `POST /schedules/{id}/approve-auto-send`, `POST /schedules/{id}/run`, `GET /schedules/{id}/runs`, `POST /schedule-runs/{id}/cancel`, `GET /exceptions`, `GET /exceptions/{id}/history`, `POST /exceptions/{id}/actions`, `GET /notifications`, `POST /notifications/read`; settings `exception_rules`, `reminders`.
- **Workers:** `automation.tick` (queued every minute per company), `schedule.run`, `notification.email`.
- **Web:** Automation list and schedule pages, Exceptions, Notifications (header link with unread count), Automation settings; control tower additions.
- **Tests:** backend 369 (+22), web unit 19 (+2), E2E 26 (+2 per device).
- Docs: ADR 0007, runbook `docs/runbooks/automation.md`.

### Changed
- Microsoft Graph send accepts messages without an attachment (reminder emails).
- Control tower: the placeholder "reminders not available" connection is replaced by real reminder activity; departments gain rejected and not-synced counts.

## [0.6.0] - 2026-09-29: M6 reports and email

### Added
- **Migration 0006:** `report` (immutable snapshot and file identity, versions per series, outdated marker), `report_item` (append-only), `email_draft` (locked once confirmed), `email_message` (one send intent per draft, guarded state transitions), `email_attempt` (append-only), `email_recipient_status` (no delivered/bounced without delivery evidence), `export.report_id`; RLS on all of them.
- **Reports:** repeatable-read snapshot, metrics from snapshot items, grounded summary (template, or an optional checked Claude paraphrase), reproducible PDF with an embedded font, Excel snapshot of the same report, outdated detection by filter intersection (worker + live check).
- **Email:** plain-text drafts with validation, content-hash confirmation, commit-intent-then-send through Microsoft Graph `sendMail`, ACCEPTED / UNKNOWN / FAILED outcomes without blind retries, reconciliation with evidence, resend and correction drafts.
- **API:** `POST/GET /reports`, `GET /reports/{id}`, `/items`, `/file`, `POST /reports/{id}/retry`, `POST /exports {report_id}`, `POST /email-drafts`, `GET/PATCH /email-drafts/{id}`, `POST /email-drafts/{id}/send`, `GET /emails`, `GET /emails/{id}`, `POST /emails/{id}/reconcile`, `POST /emails/{id}/resend-draft`, `GET /history`; company setting `internal_email_domains`; config `NARRATIVE_PROVIDER`.
- **Workers:** `report.render`, `reports.invalidate` (consumes `production_record.changed`), `email.send`.
- **Web:** Reports list, New report, report detail with PDF preview, email composer with confirmation dialog, email status page, History with tabs; control tower report and email panels; navigation now shows Reports and History.
- **Tests:** backend 347 (+32) including an imitation Microsoft Graph, web unit 17 (+3), E2E 22 (+3 per device).
- Docs: ADR 0006, runbook `docs/runbooks/reports-email.md`.

### Changed
- `POST /exports` with `report_id` now exports that report's snapshot (it previously returned 409 NOT_AVAILABLE; the M4 test was updated accordingly).
- `tenant_tx` accepts an isolation level (used for report snapshots).
- Navigation: "Processing history" is replaced by "History" (its Uploads tab links to all upload batches).

## [0.5.0] - 2026-09-29: M5 integrations

### Added
- **Migration 0005:** `integration_connection` (encrypted write-only credentials, one live connection per provider), `record_sync`, `powerbi_refresh`, `erp_sync_attempt` (append-only), `bi_access` and the Power BI views `approved_production_v` / `production_watermark_v` scoped to the connecting login; RLS on the tenant tables.
- **Credential encryption** `app/core/crypto.py` (AES-256-GCM, key ring `INTEGRATION_KEYS`, connection-bound; required in staging/production).
- **Adapters** `app/integrations/`: Google Sheets (service account), Power BI refresh (Entra client credentials), Microsoft Graph mail connection test, ERP adapter contract with a development-only mock; shared error contract.
- **Jobs:** `integrations.fanout` (consumes `production_record.changed`), `integration.test`, `sheets.sync`, `erp.sync`, `powerbi.refresh`. Ledger: coalescing `ensure_job` and one writer per (kind, object).
- **API:** `GET/POST /integrations`, `GET/PATCH/DELETE /integrations/{id}`, `POST /integrations/{id}/test|reconcile|retry-failed`, `GET /sync-jobs`, `GET /powerbi/status`, `POST /powerbi/refresh`. Records list carries real Sheets sync state; dashboard and control tower report Power BI and Sheets state.
- **CLI:** `python -m app.cli bi-access --role bi_x --tenant ...` (password from `BI_ROLE_PASSWORD`).
- **Web:** Integrations page for administrators (connect, write-only credentials, test, resend, retry failed, refresh, disconnect); Power BI badge on Overview; real states in the control tower.
- **Tests:** backend 315 (+31) with imitation Google/Microsoft/Power BI servers, web unit 14 (+3), E2E 16 (+2 per device).
- Docs: ADR 0005, runbook `docs/runbooks/integrations.md`.

## [0.4.0] - 2026-09-29: M4 records, dashboard, export and control tower

### Added
- **Migration 0004:** `export` (immutable snapshot of rows and metrics; trigger-protected) and a paging index on records.
- **Shared query layer** `app/records/query.py`: one grant-limited filter/predicate for list, dashboard, export and control tower; single-statement GROUPING SETS aggregates.
- **API:** `GET /records` (filters, 4 sorts, keyset cursor), `GET /dashboard`, `POST /exports`, `GET /exports/{id}` (signed link when READY), `GET /control-tower`.
- **Worker** `export.render`: typed XLSX (Records / Summary / protected Metadata) with inert text cells.
- **Web:** Overview dashboard (tiles per unit, department and status charts with table views and drill-down), Production records list with export, Control tower; the root now redirects to Overview.
- **Tests:** backend 284 (+16), web unit 11 (+4), E2E 12 (+2 specs: overview reconciliation/drill-down/export, control tower).

### Changed
- Sign-in lands on Overview; viewers can open Overview, records and the control tower; the upload page explains a missing role.
- Company setting defaults moved to `app/core/company.py` (the settings route re-exports them).

### Fixed
- Mobile layout: grid columns could overflow the viewport (horizontal page scroll) when a chart switched to its table view.

## [0.3.0] - 2026-09-28: M3 AI extraction and review

### Added
- **Migration 0003:** `extraction`, `evidence` (immutable), `candidate` (decided entries immutable), `candidate_change` (append-only), `duplicate_link`, `model_release`, `evaluation_run`; RLS on all.
- **Extraction job** `upload.extract`, queued once per parse: table and labelled-line extractors, a Claude extractor (structured outputs on schema v1) behind the release gate, then manual entry. Validation adds evidence resolution and anti-fabrication.
- **Claude adapters** (decision D6): `ClaudeExtractor` and `ClaudeTranscriber` (OCR provider `claude`), pinned model `claude-opus-5`, no tools, no model fallback, stable error codes.
- **Normalization** of candidates against master data and aliases, confidence triage (A7), and duplicate detection (exact file / same record / near record / pending).
- **API:** `GET /batches/{id}/candidates`, `GET|PATCH /candidates/{id}`, `GET /candidates/{id}/changes`, `POST /approvals`, `POST /candidates/{id}/reject`, `POST /uploads/{id}/candidates` (manual entry), `POST /batches/{id}/reprocess`, `GET /uploads/{id}/pages/{n}`, `GET /records/{id}`, `POST /records/{id}/revisions`, `.../approve`, `.../reject`, `POST /records/{id}/archive`.
- **Web:** U3 review screen, U4 record detail, "Review entries" from processing.
- **FR28:** evaluation harness with spec §14 gates, synthetic gold set, `app.cli evaluate` / `app.cli promote`.
- **Tests:** backend 268 (extraction units, Claude adapter via the real SDK with mocked HTTP, review flow incl. concurrency and prompt injection, evaluation gate); web unit 7; E2E 8.

### Changed
- The dispatcher only dispatches event types that have consumers; others wait in the outbox (no `NO_ROUTE` loss).
- The batch view includes extraction jobs and a to-review count; U2 shows "Finding entries" / "Ready for review".
- API errors on the web client keep all envelope details (e.g. `excluded`, `current_version`).

## [0.2.0] - 2026-09-28: M2 capture and ingestion

### Added
- **Local stack verified on Docker:** PostgreSQL 16, Redis 7, SeaweedFS 4.47 (S3; replaces MinIO, which no longer publishes free images) and ClamAV 1.4. All images pinned by digest; health checks included.
- **Migration 0002:** `batch`, `upload` (quarantine lifecycle, checksum, scan verdict, duplicate link, 24 h expiry) and `page_result` (per page, unique per pipeline version). RLS on all three.
- **API:** `GET /uploads/limits`, `POST/GET /batches`, `GET /batches/{id}`, `GET /batches/{id}/upload-slots`, `POST /uploads/{id}/complete`, `GET /jobs/{id}`, `POST /jobs/{id}/retry`, `POST /jobs/{id}/cancel`, `GET /sources/{upload_id}/file`.
- **Workers:** `upload.scan` (re-hash, content sniffing, ClamAV, promotion) and `upload.parse` (native TXT/XLSX/DOCX/PDF, OCR for scans/photos, failed-pages-only retry). Periodic expiry of abandoned uploads.
- **Sandbox:** all untrusted-byte handling runs in a child process with a timeout (plus rlimits on Linux).
- **OCR adapter interface:** honest `none` default; Azure Document Intelligence Read adapter (recorded-shape tests only).
- **Web app (Next.js 16, React 19, TypeScript):** sign-in (SSO and dev picker), U1 Upload with camera, U2 Processing, processing history.
- **Tests:** backend now 214 (inspection, parsers, sandbox, OCR adapter, real ClamAV incl. EICAR, end-to-end ingestion pipeline); web unit 6; Playwright E2E 6 (desktop + mobile).

### Changed
- `ledger.complete` accepts `FAILED` (handler finished, no usable output) with an explicit `retryable` flag.
- Storage adapter: deterministic keys for derived objects, bucket CORS for browser uploads, missing objects raise `FileNotFoundError`.

## [0.1.0] - 2026-09-28: M0 + M1 foundation

### Added
- **M0:** requirements traceability matrix (FR01–FR30, A1–A10), Phase 0 baseline measurement template, owner decision register D1–D10, ADR 0001.
- **Schema (migration 0001):** tenant, memberships and department grants, departments, machines, operators, aliases, unit aliases, sessions, one-time OIDC state, security events, append-only audit, outbox, jobs and attempts, idempotency records, production records and immutable revisions. Composite tenant foreign keys, forced row-level security for a non-privileged runtime role, and triggers for revision immutability and the approved-current-revision rule.
- **Domain rules (spec §4):** decimal quantities (ROUND_HALF_UP, 3 dp, ambiguity detection), unit normalization with dimension checks, DMY dates with ambiguity/future checks, stop-minute parsing, status normalization, and authoritative metrics (per-unit totals, ratio-of-totals achievement, N/A on zero target).
- **Auth:** OIDC SSO with PKCE/nonce/one-time state, server-side sessions, HMAC CSRF tokens, per-request role and grant resolution, and dev-only sign-in.
- **API:** `/session`, `/auth/*`, `/masters/{kind}`, `/users`, `/settings` and `/health/{live,ready}`, with the stable error envelope, request IDs, If-Match/ETag, Idempotency-Key and audit on every mutation.
- **Reliability:** transactional outbox, dispatcher, and DB-leased jobs with fencing, heartbeat, backoff with jitter, dead-letter and cancel. Redis wake-up with polling fallback.
- **Storage:** S3/MinIO adapter with generated tenant-scoped keys and signed URLs capped at 5 minutes.
- **Contracts:** extraction JSON Schema v1, F1/F2 fixtures, generated OpenAPI with a drift test.
- **Seed:** demo company, 7 departments, 14 machines, aliases, unit aliases, 5 dev memberships, F1 approved records.
- **Tests:** 153 (domain, security helpers, OIDC, contracts, RLS/constraints, job ledger, API).
