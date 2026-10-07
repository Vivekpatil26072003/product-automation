# Real pick reading register test, 6 Oct 2026 (no paid API)

**Pages:** 2 real handwritten pages of the Hourly Production Reading Register (WGS-02), pick reading, form F/WGS/201
Rev. No. 05, dated 2 Oct 2026:
- shift II page: 16-00 … 24-00
- shift III page: 24-00 … 08-00

Each page has machines 26–55. That is 260 written cells: readings, picks and B.fall / S/C marks.

**Reader (free):** Claude Code transcription, done in this development session on the Pro subscription. It stands
in for an AI / OCR reader. Two overwritten numbers were marked "?".

**Path:** the transcription went through the real app in the development database:
upload → malware scan → parse → extraction → register merge → `pick_register_value` → register page.
The result is register `16d6d317-cf3b-4765-b0b7-a269c6fe4bc6`, department Sulzer Fabric.

## Result

| Check | Result |
|---|---|
| Cells saved | 260 (131 shift III, 129 shift II); every machine row and time in its own database row |
| Shift from column times | 16-00 … → II, 24-00 … → III; nothing put in shift I |
| Written column totals = calculated | 7 of 8: 641, 682, 615, 625 (II) and 583, 603, 646 (III). 04-00 (III): 590 written, 610 calculated |
| Shift III start = shift II end | 25 of 25 machines with a start reading |
| Picks = reading − previous reading | Every cell, except machine 27 (meter in other units, see below) and the 2 cells below |

## What the app highlighted (approval blocked until a person checks)

| Where | What | Most likely |
|---|---|---|
| II, m/c 39, 18-00 | 1575 − 1357 = 218, but 18 is written | Start reading is 1557 (1575 − 18): the 16-00 reading is misread or miswritten |
| II, m/c 48, 20-00 | 916 − 881 = 35, but 25 is written | Error in the register itself: the worker's column total 682 uses 25 |
| III, m/c 27, 04-00 | 04-00 adds up to 610, but 590 is written; m/c 27 is the only picks value no reading confirms | Worker's addition (every other value in the column matches its readings); the reviewer confirms 28 from the photo |
| III, m/c 27, 06-00 | Reading overwritten on the page (2386 / 2388) | Reviewer checks the photo |
| II, m/c 41, 22-00 | 3140 or 3149 unclear; the picks (10) fit 3140 | Reviewer checks the photo |

## Notes (shown, not blocking)

- **Machine 27:** its readings rise about 2.1× (shift II) and 2.2× (shift III) the written picks in every slot.
  Its counter probably counts in other units. The picks are taken as written. If the company confirms this, a
  per-machine factor could be added later.
- **687:** written under the first column of the shift III page. The company said it is a shift / day total, but
  shift III adds up to 2442 and the day to 5005. It is kept as written and shown for a person to clarify.

## Downloads and email (automated tests, same pages)

- **Excel:** one sheet per shift with formulas, plus *Data* (260 rows) and *Checks* sheets.
- **PDF:** one page per shift.
- **CSV:** one row per value.
- **SQL:** loaded into SQLite twice, still 260 rows (re-running replaces the day); shift III picks sum = 2442.
- **Email:** sent through the recorded EmailJS transport with the PDF and the SQL file attached.

## Not tested

- A real OCR or AI reading of these photos: the free test used a transcription.
- Tesseract was not tried: it scored 0/26 on the earlier handwritten page, and this page is a denser grid.
- The Claude register reader (`app/pick_registers/ai.py`) has unit-level schema checks only. It needs an API key
  (paid) and an evaluation on real photos before use.

## Real photos read by Gemini (same day, after OCR_PROVIDER=gemini was added)

The two photos were uploaded through the real app with the real Gemini API: free key, `gemini-3.8-flash`, falling
back to `gemini-3.5-flash` / `gemini-3.1-flash-lite` while Google answered 503 "high demand". Each run used a
department with no register for the day, so nothing was confirmed by earlier values. Every saved cell was compared
with the hand transcription above.

| Run | Shift II (129) | Shift III (131) | Wrong values highlighted |
|---|---|---|---|
| 1 | 126 | 127 | 4 of 7 on the cell; the other 3 rows flagged in the next cell or the column total, which did not block approval yet |
| 2 | 126 | 127 | totals row shifted one column by Gemini; every column flagged |
| 3 (after the fixes below) | 126 | 129 | **5 of 5 on the cell**: 255 / 260 right (98%) |

The wrong readings were 2290 for 2090 (m/c 27), 2284 for 2234 (m/c 28), 3149 for 3140 (m/c 41), the "9" picks of
m/c 31 one column early, and once 1404 / 1429 for 1402 / 1427 (m/c 46).

**Fixes made from these runs:**
- A reading with no picks on a running machine is flagged.
- A written column total that does not match now blocks approval until it is checked.
- Four totals written without the empty first cell are placed under the four picks columns.
- When Gemini is busy, the next model is tried.

Gemini's output varies from run to run, so a person still checks every highlighted cell before approval.
