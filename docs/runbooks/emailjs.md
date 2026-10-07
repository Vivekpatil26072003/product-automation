# Runbook: report email through EmailJS

With `EMAIL_PROVIDER=emailjs` (root `.env`), the existing **Report → Compose email → Preview and send → Confirm send**
flow sends through EmailJS from the Sender's browser, using the Gmail service connected in the EmailJS dashboard.
Microsoft Graph is not used for this channel. `EMAIL_PROVIDER=graph` restores the server-side Microsoft 365 path unchanged.

## Flow and guarantees

1. **Confirm send** records the email intent on the server (one per confirmed draft, same content-hash check as before).
2. The browser **claims** it once (`POST /api/v1/emails/{id}/client-send/claim`). At that moment the server re-checks
   that the report is READY and still matches the saved records, then builds the template variables from the saved
   draft and the saved report. A second claim (double click, second tab) is refused with `ALREADY_CLAIMED`.
   If records changed, the email is recorded as FAILED `REPORT_OUTDATED` and nothing is sent.
3. The browser calls `emailjs.send(serviceId, templateId, variables, { publicKey })` (`@emailjs/browser`).
4. The browser records EmailJS's answer (`POST .../client-send/result`): 200 → ACCEPTED, 4xx → FAILED
   (`EMAILJS_REJECTED`), 5xx or no answer → UNKNOWN (`EMAILJS_NO_ANSWER`). If the browser closes before reporting,
   the automation tick marks the email UNKNOWN after 15 minutes (`EMAILJS_NO_REPORT`).
5. Status, attempts, recipients and audit entries appear on the email page and in **History**, as for Graph.
   UNKNOWN is reconciled by a Sender after checking EmailJS → Email History; it is never resent automatically.

Limits of this channel: the PDF is not attached (the figures are in the message); automatic sending of schedules
is refused (`BROWSER_CHANNEL`) because a browser must be present; EmailJS accepting an email is not delivery evidence.

## Configuration

| Where | Variable | Value |
|---|---|---|
| root `.env` | `EMAIL_PROVIDER` | `emailjs` |
| `apps/web/.env.local` | `NEXT_PUBLIC_EMAILJS_SERVICE_ID` | `service_75drj1q` |
| `apps/web/.env.local` | `NEXT_PUBLIC_EMAILJS_TEMPLATE_ID` | the template ID shown after saving the template below |
| `apps/web/.env.local` | `NEXT_PUBLIC_EMAILJS_PUBLIC_KEY` | EmailJS → Account → General → Public Key |

Only the **public** key is used. Never put the EmailJS private key in any file of this project. Restart the API after
changing `.env` and restart `npm run dev` (or rebuild) after changing `.env.local`. In EmailJS → Account → Security
you can restrict requests to your own domains (add `localhost` for local use).

## Template (EmailJS → Email Templates → Create New Template)

Settings panel:

| Field | Value |
|---|---|
| Subject | `{{subject}}` |
| To Email | `{{to_email}}` |
| From Name | `{{from_name}}` |
| From Email | keep "Use Default Email Address" (the connected Gmail account) |
| Reply To | `{{reply_to}}` |
| Cc | `{{cc_email}}` |
| Bcc | `{{bcc_email}}` |

Content (switch the editor to code view and paste):

