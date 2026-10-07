# ADR 0007: Automation — schedules, exceptions, reminders (M7)

**Status:** accepted for M7 · 2026-09-29

## Decisions

1. **One tick per company per minute.** The worker loop queues a coalesced `automation.tick` job for each company. It is the only process that lists companies (a new SELECT-only policy on `tenant` for the dispatcher scope); everything else in the tick runs inside that company's RLS scope. The tick claims due schedule occurrences, scans for exceptions and sends due reminders.
2. **Occurrences are computed, never stored ahead.** `app/automation/occurrences.py` derives each run from the IANA zone plus the local time:
   - A local time inside a DST gap runs at the first valid instant; a repeated local time runs once, at its first occurrence.
   - A monthly day beyond the month's end clamps to the last day.
   - Periods are the previous completed local day, Monday–Sunday week, or month.
3. **A period can never run twice.** `schedule_run` is unique on (schedule, version, period, run kind), and claiming uses `INSERT … ON CONFLICT DO NOTHING` under a row lock. A restarted scheduler that claims the same window again creates nothing.
   - After downtime, only the latest missed occurrence runs, and only within 24 hours. Older ones are recorded as SKIPPED_MISSED and need an explicit Run now.
4. **Versions and approval.** Any content edit writes an immutable `schedule_version` and revokes auto-send approval. Pausing does not change the version.
   - Auto-send requires `feature_flags.auto_send` and a Sender's confirmation of a policy hash. The hash covers version, scope, units, recipients, sender mailbox, time zone, cadence, template, title, subject and empty policy.
   - The hash is recomputed at every dispatch. A changed mailbox also invalidates it. Otherwise the run stops at a draft, with the reason recorded.
5. **Runs are a state machine driven by their own job**: QUEUED → REPORTING → DRAFTED, or SENDING → SENT / FAILED / HALTED. Each step reuses the M6 services (repeatable-read snapshot, draft validation, confirmed send), so scheduled reports get the same guarantees as manual ones.
   - Before every step, the owner's membership, Sender role and department grants are rebuilt from the database. A revocation fails the run and pauses the schedule before anything is sent.
   - An UNKNOWN email outcome HALTS the run and pauses the schedule until someone reconciles it.
   - One active run per schedule: a later run waits up to two hours, then fails visibly (OVERLAP).
   - An empty period is skipped by default. The DRAFT policy creates an empty report and draft, but never an automatic email.
6. **Exceptions are findings, not fixes.** Each rule produces an explainable item: severity, reason, object, department/date, audience. There is one live item per condition (partial unique index on `dedupe_key`), and every change is an append-only `exception_event`.
   - Items resolve automatically when the condition clears. A dismissed condition does not reopen while it persists.
   - Audiences: department items (Reviewers/Admins with a grant), reporting items (Reviewers, Senders, Admins), integration items (Admins).
   - Wording avoids attributing causes ("this is not a finding about its cause").
7. **Reminders** fire only on working days, after the company's submission cutoff, and only for departments expected to submit that still have nothing.
   - Stage times are configurable. Only the latest due stage is sent, so a scheduler that was down doesn't fire every stage at once.
   - Uploaders granted the department get the reminders; reviewers get escalations. If a department has no uploaders, the reviewers get the reminders too.
   - Each (day, department, stage, recipient) is sent at most once (unique key) and audited.
   - An email copy is optional and goes through the connected mailbox. A crash after marking the copy leaves UNKNOWN, never a second email.
8. **All automation is off by default** (`scheduling`, `auto_send`, `reminders.enabled`). The exception scan always runs because it is read-only.
