"""Backup and restore drill for the local stack (FR29, TC54).

    python infra/local/restore_drill.py [--keep]

1. Logical backup of the authoritative database (pg_dump, custom format) inside the Postgres container.
2. Restore into an isolated database `production_restore_drill` on the same server.
3. Replay the retention deletion manifest against the restored copy (deleted content is not resurrected).
4. Verify: row counts, every READY report PDF present in object storage with its recorded checksum, and the list
   of email intents that must be reconciled with provider evidence before sends are re-enabled.
5. Record timings (backup, restore, verification = measured RTO for this data volume) as JSON evidence under
   docs/evidence/.

Local limits: the object store is shared with the live stack (checked read-only; production uses versioned,
off-site copies), and logical dumps give an RPO equal to the dump interval. Production RPO <= 15 minutes needs
continuous WAL archiving / point-in-time recovery from the managed database service.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTAINER = os.environ.get("PG_CONTAINER", "prodauto-postgres-1")
SOURCE_DB, DRILL_DB, OWNER = "production", "production_restore_drill", "prod_owner"
PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def sh(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def psql(sql: str, db: str = "postgres") -> str:
    return sh("docker", "exec", CONTAINER, "psql", "-U", OWNER, "-d", db, "-v", "ON_ERROR_STOP=1", "-tAc", sql)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep the restored database for inspection")
    args = parser.parse_args()
    started = datetime.now(UTC)
    t0 = time.monotonic()
    dump = "/tmp/drill.dump"
    sh("docker", "exec", CONTAINER, "pg_dump", "-U", OWNER, "-Fc", "-f", dump, SOURCE_DB)
    dump_bytes = int(sh("docker", "exec", CONTAINER, "stat", "-c", "%s", dump).strip())
    t_backup = time.monotonic()

    psql(f"DROP DATABASE IF EXISTS {DRILL_DB}")
    psql(f"CREATE DATABASE {DRILL_DB} OWNER {OWNER}")
    sh("docker", "exec", CONTAINER, "pg_restore", "-U", OWNER, "-d", DRILL_DB, "--exit-on-error", dump)
    t_restore = time.monotonic()

    url = f"postgresql+psycopg://{OWNER}:prod_owner_dev@localhost:5433/{DRILL_DB}"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "services/api"), str(ROOT / "services")])}
    replay = subprocess.run([str(PYTHON), "-m", "app.cli", "retention-replay", "--url", url], cwd=ROOT, env=env,
                            capture_output=True, text=True, check=True).stdout.strip()  # fmt: skip
    verify = subprocess.run([str(PYTHON), "-m", "app.cli", "verify-restore", "--url", url], cwd=ROOT, env=env,
                            capture_output=True, text=True)  # fmt: skip
    t_verify = time.monotonic()
    report = json.loads(verify.stdout)
    live_records = int(psql("SELECT count(*) FROM production_record", SOURCE_DB).strip())

    evidence = {
        "drill_started_at": started.isoformat(timespec="seconds"),
        "source_database": SOURCE_DB, "restored_database": DRILL_DB, "dump_bytes": dump_bytes,
        "seconds": {"backup": round(t_backup - t0, 2), "restore": round(t_restore - t_backup, 2),
                    "verify": round(t_verify - t_restore, 2), "total_rto": round(t_verify - t0, 2)},
        "live_production_records": live_records,
        "restored": report,
        "counts_match_live": report["counts"]["production_records"] == live_records,
        "manifest_replay": replay,
        "result": "PASS" if report["ok"] and report["counts"]["production_records"] == live_records else "FAIL",
        "before_enabling_sends": "Reconcile every QUEUED/SENDING/UNKNOWN intent listed in sends_to_reconcile with "
                                 "provider evidence (Sent Items / message trace); keep SENDS_PAUSED=true until done.",
    }  # fmt: skip
    out = ROOT / "docs" / "evidence" / f"restore-drill-{started:%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    if not args.keep:
        psql(f"DROP DATABASE IF EXISTS {DRILL_DB}")
    sh("docker", "exec", CONTAINER, "rm", "-f", dump)
    print(json.dumps({k: evidence[k] for k in ("seconds", "counts_match_live", "result")}, indent=2))
    print(f"evidence: {out.relative_to(ROOT)}")
    return 0 if evidence["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
