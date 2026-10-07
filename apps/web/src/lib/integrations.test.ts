import { describe, expect, it } from "vitest";

import { type Connection, connectionBody, connectionStatus, powerBiLabel, syncStateLabel } from "./integrations";

const base: Connection = {
  id: "c1", provider: "google_sheets", name: "Sheet", state: "CONNECTED", config: {}, has_secret: true,
  config_version: 1, last_test: { at: null, ok: true }, last_error: null, last_sync_at: null,
  sync: { PENDING: 0, SYNCED: 5, FAILED: 0, CONFLICT: 0 }, version: 1,
};

describe("integrations", () => {
  it("never sends a blank secret when editing (credentials are kept)", () => {
    const body = connectionBody("power_bi", { tenant: "t", workspace_id: "w", dataset_id: "d", client_id: "", client_secret: "" }, true);
    expect(body.secret).toBeUndefined();
    expect(body.config).toEqual({ tenant: "t", workspace_id: "w", dataset_id: "d" });
  });

  it("sends the full secret object once any secret field is typed", () => {
    const body = connectionBody("power_bi", { tenant: "t", workspace_id: "w", dataset_id: "d", client_secret: "x", min_interval_minutes: "45" }, true);
    expect(body.secret).toEqual({ client_id: "", client_secret: "x" });
    expect(body.config.min_interval_minutes).toBe(45);
  });

  it("describes sync problems in words", () => {
    expect(connectionStatus(base).label).toBe("Connected");
    expect(connectionStatus({ ...base, sync: { PENDING: 0, SYNCED: 4, FAILED: 1, CONFLICT: 0 } }).label).toMatch(/failed/);
    expect(connectionStatus({ ...base, state: "CONFLICT", last_error: { code: "DUPLICATE_KEY", message: "dup" } }).detail).toBe("dup");
    expect(powerBiLabel({ state: "STALE" }).label).toBe("Out of date");
    expect(syncStateLabel("SYNCED")).toBe("In sheet");
  });
});
