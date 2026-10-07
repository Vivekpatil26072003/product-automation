import { describe, expect, it } from "vitest";

import { type DraftField, emailPending, formatAmount, formatDay, isEmail, pdfUrl, readingNote } from "./orders";

describe("orders", () => {
  it("accepts exactly one email address", () => {
    expect(isEmail("buyer@example.com")).toBe(true);
    expect(isEmail("  buyer@example.co.in ")).toBe(true);
    for (const bad of ["", "buyer", "buyer@example", "a@b.c", "a@example.com, b@example.com", "a b@example.com"]) {
      expect(isEmail(bad)).toBe(false);
    }
  });

  it("formats saved values for display", () => {
    expect(formatDay("2026-10-05")).toBe("5 Oct 2026");
    expect(formatDay(null)).toBe("");
    expect(formatAmount("12500")).toBe("12,500.00");
  });

  it("links the latest or a given revision's PDF", () => {
    expect(pdfUrl("o1")).toBe("/api/v1/orders/o1/pdf");
    expect(pdfUrl("o1", 2, true)).toBe("/api/v1/orders/o1/pdf?revision=2&download=true");
  });

  it("describes how a value was read without inventing precision", () => {
    const f: DraftField = {
      value: "500", display: null, raw: "500", source: "ai", confidence: 0.83, uncertain: true, note: null, corrected_from: null,
      evidence: [{ id: "p1-s2", page: 1, text: "Qty 500", confidence: 0.83 }],
    };
    expect(readingNote(f)).toBe('Read by AI (83% sure): "Qty 500"');
    expect(readingNote({ ...f, source: "reviewer" })).toBe("Entered or confirmed by you.");
    expect(readingNote({ ...f, confidence: null, source: "extracted" })).toBe('Read from the page: "Qty 500"');
  });

  it("knows when an email is still on its way", () => {
    expect(emailPending("QUEUED") && emailPending("SENDING")).toBe(true);
    expect(emailPending("ACCEPTED") || emailPending("UNKNOWN")).toBe(false);
  });
});
