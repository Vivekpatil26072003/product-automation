# Runbook: diary automation (photo → AI reading → review → records → owner PDF → email)

## What happens

| Step | Who / what | Where |
|---|---|---|
| 1. Photo of a diary page | Worker: **Diary photos** → *Take photo* or *Upload diary image* (uploads at once) | `/diary` |
| 2. Scan, read | Worker process: malware scan, OCR / AI reading, in the background | jobs `upload.*` |
| 3. Structure | Orders found on the page (several per page, tables without borders, Gujarati / Hindi / English): one row each | `app/orders/ai.py`, `app/orders/fields.py` |
| 4. Review | Table of all orders + full form beside the photo. Uncertain or missing values are highlighted; customer, date, quantity and money values the reader was unsure of must be confirmed or corrected | `/batches/{id}/orders` |
| 5. Save | Reviewer approves: the order is saved (revision 1), linked to its customer, or, if it repeats a saved order, the reviewer chooses *update that order* or *new order* | **Customer orders**, Overview |
| 6. Owner report | When every entry of the batch is approved or rejected and automatic reports are on: a consolidated PDF of the saved records | job `batch_report.render` |
| 7. Email | The worker emails the PDF to the owner through EmailJS (no browser needed); outcome recorded; uploader, reviewers and admins notified | job `batch_report.email`, **History → Owner reports** |

