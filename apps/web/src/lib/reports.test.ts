import { describe, expect, it } from "vitest";

import { emailStatus, formatBytes, parseRecipients, reportStatus } from "./reports";

describe("reports and email helpers", () => {
  it("splits recipients on commas, semicolons and lines", () => {
    expect(parseRecipients("a@x.com, b@y.com;\n c@z.com ,,")).toEqual(["a@x.com", "b@y.com", "c@z.com"]);
  });

  it("never calls provider acceptance delivery", () => {
    expect(emailStatus("ACCEPTED").label).toBe("Accepted by provider");
    expect(emailStatus("UNKNOWN").label).toBe("Outcome unknown");
    expect(Object.values({ a: emailStatus("ACCEPTED").label }).join()).not.toMatch(/deliver/i);
  });

  it("shows outdated before ready", () => {
    expect(reportStatus({ state: "READY", outdated: true }).label).toBe("Outdated");
    expect(reportStatus({ state: "READY", outdated: false }).label).toBe("Ready");
    expect(reportStatus({ state: "FAILED", outdated: false }).tone).toBe("error");
    expect(formatBytes(3500)).toBe("3 KB");
  });
});
