import { describe, expect, it } from "vitest";

import { cadenceText, runStatus, severityTone } from "./automation";

describe("automation helpers", () => {
  it("describes cadences in plain words", () => {
    expect(cadenceText({ cadence: "WEEKLY", local_time: "08:00", weekday: 1, monthday: null, timezone: "Asia/Kolkata" }))
      .toBe("Every Monday at 08:00 (Asia/Kolkata), reporting the previous Monday–Sunday");
    expect(cadenceText({ cadence: "MONTHLY", local_time: "06:00", weekday: null, monthday: 31, timezone: "UTC" }))
      .toContain("or the month's last day");
  });

  it("never calls a sent run delivered, and marks halted runs as errors", () => {
    expect(runStatus("SENT").label).toBe("Accepted by provider");
    expect(runStatus("HALTED").tone).toBe("error");
    expect(severityTone("CRITICAL")).toBe("error");
  });
});
