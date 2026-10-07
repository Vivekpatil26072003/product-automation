import { describe, expect, it } from "vitest";

import { defaultFilter, formatQty, readFilter, toExportFilter, toQuery } from "./filters";

describe("record filters in the URL", () => {
  const today = new Date(2026, 8, 29);

  it("defaults to the last 7 local days including today", () => {
    expect(defaultFilter(today)).toMatchObject({ date_from: "2026-09-23", date_to: "2026-09-29" });
  });

  it("round-trips through the query string", () => {
    const f = readFilter(new URLSearchParams("date_from=2026-09-27&date_to=2026-09-27&unit=m&include_archived=true"), today);
    expect(f).toMatchObject({ date_from: "2026-09-27", unit: "m", include_archived: true, department_id: "" });
    expect(toQuery(f, { sort: "date_asc" })).toBe("date_from=2026-09-27&date_to=2026-09-27&unit=m&include_archived=true&sort=date_asc");
  });

  it("maps to the export body without empty values", () => {
    const f = readFilter(new URLSearchParams("date_from=2026-09-27&date_to=2026-09-27&department_id=abc"), today);
    expect(toExportFilter(f)).toEqual({
      date_from: "2026-09-27", date_to: "2026-09-27", department_ids: ["abc"], statuses: [], units: [],
      operator_query: null, q: null, include_archived: false,
    });
  });

  it("formats quantities in the Indian grouping used on screen", () => {
    expect(formatQty("4830.000", "m")).toBe("4,830 m");
    expect(formatQty("-1170.000")).toBe("-1,170");
    expect(formatQty("980.500", "m")).toBe("980.5 m");
  });
});
