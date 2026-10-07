"""M7 automation on the real stack (FR24, A1-A3; TC44-TC47, TC58 scheduled part).

Clock-dependent behaviour is driven by calling the claim with explicit instants instead of sleeping.
Email goes to the imitation Microsoft Graph from tests/fake_providers.py.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text, update

from app import integrations
from app.automation import exceptions as exc_engine
from app.automation import reminders
from app.automation import schedules as sched
from app.db import tables as t
from app.db.engine import tenant_tx
from tests.api.test_insights import add_record
from tests.conftest import idem, sign_in
from tests.fake_providers import FakeProviders
from tests.integration.test_reports_email import connect_mail
from workers import automation, dispatcher
from workers.runtime import run_one

pytestmark = [pytest.mark.db, pytest.mark.infra]
KINDS = [
    "schedule.run",
    "report.render",
    "reports.invalidate",
    "email.send",
    "integration.test",
    "integrations.fanout",
    "notification.email",
    "automation.tick",
    "export.render",
]


@pytest.fixture
def fake(owner_engine):
    with owner_engine.begin() as conn:
        conn.execute(
            update(t.job)
            .where(t.job.c.kind.in_(KINDS), t.job.c.state.in_(("QUEUED", "RETRY_WAIT")))
            .values(state="CANCELLED")
        )
    f = FakeProviders()
    integrations.set_transport(f.transport)
    yield f
    integrations.set_transport(None)


def drain(owner_engine, tenant_id, rounds: int = 12) -> None:
    """Run jobs, making delayed follow-ups due, until nothing is left for this company."""
    for _ in range(rounds):
        dispatcher.dispatch_batch()
        while run_one(KINDS, "test-worker"):
            pass
        with owner_engine.begin() as conn:
            waiting = conn.execute(
                update(t.job)
                .where(
                    t.job.c.tenant_id == tenant_id,
                    t.job.c.kind.in_(KINDS),
                    t.job.c.state.in_(("QUEUED", "RETRY_WAIT")),
                    t.job.c.next_attempt_at > text("now()"),
                )
                .values(next_attempt_at=text("now()"))
                .returning(t.job.c.id)
            ).all()
        if not waiting:
            dispatcher.dispatch_batch()
            while run_one(KINDS, "test-worker"):
                pass
            return


def set_flags(client, seeded, **flags) -> None:
    sign_in(client, seeded, "dev-admin")
    s = client.get("/api/v1/settings")
    body = {"feature_flags": s.json()["data"]["feature_flags"] | flags}
    r = client.patch("/api/v1/settings", headers={**idem(), "If-Match": s.headers["ETag"]}, json=body)
    assert r.status_code == 200, r.text


def patch_settings(client, seeded, body) -> None:
    sign_in(client, seeded, "dev-admin")
    s = client.get("/api/v1/settings")
    r = client.patch("/api/v1/settings", headers={**idem(), "If-Match": s.headers["ETag"]}, json=body)
    assert r.status_code == 200, r.text


DAILY = {
    "name": "Daily m",
    "cadence": "DAILY",
    "local_time": "07:00",
    "timezone": "Asia/Kolkata",
    "units": ["m"],
    "to": ["plant.manager@contoso.example"],
}


def create_schedule(client, seeded, **over) -> dict:
    sign_in(client, seeded, "dev-sender")
    r = client.post("/api/v1/schedules", headers=idem(), json=DAILY | over)
    assert r.status_code == 201, r.text
    return r.json()["data"]


def claim(owner_engine, seeded, schedule_id, last_due: datetime, now: datetime) -> list:
    with owner_engine.begin() as conn:
        conn.execute(update(t.schedule).where(t.schedule.c.id == uuid.UUID(schedule_id)).values(last_due_at=last_due))
    with tenant_tx(seeded.tenant_id) as conn:
        row = conn.execute(select(t.schedule).where(t.schedule.c.id == uuid.UUID(schedule_id)).with_for_update()).one()
        return sched.claim_due(conn, row, now)


def runs(seeded, schedule_id) -> list:
    with tenant_tx(seeded.tenant_id) as conn:
        return conn.execute(
            select(t.schedule_run)
            .where(t.schedule_run.c.schedule_id == uuid.UUID(schedule_id))
            .order_by(t.schedule_run.c.due_at)
        ).all()


# F1 is on 27 Sept 2026; the 07:00 IST run on 28 Sept (01:30 UTC) reports it.
DUE_28 = datetime(2026, 9, 28, 1, 30, tzinfo=UTC)


# --- schedules ----------------------------------------------------------------------------------------------


def test_scheduling_is_an_optional_module(client, seeded, fake):
    sign_in(client, seeded, "dev-sender")
    r = client.get("/api/v1/schedules")
    assert r.status_code == 404 and r.json()["error"]["code"] == "FEATURE_DISABLED"
    set_flags(client, seeded, scheduling=True)
    sign_in(client, seeded, "dev-sender")
    assert client.get("/api/v1/schedules").json()["data"] == []
    sign_in(client, seeded, "dev-reviewer")
    assert client.get("/api/v1/schedules").status_code == 403  # Senders only


def test_create_validates_and_previews_next_runs(client, seeded, fake):  # TC44
    set_flags(client, seeded, scheduling=True)
    sign_in(client, seeded, "dev-sender")
    bad = client.post("/api/v1/schedules", headers=idem(), json=DAILY | {"cadence": "WEEKLY"})
    assert bad.status_code == 422
    bad = client.post("/api/v1/schedules", headers=idem(), json=DAILY | {"timezone": "Mars/Olympus"})
    assert bad.status_code == 422
    bad = client.post("/api/v1/schedules", headers=idem(), json=DAILY | {"to": ["not-an-address"]})
    assert bad.status_code == 422
    data = create_schedule(client, seeded, mode="AUTO_SEND")
    assert data["version"] == 1 and data["approval_state"] == "UNAPPROVED" and len(data["next_runs"]) == 3
    first = data["next_runs"][0]
    assert first["period_start"] == first["period_end"]  # previous completed local day
    p = client.get("/api/v1/schedules/preview?cadence=MONTHLY&local_time=06:00&monthday=31&timezone=UTC").json()
    assert all(x["due_at"].endswith("06:00:00+00:00") for x in p["data"])


def test_claim_is_unique_and_missed_runs_are_recorded(client, seeded, fake, owner_engine):  # TC45, TC46
    set_flags(client, seeded, scheduling=True)
    s = create_schedule(client, seeded)
    created = claim(owner_engine, seeded, s["id"], DUE_28 - timedelta(hours=1), DUE_28 + timedelta(minutes=5))
    assert len(created) == 1
    # A restarted scheduler that claims the same window again creates nothing.
    assert claim(owner_engine, seeded, s["id"], DUE_28 - timedelta(hours=1), DUE_28 + timedelta(minutes=5)) == []
    rows = runs(seeded, s["id"])
    assert len(rows) == 1 and (rows[0].period_start, rows[0].state) == (date(2026, 9, 27), "QUEUED")

    # Down for two days: only the latest occurrence runs; older ones are recorded, not run.
    later = claim(owner_engine, seeded, s["id"], DUE_28 + timedelta(minutes=5), DUE_28 + timedelta(days=2, hours=1))
    assert len(later) == 2
    states = {r.period_start: r.state for r in runs(seeded, s["id"])}
    assert states[date(2026, 9, 28)] == "SKIPPED_MISSED" and states[date(2026, 9, 29)] == "QUEUED"


def test_draft_only_run_prepares_the_report_and_draft(client, seeded, fake, owner_engine):  # TC44
    set_flags(client, seeded, scheduling=True)
    connect_mail(client, seeded)
    s = create_schedule(client, seeded)
    claim(owner_engine, seeded, s["id"], DUE_28 - timedelta(hours=1), DUE_28 + timedelta(minutes=5))
    drain(owner_engine, seeded.tenant_id)
    run = runs(seeded, s["id"])[0]
    assert run.state == "DRAFTED" and run.report_id and run.draft_id and run.email_id is None
    with tenant_tx(seeded.tenant_id) as conn:
        report = conn.execute(select(t.report).where(t.report.c.id == run.report_id)).one()
        draft = conn.execute(select(t.email_draft).where(t.email_draft.c.id == run.draft_id)).one()
    assert report.record_count == 5 and report.metrics_json["metrics"][0]["production_qty"] == "4830.000"
    assert [r["address"] for r in draft.recipients] == ["plant.manager@contoso.example"] and draft.state == "DRAFT"
    assert fake.sent == []  # draft-only never sends


def approve(client, schedule) -> None:
    r = client.post(
        f"/api/v1/schedules/{schedule['id']}/approve-auto-send",
        headers=idem(),
        json={
            "version": schedule["version"],
            "confirmed_policy_hash": schedule["policy"]["hash"],
            "confirmation": True,
        },
    )
    assert r.status_code == 200, r.text


def test_approved_auto_send_sends_once_and_edits_revoke_approval(client, seeded, fake, owner_engine):  # TC47
    set_flags(client, seeded, scheduling=True, auto_send=True)
    connect_mail(client, seeded)
    s = create_schedule(client, seeded, mode="AUTO_SEND")
    stale = client.post(
        f"/api/v1/schedules/{s['id']}/approve-auto-send",
        headers=idem(),
        json={"version": s["version"], "confirmed_policy_hash": "0" * 64, "confirmation": True},
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "STALE_POLICY"
    approve(client, s)
    claim(owner_engine, seeded, s["id"], DUE_28 - timedelta(hours=1), DUE_28 + timedelta(minutes=5))
    drain(owner_engine, seeded.tenant_id)
    run = runs(seeded, s["id"])[0]
    assert run.state == "SENT" and run.email_id is not None and len(fake.sent) == 1

    current = client.get(f"/api/v1/schedules/{s['id']}")
    edited = client.patch(
        f"/api/v1/schedules/{s['id']}",
        headers={**idem(), "If-Match": current.headers["ETag"]},
        json={"to": ["someone.else@contoso.example"]},
    ).json()["data"]
    assert edited["version"] == 2 and edited["approval_state"] == "UNAPPROVED"
    assert edited["auto_send_blocked_reason"] == "AUTO_SEND_NOT_APPROVED"
    due_29 = DUE_28 + timedelta(days=1)
    add_record(owner_engine, seeded, day="2026-09-28", qty="100", target="100")
    claim(owner_engine, seeded, s["id"], due_29 - timedelta(hours=1), due_29 + timedelta(minutes=5))
    drain(owner_engine, seeded.tenant_id)
    second = [r for r in runs(seeded, s["id"]) if r.version == 2][0]
    assert second.state == "DRAFTED" and "AUTO_SEND_NOT_APPROVED" in second.note and len(fake.sent) == 1


def test_revoked_role_pauses_the_schedule_before_sending(client, seeded, fake, owner_engine):  # TC47
    set_flags(client, seeded, scheduling=True, auto_send=True)
    connect_mail(client, seeded)
    s = create_schedule(client, seeded, mode="AUTO_SEND")
    approve(client, s)
    claim(owner_engine, seeded, s["id"], DUE_28 - timedelta(hours=1), DUE_28 + timedelta(minutes=5))
    with owner_engine.begin() as conn:
        conn.execute(
            update(t.membership).where(t.membership.c.id == seeded.users["dev-sender"]).values(roles=["VIEWER"])
        )
    drain(owner_engine, seeded.tenant_id)
    run = runs(seeded, s["id"])[0]
    assert run.state == "FAILED" and run.error_code == "PERMISSION_REVOKED" and fake.sent == []
    with tenant_tx(seeded.tenant_id) as conn:
        row = conn.execute(select(t.schedule).where(t.schedule.c.id == uuid.UUID(s["id"]))).one()
    assert not row.active and row.paused_reason == "PERMISSION_REVOKED"


def test_empty_periods_skip_or_draft_but_never_email(client, seeded, fake, owner_engine):  # TC58
    set_flags(client, seeded, scheduling=True, auto_send=True)
    connect_mail(client, seeded)
    skip = create_schedule(client, seeded, name="Skip", units=["kg"])  # F1 has no kg records
    drafted = create_schedule(client, seeded, name="Draft", units=["kg"], empty_policy="DRAFT", mode="AUTO_SEND")
    approve(client, drafted)
    for s in (skip, drafted):
        claim(owner_engine, seeded, s["id"], DUE_28 - timedelta(hours=1), DUE_28 + timedelta(minutes=5))
    drain(owner_engine, seeded.tenant_id)
    assert runs(seeded, skip["id"])[0].state == "SKIPPED_EMPTY"
    d = runs(seeded, drafted["id"])[0]
    assert d.state == "DRAFTED" and "EMPTY_REPORT" in d.note and fake.sent == []


def test_overlapping_run_waits_then_fails_visibly(client, seeded, fake, owner_engine):
    set_flags(client, seeded, scheduling=True)
    s = create_schedule(client, seeded)
    claim(owner_engine, seeded, s["id"], DUE_28 - timedelta(hours=1), DUE_28 + timedelta(days=1, minutes=5))
    first, second = runs(seeded, s["id"])
    assert first.state == "SKIPPED_MISSED" or first.state == "QUEUED"
    with owner_engine.begin() as conn:  # an earlier run still active, and the later one is 3 hours overdue
        conn.execute(update(t.schedule_run).where(t.schedule_run.c.id == first.id).values(state="REPORTING"))
        conn.execute(
            update(t.schedule_run)
            .where(t.schedule_run.c.id == second.id)
            .values(due_at=text("now() - interval '3 hours'"))
        )
        conn.execute(update(t.job).where(t.job.c.object_id == first.id).values(state="CANCELLED"))
    drain(owner_engine, seeded.tenant_id, rounds=2)
    later = [r for r in runs(seeded, s["id"]) if r.id == second.id][0]
    assert later.state == "FAILED" and later.error_code == "OVERLAP"


def test_unknown_email_halts_the_schedule(client, seeded, fake, owner_engine):
    set_flags(client, seeded, scheduling=True, auto_send=True)
    connect_mail(client, seeded)
    s = create_schedule(client, seeded, mode="AUTO_SEND")
    approve(client, s)
    fake.mail_mode = "timeout_after_accept"
    claim(owner_engine, seeded, s["id"], DUE_28 - timedelta(hours=1), DUE_28 + timedelta(minutes=5))
    drain(owner_engine, seeded.tenant_id)
    run = runs(seeded, s["id"])[0]
    assert run.state == "HALTED" and len(fake.sent) == 1
    with tenant_tx(seeded.tenant_id) as conn:
        row = conn.execute(select(t.schedule).where(t.schedule.c.id == uuid.UUID(s["id"]))).one()
    assert not row.active and row.paused_reason == "UNKNOWN_SEND"


def test_run_now_is_draft_by_default_and_unique_per_period(client, seeded, fake, owner_engine):
    set_flags(client, seeded, scheduling=True)
    s = create_schedule(client, seeded, mode="AUTO_SEND")
    body = {"period_start": "2026-09-27", "period_end": "2026-09-27"}
    first = client.post(f"/api/v1/schedules/{s['id']}/run", headers=idem(), json=body)
    assert first.status_code == 202 and first.json()["data"]["mode"] == "DRAFT_ONLY"
    again = client.post(f"/api/v1/schedules/{s['id']}/run", headers=idem(), json=body)
    assert again.status_code == 200 and again.json()["data"]["id"] == first.json()["data"]["id"]
    auto = client.post(f"/api/v1/schedules/{s['id']}/run", headers=idem(), json=body | {"mode": "AUTO_SEND"})
    assert auto.status_code == 409  # not approved
    future = client.post(
        f"/api/v1/schedules/{s['id']}/run",
        headers=idem(),
        json={"period_start": "2099-01-01", "period_end": "2099-01-01"},
    )
    assert future.status_code == 422
    cancel = client.post(f"/api/v1/schedule-runs/{first.json()['data']['id']}/cancel", headers=idem())
    assert cancel.status_code == 202 and cancel.json()["data"]["state"] == "CANCELLED"
    drain(owner_engine, seeded.tenant_id)
    assert runs(seeded, s["id"])[0].report_id is None  # cancelled before any effect


def test_pause_blocks_claims(client, seeded, fake, owner_engine):
    set_flags(client, seeded, scheduling=True)
    s = create_schedule(client, seeded)
    current = client.get(f"/api/v1/schedules/{s['id']}")
    paused = client.patch(
        f"/api/v1/schedules/{s['id']}", headers={**idem(), "If-Match": current.headers["ETag"]}, json={"active": False}
    ).json()["data"]
    assert not paused["active"] and paused["version"] == 1 and paused["next_runs"] == []
    with owner_engine.begin() as conn:
        conn.execute(
            update(t.schedule)
            .where(t.schedule.c.id == uuid.UUID(s["id"]))
            .values(next_due_at=text("now() - interval '1 minute'"))
        )
    automation.enqueue_ticks()
    drain(owner_engine, seeded.tenant_id)
    assert runs(seeded, s["id"]) == []


# --- exceptions (A1) -------------------------------------------------------------------------------------------


def scan(seeded, day="2026-09-27"):
    with tenant_tx(seeded.tenant_id) as conn:
        return exc_engine.scan(conn, seeded.tenant_id, date.fromisoformat(day))


def test_exception_rules_open_resolve_and_respect_dismissal(client, seeded, fake, owner_engine):
    rid = add_record(
        owner_engine, seeded, day="2026-09-27", dept="WARPING", machine="W-01", qty="30", target="100", stop=300
    )
    result = scan(seeded)
    assert result["opened"] >= 2
    sign_in(client, seeded, "dev-reviewer")
    items = client.get("/api/v1/exceptions").json()["data"]
    kinds = {i["kind"] for i in items if i["object_id"] == str(rid)}
    assert kinds == {"UNUSUAL_PRODUCTION", "UNUSUAL_STOP"}
    prod = next(i for i in items if i["kind"] == "UNUSUAL_PRODUCTION" and i["object_id"] == str(rid))
    assert "30.0%" in prod["reason"] and "not a finding about its cause" in prod["reason"]

    stop = next(i for i in items if i["kind"] == "UNUSUAL_STOP")
    assert (
        client.post(f"/api/v1/exceptions/{stop['id']}/actions", headers=idem(), json={"action": "DISMISS"}).status_code
        == 422
    )  # a note is required
    assert (
        client.post(
            f"/api/v1/exceptions/{stop['id']}/actions",
            headers=idem(),
            json={"action": "DISMISS", "note": "Planned maintenance stop"},
        ).status_code
        == 200
    )
    scan(seeded)
    live = client.get("/api/v1/exceptions").json()["data"]
    assert not any(i["kind"] == "UNUSUAL_STOP" and i["object_id"] == str(rid) for i in live)  # not reopened

    # The record is archived: the production exception resolves by itself, with history.
    assert (
        client.post(f"/api/v1/records/{rid}/archive", headers=idem(), json={"reason": "Wrong entry"}).status_code == 200
    )
    scan(seeded)
    resolved = client.get("/api/v1/exceptions?status=RESOLVED").json()["data"]
    assert any(i["id"] == prod["id"] for i in resolved)
    hist = client.get(f"/api/v1/exceptions/{prod['id']}/history").json()["data"]
    assert [h["action"] for h in hist] == ["OPENED", "AUTO_RESOLVED"]
    with tenant_tx(seeded.tenant_id) as conn:  # exceptions never touch production data
        state = conn.execute(select(t.production_record.c.state).where(t.production_record.c.id == rid)).scalar_one()
    assert state == "ARCHIVED"


def test_missing_submissions_and_audiences(client, seeded, fake, owner_engine):
    scan(seeded, day="2026-09-23")  # a past working day with no entries
    sign_in(client, seeded, "dev-reviewer")
    missing = [i for i in client.get("/api/v1/exceptions").json()["data"] if i["kind"] == "MISSING_SUBMISSION"]
    assert missing and all(i["production_date"] == "2026-09-23" for i in missing)
    sign_in(client, seeded, "dev-viewer")
    assert client.get("/api/v1/exceptions").json()["data"] == []  # viewers do not work exceptions
    with owner_engine.begin() as conn:  # an integration problem is for administrators only
        conn.execute(
            text(
                "INSERT INTO report (tenant_id, series_id, version, title, filter_json, date_from, date_to, "
                "department_ids, timezone, data_version, record_count, include_detail, is_empty, metrics_json, "
                "facts_json, template_version, state, error_code, created_by) VALUES (:t, gen_random_uuid(), 1,"
                " 'Broken', '{}', '2026-09-27', '2026-09-27', '{}', 'Asia/Kolkata', 1, 0, true, true, '{}', "
                "'{}', 'x', 'FAILED', 'RENDER_FAILED', :u)"
            ),
            {"t": seeded.tenant_id, "u": seeded.users["dev-sender"]},
        )
    scan(seeded)
    sign_in(client, seeded, "dev-sender")
    kinds = {i["kind"] for i in client.get("/api/v1/exceptions").json()["data"]}
    assert "REPORT_FAILED" in kinds and "MISSING_SUBMISSION" not in kinds  # senders see reporting items only


# --- reminders (A2, A3) ----------------------------------------------------------------------------------------


def test_reminders_are_staged_deduplicated_and_audited(client, seeded, fake, owner_engine):
    connect_mail(client, seeded)
    patch_settings(
        client,
        seeded,
        {
            "working_days": [1, 2, 3, 4, 5, 6, 7],
            "submission_cutoff_local_time": "00:00",
            "reminders": {
                "enabled": True,
                "first_after_minutes": 0,
                "second_after_minutes": 1440,
                "escalate_after_minutes": 2880,
                "email": True,
            },
        },
    )
    now = datetime.now(UTC)
    with tenant_tx(seeded.tenant_id) as conn:
        created = reminders.evaluate(conn, seeded.tenant_id, now)
    with tenant_tx(seeded.tenant_id) as conn:
        assert reminders.evaluate(conn, seeded.tenant_id, now) == 0  # the same stage is never sent twice
    assert created > 0
    drain(owner_engine, seeded.tenant_id)
    sign_in(client, seeded, "dev-uploader")
    inbox = client.get("/api/v1/notifications").json()
    mine = [n for n in inbox["data"] if n["kind"] == "REMINDER_FIRST"]
    assert len(mine) == 1 and "Tapeline" in mine[0]["title"] and inbox["unread"] >= 1
    assert mine[0]["email_state"] == "ACCEPTED"
    assert any(m["subject"] == mine[0]["title"] and "attachments" not in m for m in fake.sent)
    assert client.post("/api/v1/notifications/read", headers=idem(), json={"ids": []}).json()["data"]["marked"] >= 1
    assert client.get("/api/v1/notifications").json()["unread"] == 0
    with tenant_tx(seeded.tenant_id) as conn:
        audited = conn.execute(select(t.audit_event).where(t.audit_event.c.action == "NOTIFICATION_FIRST")).all()
    assert len(audited) == created

    # Escalation goes to supervisors (reviewers), once, when its time comes.
    with tenant_tx(seeded.tenant_id) as conn:
        esc = reminders.evaluate(conn, seeded.tenant_id, now + timedelta(days=2))
    assert esc > 0
    sign_in(client, seeded, "dev-reviewer")
    assert any(n["kind"] == "ESCALATION" for n in client.get("/api/v1/notifications").json()["data"])


def test_tick_runs_everything_for_each_company(client, seeded, fake, owner_engine):
    # A day inside the exception scan's look-back window (7 days), whatever today's date is.
    day = (datetime.now(ZoneInfo("Asia/Kolkata")).date() - timedelta(days=1)).isoformat()
    add_record(owner_engine, seeded, day=day, qty="10", target="100")
    assert automation.enqueue_ticks() >= 1
    drain(owner_engine, seeded.tenant_id)
    sign_in(client, seeded, "dev-reviewer")
    tower = client.get(f"/api/v1/control-tower?date={day}").json()["data"]
    assert tower["exceptions"]["WARNING"] >= 1 and "reminders" in tower
    assert all("rejected" in d and "sync_failed" in d for d in tower["departments"])
