"""End-to-end ingestion through the API, signed uploads, ClamAV and the workers.

Covers FR02–FR04, FR21, FR25, FR27 and TC03, TC04, TC05, TC08, TC15 (exact-file part), TC41 (scope).
Needs PostgreSQL, object storage and clamd from infra/local; skipped otherwise.
"""

import hashlib
import json
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from app.db import tables as t
from app.ingestion.ocr import OcrLine, OcrPage
from app.ingestion.scanner import ScannerUnavailable, get_scanner
from app.storage.objects import get_storage
from tests import filegen
from tests.conftest import idem, sign_in
from workers.runtime import run_one

pytestmark = [pytest.mark.db, pytest.mark.infra]
KINDS = ["upload.scan", "upload.parse", "upload.extract"]  # the full pipeline since M3


@pytest.fixture(autouse=True, scope="module")
def _infra():
    try:
        get_storage().ensure_bucket()
        get_scanner().scan(b"ping")
    except (ScannerUnavailable, Exception) as exc:  # noqa: BLE001
        pytest.skip(f"storage or scanner unreachable: {type(exc).__name__}")


def drain() -> None:
    while run_one(KINDS, "test-worker"):
        pass


def start_batch(client, seeded, files: dict[str, bytes], department: str = "TAPELINE", put: bool = True):
    manifest = [{"name": n, "bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()} for n, b in files.items()]
    r = client.post(
        "/api/v1/batches",
        json={"department_id": str(seeded.departments[department]), "files": manifest},
        headers=idem(),
    )
    assert r.status_code == 201, r.text
    body = r.json()["data"]
    if put:
        for slot, data in zip(body["uploads"], files.values(), strict=True):
            assert httpx.put(slot["put_url"], content=data, headers=slot["headers"]).status_code == 200
    return body


def complete_all(client, body, files: dict[str, bytes]) -> None:
    for slot, data in zip(body["uploads"], files.values(), strict=True):
        r = client.post(
            f"/api/v1/uploads/{slot['id']}/complete",
            json={"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)},
            headers=idem(),
        )
        assert r.status_code == 202, r.text
        assert r.json()["data"]["kind"] == "upload.scan"


def batch(client, batch_id) -> dict:
    r = client.get(f"/api/v1/batches/{batch_id}")
    assert r.status_code == 200, r.text
    return r.json()["data"]


def by_name(view) -> dict:
    return {f["name"]: f for f in view["files"]}


def test_every_accepted_format_is_scanned_and_parsed(client, seeded):  # TC03
    sign_in(client, seeded, "dev-reviewer")
    files = {
        "note.txt": filegen.txt(),
        "sheet.xlsx": filegen.xlsx(),
        "note.docx": filegen.docx(),
        "report.pdf": filegen.pdf(text_pages=2),
    }
    body = start_batch(client, seeded, files)
    complete_all(client, body, files)
    drain()
    view = batch(client, body["batch_id"])
    for f in view["files"]:
        assert f["state"] == "READY" and f["scan_status"] == "CLEAN" and f["scanner"].startswith("clamav"), f
        assert f["jobs"]["parse"]["state"] == "SUCCEEDED", f
        assert f["pages"]["failed"] == [] and len(f["pages"]["succeeded"]) == f["pages"]["total"]
    assert view["summary"]["in_progress"] is False
    assert by_name(view)["sheet.xlsx"]["pages"]["total"] == 2

    # Evidence is stored as derived page documents with exact offsets.
    up = uuid.UUID(by_name(view)["note.txt"]["id"])
    key = f"derived/{seeded.tenant_id}/{up}.p1.ingest-1.json"
    doc = json.loads(get_storage().get_bytes(key, 10_000_000))
    assert doc["spans"][4]["text"] == "Production 1250 m"
    assert doc["text"][doc["spans"][4]["char_start"] : doc["spans"][4]["char_end"]] == "Production 1250 m"


def test_hostile_files_are_rejected_with_reasons_and_never_parsed(client, seeded, owner_engine):  # TC05
    sign_in(client, seeded, "dev-reviewer")
    files = {
        "photo.jpg": filegen.executable_as_jpg(),
        "locked.pdf": filegen.encrypted_pdf(),
        "virus.txt": filegen.EICAR,
        "macro.xlsx": filegen.macro_xlsx(),
        "bomb.docx": filegen.zip_bomb_docx(),
    }
    body = start_batch(client, seeded, files)
    complete_all(client, body, files)
    drain()
    view = by_name(batch(client, body["batch_id"]))
    assert {n: f["reject"]["code"] for n, f in view.items()} == {
        "photo.jpg": "SPOOFED_TYPE",
        "locked.pdf": "PROTECTED_OR_LEGACY",
        "virus.txt": "MALWARE_DETECTED",
        "macro.xlsx": "MACROS_NOT_ALLOWED",
        "bomb.docx": "ARCHIVE_BOMB",
    }
    assert all(f["state"] == "REJECTED" and f["jobs"]["parse"] is None for f in view.values())
    with owner_engine.connect() as conn:
        ids = [uuid.UUID(f["id"]) for f in view.values()]
        assert (
            conn.execute(
                select(func.count()).select_from(t.page_result).where(t.page_result.c.upload_id.in_(ids))
            ).scalar_one()
            == 0
        )
        keys = conn.execute(select(t.upload.c.object_key).where(t.upload.c.id.in_(ids))).scalars().all()
    assert all(k.startswith("quarantine/") for k in keys)  # never promoted


def test_limits_enforced_on_server_and_nothing_created(client, seeded, owner_engine):  # TC04
    sign_in(client, seeded, "dev-reviewer")
    dep = str(seeded.departments["TAPELINE"])
    sha = "0" * 64
    too_many = [{"name": f"n{i}.txt", "bytes": 10, "sha256": sha} for i in range(21)]
    r = client.post("/api/v1/batches", json={"department_id": dep, "files": too_many}, headers=idem())
    assert r.status_code == 422 and r.json()["error"]["fields"][0]["code"] == "TOO_MANY_FILES"
    mixed = [
        {"name": "ok.pdf", "bytes": 10, "sha256": sha},
        {"name": "old.doc", "bytes": 10, "sha256": sha},
        {"name": "huge.png", "bytes": 21 * 1024 * 1024, "sha256": sha},
    ]
    r = client.post("/api/v1/batches", json={"department_id": dep, "files": mixed}, headers=idem())
    fields = {(f["field"], f["code"]) for f in r.json()["error"]["fields"]}
    assert r.status_code == 422 and fields == {
        ("files.1.name", "UNSUPPORTED_TYPE"),
        ("files.2.bytes", "FILE_TOO_LARGE"),
    }
    with owner_engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).select_from(t.batch).where(t.batch.c.tenant_id == seeded.tenant_id)
            ).scalar_one()
            == 0
        )


