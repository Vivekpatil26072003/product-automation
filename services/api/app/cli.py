"""Operational commands: python -m app.cli {migrate,seed-demo,openapi,ensure-bucket,evaluate,promote,bi-access}."""

import argparse
import json
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.core.config import get_settings
from app.db.engine import engine_for

ROOT = Path(__file__).resolve().parents[3]
DEMO_TENANT = "Demo Manufacturing (illustrative data)"


def alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(ROOT / "services/api/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "services/api/migrations"))
    if url:
        cfg.cmd_opts = argparse.Namespace(x=[f"url={url}"])
    return cfg


def migrate(url: str | None = None) -> None:
    command.upgrade(alembic_config(url), "head")


def seed_demo() -> None:
    from app.seed.demo import seed_tenant, tenant_exists

    with engine_for(get_settings().database_owner_url).begin() as conn:
        if tenant_exists(conn, DEMO_TENANT):
            print(f"'{DEMO_TENANT}' already exists; nothing to do.")
            return
        res = seed_tenant(conn, DEMO_TENANT)
    print(
        f"Seeded tenant {res.tenant_id}: {len(res.departments)} departments, {len(res.machines)} machines, "
        f"{len(res.records)} F1 records. Dev sign-in subjects: {', '.join(sorted(res.users))}"
    )


def export_openapi(path: Path) -> None:
    from app.main import create_app

    path.write_text(json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def _tenant_id(conn, name: str):
    from sqlalchemy import select

    from app.db import tables as t

    tenant = conn.execute(select(t.tenant.c.id).where(t.tenant.c.name == name)).scalar_one_or_none()
    if tenant is None:
        raise SystemExit(f"tenant {name!r} not found")
    return tenant


def evaluate_cmd(manifest: Path, extractor: str, record: bool, tenant: str) -> int:
    """FR28: score an extractor on a gold set; optionally record the run (and the AI release it measured)."""
    from app.evaluation import releases
    from app.evaluation.harness import evaluate

    ai = None
    model = prompt = None
    if extractor == "claude":
        from app.extraction.claude import ClaudeExtractor

        claude = ClaudeExtractor()
        model, prompt = claude.model, claude.prompt_hash
        ai = lambda spans, sid: claude.extract(spans, "DMY", sid)  # noqa: E731
    report = evaluate(manifest, ai_extract=ai)
    print(json.dumps({k: report[k] for k in ("dataset", "documents", "metrics", "gates", "passed")}, indent=2))
    if record:
        with engine_for(get_settings().database_owner_url).begin() as conn:
            run_id, release_id = releases.record_run(
                conn,
                _tenant_id(conn, tenant),
                report,
                extractor="claude-extract" if extractor == "claude" else "deterministic",
                model=model,
                prompt_hash=prompt,
            )
        print(f"recorded evaluation run {run_id}")
        if release_id:
            print(
                f"release {release_id} ({'passed: promote with' if report['passed'] else 'failed: cannot be promoted'}"
                f" python -m app.cli promote {release_id})"
            )
    return 0 if report["passed"] else 1


def promote_cmd(release_id: str, tenant: str) -> int:
    import uuid

    from app.evaluation import releases

    with engine_for(get_settings().database_owner_url).begin() as conn:
        try:
            releases.promote(conn, _tenant_id(conn, tenant), uuid.UUID(release_id), None)
        except releases.PromotionRefused as exc:
            print(f"refused: {exc}")
            return 1
    print("release approved")
    return 0


def bi_access_cmd(role: str, tenant: str, revoke: bool) -> int:
    """Password comes from BI_ROLE_PASSWORD so it never appears in shell history or process lists."""
    import os

    from app.integrations import bi_access

    with engine_for(get_settings().database_owner_url).begin() as conn:
        tenant_id = _tenant_id(conn, tenant)
        if revoke:
            bi_access.revoke(conn, role, tenant_id)
            print(f"{role} can no longer read {tenant!r}")
            return 0
        try:
            created = bi_access.grant(conn, role, os.environ.get("BI_ROLE_PASSWORD"), tenant_id)
        except ValueError as exc:
            print(f"refused: {exc}")
            return 1
    print(f"{'created' if created else 'updated'} {role}: read-only access to {tenant!r} via approved_production_v")
    return 0


def ops_cmd(cmd: str, args: argparse.Namespace) -> int:
    """Operator procedures (M8). All use the owner connection; see docs/runbooks/operations.md."""
    from app.ops import restore, retention

    owner = engine_for(get_settings().database_owner_url)
    if cmd == "retention-replay":
        with engine_for(args.url or get_settings().database_owner_url).begin() as conn:
            n = retention.replay_manifest(conn)
        print(f"deletion manifest replayed: {n} object keys deleted again (missing ones are fine)")
        return 0
    if cmd == "purge-logs":
        print(json.dumps(restore.purge_logs(owner, args.days)))
        return 0
    if cmd == "verify-restore":
        report = restore.verify(engine_for(args.url))
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    raise SystemExit(f"unknown command {cmd}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("seed-demo")
    sub.add_parser("ensure-bucket")
    ev = sub.add_parser("evaluate", help="score an extractor on a gold set (FR28)")
    ev.add_argument("--manifest", type=Path, default=ROOT / "tests/ai_eval/gold_synthetic.json")
    ev.add_argument("--extractor", choices=["deterministic", "claude"], default="deterministic")
    ev.add_argument("--record", action="store_true", help="store the run and register the measured release")
    ev.add_argument("--tenant", default=DEMO_TENANT)
    pr = sub.add_parser("promote", help="approve an AI release whose evaluation passed (FR28)")
    pr.add_argument("release_id")
    pr.add_argument("--tenant", default=DEMO_TENANT)
    bi = sub.add_parser("bi-access", help="read-only Power BI database login for one company (FR15)")
    bi.add_argument("--role", required=True, help="bi_<name>; password from BI_ROLE_PASSWORD")
    bi.add_argument("--tenant", default=DEMO_TENANT)
    bi.add_argument("--revoke", action="store_true")
    rp = sub.add_parser("retention-replay", help="after a restore: delete again every object in the deletion manifest")
    rp.add_argument("--url", default=None, help="owner URL of the restored database (default: configured owner URL)")
    pl = sub.add_parser("purge-logs", help="delete operational records older than the log retention (FR25)")
    pl.add_argument("--days", type=int, default=30)
    vr = sub.add_parser("verify-restore", help="check a restored database against object storage (FR29)")
    vr.add_argument("--url", required=True, help="owner URL of the restored database")
    p = sub.add_parser("openapi")
    p.add_argument("--out", type=Path, default=ROOT / "packages/contracts/openapi.json")
    args = parser.parse_args(argv)

    if args.cmd == "migrate":
        migrate()
    elif args.cmd == "seed-demo":
        seed_demo()
    elif args.cmd == "ensure-bucket":
        from app.storage.objects import get_storage

        get_storage().ensure_bucket()
        print("bucket ready")
    elif args.cmd == "evaluate":
        return evaluate_cmd(args.manifest, args.extractor, args.record, args.tenant)
    elif args.cmd == "promote":
        return promote_cmd(args.release_id, args.tenant)
    elif args.cmd == "bi-access":
        return bi_access_cmd(args.role, args.tenant, args.revoke)
    elif args.cmd in ("retention-replay", "purge-logs", "verify-restore"):
        return ops_cmd(args.cmd, args)
    elif args.cmd == "openapi":
        export_openapi(args.out)


if __name__ == "__main__":
    sys.exit(main())
