"""Outbox and job ledger reliability (FR27, TC51): dispatch once, lease recovery, fencing, retries."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.db import tables as t
from app.db.engine import dispatcher_tx, tenant_tx
from app.jobs import ledger
from app.outbox import service as outbox
from workers import dispatcher, registry
from workers.runtime import PermanentError, RetryableError, run_one

pytestmark = pytest.mark.db


@pytest.fixture
def kinds():
    """Register throwaway handlers per test so tests never collide on job kinds."""
    added = []

    def add(fn):
        kind = f"test.{uuid.uuid4().hex[:8]}"
        registry.HANDLERS[kind] = fn
        added.append(kind)
        return kind

    yield add
    for k in added:
        registry.HANDLERS.pop(k, None)


def _job(tenant_id, job_id):
    with tenant_tx(tenant_id) as conn:
        return conn.execute(select(t.job).where(t.job.c.id == job_id)).one()


def _new_job(tenant_id, kind, max_attempts=5):
    with tenant_tx(tenant_id) as conn:
        return ledger.create_job(
            conn, tenant_id=tenant_id, kind=kind, object_id=uuid.uuid4(), max_attempts=max_attempts
        )


def test_outbox_dispatch_creates_one_job_and_worker_completes_it(seeded):
    obj = uuid.uuid4()
    with tenant_tx(seeded.tenant_id) as conn:
        assert outbox.enqueue(conn, tenant_id=seeded.tenant_id, event_type="system.ping",
                              event_key=f"ping:{obj}", payload={"object_id": str(obj)})  # fmt: skip
        # Redelivery of the same event is a no-op.
        assert not outbox.enqueue(conn, tenant_id=seeded.tenant_id, event_type="system.ping",
                                  event_key=f"ping:{obj}", payload={"object_id": str(obj)})  # fmt: skip
    assert "system.noop" in dispatcher.dispatch_batch()
    assert dispatcher.dispatch_batch() == set()  # already dispatched
    while run_one(["system.noop"], "w1"):
        pass
    with tenant_tx(seeded.tenant_id) as conn:
        jobs = conn.execute(select(t.job).where(t.job.c.object_id == obj)).all()
    assert len(jobs) == 1 and jobs[0].state == "SUCCEEDED" and jobs[0].result == {"echo": str(obj)}


def test_create_job_is_idempotent(seeded):
    obj = uuid.uuid4()
    with tenant_tx(seeded.tenant_id) as conn:
        a = ledger.create_job(conn, tenant_id=seeded.tenant_id, kind="system.noop", object_id=obj)
        b = ledger.create_job(conn, tenant_id=seeded.tenant_id, kind="system.noop", object_id=obj)
    assert a == b


def test_expired_lease_is_reclaimed_and_stale_worker_is_fenced(seeded, kinds):
    kind = kinds(lambda ctx: registry.Outcome())
    job_id = _new_job(seeded.tenant_id, kind)
    with dispatcher_tx() as conn:
        first = ledger.claim_next(conn, [kind], "crashed-worker")
    # Simulate a crash: the lease runs out without a heartbeat.
    with dispatcher_tx() as conn:
        conn.execute(update(t.job).where(t.job.c.id == job_id).values(lease_until=func.now() - timedelta(minutes=5)))
        second = ledger.claim_next(conn, [kind], "new-worker")
    assert second is not None and second.job_id == job_id and second.attempt_no == 2
    assert second.lease_token != first.lease_token

    with pytest.raises(ledger.StaleLease), tenant_tx(seeded.tenant_id) as conn:  # old worker wakes up
        ledger.complete(conn, first)
    with tenant_tx(seeded.tenant_id) as conn:
        ledger.complete(conn, second, result={"by": "new-worker"})
    job = _job(seeded.tenant_id, job_id)
    assert job.state == "SUCCEEDED" and job.result == {"by": "new-worker"}
    with tenant_tx(seeded.tenant_id) as conn:
        outcomes = conn.execute(select(t.job_attempt.c.outcome).where(t.job_attempt.c.job_id == job_id)
                                .order_by(t.job_attempt.c.attempt_no)).scalars().all()  # fmt: skip
    assert outcomes == ["LEASE_EXPIRED", "SUCCEEDED"]


def test_transient_failures_back_off_then_dead_letter(seeded, kinds):
    def flaky(ctx):
        raise RetryableError("PROVIDER_TIMEOUT", "timed out")

    kind = kinds(flaky)
    job_id = _new_job(seeded.tenant_id, kind, max_attempts=2)
    assert run_one([kind], "w")
    job = _job(seeded.tenant_id, job_id)
    assert job.state == "RETRY_WAIT" and job.error_code == "PROVIDER_TIMEOUT" and job.lease_token is None
    assert not run_one([kind], "w")  # backoff: not due yet
    with dispatcher_tx() as conn:
        conn.execute(update(t.job).where(t.job.c.id == job_id).values(next_attempt_at=t.job.c.created_at))
    assert run_one([kind], "w")
    job = _job(seeded.tenant_id, job_id)
    assert job.state == "FAILED" and job.attempts == 2 and job.retryable  # a person may retry later


def test_permanent_failure_is_not_retried(seeded, kinds):
    def poison(ctx):
        raise PermanentError("ENCRYPTED_DOCUMENT", "Password-protected file")

    kind = kinds(poison)
    job_id = _new_job(seeded.tenant_id, kind)
    run_one([kind], "w")
    job = _job(seeded.tenant_id, job_id)
    assert job.state == "FAILED" and job.attempts == 1 and not job.retryable


def test_error_messages_are_redacted(seeded, kinds):
    def leaky(ctx):
        raise PermanentError("BAD", "failed GET https://s3/x?X-Amz-Signature=deadbeef")

    kind = kinds(leaky)
    job_id = _new_job(seeded.tenant_id, kind)
    run_one([kind], "w")
    assert "deadbeef" not in _job(seeded.tenant_id, job_id).error_message


def test_cancel_queued_job(seeded, kinds):
    kind = kinds(lambda ctx: registry.Outcome())
    job_id = _new_job(seeded.tenant_id, kind)
    with tenant_tx(seeded.tenant_id) as conn:
        assert ledger.request_cancel(conn, job_id) == "CANCELLED"
    assert not run_one([kind], "w")
    assert _job(seeded.tenant_id, job_id).state == "CANCELLED"


def test_ensure_job_coalesces_until_started_then_opens_next_generation(seeded, kinds):
    kind = kinds(lambda ctx: registry.Outcome())
    obj = uuid.uuid4()
    with tenant_tx(seeded.tenant_id) as conn:
        a = ledger.ensure_job(conn, tenant_id=seeded.tenant_id, kind=kind, object_id=obj)
        b = ledger.ensure_job(conn, tenant_id=seeded.tenant_id, kind=kind, object_id=obj)
    assert a == b  # not started yet: one job covers both requests
    with dispatcher_tx() as conn:
        claim = ledger.claim_next(conn, [kind], "w1")
    assert claim.job_id == a
    with tenant_tx(seeded.tenant_id) as conn:  # a change arriving while it runs gets a follow-up run
        c = ledger.ensure_job(conn, tenant_id=seeded.tenant_id, kind=kind, object_id=obj, delay_seconds=0)
    assert c != a and _job(seeded.tenant_id, c).generation == 2


def test_single_writer_per_object(seeded, kinds):
    kind = kinds(lambda ctx: registry.Outcome())
    obj = uuid.uuid4()
    with tenant_tx(seeded.tenant_id) as conn:
        first = ledger.create_job(conn, tenant_id=seeded.tenant_id, kind=kind, object_id=obj, generation=1)
        second = ledger.create_job(conn, tenant_id=seeded.tenant_id, kind=kind, object_id=obj, generation=2)
    with dispatcher_tx() as conn:
        claim = ledger.claim_next(conn, [kind], "w1")
    assert claim.job_id == first
    with dispatcher_tx() as conn:  # generation 2 waits while generation 1 holds a live lease
        assert ledger.claim_next(conn, [kind], "w2") is None
    with tenant_tx(seeded.tenant_id) as conn:
        ledger.complete(conn, claim)
    with dispatcher_tx() as conn:
        assert ledger.claim_next(conn, [kind], "w2").job_id == second
