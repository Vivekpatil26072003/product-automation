"use client";

import { useCallback, useEffect, useState } from "react";

import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend, type Issue } from "@/lib/api";
import {
  type Connection, type PowerBiStatus, type Provider, PROVIDERS, connectionBody, connectionStatus, powerBiLabel,
} from "@/lib/integrations";

// Integrations (FR13, FR15, FR22): administrators connect destinations. Credentials are write-only:
// the page never receives them back, only whether they are stored. Polls while a test or sync is running.

type ListResponse = {
  data: Connection[];
  providers: { provider: Provider; available: boolean }[];
  encryption_configured: boolean;
};

const POLL_MS = 3000;

function busy(c: Connection): boolean {
  return c.state === "NEEDS_TEST" || (c.state === "CONNECTED" && (c.sync?.PENDING ?? 0) > 0);
}

function when(iso: string | null, tz: string): string {
  return iso ? new Date(iso).toLocaleString("en-IN", { timeZone: tz }) : "never";
}

export default function IntegrationsPage() {
  const { session } = useSession();
  const [list, setList] = useState<ListResponse | null>(null);
  const [powerBi, setPowerBi] = useState<PowerBiStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [editing, setEditing] = useState<Provider | null>(null);
  const [pollRun, setPollRun] = useState(0);

  const load = useCallback(async () => {
    try {
      const [l, p] = await Promise.all([
        apiGet<ListResponse>("/integrations"),
        apiGet<{ data: PowerBiStatus }>("/powerbi/status"),
      ]);
      setList(l);
      setPowerBi(p.data);
      setError(null);
      return l;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not load integrations.");
      return null;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const l = await load();
      if (cancelled || (l && !l.data.some(busy))) return;
      timer = setTimeout(tick, POLL_MS);
    };
    timer = setTimeout(tick, pollRun === 0 ? 0 : POLL_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [load, pollRun]);

  const refreshSoon = () => setPollRun((n) => n + 1);

  async function command(c: Connection, path: string, done: string) {
    setNotice("");
    try {
      await apiSend("POST", `/integrations/${c.id}/${path}`);
      setNotice(done);
      await load();
      refreshSoon();
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : "The action failed.");
    }
  }

  async function disconnect(c: Connection) {
    if (!window.confirm(`Disconnect ${PROVIDERS[c.provider].title}? Stored credentials are erased; nothing more is sent.`)) return;
    try {
      await apiSend("DELETE", `/integrations/${c.id}`, undefined, { ifMatch: `"${c.version}"` });
      setNotice(`${PROVIDERS[c.provider].title} disconnected. Its history is kept.`);
      await load();
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : "Could not disconnect.");
    }
  }

  async function refreshPowerBi() {
    try {
      await apiSend("POST", "/powerbi/refresh");
      setNotice("Refresh requested. Requests close together are combined into one refresh.");
      await load();
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : "Could not request a refresh.");
    }
  }

  if (!hasRole(session, "ADMIN")) {
    return <p>Only administrators can manage integrations.</p>;
  }

  return (
    <>
      <h1>Integrations</h1>
      <p className="muted">Only approved records leave this system. Destinations receive nothing until their connection test passes.</p>
      {error && <div className="banner error" role="alert">{error}</div>}
      {list && !list.encryption_configured && (
        <div className="banner warning" role="alert">
          Credential encryption is not configured on the server, so connections cannot be saved. Ask the operator to set INTEGRATION_KEYS.
        </div>
      )}
      <p aria-live="polite" className="meta">{notice}</p>
      {!list && !error && <div className="skeleton" aria-busy="true" style={{ height: 120 }} />}
      {list && (
        <div className="stack">
          {list.providers.map(({ provider, available }) => {
            const c = list.data.find((x) => x.provider === provider) ?? null;
            const spec = PROVIDERS[provider];
            const st = c ? connectionStatus(c) : null;
            return (
              <section key={provider} className="card stack" aria-labelledby={`int-${provider}`}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <h2 id={`int-${provider}`} style={{ margin: 0 }}>
                    {spec.title} {c?.mock && <span className="badge tone-warning">Mock adapter (development only)</span>}
                  </h2>
                  {st ? <span className={`badge tone-${st.tone}`}>{st.label}</span> : <span className="badge tone-neutral">Not set up</span>}
                </div>
                <p className="meta" style={{ margin: 0 }}>{spec.purpose}</p>
                {!available && !c && <p className="meta">Turn on ERP integration in company settings to set this up.</p>}
                {c && (
                  <>
                    {st?.detail && <p className={st.tone === "error" ? "reason" : "meta"} style={{ margin: 0 }}>{st.detail}</p>}
                    <dl className="kv">
                      <div><dt className="meta">Name</dt><dd>{c.name}</dd></div>
                      {spec.config.map((f) =>
                        c.config[f.key] !== undefined ? (
                          <div key={f.key}><dt className="meta">{f.label}</dt><dd style={{ overflowWrap: "anywhere" }}>{String(c.config[f.key])}</dd></div>
                        ) : null,
                      )}
                      <div><dt className="meta">Credentials</dt><dd>{c.has_secret ? "Stored (hidden)" : "None"}</dd></div>
                      <div><dt className="meta">Last test</dt><dd>{when(c.last_test.at, session.timezone)}{c.last_test.ok === false ? " (failed)" : ""}</dd></div>
                      {c.sync && (
                        <div>
                          <dt className="meta">Records</dt>
                          <dd className="tnum">{c.sync.SYNCED} sent · {c.sync.PENDING} waiting · {c.sync.FAILED} failed · {c.sync.CONFLICT} conflicts</dd>
                        </div>
                      )}
                      <div><dt className="meta">Last {provider === "power_bi" ? "refresh request" : "sync"}</dt><dd>{when(c.last_sync_at, session.timezone)}</dd></div>
                    </dl>
                    {provider === "power_bi" && powerBi && powerBi.state !== "NOT_CONFIGURED" && (
                      <p style={{ margin: 0 }}>
                        <span className={`badge tone-${powerBiLabel(powerBi).tone}`}>{powerBiLabel(powerBi).label}</span>{" "}
                        <span className="meta">
                          Power BI holds data version {powerBi.refreshed_data_version ?? "none"} of {powerBi.data_version}; last refreshed{" "}
                          {when(powerBi.last_refreshed_at ?? null, session.timezone)}.
                        </span>
                      </p>
                    )}
                  </>
                )}
                {editing === provider ? (
                  <ConnectionForm
                    provider={provider}
                    existing={c}
                    onCancel={() => setEditing(null)}
                    onSaved={(msg) => {
                      setEditing(null);
                      setNotice(msg);
                      void load();
                      refreshSoon();
                    }}
                  />
                ) : (
                  <div className="row">
                    {!c && available && (
                      <button className="primary" disabled={!list.encryption_configured && spec.secret.length > 0} onClick={() => setEditing(provider)}>
                        Connect {spec.title}
                      </button>
                    )}
                    {c && <button onClick={() => setEditing(provider)}>Edit settings</button>}
                    {c && <button onClick={() => command(c, "test", "Connection test queued.")}>Test connection</button>}
                    {c?.sync && c.state === "CONNECTED" && (
                      <button onClick={() => command(c, "reconcile", "Every approved record will be checked and re-sent where needed.")}>Resend all records</button>
                    )}
                    {c?.sync && c.state === "CONNECTED" && c.sync.FAILED > 0 && (
                      <button onClick={() => command(c, "retry-failed", "Failed records queued again.")}>Retry failed records</button>
                    )}
                    {c && provider === "power_bi" && c.state === "CONNECTED" && <button onClick={refreshPowerBi}>Refresh Power BI now</button>}
                    {c && <button className="danger" onClick={() => disconnect(c)}>Disconnect</button>}
                  </div>
                )}
              </section>
            );
          })}
        </div>
      )}
    </>
  );
}

