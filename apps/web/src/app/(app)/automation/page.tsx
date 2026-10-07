"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { type ScheduleBody, ScheduleForm } from "@/components/automation/ScheduleForm";
import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, type Issue, apiGet, apiSend } from "@/lib/api";
import { type Schedule, cadenceText } from "@/lib/automation";

// U9 Automation: the Sender's scheduled reports. Draft-only unless auto-send is approved per version.

export default function AutomationPage() {
  const { session } = useSession();
  const router = useRouter();
  const [rows, setRows] = useState<Schedule[] | null>(null);
  const [error, setError] = useState<{ code: string; text: string } | null>(null);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [issues, setIssues] = useState<Issue[]>([]);

  useEffect(() => {
    let active = true;
    apiGet<{ data: Schedule[] }>("/schedules").then(
      (r) => active && (setRows(r.data), setError(null)),
      (e) => active && setError({ code: e instanceof ApiError ? e.code : "", text: e instanceof ApiError ? e.message : "Schedules could not be loaded." }),
    );
    return () => {
      active = false;
    };
  }, []);

  async function create(body: ScheduleBody) {
    setBusy(true);
    setIssues([]);
    try {
      const r = await apiSend<{ data: Schedule }>("POST", "/schedules", body);
      router.push(`/automation/${r.data.id}`);
    } catch (e) {
      setIssues(e instanceof ApiError ? e.fields : []);
      setError({ code: "", text: e instanceof ApiError ? e.message : "The schedule could not be saved." });
    } finally {
      setBusy(false);
    }
  }

  if (!hasRole(session, "SENDER")) return <p>Scheduled reports are managed by Senders.</p>;
  if (error?.code === "FEATURE_DISABLED") {
    return (<><h1>Automation</h1><div className="card"><p>Scheduled reports are turned off for this company. An administrator can turn them on in Automation settings.</p></div></>);
  }

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>Automation</h1>
        {!creating && <button className="primary" onClick={() => setCreating(true)}>New schedule</button>}
      </div>
      <p className="muted">Scheduled reports use approved records only. Each run prepares a draft unless automatic sending has been approved.</p>
      {error && <div className="banner error" role="alert">{error.text}</div>}
      {creating && <ScheduleForm initial={null} busy={busy} issues={issues} onSubmit={create} submitLabel="Create schedule" />}
      {!rows && !error && <div className="skeleton" aria-busy="true" style={{ height: 80 }} />}
      {rows && rows.length === 0 && !creating && <div className="card"><p>No schedules yet.</p></div>}
      {rows && rows.length > 0 && (
        <ul className="stack" style={{ listStyle: "none", padding: 0 }}>
          {rows.map((s) => (
            <li key={s.id} className="card">
              <div className="row" style={{ justifyContent: "space-between" }}>
                <Link href={`/automation/${s.id}`}><strong>{s.name}</strong></Link>
                <span className={`badge tone-${s.active ? "success" : "neutral"}`}>{s.active ? "Active" : "Paused"}</span>
              </div>
              <p className="meta" style={{ margin: "4px 0" }}>{cadenceText(s.config)} · version {s.version}</p>
              <p style={{ margin: 0 }}>
                {s.config.mode === "AUTO_SEND" ? (s.auto_send_active ? "Sends automatically (approved)" : "Automatic sending not approved: prepares drafts") : "Prepares drafts"}
                {s.next_runs[0] && <span className="meta"> · next {new Date(s.next_runs[0].due_at).toLocaleString("en-IN", { timeZone: s.config.timezone, dateStyle: "medium", timeStyle: "short" })}</span>}
              </p>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
