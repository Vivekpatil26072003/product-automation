# ADR 0003: AI extraction and review (M3)

**Status:** accepted for M3 · 2026-09-28

## Decisions

1. **AI provider: Anthropic Claude (owner decision D6, 2026-09-28).** Model `claude-opus-5` is set in configuration (`ANTHROPIC_MODEL`) and pinned per job. It is used through the official `anthropic` SDK (1.8) with structured outputs: the existing `extraction.v1.json` goes in `output_config.format`. The API key comes only from `ANTHROPIC_API_KEY` and is never stored in settings, the database or logs. **Not yet verified against the live API:** there was no key at build time, so the tests use the real SDK against a mocked HTTP transport.
2. **Claude doubles as the OCR provider** (`OCR_PROVIDER=claude`). It transcribes each page image into lines, marking unreadable words `[illegible]` and never correcting them. Extraction then runs on text spans exactly as for native files, so photos go through the same evidence and validation rules. These lines carry no polygons or confidence, so evidence is page-level and triage reports `UNASSESSED` (spec §8).
3. **Extractor order per page:** table (header row with ≥4 known columns), then labelled lines ("Production 1250 m"), then AI if configured, then manual entry. The deterministic extractors are the spec's "native parsing plus template" first choice and cost nothing per page.
4. **Every extractor output passes the same checks:** strict schema v1; evidence IDs must be spans actually supplied; and **anti-fabrication**: a non-null value whose text is not in its cited evidence becomes null with `UNSUPPORTED_VALUE`. A model cannot introduce a value that isn't in the source.
5. **No tools and no server-side model fallback.** The extraction model can only return the schema, and document text is passed as data inside a JSON payload. The SDK's refusal fallback is deliberately **not** enabled: it would silently switch to a model that hasn't been evaluated. A refusal routes the page to manual entry instead.
6. **Release gate (FR28).** `model_release` / `evaluation_run` record which extractor + model + prompt hash + schema passed evaluation. In staging and production an unapproved AI version never runs (`AI_MODEL_NOT_APPROVED`, then manual entry). In development it runs but every candidate carries an `UNEVALUATED_MODEL` warning. `python -m app.cli promote` refuses unless the release's latest run passed every gate.
7. **Confidence is separate from validation (A7).** `confidence` is OK / ATTENTION / UNASSESSED; validation issues are a separate list. Every entry still requires approval (A8).
8. **Duplicates are live.** Links are stored at extraction for history. For open entries, duplicate issues are recomputed on every read and again inside the approval transaction, under a per-(date, machine) advisory lock. This way a record approved by someone else is caught (TC16) and shown to the reviewer.
9. **Approval is one transaction:** row locks, re-validation, duplicate re-check, and an explicit acknowledgement when pages failed or entries of the same file are left out. It creates the record with revision 1 and full provenance, the audit event and an outbox event, and bumps `data_version` once. Any invalid row commits nothing.
10. **Record change events wait for their consumers.** The dispatcher now only dispatches event types that have a route. `production_record.changed` events stay pending until the M5 Sheets and Power BI consumers exist, rather than being marked `NO_ROUTE` and lost.

## Data handling (owner action)

Before real production notes are sent to Claude, the data owner must confirm: the organisation's data-processing terms with Anthropic, the account's retention configuration and region, and which departments' notes may be processed (spec §8 "Data policy", decision D6). The configuration default is `AI_PROVIDER=none` / `OCR_PROVIDER=none`.
