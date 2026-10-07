"""Pilot load test (TC55, spec §14 "Quality targets") against an isolated database.

    python infra/local/perf/load_test.py [--records 100000] [--users 20] [--seconds 120]

- Creates `production_perf`, migrates it, seeds a company with N approved records spread over a year.
- Starts its own API (port 8010) and a report worker against that database; the live stack is not touched.
- 20 concurrent signed-in users loop over the records list (default window, 30 days, next page) and the
  dashboard (30 and 90 days). Reports p50/p95/p99 and errors per endpoint against the targets:
  list p95 <= 500 ms, dashboard p95 <= 1 s. Then measures one ~1,000-record report render (target p95 <= 30 s).
- Writes docs/evidence/load-test-<timestamp>.json. The spec's 30-minute run is a staging procedure; the local
  run is shorter and on a developer laptop, so results indicate headroom, not production capacity.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "services/api"), str(ROOT / "services")]
CONTAINER = os.environ.get("PG_CONTAINER", "prodauto-postgres-1")
DB = "production_perf"
OWNER_URL = f"postgresql+psycopg://prod_owner:prod_owner_dev@localhost:5433/{DB}"
APP_URL = f"postgresql+psycopg://prod_app:prod_app_dev@localhost:5433/{DB}"
PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
PORT = 8010
BASE = f"http://127.0.0.1:{PORT}/api/v1"

SEED_SQL = """
CREATE TEMP TABLE g AS
WITH ms AS (SELECT array_agg(id ORDER BY code) AS ids, array_agg(department_id ORDER BY code) AS deps, count(*) AS n
            FROM machine WHERE tenant_id = :t)
SELECT i, gen_random_uuid() AS rid, gen_random_uuid() AS vid, (DATE '2026-09-28' - (i % 365)) AS d,
       ms.ids[1 + (i % ms.n)] AS machine_id, ms.deps[1 + (i % ms.n)] AS dept,
       CASE WHEN i % 3 = 0 THEN 'kg' ELSE 'm' END AS unit
FROM generate_series(1, :n) AS i, ms;
INSERT INTO production_record (id, tenant_id, department_id, current_revision_id, production_date, created_by)
SELECT rid, :t, dept, vid, d, :u FROM g;
INSERT INTO record_revision (id, tenant_id, record_id, number, production_date, department_id, machine_id,
  operator_name, production_qty, target_qty, unit, status, stop_minutes, approval_state, created_by, approved_by,
  approved_at)
SELECT vid, :t, rid, 1, d, dept, machine_id, 'Operator ' || (i % 40), 400 + (i % 900), 1000, unit,
       (ARRAY['RUNNING','COMPLETED','PENDING','HOLD'])[1 + (i % 4)], i % 90, 'APPROVED', :u, :u, now()