def test_complete_verifies_the_upload(client, seeded):
    sign_in(client, seeded, "dev-reviewer")
    data = filegen.txt()
    body = start_batch(client, seeded, {"n.txt": data}, put=False)
    up = body["uploads"][0]["id"]
    good = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    r = client.post(f"/api/v1/uploads/{up}/complete", json=good, headers=idem())
    assert r.status_code == 409 and r.json()["error"]["code"] == "UPLOAD_MISSING"
    r = client.post(f"/api/v1/uploads/{up}/complete", json={**good, "sha256": "1" * 64}, headers=idem())
    assert r.status_code == 422 and r.json()["error"]["code"] == "CHECKSUM_MISMATCH"
    # Bytes that differ from the declared checksum are refused by storage itself.
    slot = body["uploads"][0]
    assert httpx.put(slot["put_url"], content=b"tampered", headers=slot["headers"]).status_code >= 400


def test_partial_pdf_then_retry_processes_only_failed_pages(client, seeded, owner_engine, monkeypatch):  # TC08
    sign_in(client, seeded, "dev-reviewer")
    files = {"scan.pdf": filegen.pdf(text_pages=2, image_pages=1)}
    body = start_batch(client, seeded, files)
    complete_all(client, body, files)
    drain()
    f = by_name(batch(client, body["batch_id"]))["scan.pdf"]
    parse = f["jobs"]["parse"]
    assert parse["state"] == "PARTIAL" and parse["retryable"] and parse["error"]["code"] == "OCR_NOT_CONFIGURED"
    assert f["pages"] == {"total": 3, "processed": 3, "succeeded": [1, 2], "failed": [3]}

    class FakeOcr:
        name = "fake"

        def read(self, image_png: bytes) -> OcrPage:
            assert image_png.startswith(b"\x89PNG")
            return OcrPage("fake", [OcrLine("Production 1250 m", [[0.1, 0.1], [0.4, 0.1]], 0.8)])

    monkeypatch.setattr("workers.ingestion.get_ocr", lambda: FakeOcr())
    r = client.post(f"/api/v1/jobs/{parse['id']}/retry", json={"failed_only": True}, headers=idem())
    assert r.status_code == 202 and r.json()["data"]["generation"] == 2
    again = client.post(f"/api/v1/jobs/{parse['id']}/retry", json={"failed_only": True}, headers=idem())
    assert again.status_code == 409  # superseded by generation 2
    drain()
    f = by_name(batch(client, body["batch_id"]))["scan.pdf"]
    assert f["jobs"]["parse"]["state"] == "SUCCEEDED" and f["pages"]["succeeded"] == [1, 2, 3]
    with owner_engine.connect() as conn:
        attempts = dict(
            conn.execute(
                select(t.page_result.c.page_no, t.page_result.c.attempts).where(
                    t.page_result.c.upload_id == uuid.UUID(f["id"])
                )
            ).all()
        )
    assert attempts == {1: 1, 2: 1, 3: 2}  # completed pages were not reprocessed
    storage = get_storage()
    assert storage.head(f"derived/{seeded.tenant_id}/{f['id']}.p3.png") is not None