function ConnectionForm({ provider, existing, onCancel, onSaved }: {
  provider: Provider;
  existing: Connection | null;
  onCancel: () => void;
  onSaved: (message: string) => void;
}) {
  const spec = PROVIDERS[provider];
  const [values, setValues] = useState<Record<string, string>>(() => ({
    name: existing?.name ?? spec.title,
    ...Object.fromEntries(spec.config.map((f) => [f.key, existing?.config[f.key] !== undefined ? String(existing.config[f.key]) : ""])),
  }));
  const [issues, setIssues] = useState<Issue[]>([]);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const fieldError = (path: string) => issues.find((i) => i.field === path)?.message;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setIssues([]);
    setMessage("");
    const { config, secret } = connectionBody(provider, values, existing !== null);
    try {
      if (existing) {
        await apiSend("PATCH", `/integrations/${existing.id}`, { name: values.name, config, ...(secret ? { secret } : {}) },
          { ifMatch: `"${existing.version}"` });
      } else {
        await apiSend("POST", "/integrations", { provider, name: values.name, config, ...(secret ? { secret } : {}) });
      }
      onSaved("Saved. The connection is being tested; nothing is sent until the test passes.");
    } catch (err) {
      if (err instanceof ApiError) {
        setIssues(err.fields);
        setMessage(err.message);
      } else {
        setMessage("Could not save.");
      }
    } finally {
      setSaving(false);
    }
  }

  const input = (f: { key: string; label: string; secret?: boolean; multiline?: boolean; type?: string; hint?: string; optional?: boolean }, path: string) => {
    const id = `f-${provider}-${f.key}`;
    const err = fieldError(path);
    const common = {
      id,
      value: values[f.key] ?? "",
      "aria-invalid": err ? true : undefined,
      "aria-describedby": err || f.hint ? `${id}-help` : undefined,
      autoComplete: "off",
      onChange: (ev: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setValues((v) => ({ ...v, [f.key]: ev.target.value })),
    };
    const placeholder = f.secret && existing?.has_secret ? "Stored. Leave blank to keep it." : undefined;
    return (
      <div key={f.key}>
        <label htmlFor={id}>{f.label}{f.optional ? " (optional)" : ""}</label>
        {f.multiline ? (
          <textarea {...common} rows={4} placeholder={placeholder} spellCheck={false} className="wide" />
        ) : (
          <input {...common} type={f.secret ? "password" : (f.type ?? "text")} placeholder={placeholder} className="wide" />
        )}
        {(err || f.hint) && <p id={`${id}-help`} className={err ? "reason" : "meta"} style={{ margin: "4px 0 0" }}>{err ?? f.hint}</p>}
      </div>
    );
  };

  return (
    <form onSubmit={save} className="stack" aria-label={`${spec.title} connection`}>
      {input({ key: "name", label: "Display name" }, "name")}
      {spec.config.map((f) => input(f, `config.${f.key}`))}
      {spec.secret.map((f) => input(f, `secret.${f.key}`))}
      {spec.secret.length > 0 && <p className="meta" style={{ margin: 0 }}>Credentials are encrypted and can never be viewed again, only replaced.</p>}
      {message && <div className="banner error" role="alert">{message}</div>}
      <div className="row">
        <button type="submit" className="primary" disabled={saving}>{saving ? "Saving…" : "Save and test"}</button>
        <button type="button" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}
