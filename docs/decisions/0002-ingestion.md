# ADR 0002: Capture and ingestion (M2)

**Status:** accepted for M2 · 2026-09-28

## Decisions

1. **Local object store: SeaweedFS instead of MinIO.** MinIO stopped publishing free community container images in 2025: `minio/minio` no longer exists on Docker Hub and `quay.io/minio/minio` requires authentication. SeaweedFS 4.47 (Apache-2.0, pinned by digest) provides the S3 API. Verified with it: signed PUT/GET, bucket CORS, and rejection of bytes that don't match a signed `x-amz-checksum-sha256`. The application uses only the S3 API, so production can use any S3-compatible service.
2. **Direct-to-storage uploads with checksum-bound signed URLs.** `POST /batches` returns 5-minute signed PUT URLs whose signature includes the declared SHA-256, so storage refuses different bytes. URLs are generated per response and never stored, including in idempotent replays. The server re-hashes the bytes during the scan anyway.
3. **Job rows are created in the command's transaction, not via the outbox.** `complete` inserts the `upload.scan` job and the scan inserts `upload.parse` in the same commit as the state change. The job row is itself the durable record, so no post-commit dispatch is needed; the outbox stays for fan-out events.
4. **Deterministic derived keys** (`originals/<tenant>/<upload>`, `derived/<tenant>/<upload>.p<n>.<pipeline>.json`) make re-runs overwrite rather than duplicate. `page_result` is unique per (upload, page, pipeline version), so a retry skips pages that already succeeded.
5. **Every step that reads untrusted bytes runs in a child process** (inspection, parsing, PDF rendering, image normalisation) with a 60 s timeout, plus 512 MiB and CPU rlimits on Linux. Network and filesystem restrictions are deployment controls (see the runbook).
6. **The scanner is mandatory.** An unreachable ClamAV is a transient failure and is never treated as clean. `MALWARE_SCANNER=disabled` exists for development only, is shown to users as "Not scanned", and is refused in staging/production.
7. **OCR is honest by default.** With `OCR_PROVIDER=none`, scan/photo pages fail as `OCR_NOT_CONFIGURED`: visible, retryable, never invented. The Azure Document Intelligence Read adapter follows its documented REST API but is only tested against recorded response shapes until a staging credential exists.
8. **Evidence without invented geometry.** Native text keeps exact character offsets (plus sheet/cell or paragraph/table coordinates). Polygons exist only when OCR supplies them.
9. **Web app:** Next.js 16 + React 19 + TypeScript, no CSS framework. It uses the spec §6 tokens, and `/api/*` is rewritten to the API so cookies and CSRF stay same-origin. Navigation lists only screens that exist.
