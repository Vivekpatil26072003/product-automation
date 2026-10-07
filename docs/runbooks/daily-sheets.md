# Runbook: daily production sheets (notebook photo → database → Excel / PDF / CSV → email)

## What happens

1. A worker photographs the notebook page of the day's production report on **Diary photos** (or uploads a photo,
   a text PDF of the report, or typed notes on *Upload notes*).
2. **Reading.** The page text comes from OCR (photos) or the file itself. Each line that starts with a row of the
   report ("Production in Meters 52104 52949 52919") gives that row's three shift values; section headings
   ("NORMAL FIBC", "Downtime", "PRASHANT"…) decide the table. With `AI_PROVIDER=claude`, Claude reads handwritten
   pages (any layout, Gujarati / Hindi / English) into the same cells (`app/shift_reports/ai.py`).
3. The values go into **one sheet per department and day** (database tables `shift_report` and
   `shift_report_value`, one row per written value). A second photo of the same day fills empty cells; a value
   that differs from one already saved is never overwritten: it is highlighted for a person.
4. **Check** (*Daily sheets* → the day, or *Check sheet* on the upload): values that were hard to read are
   highlighted with the reason. Correct them or press **OK**, then **Save changes**. A Reviewer **approves**.
5. **Calculated, never read:** Total (sum, or average for average rows), To date (average of the daily totals
   this month), theoretical picks, loss of pick, the three efficiency %, cumulative picks/hour, warping metres/min
   and every Total row. Formulas were taken from `SULZER PROD 02.10.2026.xls` and checked against its values
   (tests: `tests/unit/test_daily_sheet.py`).
6. **Daily sheets** lists the saved days (filter by date, status, supervisor) with **Excel**, **PDF**, **CSV**
   and **Send Email** on every row.

## What is read (catalog: `services/api/app/shift_reports/catalog.py`)

| Part | Tables |
|---|---|
| Weaving | Sulzer production; Ground cover; Normal CONDV; Normal FIBC (running looms, picks, metres, kg, metres/loom/day, avg width); Machine status (day) |
| Wastage & manpower | Sulzer fabric wastage; Sulzer wastage; Sulzer manpower |
| Downtime | Downtime per shift (mechanical … bad bobbin); Reason for low running m/cs (day); Mech. & others detail (day) |
| Warping | Prashant and Hacoba (metres, kg, hours, beams, breakages, bobbin change, warp leasing, downtime rows); warping wastage; warping manpower |

Shift supervisors ("Shift I supervisor: …") and machine-number notes ("B/F= 75,37…", "NEW= …") are kept with the day.

Table parameters used by the formulas, from the company's sheet (administrators can change them, with the targets,
through `PUT /api/v1/sheet-targets`):

| Table | Installed looms | Picks/hour (efficiency) | Picks/hour (theoretical-picks row) |
|---|---|---|---|
| Sulzer | 92 | 14.86 | 14.86 |
| Ground cover | 3 | 14.86 | 9.6 |
| Normal CONDV | 19 | 14.70 | 14 |
| Normal FIBC | 73 | 14.86 | 14 |

## How the notebook should be written (best results)

- One row per line: the row name, then the three shift values in order I, II, III: `Picks 7026 7087 7071`.
- Section names on their own line: `SULZER PROD`, `NORMAL FIBC`, `Downtime`, `WARPING`, `PRASHANT`, `HACOBA`.
- The date on its own line (`2-Oct-26`), supervisors as `Shift I supervisor : name`.
- Totals and percentages need not be written; if they are, a written total is used to check the shift values.
- A line with only one or two numbers is filled in order and highlighted (which shift is not certain).

## Reading handwriting

Handwriting needs a reader: `OCR_PROVIDER=azure` (free F0 tier, see `customer-orders.md`) gives the text lines,
and `AI_PROVIDER=claude` + `ANTHROPIC_API_KEY` reads the page layout into cells. Without AI, OCR lines are read with
the line rules above; without any reader a photo cannot be read and the sheet is entered by hand on the sheet page.
AI output is checked: cells must exist in the sheet, values must be numbers, and a value whose digits are not in the
transcribed lines is highlighted. Nothing read is saved as approved without a person.

Tested on the company's real report (`SULZER PROD 02.10.2026.pdf`, text): 290 values and the date read; the cells
the PDF leaves ambiguous (empty cells in the Excel that the PDF text drops, a row printed twice with different
meanings) are highlighted, not guessed. A real handwritten notebook page has not been tested yet (needs the AI key).

## Files

- **Excel**: sheet *Report* in the company's layout; Total and calculated rows are live formulas, table parameters
  are written above each loom table; sheet *Data* = one row per saved value (date, department, section, row,
  shift, supervisor, value, entered by); sheet *Notes*. About 18 KB.
- **PDF**: printable, standard fonts, about 7 KB; sections with nothing written are left out.
- **CSV**: one line per sheet row (target, I, II, III, day, total, to date, calculated yes/no), UTF-8 with BOM.

## Email

Sent by the worker through EmailJS with the settings in **Owner report & email**. The message lists the key figures
(production m / kg, picks, total efficiency, running looms, downtime, warping) per shift with total and to date,
also as an HTML table (`{{{orders_html}}}`). Attachment:

- **PDF** goes in the template's existing variable attachment `pdf_file` (works with the current template).
- **Excel / CSV** go in a second variable attachment: EmailJS → template → Attachments → *Add Attachment* →
  Variable Attachment, parameter `sheet_file`, filename `{{sheet_file_name}}`. Without it, choose PDF.

States, retries and the "unknown" case work as for order emails (one send in flight per sheet, address and format).
