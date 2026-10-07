"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend } from "@/lib/api";
import { EXCEPTION_KIND, type ExceptionItem, severityTone } from "@/lib/automation";

// A1 Exceptions: explainable findings, most severe first. Acknowledge, resolve or dismiss with a note.
// Findings never change production data; fixing the cause (e.g. correcting the entry) resolves them.

const STATUS = [["LIVE", "Open"], ["RESOLVED", "Resolved"], ["DISMISSED", "Dismissed"]] as const;

export default function ExceptionsPage() {
  const { session } = useSession();
  const [status, setStatus] = useState<string>("LIVE");
  const [data, setData] = useState<{ status: string; items: ExceptionItem[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState("");

  const load = useCallback(async (s: string) => {
    try {
      const r = await apiGet<{ data: ExceptionItem[] }>(`/exceptions?status=${s}`);
      setData({ status: s, items: r.data });
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Exceptions could not be loaded.");
    }
  }, []);

  useEffect(() => {
    let active = true;
    apiGet<{ data: ExceptionItem[] }>(`/exceptions?status=${status}`).then(
      (r) => active && (setData({ status, items: r.data }), setError(null)),
      (e) => active && setError(e instanceof ApiError ? e.message : "Exceptions could not be loaded."),
    );
    return () => {
      active = false;
    };
  }, [status]);

  async function act(item: ExceptionItem, action: "ACKNOWLEDGE" | "RESOLVE" | "DISMISS") {
    try {
      await apiSend("POST", `/exceptions/${item.id}/actions`, { action, note: notes[item.id] || null });
      setNotice(action === "ACKNOWLEDGE" ? "Acknowledged." : action === "RESOLVE" ? "Marked resolved." : "Dismissed; it will not reopen while the condition lasts.");
      await load(status);
    } catch (e) {
      setNotice(e instanceof ApiError ? (e.fields[0]?.message ?? e.message) : "The action failed.");
    }
  }

  const canAct = hasRole(session, "REVIEWER", "SENDER", "ADMIN");
  const items = data?.status === status ? data.items : null;
  return (
    <>
      <h1>Exceptions</h1>
      <p className="muted">Items that need a look. They explain what was found; they never change production data.</p>
      <div className="row" role="tablist" aria-label="Status" style={{ marginBottom: 12 }}>
        {STATUS.map(([k, label]) => (
          <button key={k} role="tab" aria-selected={status === k} className={status === k ? "primary" : undefined} onClick={() => setStatus(k)}>{label}</button>
        ))}
      </div>
      {error && <div className="banner error" role="alert">{error}</div>}
      <p aria-live="polite" className="meta">{notice}</p>
      {!items && !error && <div className="skeleton" aria-busy="true" style={{ height: 80 }} />}
      {items && items.length === 0 && <div className="card"><p>{status === "LIVE" ? "Nothing needs attention." : "Nothing here."}</p></div>}
      {items && items.length > 0 && (
        <ul className="stack" style={{ listStyle: "none", padding: 0 }}>
          {items.map((i) => (
            <li key={i.id} className="card stack" style={{ gap: 6 }}>
              <div className="row">
                <span className={`badge tone-${severityTone(i.severity)}`}>{i.severity === "CRITICAL" ? "Critical" : i.severity === "WARNING" ? "Warning" : "Info"}</span>
                <strong>{EXCEPTION_KIND[i.kind] ?? i.kind}</strong>
                {i.production_date && <span className="meta">{i.production_date}</span>}
                {i.status === "ACKNOWLEDGED" && <span className="badge tone-neutral">Acknowledged</span>}
              </div>
              <p style={{ margin: 0 }}>{i.reason}</p>
              {i.link && <Link href={i.link}>Open</Link>}
              {i.resolution && <p className="meta" style={{ margin: 0 }}>{i.resolution}</p>}
              {canAct && status === "LIVE" && (
                <div className="row">
                  <label htmlFor={`n-${i.id}`} className="sr-only">Note</label>
                  <input id={`n-${i.id}`} type="text" placeholder="Note (required to resolve or dismiss)" value={notes[i.id] ?? ""}
                    onChange={(e) => setNotes((n) => ({ ...n, [i.id]: e.target.value }))} style={{ flex: "1 1 220px" }} />
                  {i.status === "OPEN" && <button onClick={() => act(i, "ACKNOWLEDGE")}>Acknowledge</button>}
                  <button onClick={() => act(i, "RESOLVE")}>Resolve</button>
                  <button className="danger" onClick={() => act(i, "DISMISS")}>Dismiss</button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