The batch screen and the worker screen show each step with its real state: Uploaded → Reading → Review →
Approved and saved → PDF report → Emailed to owner. A step that failed says why (for example "OCR not
configured" or EmailJS's refusal) and is never shown as done.

Manual actions remain: *Create report* / *Create and email to owner* / *Retry email* / *Send again* on the batch
screen, and *Send Email* on each order (to any address, with that order's latest PDF).

## 1. AI reading (required for handwriting)

Handwriting needs a reader. Choose one; values are set in the root `.env` (never in code or chat), then restart
the API **and** the worker.

**A. Claude for reading and structuring (recommended for mixed layouts and Gujarati/Hindi; paid per page)**

```
OCR_PROVIDER=claude          # Claude reads the photo into lines
AI_PROVIDER=claude           # Claude turns the lines (and the photo) into orders
ANTHROPIC_API_KEY=<your key from console.anthropic.com>
ANTHROPIC_MODEL=claude-opus-5-5
```

**B. Azure Read (free F0 tier, 500 pages/month) + Claude for structuring**: cheaper reading, same structuring:

```
OCR_PROVIDER=azure
AZURE_DI_ENDPOINT=https://<resource>.cognitiveservices.azure.com
AZURE_DI_KEY=<Key 1>
AI_PROVIDER=claude
ANTHROPIC_API_KEY=<key>
```

**C. Azure Read only (no AI cost)**: works for notes written as `Label : value` lines (English, Gujarati,
Hindi labels; several customers per page). Tables without labels and free paragraphs then need manual entry.

Notes:
- Each value keeps the line it was read from; values the model could not tie to the page, low-confidence
  OCR readings and values the model flags as unclear are highlighted and must be confirmed. Nothing is saved
  without a reviewer's approval.
- The AI never runs in staging/production until the model + prompt has passed evaluation (FR28 release gate,
  extractor name `claude-orders`); in development it runs and every order shows "not evaluated yet".
- If the AI fails (wrong key, outage), pages fall back to the label reader or manual entry; nothing is lost.
- Photos are sent to the configured provider (Anthropic / Microsoft) for reading. Get the data owner's approval.
- Photos uploaded before the reader was configured: open the batch and use **Retry failed pages**.

## 2. EmailJS (one shared template; server sending)

1. EmailJS dashboard → **Account → Security**: allow **API requests for non-browser applications** (the worker
   sends from the server; without this EmailJS answers 403).
2. **Email Templates → Create New Template** (one template for owner reports *and* order PDFs):

   | Setting | Value |
   |---|---|
   | Subject | `{{subject}}` |
   | To Email | `{{to_email}}` |
   | From Name | `{{from_name}}` |
   | Reply To | `{{reply_to}}` |
   | Content | `<p style="white-space: pre-line">{{message}}</p>` |
   | Attachments → **Variable Attachment** | Parameter name `pdf_file`, Filename `{{attachment_name}}`, Content type PDF |

   Variables sent: `to_email`, `subject`, `message`, `record_reference`, `customer_name`, `company_name`,
   `from_name`, `reply_to`, `attachment_name`, `pdf_file` (the PDF as `data:application/pdf;base64,…`),
   `email_reference`, and the diary data itself:

   | Variable | Content |
   |---|---|
   | `order_number`, `customer_name`, `order_date`, `delivery_date`, `quantity`, `rate`, `total`, `additional_details` | The order (one-order emails). For a report with several orders: `order_number` is the report reference, `customer_name` lists the customers, `total` is the sum of written totals, `additional_details` lists every order |
   | `title` | Same as `subject` |
   | `name` | Company name |
   | `email` | The recipient |
   | `orders_text` | Every order, one line each (`{{orders_text}}`) |
   | `orders_html` | Every order as an HTML table; use **triple** braces `{{{orders_html}}}` so EmailJS does not escape it |
   | `order_count` | Number of orders |

   An existing order template (for example one using `{{order_number}}`, `{{customer_name}}`, `{{order_date}}`,
   `{{delivery_date}}`, `{{quantity}}`, `{{rate}}`, `{{total}}`, `{{additional_details}}`, `{{title}}`) works as is,
   provided its **To Email** is `{{to_email}}` (the default "Contact Us" template sends to your own address) and
   it has the variable attachment `pdf_file`. To show every order of a report, add `{{{orders_html}}}` to the
   content.
3. In the app, as Administrator: **Owner report & email** (`/settings/owner-report`): owner email, company name,
   service ID (`service_75drj1q`), template ID, public key, private key (stored encrypted, never shown again),
   request size limit (50 KB on the free plan). Tick *Email the owner automatically* and **Save**.
4. Click **Send test email to owner**: a real email with a small PDF attached. It proves the service, template,
   keys and the variable attachment together.

EmailJS free plan: 200 emails/month, 50 KB per request. Order PDFs are about 3 KB; a batch report with 20
orders measured 5.8 KB. A report too large for the limit is refused with a clear message (never sent half).

## 3. Failure handling

| Situation | What the system does | What you do |
|---|---|---|
| EmailJS refuses (wrong ID/key/template, non-browser API off) | Email **Not sent** with EmailJS's reason; PDF kept | Fix the setting, then **Retry email** |
| EmailJS busy (429) or unreachable | Retries automatically (up to 5 attempts), then **Not sent** | **Retry email** later |
| No answer after the request was sent | **Result unknown**; never resent automatically | Check EmailJS → Email History, record *It was sent / not sent* |
| Worker stopped mid-send | **Result unknown** (INTERRUPTED) | Same as above |
| Same report requested twice | Second request refused (one send in flight; *Send again* needed after a send) | - |
| AI / OCR not configured or failing | Page marked unreadable with the reason; *Enter customer order* beside the photo | Configure, then **Retry failed pages** |

## 4. Live acceptance test (needs your keys; not run by the automated tests)

1. Configure §1 (A or B) and §2; restart API and worker; run the EmailJS test email (§2.4).
2. Sign in as a Reviewer (or Uploader), open **Diary photos**, take/upload a real diary photo.
3. Wait for *Reading* to finish (the steps update by themselves); open **Review**.
4. Compare each highlighted value with the photo, correct or confirm it; approve each order.
5. Check **Customer orders** (values as approved), the **Overview** card, the customer link.
6. Open the batch: *PDF report* done → **View PDF**; *Emailed to owner* done.
7. Check the owner's inbox: email with `Diary_Report_B-xxxxxxxx_v1.pdf` attached; **History → Owner reports**.
8. Failure drill: set a wrong template ID, *Create and email to owner* → *Not sent* with the reason;
   fix it, **Retry email** → *Sent to owner*.
