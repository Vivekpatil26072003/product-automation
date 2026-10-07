# Real handwriting test, 4 Oct 2026 (no paid API)

Pages: 2 real handwritten pages, both photographed: Sulzer production report, 2 Oct 2026, shift 1 (26 input values +
13 calculated values written); customer order note, 26 Sep 2026 (14 fields).
Answer key: the company's official sheet `SULZER PROD 02.10.2026.xls` (shift I column) for the production page.

Readers (free):
- **Tesseract 5.5** (local, automatic).
- **Claude Code transcription** (this development session, Pro subscription; stands in for an AI/OCR reader).
Both outputs went through the application's real line reader; the transcription also went through the real
upload → scan → parse → extraction → database → sheet view path in the development database.

## Results: production page (26 values, shift I)

| Reader | Right cell, right value | Of which highlighted | Wrong value | Missed |
|---|---|---|---|---|
| Transcription, reader before this test | 10 | 10 | 1 (highlighted) | 15 |
| Transcription, reader after this test | **26** | 4 (warping machine not named) | 0 | 0 |
| Tesseract (automatic) | 0 | - | 2 (both highlighted) | 24 |

Shift mapping: every value went to Shift I (page header "Shift : 1"); nothing was put in shifts II / III.
Independent check: 22 of 26 values read equal the official Excel; the other 4 differ in the diary itself.

## Results: order note (14 fields)

| Reader | Right | Highlighted | Wrong | Missed |
|---|---|---|---|---|
| Transcription | 14 | 1 (delivery date 05/10 could be day/month or month/day) | 0 | 0 |
| Tesseract | 0 (no order recognised) | - | - | 14 |

## Errors in the diary itself (not reading errors)

| Written | Official shift I | What it is | Caught by the app |
|---|---|---|---|
| Sulzer running loom/day 80.04 | 67.91 | the target | yes: written efficiencies do not fit it, highlighted |
| Sulzer mandays 42 | 66 | the shift II value | no (nothing on the page contradicts it) |
| Total downtime 403.81 | 532.66 | the day's mechanical total | yes: note "does not match the rows of shift I (293.34)" |
| Normal CONDV running looms | 14.5 | not written | its written efficiencies are listed as "not checked" |
| Warping production | Prashant | machine not named | yes: highlighted, reviewer chooses |

## Handwriting difficulties seen

Stray strokes before numbers (":-76.32%", "-1,047", "1,406" with a flagged 1), a mark inside "9,700", thousands commas,
units next to numbers ("kg", "hrs", "%", "m/min"), two columns per line, ":" written loosely.
Tesseract: 7↔1 and 7↔4 (73.81 → 13.81, 73.16 → 43.16), 9↔4 (169.54 → 164.54), digits dropped or merged
(9,700 → 478100), "52, 104" split into two numbers, labels garbled, date misread as 2066.

## Changes made to the reader (all existing tests still pass)

One shift per page ("Shift : 1", "(Shift 1)"); diary labels ("Running loom/day", "Production (mtr)/(kg)",
"Total weight", "Weight", "Speed", "Sulzer/Warping mandays", "Total downtime"); numbered headings ("1. Sulzer");
two "label : value" columns per line; written calculated values cross-checked against the sheet's own calculation
(a matching figure confirms its input, a mismatch highlights the probable wrong value); a page date more than
31 days ahead or 3 years back is not used; on one-shift pages "52, 104" is one number; three shift values that
differ by more than 25× are highlighted.