def test_image_without_ocr_fails_visibly(client, seeded):
    sign_in(client, seeded, "dev-reviewer")
    files = {"photo.jpg": filegen.jpeg()}
    body = start_batch(client, seeded, files)
    complete_all(client, body, files)
    drain()
    f = by_name(batch(client, body["batch_id"]))["photo.jpg"]
    assert f["state"] == "READY" and f["jobs"]["parse"]["state"] == "FAILED"
    assert f["jobs"]["parse"]["error"]["code"] == "OCR_NOT_CONFIGURED" and f["jobs"]["parse"]["retryable"]


def test_exact_duplicate_file_is_linked(client, seeded):  # TC15 (file part)
    sign_in(client, seeded, "dev-reviewer")
    files = {"note.txt": filegen.txt()}
    first = start_batch(client, seeded, files)
    complete_all(client, first, files)
    drain()
    second = start_batch(client, seeded, files)
    complete_all(client, second, files)
    drain()
    original = by_name(batch(client, first["batch_id"]))["note.txt"]["id"]
    assert by_name(batch(client, second["batch_id"]))["note.txt"]["duplicate_of"] == original


def test_source_access_requires_scan_and_scope(client, seeded, owner_engine):
    sign_in(client, seeded, "dev-reviewer")
    data = filegen.txt()
    body = start_batch(client, seeded, {"n.txt": data})
    up = body["uploads"][0]["id"]
    complete_all(client, body, {"n.txt": data})
    assert client.get(f"/api/v1/sources/{up}/file").status_code == 409  # not scanned yet
    drain()
    r = client.get(f"/api/v1/sources/{up}/file")
    assert r.status_code == 200 and httpx.get(r.json()["data"]["url"]).content == data
    with owner_engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count())
                .select_from(t.audit_event)
                .where(t.audit_event.c.object_id == uuid.UUID(up), t.audit_event.c.action == "SOURCE_ACCESSED")
            ).scalar_one()
            == 1
        )

    sign_in(client, seeded, "dev-viewer")  # viewers have no source access by default
    assert client.get(f"/api/v1/sources/{up}/file").status_code == 403
    sign_in(client, seeded, "dev-uploader")  # an uploader sees only their own batches
    assert client.get(f"/api/v1/batches/{body['batch_id']}").status_code == 404
    assert client.get(f"/api/v1/sources/{up}/file").status_code == 404


def test_uploader_scope(client, seeded):
    sign_in(client, seeded, "dev-uploader")  # granted Tapeline only
    r = client.post(
        "/api/v1/batches",
        json={
            "department_id": str(seeded.departments["WARPING"]),
            "files": [{"name": "a.txt", "bytes": 3, "sha256": "0" * 64}],
        },
        headers=idem(),
    )
    assert r.status_code == 403
    body = start_batch(client, seeded, {"mine.txt": filegen.txt()}, put=False)
    listing = client.get("/api/v1/batches").json()
    assert [b["id"] for b in listing["data"]] == [body["batch_id"]]
    sign_in(client, seeded, "dev-sender")
    assert (
        client.post(
            "/api/v1/batches", json={"department_id": str(seeded.departments["TAPELINE"]), "files": []}, headers=idem()
        ).status_code
        == 403
    )


def test_cancel_before_scan_leaves_file_unprocessed(client, seeded):
    sign_in(client, seeded, "dev-reviewer")
    files = {"n.txt": filegen.txt()}
    body = start_batch(client, seeded, files)
    complete_all(client, body, files)
    job = by_name(batch(client, body["batch_id"]))["n.txt"]["jobs"]["scan"]
    r = client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=idem())
    assert r.status_code == 202 and r.json()["data"]["state"] == "CANCELLED"
    drain()
    f = by_name(batch(client, body["batch_id"]))["n.txt"]
    assert f["state"] == "QUARANTINED" and f["jobs"]["parse"] is None


def test_batch_create_replay_returns_same_batch_with_fresh_urls(client, seeded):
    sign_in(client, seeded, "dev-reviewer")
    data = filegen.txt()
    payload = {
        "department_id": str(seeded.departments["TAPELINE"]),
        "files": [{"name": "n.txt", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}],
    }
    headers = idem()
    a = client.post("/api/v1/batches", json=payload, headers=headers).json()["data"]
    b = client.post("/api/v1/batches", json=payload, headers=headers).json()["data"]
    assert a["batch_id"] == b["batch_id"] and b["uploads"][0]["put_url"]


def test_abandoned_uploads_expire(client, seeded, owner_engine):
    from app.ingestion.maintenance import expire_uploads

    sign_in(client, seeded, "dev-reviewer")
    body = start_batch(client, seeded, {"late.txt": filegen.txt()}, put=False)
    with owner_engine.begin() as conn:
        conn.execute(
            update(t.upload)
            .where(t.upload.c.id == uuid.UUID(body["uploads"][0]["id"]))
            .values(expires_at=func.now() - timedelta(minutes=1))
        )
    assert expire_uploads() >= 1
    assert by_name(batch(client, body["batch_id"]))["late.txt"]["state"] == "EXPIRED"
