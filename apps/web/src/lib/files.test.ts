import { describe, expect, it } from "vitest";

import type { BatchFile, Limits } from "./api";
import { checkQueue, formatBytes, limitsSentence, MiB } from "./files";
import { fileStatus, scanLabel } from "./status";

const limits: Limits = {
  max_files: 20,
  max_file_bytes: 20 * MiB,
  max_batch_bytes: 100 * MiB,
  max_pdf_pages: 50,
  max_office_rows: 10000,
  accepted_extensions: [".jpg", ".jpeg", ".png", ".pdf", ".xlsx", ".docx", ".txt"],
};

describe("checkQueue", () => {
  it("accepts supported files and explains each rejection", () => {
    const out = checkQueue(
      [
        { name: "note.JPG", size: 1000 },
        { name: "old.doc", size: 10 },
        { name: "big.pdf", size: 21 * MiB },
        { name: "empty.txt", size: 0 },
      ],
      limits,
    );
    expect(out[0]?.reason).toBeNull();
    expect(out[1]?.reason).toMatch(/\.doc is not accepted/);
    expect(out[2]?.reason).toBe("Larger than 20 MiB.");
    expect(out[3]?.reason).toBe("The file is empty.");
  });

  it("flags files beyond the count limit and an oversized batch", () => {
    const many = Array.from({ length: 21 }, (_, i) => ({ name: `n${i}.txt`, size: 1 }));
    expect(checkQueue(many, limits)[20]?.reason).toMatch(/Only 20 files/);
    const heavy = Array.from({ length: 6 }, (_, i) => ({ name: `p${i}.pdf`, size: 19 * MiB }));
    expect(checkQueue(heavy, limits).every((c) => c.reason?.includes("100 MiB"))).toBe(true);
  });

  it("states the exact limits", () => {
    expect(limitsSentence(limits)).toContain("Up to 20 files, 20 MiB each and 100 MiB in total");
    expect(formatBytes(1536)).toBe("1.5 KiB");
  });
});

function file(overrides: Partial<BatchFile>): BatchFile {
  return {
    id: "u1", slot_no: 1, name: "scan.pdf", extension: "pdf", bytes: 10, state: "READY", scan_status: "CLEAN",
    scanner: "clamav 1.4", reject: null, duplicate_of: null,
    pages: { total: 3, processed: 3, succeeded: [1, 2], failed: [3] }, jobs: { scan: null, parse: null, extract: null },
    to_review: 0,
    ...overrides,
  };
}

const job = (state: string, extra = {}) =>
  ({ id: "j", kind: "upload.parse", state, processed: 3, total: 3, error: null, attempt: 1, max_attempts: 5,
     retryable: true, cancel_requested: false, next_attempt_at: null, generation: 1, ...extra }) as never;

describe("fileStatus", () => {
  it("names failed pages on partial reads instead of showing a percentage", () => {
    const s = fileStatus(file({ jobs: { scan: null, parse: job("PARTIAL", { error: { message: "OCR missing" } }), extract: null } }));
    expect(s).toMatchObject({ label: "Partly read", tone: "warning" });
    expect(s.detail).toContain("Pages 3 could not be read");
  });

  it("uses counts for progress", () => {
    const s = fileStatus(file({ pages: { total: 5, processed: 3, succeeded: [1, 2, 3], failed: [] },
                                jobs: { scan: null, parse: job("RUNNING"), extract: null } }));
    expect(s.detail).toBe("3 of 5 pages processed");
  });

  it("points to review once entries are found", () => {
    const s = fileStatus(file({ pages: { total: 1, processed: 1, succeeded: [1], failed: [] }, to_review: 2,
                                jobs: { scan: null, parse: job("SUCCEEDED"), extract: job("SUCCEEDED") } }));
    expect(s).toMatchObject({ label: "Ready for review", detail: "2 entries to check", tone: "success" });
  });

  it("shows rejection reasons and dev-only scan skips in words", () => {
    const rejected = fileStatus(file({ state: "REJECTED", reject: { code: "SPOOFED_TYPE", message: "Not a document." } }));
    expect(rejected).toMatchObject({ label: "Not processed", detail: "Not a document.", tone: "error" });
    expect(scanLabel(file({ scan_status: "SKIPPED_DEV" }))).toMatch(/disabled in this development environment/);
  });
});