```html
<div style="font-family: Arial, sans-serif; color: #172033; font-size: 14px; line-height: 1.5;">
  <p style="white-space: pre-line;">{{message}}</p>
  <hr style="border: 0; border-top: 1px solid #D5DDE8;">
  <h3 style="margin: 12px 0 4px;">{{report_title}}</h3>
  <p style="margin: 0; color: #526174;">Report {{report_code}} version {{report_version}} · {{period}} ({{timezone}}) · generated {{generated_at}}</p>
  <table style="border-collapse: collapse; margin: 12px 0;">
    <tr><td style="padding: 2px 12px 2px 0;">Production</td><td>{{production_total}} {{unit}}</td></tr>
    <tr><td style="padding: 2px 12px 2px 0;">Target</td><td>{{target_total}} {{unit}}</td></tr>
    <tr><td style="padding: 2px 12px 2px 0;">Achievement</td><td>{{achievement_pct}}</td></tr>
    <tr><td style="padding: 2px 12px 2px 0;">Variance</td><td>{{variance}} {{unit}}</td></tr>
    <tr><td style="padding: 2px 12px 2px 0;">Approved records</td><td>{{record_count}} across {{department_count}} departments</td></tr>
    <tr><td style="padding: 2px 12px 2px 0;">Pending (not included)</td><td>{{excluded_pending}}</td></tr>
    <tr><td style="padding: 2px 12px 2px 0;">Stoppages</td><td>{{stop_total_minutes}} minutes</td></tr>
  </table>
  <p style="margin: 8px 0 2px;"><strong>By unit</strong></p>
  <p style="white-space: pre-line; margin: 0;">{{unit_summary}}</p>
  <p style="margin: 8px 0 2px;"><strong>By department</strong></p>
  <p style="white-space: pre-line; margin: 0;">{{department_rows}}</p>
  <p style="margin: 8px 0 2px;"><strong>Status</strong></p>
  <p style="margin: 0;">{{status_summary}}</p>
  <p style="margin: 8px 0 2px;"><strong>Summary</strong></p>
  <p style="margin: 0;">{{summary}}</p>
  <p style="margin: 16px 0 0; color: #526174; font-size: 12px;">Reference {{email_reference}}</p>
</div>
```

Save, then copy the template ID into `NEXT_PUBLIC_EMAILJS_TEMPLATE_ID`.

## Variable mapping (built in `services/api/app/mail/emailjs.py`, all strings, never null)

| Variable | Source |
|---|---|
| `to_email`, `cc_email`, `bcc_email` | `email_draft.recipients` (To/Cc/Bcc typed in the draft), comma-separated; empty if none |
| `reply_to` | signed-in Sender's `membership.email` |
| `from_name` | Sender's `membership.display_name` (fallback "Production Team") |
| `subject`, `message` | `email_draft.subject`, `email_draft.body` (the confirmed text) |
| `report_title`, `report_version`, `timezone` | `report.title`, `report.version`, `report.timezone` |
| `report_code` | report series code (same as the PDF name and report page) |
| `period`, `date_from`, `date_to` | `report.facts_json.period.label`, `report.date_from`, `report.date_to` |
| `record_count` | `report.record_count` (approved records in the report) |
| `department_count`, `excluded_pending` | `report.facts_json` |
| `unit`, `production_total`, `target_total`, `achievement_pct`, `variance` | `report.metrics_json.metrics` when the report has one unit; "see unit summary" when it mixes units; achievement "N/A" without a target |
| `unit_summary` | one line per unit from `metrics_json.metrics` |
| `department_rows` | one line per department from `metrics_json.departments` |
| `status_summary` | `metrics_json.status_counts` (Running, Completed, Pending, On hold) |
| `stop_total_minutes` | `metrics_json.stop_total_minutes` |
| `summary` | the report's stored summary sentences |
| `generated_at` | `report.created_at` in the report timezone |
| `email_reference` | `email_message.id` (matches the email page and History) |

## Troubleshooting

| Symptom | Cause / action |
|---|---|
| "EmailJS is not configured in the web app: set …" | Fill the listed variables in `apps/web/.env.local`, restart the web app |
| FAILED, HTTP 400 "The template ID is invalid" / "The Public Key is invalid" | Wrong ID or key in `.env.local` |
| FAILED, HTTP 422 "The recipients address is empty" | Template **To Email** is not `{{to_email}}` |
| FAILED `REPORT_OUTDATED` | Records changed after confirmation: create a new report version and a new draft |
| UNKNOWN | Check EmailJS → Email History and Gmail Sent, then record the outcome on the email page |
| Production build blocks the request (CSP) | `connect-src` must include `https://api.emailjs.com` (set in `apps/web/next.config.mjs`) |
