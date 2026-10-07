# Runbook: capture and ingestion (M2)

## Flow

```
browser ──POST /batches──▶ API: validate limits + grant, create batch/uploads (UPLOADING), return signed PUT URLs
browser ──PUT (checksum-bound)──▶ object store: quarantine/<tenant>/<upload>
browser ──POST /uploads/{id}/complete──▶ API: HEAD object, verify size+checksum ─▶ QUARANTINED + job upload.scan
worker  upload.scan : re-hash bytes · sniff content (sandboxed) · ClamAV ─▶ READY (originals/) + job upload.parse
                                                                        └▶ REJECTED (reason kept, file stays in quarantine)
worker  upload.parse: native text (TXT/XLSX/DOCX/PDF text layer) or OCR (scans, photos) per page
                      ─▶ page_result rows + derived/<tenant>/<upload>.p<n>.ingest-1.json (text + evidence spans)
```

No parser sees a file before its scan verdict. A retry of a parse job (new generation) only processes pages that are missing or failed.

## States a person can see (U2)

| Upload state | Meaning | What to do |
|---|---|---|
| UPLOADING | Slot issued, bytes not confirmed | Upload again (slots expire after 24 h, then EXPIRED) |
| QUARANTINED | Bytes verified, waiting for or undergoing scan | Wait; if the scan job is RETRY_WAIT/FAILED with SCANNER_UNAVAILABLE, check clamd |
| REJECTED | Unsafe or unsupported (see code below) | Upload a supported, unprotected file |
| READY | Scanned clean, promoted to originals | Parse job shows pages processed/failed |

| Reject code | Cause |
|---|---|
| SPOOFED_TYPE | Content does not match the extension (e.g. a program named .jpg) |
| PROTECTED_OR_LEGACY | Password-protected PDF/Office file, or legacy .doc/.xls renamed |
| MACROS_NOT_ALLOWED | Office file contains a VBA project |
| ARCHIVE_BOMB | Office archive expands beyond 200 MiB or a 100:1 ratio |
| IMAGE_TOO_LARGE / TOO_MANY_PAGES | Over 40 megapixels / over 50 pages or sheets |
| NOT_UTF8_TEXT / CORRUPT_FILE | Unreadable text encoding / damaged file |
| CHECKSUM_MISMATCH / UPLOAD_MISSING | Bytes differ from the declaration / never arrived |
| MALWARE_DETECTED | ClamAV verdict (signature is recorded in the audit event, not shown to users) |

| Parse failure code | Cause | Fix |
|---|---|---|
| OCR_NOT_CONFIGURED | Scan/photo page and `OCR_PROVIDER=none` | Configure an OCR provider, then **Retry failed pages** |
| OCR_AUTH_FAILED | Provider rejected the credential | Rotate the credential, then retry |
| OCR_UNAVAILABLE / OCR_TIMEOUT | Provider busy (429/5xx) or slow | Automatic retry with backoff |
| ROW_LIMIT | Office file has more than 10,000 rows | Split the file |
| PARSE_TIMEOUT / PARSE_CRASHED / PARSE_FAILED | Parser exceeded 60 s or crashed | Re-save the file and upload again |

## Operations

- **Scanner down:** scan jobs go to RETRY_WAIT with `SCANNER_UNAVAILABLE` and eventually FAILED (retryable). Nothing is treated as clean. Check `docker compose -f infra/local/docker-compose.yml ps clamav`. On first start clamd downloads signatures (several minutes, about 1.5 GB RAM).
- **Development without a scanner:** `MALWARE_SCANNER=disabled` gives `scan_status=SKIPPED_DEV` (shown in the UI). Configuration refuses this outside development/test.
- **OCR:** `OCR_PROVIDER=azure` plus `AZURE_DI_ENDPOINT` / `AZURE_DI_KEY`. The Azure adapter is tested against recorded response shapes only. **Verify it with a staging credential before relying on it** (spec §19 release gate).
- **Parser isolation:** every inspection, parse and render runs in a separate process with a 60 s timeout (plus 512 MiB address-space and CPU limits on Linux). Production workers must additionally run as non-root with a read-only filesystem and **no outbound network except the OCR endpoint and object store** (container/network policy, not application code).
- **Housekeeping:** the worker expires abandoned uploads every 10 minutes and deletes their quarantine objects.
- **Local object store:** SeaweedFS (S3 API on :9000). MinIO no longer publishes free community images. The bucket and its CORS rules are created by `python -m app.cli ensure-bucket`.
