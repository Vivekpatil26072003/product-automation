# Runbook: reports and email

## Creating a report (Reviewer or Sender)

**Reports → New report**: choose the period (at most 366 days), the departments (none = all you can see), the unit and a title. The server snapshots the approved records at that moment, and the PDF is created in the background.

- **No approved records:** you are asked to confirm an empty report. It shows 0 and N/A.
- **More than 10,000 records:** refused. Narrow the period or scope.
- **PDF failed:** no file exists. **Retry PDF** renders the same snapshot again.
- **Outdated:** a record in the period was corrected, added or archived after the snapshot. The report stays readable and downloadable exactly as it was, but it cannot be emailed. Use **Generate new version**; earlier versions are kept.
- **Waiting for review (excluded):** entries not yet approved are never in a report. The count is shown so nobody assumes the report is complete.

## Emailing a report (Sender)

Prerequisite: an administrator has connected **Microsoft 365 email** (Settings → Integrations) and the test passed.

1. Open a Ready, current report and choose **Compose email**. The draft saves automatically.
2. Recipients: at least one To address, and at most 50 addresses across To, Cc and Bcc. Addresses outside the company domains (settings `internal_email_domains` plus the sender mailbox's domain) trigger a warning.
3. Figures in the message must match the report. If you change or add a production figure, sending is blocked until it matches.
4. **Preview and send** shows exactly what will go out: every recipient including Bcc, the report version and the attachment. **Confirm send** sends that preview only. If anything changed since the preview, you must preview again.

## Email statuses

| Status | Meaning | What to do |
|---|---|---|
| Sending | Queued or in progress; a busy provider (429) is retried automatically | Wait |
| Accepted by provider | Microsoft accepted the message. This is **not** proof of delivery to each mailbox | Nothing |
| Outcome unknown | The request may have reached Microsoft, but no answer arrived | Check the sender mailbox's **Sent Items** or the Exchange message trace, then **Reconcile the outcome** with the evidence reference. Nothing is resent automatically |
| Not sent | Refused (for example an invalid recipient), or blocked before sending (report outdated, mail disconnected, attachment changed) | Read the reason; **Prepare new draft** if needed |

A resend is always a separate email with a reason; the original record is kept. To correct figures after an accepted email, generate a new report version and create a draft from it. Use `correction_of_email_id` so the body says it replaces the earlier report.

## Operations

- Jobs: `report.render`, `reports.invalidate`, `email.send` (plus `export.render` for the Excel snapshot).
- Tables: `report`, `report_item`, `email_draft`, `email_message`, `email_attempt`, `email_recipient_status`. Snapshot columns, confirmed drafts and send intents are protected by triggers.
- Audit actions: `REPORT_REQUESTED`, `REPORT_OUTDATED`, `REPORT_RETRIED`, `REPORT_DOWNLOADED`, `EMAIL_DRAFT_CREATED`, `EMAIL_DRAFT_UPDATED`, `EMAIL_SEND_CONFIRMED`, `EMAIL_ACCEPTED` / `EMAIL_RETRY` / `EMAIL_UNKNOWN` / `EMAIL_FAILED`, `EMAIL_RECONCILED`.
- AI summary: `NARRATIVE_PROVIDER=claude` (worker environment, with `ANTHROPIC_API_KEY`). Default `template`. The report shows which was used and why AI wording was rejected, if it was.
- Attachment limit: 3 MiB per message (Graph inline attachment limit). Larger PDFs are blocked with a message.
