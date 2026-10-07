# Phase 0: baseline measurement and owner decisions

The spec forbids claiming savings before measuring the current manual process (§4, A10). Measure **one representative week before the pilot**, then repeat the same measurements during the pilot.

## 1. Manual baseline log (fill in daily for one week)

| Date | Activity | Person/role | Minutes spent | Count (rows, reports, follow-ups…) | Notes |
|---|---|---|---|---|---|
| | Data entry: typing production notes into Excel/ERP | | | rows typed | |
| | Validation: checking missing/wrong values | | | corrections made | |
| | Dashboard preparation/refresh | | | refreshes | |
| | Daily report (PDF) preparation | | | reports | |
| | Email preparation | | | emails | |
| | Pending-entry follow-up (calls/messages) | | | follow-ups | |
| | Historical search (finding old reports) | | | lookups | |

Weekly summary to record: total minutes per activity, minutes per report, rows typed per week, correction rate (corrections ÷ rows), follow-ups per week.

## 2. Owner decisions (spec §19, D1–D10)

M1 uses the baselines below. Each is **unconfirmed** until the named owner signs off.

| Decision | Baseline used in code | Where it lives | Owner sign-off |
|---|---|---|---|
| D1 Roles and approval | Uploader/Reviewer/Sender/Admin/Viewer; every record reviewed; self-approval allowed with the role; Admin does not inherit approval/send | `domain/enums.py`, membership roles | ☐ |
| D2 Company and scope | One company; department grants; Asia/Kolkata; DMY dates | tenant row, settings | ☐ |
| D3 Formats and volumes | JPG/PNG/PDF/XLSX/DOCX/TXT; 20 files / 20 MiB each / 100 MiB (M2) | M2 | ☐ |
| D4 Production semantics | Daily event record; end-of-period status; stop minutes 0–1440; units m/kg/pcs | migration `0001`, `domain/*` | ☐ |
| D5 Integrations | Sheets one-way; local XLSX; Power BI imported SQL view; Microsoft 365 send | M5/M6 | ☐ |
| D6 AI data and languages | Candidate Azure OCR + pinned extraction model; no training on uploads | M3 | ☐ |
| D7 Retention and recovery | Source 180 d, business 365 d, logs 30 d; RPO 15 min / RTO 4 h | settings `retention_days` | ☐ |
| D8 Automation | Draft-only default; auto-send off; empty periods skipped | settings `feature_flags` | ☐ |
| D9 Report sharing | Sender distributes; external-domain warning | M6 | ☐ |
| D10 Quality and spend | Proposed targets; daily spend ceiling unset until configured | settings `daily_spend_limit` | ☐ |

**Also to confirm (A6):** the seven seeded departments, including whether **Purchase** expects a daily production submission (currently `expected_daily_submission = true` for all seven), and the real machine codes. Only `T-04` comes from the spec; the other machine codes are demo placeholders.
