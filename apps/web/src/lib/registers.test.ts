import { describe, expect, it } from "vitest";

import { cellText, compareMachines, parseCell, registerFileUrl, regKey } from "./registers";

describe("pick registers", () => {
  it("shows a cell the way it is written: reading, picks, mark", () => {
    expect(cellText({ reading: "2282", picks: "24", status: null })).toBe("2282 24");
    expect(cellText({ reading: "1815", picks: null, status: "S/C" })).toBe("1815 S/C");
    expect(cellText({ reading: null, picks: null, status: "B.FALL" })).toBe("B.FALL");
    expect(cellText(undefined)).toBe("");
  });

  it("parses what a person types into a cell", () => {
    expect(parseCell("2282 24", 1)).toEqual({ ok: true, value: { reading: "2282", picks: "24", status: null } });
    expect(parseCell(" 1815  s/c ", 0)).toEqual({ ok: true, value: { reading: "1815", picks: null, status: "S/C" } });
    expect(parseCell("b.fall", 2)).toEqual({ ok: true, value: { reading: null, picks: null, status: "B.FALL" } });
    expect(parseCell("", 3)).toEqual({ ok: true, value: { reading: null, picks: null, status: null } });
    expect(parseCell("2,282 24", 1)).toEqual({ ok: true, value: { reading: "2282", picks: "24", status: null } });
  });

  it("refuses cells that cannot be stored", () => {
    expect(parseCell("2230 24", 0).ok).toBe(false); // the start reading has no picks
    expect(parseCell("2282 24 7", 1).ok).toBe(false);
    expect(parseCell("S/C 1815", 1).ok).toBe(false); // numbers first, then the mark
    expect(parseCell("1.23456", 1).ok).toBe(false);
  });

  it("orders machines by number and builds links and keys", () => {
    expect(["55", "9", "12A", "27"].sort(compareMachines)).toEqual(["9", "12A", "27", "55"]);
    expect(registerFileUrl("r1", "sql")).toBe("/api/v1/registers/r1/file?format=sql");
    expect(regKey("II", "27", 1)).toBe("II|27|1");
  });
});
