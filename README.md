# AI Production Automation System

This system turns production notes into verified production records, dashboards, PDF reports and controlled email, following **Specification v1.1**. People stay accountable: every record needs human approval, and nothing is sent without explicit confirmation.

**Current state: M1–M8 complete in local scope; not production-ready until the staging blockers in [docs/release-evidence.md](docs/release-evidence.md) are closed.** You can upload or photograph notes, review extracted entries beside their source evidence, approve them into production records, correct records through approved revisions, and see them in the Overview dashboard, the records list, an Excel export and the control tower. Administrators can connect Google Sheets, Power BI, a Microsoft 365 mailbox and (development only) a mock ERP; see [docs/runbooks/integrations.md](docs/runbooks/integrations.md). These adapters are tested against imitation providers only. Reviewers and Senders can create snapshot PDF reports; Senders can email them through the connected Microsoft 365 mailbox after an explicit confirmation (see [docs/runbooks/reports-email.md](docs/runbooks/reports-email.md)); sending has only been tested against an imitation provider. Optional automation (off by default): scheduled reports that prepare drafts or, once approved, send automatically; an exception list; reminders and escalation for missing entries (see [docs/runbooks/automation.md](docs/runbooks/automation.md)). See [docs/implementation-status.md](docs/implementation-status.md) and [docs/traceability.md](docs/traceability.md). This is not production-ready.

## Layout

```
apps/web/              Next.js web app: Overview, records, control tower, upload, processing, review, record detail
services/api/app/      FastAPI: auth, domain rules, masters, ingestion, extraction, review, records,
                       evaluation, audit, outbox, jobs, storage
services/api/migrations/  Alembic migrations (authoritative DDL, RLS, triggers, grants)
services/workers/      Outbox dispatcher and job worker (upload.scan/parse/extract, export.render)
packages/contracts/    OpenAPI, extraction JSON Schema v1, fixtures (F1 golden data)
infra/local/           Docker Compose: PostgreSQL 16, Redis, SeaweedFS (S3), ClamAV, optional mock OIDC
tests/{unit,api,integration}/
docs/                  traceability, status, decisions, runbooks, Phase 0 baseline
```

## Local setup (Windows)

Prerequisites: Python 3.12+ (tested on 3.14), Node.js 20+ (tested on 24), Docker Desktop with **WSL 2**.

```powershell
# 1. Infrastructure (ClamAV downloads signatures on first start: a few minutes, about 1.5 GB RAM)
docker compose -f infra/local/docker-compose.yml up -d --wait
# optional local SSO provider: docker compose -f infra/local/docker-compose.yml --profile oidc up -d

# 2. Python environment
python -m venv .venv
.venv\Scripts\pip install -r requirements.lock.txt
copy .env.example .env

# 3. Database, bucket (with browser CORS) and demo data
$env:PYTHONPATH = "services/api;services"
.venv\Scripts\python -m app.cli migrate
.venv\Scripts\python -m app.cli ensure-bucket
.venv\Scripts\python -m app.cli seed-demo

# 4. Run: three terminals
.venv\Scripts\python -m uvicorn app.main:app --port 8000     # API; docs at http://localhost:8000/api/docs
.venv\Scripts\python -m workers                              # scan, parse and extraction worker
cd apps/web; copy .env.local.example .env.local; npm install; npm run dev   # http://localhost:3000
```

Open http://localhost:3000 and pick a seeded development user. `Reviewer (all departments)` can upload.

Notes written as "Label value" lines or tables are extracted without AI. Free-form notes, photos and scans need Claude (`AI_PROVIDER=claude`, `OCR_PROVIDER=claude`, `ANTHROPIC_API_KEY` in the worker's environment). Get data-owner approval first; see [docs/runbooks/extraction-review.md](docs/runbooks/extraction-review.md). Without it, such files go to manual entry on the review screen, clearly marked.

**Daily production sheets:** a photo of the day's report notebook is read into that day's sheet in the database (one value per shift and row), checked, approved, and listed under *Daily sheets* with Excel / PDF / CSV download and email. See [docs/runbooks/daily-sheets.md](docs/runbooks/daily-sheets.md).

**Pick reading registers (WGS-02):** a photo of a shift page of the hourly production reading register is read into that day's register (one database row per machine, time and shift: meter reading, picks, mark), checked (picks = reading − previous reading, column totals, shift-to-shift readings), approved, and listed under *Pick registers* with Excel / PDF / CSV / SQL download and email. See [docs/runbooks/pick-registers.md](docs/runbooks/pick-registers.md).

**Diary automation:** a worker photographs a diary page on *Diary photos*; orders are read (Claude AI or Azure Read), reviewed, saved to Customer orders, and a PDF report is emailed to the owner automatically. Setup and the live acceptance test: [docs/runbooks/diary-automation.md](docs/runbooks/diary-automation.md).

Customer order notes (typed or handwritten) become order forms, then saved orders with a PDF and a *Send Email* button; handwriting needs an OCR reader (Azure free tier). See [docs/runbooks/customer-orders.md](docs/runbooks/customer-orders.md).

Report emails go out through EmailJS (`EMAIL_PROVIDER=emailjs`) with the Gmail service connected in your EmailJS account: create the template and fill `NEXT_PUBLIC_EMAILJS_*` in `apps/web/.env.local` as described in [docs/runbooks/emailjs.md](docs/runbooks/emailjs.md). `EMAIL_PROVIDER=graph` uses a connected Microsoft 365 mailbox instead.

## Tests and checks

```powershell
.venv\Scripts\python -m pytest             # needs the compose stack; db/infra tests skip (not pass) without it
.venv\Scripts\ruff check .; .venv\Scripts\ruff format --check .
.venv\Scripts\python -m app.cli openapi    # regenerate packages/contracts/openapi.json after API changes
.venv\Scripts\python -m app.cli evaluate   # FR28 evaluation on the synthetic gold set (expected to fail the gate)
cd apps/web
npm run typecheck; npm run lint; npm test
npx playwright install chromium; npm run e2e   # starts API, worker and web; needs the stack + seeded dev DB
```

## Demo data

All seeded data is **illustrative**. The F1 fixture reproduces the spec's reconciled example (5 records, 4,830 m against 6,000 m, 80.5%). The seven departments are configurable examples subject to company confirmation, and machine codes other than `T-04` are placeholders.