FROM g;
UPDATE tenant SET data_version = data_version + 1 WHERE id = :t;
"""


def psql(sql: str) -> None:
    subprocess.run(["docker", "exec", CONTAINER, "psql", "-U", "prod_owner", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                    "-c", sql], check=True, capture_output=True)  # fmt: skip


def prepare(records: int) -> tuple[str, str]:
    from sqlalchemy import create_engine, text

    from app.cli import migrate
    from app.seed.demo import seed_tenant

    psql(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
    psql(f"CREATE DATABASE {DB} OWNER prod_owner")
    migrate(OWNER_URL)
    engine = create_engine(OWNER_URL)
    with engine.begin() as conn:
        res = seed_tenant(conn, "Perf company", subject_prefix="perf-", with_f1=False)
        params = {"t": res.tenant_id, "u": res.users["dev-reviewer"], "n": records}
        for statement in (x.strip() for x in SEED_SQL.split(";")):
            if statement:
                conn.execute(text(statement), {k: v for k, v in params.items() if f":{k}" in statement})
        conn.execute(text("ANALYZE"))
    engine.dispose()
    return str(res.tenant_id), "perf-dev-reviewer"


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))] * 1000, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=int, default=100_000)
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--seconds", type=int, default=120)
    args = ap.parse_args()
    started = datetime.now(UTC)
    t_seed = time.monotonic()
    _, subject = prepare(args.records)
    seed_seconds = round(time.monotonic() - t_seed, 1)

    env = {**os.environ, "DATABASE_URL": APP_URL, "DATABASE_OWNER_URL": OWNER_URL,
           "PYTHONPATH": os.pathsep.join([str(ROOT / "services/api"), str(ROOT / "services")])}  # fmt: skip
    api = subprocess.Popen([str(PYTHON), "-m", "uvicorn", "app.main:app", "--port", str(PORT), "--workers", "4",
                            "--log-level", "warning"], cwd=ROOT, env=env)  # fmt: skip
    worker = subprocess.Popen([str(PYTHON), "-m", "workers", "--kinds", "report.render"], cwd=ROOT, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{BASE}/health/live", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(1)
        results: dict[str, list[float]] = {}
        errors: dict[str, int] = {}
        lock = threading.Lock()
        stop = time.monotonic() + args.seconds

        def user() -> None:
            with httpx.Client(base_url=BASE, timeout=30) as c:
                r = c.post("/auth/dev-login", json={"subject": subject})
                r.raise_for_status()
                first = c.get("/records").json()
                cursor = first.get("next_cursor")
                mix = [("records_default", "/records"),
                       ("records_30d", "/records?date_from=2026-08-30&date_to=2026-09-28&size=50"),
                       ("records_next_page", f"/records?cursor={cursor}" if cursor else "/records"),
                       ("dashboard_30d", "/dashboard?date_from=2026-08-30&date_to=2026-09-28"),
                       ("dashboard_90d", "/dashboard?date_from=2026-07-01&date_to=2026-09-28")]  # fmt: skip
                i = 0
                while time.monotonic() < stop:
                    name, path = mix[i % len(mix)]
                    i += 1
                    t0 = time.perf_counter()
                    try:
                        ok = c.get(path).status_code == 200
                    except httpx.HTTPError:
                        ok = False
                    dt = time.perf_counter() - t0
                    with lock:
                        results.setdefault(name, []).append(dt)
                        if not ok:
                            errors[name] = errors.get(name, 0) + 1

        threads = [threading.Thread(target=user) for _ in range(args.users)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        endpoints = {name: {"requests": len(v), "errors": errors.get(name, 0), "p50_ms": pct(v, 50),
                            "p95_ms": pct(v, 95), "p99_ms": pct(v, 99)} for name, v in sorted(results.items())}  # fmt: skip
        list_p95 = max(e["p95_ms"] for k, e in endpoints.items() if k.startswith("records"))
        dash_p95 = max(e["p95_ms"] for k, e in endpoints.items() if k.startswith("dashboard"))

        with httpx.Client(base_url=BASE, timeout=60) as c:  # one ~1,000-record report (4 days of data)
            login = c.post("/auth/dev-login", json={"subject": "perf-dev-sender"})
            login.raise_for_status()
            csrf = login.json()["data"]["csrf_token"]
            t0 = time.monotonic()
            r = c.post("/reports", headers={"X-CSRF-Token": csrf, "Idempotency-Key": os.urandom(8).hex()},
                       json={"filter": {"date_from": "2026-09-25", "date_to": "2026-09-28"}, "include_detail": True})
            report_id = r.json()["data"]["report_id"]
            state, rows = "QUEUED", 0
            while time.monotonic() - t0 < 180 and state not in ("READY", "FAILED"):
                time.sleep(0.5)
                data = c.get(f"/reports/{report_id}").json()["data"]
                state, rows = data["state"], data["record_count"]
            report_seconds = round(time.monotonic() - t0, 2)

        evidence = {
            "run_started_at": started.isoformat(timespec="seconds"),
            "dataset": {"records": args.records, "seed_seconds": seed_seconds},
            "workload": {"concurrent_users": args.users, "seconds": args.seconds, "api_processes": 4},
            "endpoints": endpoints,
            "targets": {"records_list_p95_ms": {"target": 500, "measured": list_p95, "met": list_p95 <= 500},
                        "dashboard_p95_ms": {"target": 1000, "measured": dash_p95, "met": dash_p95 <= 1000},
                        "report_seconds": {"target": 30, "records": rows, "state": state, "measured": report_seconds,
                                           "met": state == "READY" and report_seconds <= 30}},
            "environment": "developer laptop, Docker Desktop (WSL 2), PostgreSQL 16 container; not production sizing",
        }  # fmt: skip
        out = ROOT / "docs" / "evidence" / f"load-test-{started:%Y%m%dT%H%M%SZ}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence["targets"], indent=2))
        print(f"evidence: {out.relative_to(ROOT)}")
        return 0 if all(v["met"] for v in evidence["targets"].values()) else 1
    finally:
        for proc in (api, worker):  # uvicorn --workers spawns children: stop the whole tree
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
            else:
                proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
