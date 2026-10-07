// Integrations (M5): contracts and plain-language status. Status is always words; tone only reinforces it.

import type { Tone } from "./status";

export type Provider = "google_sheets" | "power_bi" | "ms_graph_mail" | "erp";
export type ConnectionState = "NEEDS_TEST" | "CONNECTED" | "TEST_FAILED" | "RECONNECT_REQUIRED" | "CONFLICT" | "DISCONNECTED";

export type Connection = {
  id: string;
  provider: Provider;
  name: string;
  state: ConnectionState;
  config: Record<string, string | number>;
  has_secret: boolean;
  config_version: number;
  last_test: { at: string | null; ok: boolean | null };
  last_error: { code: string; message: string } | null;
  last_sync_at: string | null;
  sync?: { PENDING: number; SYNCED: number; FAILED: number; CONFLICT: number };
  mock?: boolean;
  version: number;
};

export type PowerBiStatus = {
  state: "NOT_CONFIGURED" | "NOT_CONNECTED" | "NEVER_REFRESHED" | "REFRESHING" | "FRESH" | "STALE" | "FAILED";
  stale?: boolean;
  data_version?: number;
  refreshed_data_version?: number | null;
  last_refreshed_at?: string | null;
  stale_after_minutes?: number;
  last_request?: { state: string; requested_at: string; error_code: string | null } | null;
};

export type FieldSpec = { key: string; label: string; secret?: boolean; multiline?: boolean; type?: "text" | "number" | "email"; hint?: string; optional?: boolean };

export const PROVIDERS: Record<Provider, { title: string; purpose: string; config: FieldSpec[]; secret: FieldSpec[] }> = {
  google_sheets: {
    title: "Google Sheets",
    purpose: "A read-only copy of approved records in a company spreadsheet tab. One row per record, updated in place.",
    config: [
      { key: "spreadsheet_id", label: "Spreadsheet ID", hint: "The long ID in the spreadsheet's web address." },
      { key: "tab", label: "Tab name", optional: true, hint: "Default: Production_Data. Share the sheet with the service account as Editor." },
    ],
    secret: [{ key: "service_account_json", label: "Service account key (JSON)", secret: true, multiline: true }],
  },
  power_bi: {
    title: "Power BI",
    purpose: "Refreshes your Power BI semantic model after approved data changes. Power BI reads the approved-data views.",
    config: [
      { key: "tenant", label: "Microsoft Entra tenant", hint: "Tenant ID or domain, e.g. contoso.onmicrosoft.com." },
      { key: "workspace_id", label: "Workspace ID" },
      { key: "dataset_id", label: "Semantic model ID" },
      { key: "min_interval_minutes", label: "Minimum minutes between refreshes", type: "number", optional: true },
      { key: "stale_after_minutes", label: "Show as out of date after (minutes)", type: "number", optional: true },
    ],
    secret: [
      { key: "client_id", label: "Application (client) ID", secret: true },
      { key: "client_secret", label: "Client secret", secret: true },
    ],
  },
  ms_graph_mail: {
    title: "Microsoft 365 email",
    purpose: "Sends reports from a company mailbox (sending arrives with reports). The test checks the Mail.Send permission; it sends nothing.",
    config: [
      { key: "tenant", label: "Microsoft Entra tenant" },
      { key: "sender_mailbox", label: "Sender mailbox", type: "email" },
    ],
    secret: [
      { key: "client_id", label: "Application (client) ID", secret: true },
      { key: "client_secret", label: "Client secret", secret: true },
    ],
  },
  erp: {
    title: "ERP",
    purpose: "One-way push of approved records to the ERP through an adapter. Only the development mock adapter exists today.",
    config: [{ key: "adapter", label: "Adapter", hint: "\"mock\" in development and tests only." }],
    secret: [],
  },
};

export function connectionStatus(c: Connection): { label: string; tone: Tone; detail: string | null } {
  const err = c.last_error?.message ?? null;
  switch (c.state) {
    case "NEEDS_TEST":
      return { label: "Testing connection", tone: "progress", detail: "Nothing is sent until the test passes." };
    case "CONNECTED": {
      const s = c.sync;
      if (s && s.CONFLICT > 0) return { label: "Connected, with conflicts", tone: "warning", detail: `${s.CONFLICT} record(s) are newer in the destination and were not overwritten.` };
      if (s && s.FAILED > 0) return { label: "Connected, some records failed", tone: "warning", detail: `${s.FAILED} record(s) failed. Retry them below.` };
      if (s && s.PENDING > 0) return { label: "Syncing", tone: "progress", detail: `${s.PENDING} record(s) waiting.` };
      return { label: "Connected", tone: "success", detail: err };
    }
    case "TEST_FAILED":
      return { label: "Test failed", tone: "error", detail: err };
    case "RECONNECT_REQUIRED":
      return { label: "Reconnect required", tone: "error", detail: err ?? "The provider refused the credentials. Replace them and test again." };
    case "CONFLICT":
      return { label: "Stopped: destination conflict", tone: "error", detail: err ?? "Repair the destination, then test again." };
    case "DISCONNECTED":
      return { label: "Disconnected", tone: "neutral", detail: null };
  }
}

export function powerBiLabel(s: PowerBiStatus): { label: string; tone: Tone } {
  switch (s.state) {
    case "FRESH":
      return { label: "Up to date", tone: "success" };
    case "STALE":
      return { label: "Out of date", tone: "warning" };
    case "REFRESHING":
      return { label: "Refreshing", tone: "progress" };
    case "FAILED":
      return { label: "Refresh failed", tone: "error" };
    case "NEVER_REFRESHED":
      return { label: "Not refreshed yet", tone: "warning" };
    case "NOT_CONNECTED":
      return { label: "Not connected", tone: "neutral" };
    default:
      return { label: "Not set up", tone: "neutral" };
  }
}

export function syncStateLabel(state: string): string {
  return (
    { NOT_CONFIGURED: "Not connected", NOT_SYNCED: "Not sent yet", PENDING: "Waiting", SYNCED: "In sheet",
      FAILED: "Failed", CONFLICT: "Conflict" } as Record<string, string>
  )[state] ?? state;
}

/** Build the request body: blank optional config is omitted; secrets are sent only when typed. */
export function connectionBody(provider: Provider, values: Record<string, string>, editing: boolean) {
  const spec = PROVIDERS[provider];
  const config: Record<string, string | number> = {};
  for (const f of spec.config) {
    const v = (values[f.key] ?? "").trim();
    if (v === "") continue;
    config[f.key] = f.type === "number" ? Number(v) : v;
  }
  const typed = spec.secret.filter((f) => (values[f.key] ?? "").trim() !== "");
  let secret: Record<string, string> | undefined;
  if (typed.length > 0 || (!editing && spec.secret.length > 0)) {
    secret = Object.fromEntries(spec.secret.map((f) => [f.key, (values[f.key] ?? "").trim()]));
  }
  if (provider === "erp") config.direction = "OUTBOUND";
  return { config, secret };
}
