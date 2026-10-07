# Runbook: automation (scheduled reports, exceptions, reminders)

Administrators turn features on at **Automation settings**. Everything is off by default.

## Scheduled reports (Senders)

1. An administrator enables **Scheduled reports**. **Allow automatic sending** is a separate switch.
2. **Automation → New schedule**: set the repeat (daily, weekly on a weekday, or monthly on a day, where 29–31 clamps to the month end), the time, the time zone, the departments (only yours), the unit, the recipients and what happens when a period is empty.
   - The editor shows the next three runs and the period each will report.
3. Each run snapshots the approved records for the previous completed period, creates the PDF and prepares an email draft. The Sender reviews the draft and sends it from the report page.
4. **Automatic sending**: set the mode to "Send automatically", then **Review and approve automatic sending**. The approval covers exactly that version, those recipients and that mailbox.
   - Any edit, a new mailbox or a lost permission cancels it, and later runs become drafts again with the reason shown.

| Run status | Meaning |
|---|---|
| Draft ready | Report and draft prepared; nothing sent |
| Accepted by provider | Sent automatically; Microsoft accepted it (delivery is not confirmed) |
| Skipped: no approved records | Empty period with the Skip policy |
| Missed (not run) | The scheduler was down more than 24 hours; use **Run now** if the report is still needed |
| Waiting for an earlier run | Another run of the same schedule is still active; after 2 hours it fails as OVERLAP |
| Stopped: email outcome unknown | Reconcile the email (email page), then **Resume** the schedule |
| Failed: PERMISSION_REVOKED | The owner lost the Sender role or a department; the schedule was paused |

**Run now** reports a finished period, always as a draft. Running the same period again returns the existing run.

## Exceptions

**Exceptions** lists what needs a look, most severe first. Acknowledge it, or Resolve or Dismiss it with a note.

- An item closes by itself when its condition clears, for example after the entry is corrected.
- A dismissed condition stays dismissed while it persists.
- Thresholds (low/high achievement %, stop minutes, days to check) are in Automation settings.
- The scan runs every minute and never changes production data.

## Reminders for missing entries

- Enable **Send reminders** and set the minutes after the submission cutoff for the first reminder, the second reminder and the escalation to supervisors.
- The cutoff and working days are set on the same page.
- Uploaders granted a department receive its reminders; reviewers receive escalations. Each is sent once per day and stage.
- Optional email copies use the connected Microsoft 365 mailbox.
- Users see reminders under **Notifications** in the header.

## Operations

- Jobs: `automation.tick` (per company, every minute), `schedule.run`, `notification.email`.
- Tables: `schedule`, `schedule_version`, `schedule_run`, `exception_item`, `exception_event`, `notification`.
- Audit actions: `SCHEDULE_CREATED`, `SCHEDULE_UPDATED`, `SCHEDULE_AUTO_SEND_APPROVED`, `SCHEDULE_RUN_<STATE>`, `EXCEPTION_<STATUS>`, `NOTIFICATION_<STAGE>`.
