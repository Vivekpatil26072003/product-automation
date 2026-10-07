# Runbook: extraction and review (M3)

## Flow

```
upload.parse (≥1 readable page) ──▶ upload.extract
   per page: table ─▶ labelled lines ─▶ Claude (AI_PROVIDER=claude, release gate) ─▶ manual entry
   validate (schema v1, evidence resolves, anti-fabrication) ─▶ normalize (M1 rules + master data)
   ─▶ confidence triage ─▶ duplicate check ─▶ candidates (NEEDS_REVIEW)
reviewer (U3) ──▶ PATCH /candidates/{id} (autosave, If-Match) ──▶ POST /approvals (atomic)
   ─▶ production_record + revision 1 (provenance) + audit + outbox + data_version+1
record (U4) ──▶ POST /records/{id}/revisions ──▶ …/approve   (or …/archive)
```

## Turning on Claude

1. Get data-owner approval (see ADR 0003, "Data handling").
2. Put the key in the environment of the **worker** (never commit it): `ANTHROPIC_API_KEY=…`
3. Set `AI_PROVIDER=claude` and, for photos and scans, `OCR_PROVIDER=claude`. Restart the worker.
4. Development: extraction runs and candidates show "unevaluated model version".
5. Staging/production: nothing runs until the release is evaluated and promoted:
   ```powershell
   .venv\Scripts\python -m app.cli evaluate --extractor claude --manifest <held-out gold set> --record
   .venv\Scripts\python -m app.cli promote <release id printed by evaluate>
   ```
   `promote` refuses when the latest run for that exact model + prompt + schema failed any gate.

## Evaluation gates (spec §14)

| Metric | Gate |
|---|---|
| critical_accuracy (date, machine, production, target, unit) | ≥ 0.98 |
| record_accuracy (all required fields) | ≥ 0.95 |
| routing_recall (wrong critical values flagged for attention) | ≥ 0.99 |
| fabricated_critical (values where the gold is empty) | = 0 |
| missing_blocked (empty required values block approval) | = 1.0 |
| invalid_outputs (schema rejections) | = 0 |

`tests/ai_eval/gold_synthetic.json` only checks the harness. On it the deterministic extractors score 1.0 on every slice except free text (0.0), so the run fails the accuracy gates, as it should. **Promotion needs a held-out gold set of authorized real notes** (at least 300 documents / 1,000 records, stratified by writer, layout, language and photo quality).

## Codes a reviewer may see

| Code | Meaning | Resolution |
|---|---|---|
| MISSING_VALUE | Not in the note | Enter it (target 0 only if confirmed) |
| UNKNOWN_MACHINE / UNKNOWN_DEPARTMENT | Name not in master data or aliases | Choose from the list; ask an admin to add an alias |
| MACHINE_DEPARTMENT_MISMATCH | Machine belongs elsewhere | Fix machine or department |
| AMBIGUOUS_DATE / AMBIGUOUS_NUMBER | Could be read two ways | Confirm by re-entering the value |
| DIMENSION_MISMATCH / FRACTIONAL_PCS | Units don't fit | Fix unit or quantities |
| UNSUPPORTED_VALUE / EVIDENCE_MISSING | Extractor value not found in the source (dropped) | Enter the value from the source |
| FROM_UPLOAD_CONTEXT (warning) | Department proposed from the upload | Confirm |
| DUPLICATE_UNRESOLVED | Same file, same record, or same date + machine already approved | Keep with a reason, or mark duplicate and reject |
| UNEVALUATED_MODEL (warning) | Read by an AI version that has not passed evaluation | Check every value |
| PARTIAL_ACK_REQUIRED (on approve) | Failed pages or unselected entries in the same file | Confirm they stay excluded |

## Failure handling

- AI 429/529/5xx/network: the job retries with backoff (Retry-After honoured). Completed extractions are not repeated.
- AI auth/model/refusal/truncation/invalid output: extraction is recorded FAILED with the code, and the file shows **Enter manually**.
- Re-extract (`POST /batches/{id}/reprocess`): entries nobody edited are replaced; edited entries stay, with the new one linked through `previous_candidate_id`.
