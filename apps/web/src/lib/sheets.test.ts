import { describe, expect, it } from "vitest";

import { cellKey, dayLabel, fileUrl, fmt, isEmail, validNumber } from "./sheets";

describe("daily sheets", () => {
  it("shows numbers with Indian grouping and never invents a value", () => {
    expect(fmt("157972")).toBe("1,57,972");
    expect(fmt("64.5639")).toBe("64.56");
    expect(fmt(null)).toBe("");
    expect(fmt("")).toBe("");
  });

  it("accepts only numbers a sheet cell can hold", () => {
    for (const ok of ["7026", "3.33", "-2.6", "1,215", "0.1234", ""]) expect(validNumber(ok)).toBe(true);
    for (const bad of ["12a", "1.23456", "1/2", "--3"]) expect(validNumber(bad)).toBe(false);
  });

  it("builds download links and cell keys", () => {
    expect(fileUrl("s1", "xlsx")).toBe("/api/v1/sheets/s1/file?format=xlsx");
    expect(fileUrl("s1", "pdf", false)).toBe("/api/v1/sheets/s1/file?format=pdf&download=false");
    expect(cellKey("sulzer", "picks", "II")).toBe("sulzer|picks|II");
  });

  it("formats the sheet day and checks one email address", () => {
    expect(dayLabel("2026-10-02")).toBe("2 Oct 2026");
    expect(isEmail("owner@company.com")).toBe(true);
    expect(isEmail("a@b")).toBe(false);
  });
});
