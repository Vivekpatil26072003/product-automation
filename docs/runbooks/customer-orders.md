# Runbook: customer orders (handwritten note → order form → saved order → PDF → email)

## Flow

1. **Upload** the order note (photo, scan, PDF or typed text) on *Upload notes*, as for production notes.
2. **Reading.** Typed text is read directly. Photos and scans need an OCR reader (`OCR_PROVIDER=azure`, below).
   Pages that read as an order note (`Customer : …`, `Rate : …`, `Total : …`; see the mapping below) become an
   **order form**; all other pages go through the production-record extractors exactly as before.
3. **Review** (*Review entries → Review orders*): the page is shown beside the form. Every value read from the
   page is filled in and shows the line it came from; anything not found stays empty and editable. Changes
   autosave. Problems are listed per field (errors block approval; warnings such as *Quantity × rate ≠ total*
   do not). If nothing could be read (for example no OCR reader yet), *Enter customer order* opens an empty
   form beside the photo.
4. **Approve** (Reviewer): the order is saved once (a second click or a replay cannot create a duplicate) and
   appears in **Customer orders**, on the **Overview** card and in **History → Orders**.
5. **Correct** a saved order (*Correct values*, Reviewer, with a reason): a new revision; earlier revisions and
   their PDFs stay available. Lists, PDF and emails always use the latest saved revision.
6. **PDF**: *View PDF* renders the saved revision (about 3 KB; standard PDF fonts, so it fits an EmailJS
   attachment). The file name is `Order_<reference>_r<revision>.pdf`; the reference is the note's order
   number, or `ORD-xxxxxxxx` when the note has none.
7. **Send Email** (Reviewer or Sender): type one address. The server checks the order has not changed since
   the page was loaded, renders that order's latest PDF, records the send, and returns the EmailJS variables
   with the PDF; the browser sends through EmailJS and records the answer (sent / not sent / unknown) in the
   order's *Emails* list and in *History → Order emails*. A failure never changes the order; send again to retry.

Roles and scope are the same as for production records: Uploaders enter and correct order forms of their own
uploads; Reviewers approve, reject and correct orders; Viewers read; everything is limited to the user's
departments and company.

## Reading handwriting: Azure AI Document Intelligence (free F0 tier)

The reader is already built in (`app/ingestion/ocr.py`, `AzureReadOcr`, prebuilt-read model). Setup:

1. Sign in at <https://portal.azure.com> (a free Azure account; sign-up verifies identity with a phone and a
   card, and the F0 tier is not charged).
2. *Create a resource* → **Document Intelligence** (formerly Form Recognizer) → pick a resource group and a
   region → **Pricing tier: Free F0** (500 pages per month, 20 calls per minute) → *Create*.
3. Open the resource → **Keys and Endpoint**. In the project's root `.env` (never in code or chat):

   ```
   OCR_PROVIDER=azure
   AZURE_DI_ENDPOINT=https://<your-resource>.cognitiveservices.azure.com
   AZURE_DI_KEY=<Key 1>
   ```

4. Restart the API and the worker. For a photo that was uploaded before, open its batch (*Processing*) and use
   **Retry failed pages**: it is read again without uploading it again.

Uploaded photos are sent to Microsoft Azure for reading (in the region you chose). The adapter is tested
against recorded Azure responses; check the first real upload's form against the photo.

Without a reader (`OCR_PROVIDER=none`) nothing is invented: the photo is kept, the processing screen says it
could not be read, and *Enter customer order* gives an empty form beside it.

## Note labels → form fields (`services/api/app/orders/fields.py`)

| Field | Labels recognised on the note (any case; `:`, `-`, `=` or no separator before a number) |
|---|---|
| Customer name | Customer, Customer name, Party, Party name, Client, Client name |
| Customer number | Customer no / number / code / id, Cust no, Party code |
| Customer email | Email, E-mail, Email id, Mail id, Customer email |
| Mobile number | Mobile, Mobile no, Mob, Phone, Phone no, Contact, Contact no |
| Order number | Order no / number / id / ref, PO no / number |
| Order date | Order date, Date of order; a bare *Date* when there is no order date |
| Delivery date | Delivery date, Delivery, Dispatch date, Due date |
| Package, Size, Material | Package / Packaging / Product / Item; Size / Dimensions; Material |
| Quantity | Quantity, Qty, Nos, No of boxes |
| Rate, Total, Advance, Remaining | Rate / Price / Unit price; Total / Amount / Grand total; Advance; Remaining / Balance |
| Priority, Remarks, Employee | Priority / Urgency; Remark(s) / Note(s) (continues onto following lines); Employee / Taken by / Salesman / Staff |

Required to approve: customer name, order date, quantity. Dates follow the company's date order (day/month);
`05/10/2026` is read as 5 Oct 2026 and flagged so the reviewer confirms it. Amounts accept `Rs`, `₹`, `/-`
and Indian or international digit grouping.

## Email

Order PDFs and owner reports are sent by the worker through the EmailJS REST API with one shared template and
the settings in **Owner report & email** (administrators). Setup, template and variables:
[diary-automation.md](diary-automation.md) §2. *Send Email* on an order queues the email; the dialog and the
order's *Emails* list show EmailJS's answer (sent / not sent with the reason / unknown).

## Troubleshooting

| Symptom | Cause / action |
|---|---|
| Photo uploaded, "no OCR reader is set up" | `OCR_PROVIDER=none`: set up Azure (above), restart, *Retry failed pages*; or *Enter customer order* |
| `OCR_AUTH_FAILED` | Wrong `AZURE_DI_KEY` / endpoint |
| Page read, but it went to production entries | The note has no customer, order number or money label; enter it with *Enter customer order* |
| "Email is not set up yet" | An administrator completes Settings -> Owner report & email |
| "EmailJS did not send it: The template ID not found" / "Public Key is invalid" | Wrong IDs in `.env.local` |
| Email arrives without the PDF | Template attachment is not *Variable Attachment* with parameter `pdf_file` |
| "This order was changed; the latest PDF is revision N" | Someone corrected the order meanwhile; the list reloads; send again |
| Email shows *Unknown* | The browser closed before EmailJS answered: check EmailJS → Email History before sending again |
