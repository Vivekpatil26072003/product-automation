# Runbook: pick reading registers (WGS-02 photo → database → Excel / PDF / CSV / SQL → email)

The **Hourly Production Reading Register (WGS-02), Pick reading** (form F/WGS/201): one page per shift, machine
numbers down the side, five time columns and a Total column. The first time column holds the meter reading carried
over from the previous shift. Each later column holds the meter reading, with the picks of those two hours written
under it. Column totals are written at the bottom.

## What happens

1. A worker photographs a register page on **Diary photos**. They can also upload a photo, or typed notes on
   *Upload notes*.
2. **Recognised.** The page is recognised by its printed heading ("HOURLY PRODUCTION READING REGISTER",
   "PICK - READING", "WGS-02"), or by a row of column times with machine rows under it. Register pages are taken
   first, so the other readers (orders, daily sheets, production notes) never see them.
3. **Shift from the column times.** Shift I is 08-00 … 16-00, shift II is 16-00 … 24-00, shift III is 24-00 … 08-00.
   The production day runs 08:00 to 08:00, so shift III (the night after shift II) belongs to the same register
   date. This matches the real pages, where the shift III start readings equal the shift II end readings.
4. **Reading.**
   - **Photos need a reader.** With `OCR_PROVIDER=none` (the default) a photo is not read at all. Set
     `OCR_PROVIDER=gemini` and `GEMINI_API_KEY` in the server `.env` (a free key from
     https://aistudio.google.com/apikey; never in chat or git), then restart the API and the worker. Gemini
     transcribes the page into rows (`27 | 2230 | 2282 24 | …`, unclear digits marked `?`), and the register reader
     and checks below take over (`app/ingestion/gemini.py`). On the free plan Google may use the uploaded photos to
     improve its products; a paid Gemini plan or Azure (F0) does not.
   - With `AI_PROVIDER=claude`, Claude reads the handwritten grid (`app/pick_registers/ai.py`). Its prompt
     explains rows that drift with slanted handwriting.
   - Without AI, the OCR lines are read as rows (`app/pick_registers/reader.py`). This works for typed notes and
     clean OCR; plain OCR of handwriting is rarely good enough.
5. **Saved.** The values go into **one register per department and day**: database table `pick_register`, and
   `pick_register_value` with one row per machine, time and shift holding the reading, the picks and the mark
   (B.fall, S/C …). The worker's totals go in `pick_register_total`. A second photo fills empty cells and confirms
   equal ones. A different value never overwrites the saved one: the cell is highlighted instead.
6. **Calculated, never read:** machine total per shift, column totals, machines stopped per time, shift totals and
   the day total.
7. **Checks.** Anything that does not add up is highlighted on its cell with the reason, and approval is blocked
   until a person looks at it:
   - **Picks:** reading − previous reading = picks. A counter that passes 9999 / 999 starts again from 0.
   - **Column totals:** the written column total = the sum of the picks. If every picks value in the column is
     confirmed by its readings, the written total itself is reported as probably wrong (a note, not blocking).
   - **Shift start:** the start reading of a shift = the last reading of the previous shift. For shift I, that
     is the previous day's shift III.
   - **Meter in other units:** a machine whose readings rise by the same multiple of the written picks in every
     slot is reported once as a note (on 2 Oct 2026, machine 27 rose about 2.2 times the picks).
   - **Shift / day total:** a figure under the first column is compared with the calculated shift total (a note).
8. **Check the register** (*Pick registers* → the day, or *Check register* on the upload). For each highlighted
   cell, either correct it or press **OK**, then **Save changes**.
   - Each cell is typed as written: reading, then picks, then a mark, e.g. `2282 24`, `1815 S/C` or `B.FALL`.
   - OK records the exact check that was accepted. If one of its numbers changes later, the check returns.
   - A Reviewer **approves**. Changing an approved register needs a reason, and every change is kept in the
     history.
9. **Pick registers** lists the days with picks per shift. Every row has **Excel**, **PDF**, **CSV**, **SQL** and
   **Send Email**.

## Files

| File | Content |
|---|---|
| Excel | One sheet per shift laid out like the page. The machine Total column and calculated totals are formulas. Then the written totals and M/c stop. Sheet *Data* (one row per saved value) and sheet *Checks*. |
| PDF | One landscape page per shift: "reading (picks)" per cell, highlighted cells, totals, then checks and notes. |
| CSV | One row per saved value, plus the calculated and written totals (UTF-8 with BOM). |
| SQL | `CREATE TABLE IF NOT EXISTS pick_reading / pick_reading_total`, a `DELETE` of this day and department, then the `INSERT` rows. It is standard SQL: PostgreSQL, MySQL / MariaDB and SQLite load it as it is, and running it again replaces the day. Text from the page is escaped. The tests load it into SQLite. |

## Email

Send Email (Reviewer, Sender, Admin) works like the daily sheet email:
- The exact file is stored when the email is queued, then the worker sends it through EmailJS (`register.email`
  job).
- The message has the picks per shift, both as text and as a table (`orders_text`, `orders_html`).
- A PDF goes in the template's `pdf_file` attachment. Excel, CSV and SQL go in `sheet_file` (named by
  `sheet_file_name`): the EmailJS template needs that second attachment slot.

## Limits

- Real handwriting needs the AI reader (or an OCR service that returns the grid). The free test of 6 Oct 2026 used
  a transcription instead; see `docs/evidence/pick-register-test-2026-10-06.md`.
- The Claude register reader is not yet evaluated on real photos. In staging and production it runs only after its
  model release is approved (FR28 gate).
- The shift times are fixed in `app/pick_registers/layout.py` (I 08-16, II 16-24, III 00-08).

## Checking faster

- **One-click corrections:** a highlighted cell may show **Use 2234** (with the reason, e.g. "2210 + 24 = 2234 and
  2259 - 25 = 2234"). It appears only when two independent numbers agree. Click it, then **Save changes**.
- **Send each photo once:** an identical photo of the same page is not read again (noted on the register). A new
  photo of the same page is compared cell by cell; differences are highlighted.
- **Speed:** a page takes 10-25 s with Gemini flash-lite. Photos sent together are read in parallel (3 workers).
