# Release evidence and go-live blockers (M8)

_Prepared 2026-09-29 on the local Docker Compose stack. This is evidence for a pilot decision, not a production-readiness claim._

## What is demonstrated

| Area | Evidence | Result |
|---|---|---|
| Backend behaviour | `pytest`: unit, API and integration tests against PostgreSQL 16, Redis, SeaweedFS and ClamAV | **378 passed**, 0 failed |
| Web | `tsc`, `eslint`, `vitest`, `next build` | clean · 19 passed · build OK |
| Browser flows | Playwright, desktop and Pixel 7, 2 consecutive runs | **37 passed** (+1 desktop-only skip) each run |
| Production build + CSP | Full E2E suite run against `next build && next start` with the production CSP | 33 of 33 run (before the operations spec was added) |
| Accessibility (TC50) | axe WCAG 2.2 A/AA on every primary screen per role; 360 px and 200% zoom; keyboard confirmation dialog | 0 critical/serious violations; no sideways scrolling (one layout bug found and fixed) |
| End-to-end consistency (TC56, TC57) | Database, Google Sheet, dashboard, PDF, email and history compared for F1, then a correction | 4,830 / 6,000 / 80.5% everywhere; after correction 4,880 / 81.3%, old PDF unchanged, no duplicate email |
| Retention (TC48) | Holds, dry run, purge, storage failure retry, restore without resurrection | pass |
| No leakage (TC49) | Secret, document-text and signed-URL canaries through provider failures, logs and error responses | none leaked |
| Restore drill (TC54) | `docs/evidence/restore-drill-20260929T163351Z.json` | PASS: 69 records, 73 report PDFs checksum-verified, total 5.2 s |
| Load (TC55) | `docs/evidence/load-test-20260929T163736Z.json`: 100,000 records, 20 users, 90 s, 6,386 requests, 0 errors | list p95 388 ms (target 500), dashboard p95 789 ms (target 1,000), 1,095-record PDF 7.2 s (target 30) |
| Dependencies | `pip-audit -r requirements.lock.txt`; `npm audit --omit=dev` | no known vulnerabilities |
| Traceability | `docs/traceability.md` | 27 of 30 baseline FRs Done (in their stated scope); FR01, FR04 and FR28 Partial (external dependencies below) |

## Go-live blockers (must be closed in staging with real accounts)

1. **Company SSO:** a live OIDC round trip with the company identity provider (FR01). Only the development sign-in has run end to end.
2. **Real notes:** OCR and Claude extraction on an authorized, held-out set of the company's handwritten notes. The FR28 gates must pass before the AI model is enabled (FR04, FR05, FR28). No API key has been used yet.
3. **Providers:** staged tests with real Google Sheets, Power BI (gateway, workspace access) and a Microsoft 365 sender mailbox with an application access policy (FR13, FR15, FR20, TC26–TC42). All adapters have so far been verified only against imitation servers.
4. **Recovery targets:** point-in-time recovery on the managed database with a measured RPO ≤ 15 min and RTO ≤ 4 h, plus a restore of versioned object storage (FR29). The local drill uses logical dumps and a shared bucket.
5. **CI:** the workflow in `.github/workflows/ci.yml` has not yet run on a CI runner.
6. **Load in staging:** the 30-minute pilot workload on staging hardware (the local run was 90 s on a laptop).
7. **Manual accessibility pass:** screen reader (NVDA/VoiceOver) and a real phone camera (FR03, FR26).
8. **Business approvals:** retention periods, the reminder policy, auto-send policy, recipient domains and the ROI baseline measurement week (decision log in `docs/phase0-baseline.md`).

## Known limitations accepted for the pilot

- PDF font covers Latin scripts only; other scripts print as "?" and are counted on the report.
- Attachments over 3 MiB are blocked (Graph inline limit; upload sessions not built).
- Exceptions are rule-based only; there is no AI exception detection.
- The optional auto-send wait-for-late-approvals cutoff is not implemented; unapproved entries are excluded and counted.
- Delivery and bounce evidence is not collected, so emails never go beyond "accepted by provider".
- No real ERP adapter (no vendor named); the mock is refused outside development.
