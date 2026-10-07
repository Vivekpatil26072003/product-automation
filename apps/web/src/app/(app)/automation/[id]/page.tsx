"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useRef, useState } from "react";

import { type ScheduleBody, ScheduleForm } from "@/components/automation/ScheduleForm";
import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, type Issue, apiGet, apiSend } from "@/lib/api";
import { BLOCKED_REASON, type Schedule, cadenceText, runStatus } from "@/lib/automation";

// U9 schedule: details, edit (new version; approval revoked), pause/resume, approve automatic sending for this
// exact version and policy, Run now for a completed period (draft by default), and run history.

const POLL_MS = 4000;

export default function SchedulePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { session } = useSession();
  const [s, setS] = useState<Schedule | null>(null);
  const [error, setError] = useState<{ status: number; text: string } | null>(null);
  const [notice, setNotice] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [issues, setIssues] = useState<Issue[]>([]);
  const [period, setPeriod] = useState({ start: "", end: "" });
  const [pollRun, setPollRun] = useState(0); // bump to (re)start polling after starting a run
  const dialog = useRef<HTMLDialogElement>(null);

  const load = useCallback(async () => {
    try {
      const { data } = await apiGet<{ data: Schedule }>(`/schedules/${id}`);
      setS(data);
      setError(null);
      return data;
    } catch (e) {
      setError({ status: e instanceof ApiError ? e.status : 0, text: e instanceof ApiError ? e.message : "Could not load." });
      return null;
    }
  }, [id]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const data = await load();
      const active = data?.runs?.some((r) => ["QUEUED", "WAITING", "REPORTING", "SENDING"].includes(r.state));
      if (cancelled || !active) return;
      timer = setTimeout(tick, POLL_MS);
    };
    timer = setTimeout(tick, pollRun === 0 ? 0 : POLL_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [load, pollRun]);

  async function act(label: string, fn: () => Promise<void>) {
    setBusy(true);
    setNotice("");
    try {
      await fn();
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : `${label} failed.`);
      setIssues(e instanceof ApiError ? e.fields : []);
    } finally {
      setBusy(false);
    }
  }

  const save = (body: ScheduleBody) => act("Save", async () => {
    if (!s) return;
    await apiSend("PATCH", `/schedules/${id}`, body, { ifMatch: `"${s.row_version}"` });
    setEditing(false);
    setNotice("Saved as a new version. Automatic sending, if used, must be approved again.");
    await load();
  });
  const toggle = () => act("Update", async () => {
    if (!s) return;
    await apiSend("PATCH", `/schedules/${id}`, { active: !s.active }, { ifMatch: `"${s.row_version}"` });
    await load();
  });
  const approve = () => act("Approval", async () => {
    if (!s) return;
    await apiSend("POST", `/schedules/${id}/approve-auto-send`, { version: s.version, confirmed_policy_hash: s.policy.hash, confirmation: true });
    dialog.current?.close();
    setNotice("Automatic sending approved for this version.");
    await load();
  });
  const runNow = () => act("Run now", async () => {
    const body = period.start && period.end ? { period_start: period.start, period_end: period.end } : {};
    await apiSend("POST", `/schedules/${id}/run`, { ...body, mode: "DRAFT_ONLY" });
    setNotice("Run started as a draft. It appears below.");
    await load();
    setPollRun((n) => n + 1);
  });
  const cancelRun = (runId: string) => act("Cancel", async () => {
    await apiSend("POST", `/schedule-runs/${runId}/cancel`);
    await load();
  });

  if (!hasRole(session, "SENDER")) return <p>Scheduled reports are managed by Senders.</p>;
  if (error?.status === 404) return <p>This schedule does not exist, scheduling is off, or you do not have access to it.</p>;
  if (!s) return error ? <div className="banner error" role="alert">{error.text}</div> : <div className="skeleton" aria-busy="true" style={{ height: 120 }} />;

  const recipients = [...s.to.map((a) => `To ${a}`), ...s.cc.map((a) => `Cc ${a}`), ...s.bcc.map((a) => `Bcc ${a}`)];
  return (
    <>
      <p><Link href="/automation">All schedules</Link></p>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>{s.name}</h1>
        <span className={`badge tone-${s.active ? "success" : "neutral"}`} role="status">{s.active ? "Active" : "Paused"}</span>
      </div>
      <p className="meta">{cadenceText(s.config)} · version {s.version}</p>
      {!s.active && s.paused_reason && s.paused_reason !== "PAUSED_BY_USER" && (
        <div className="banner warning" role="alert">
          Paused automatically: {s.paused_reason === "UNKNOWN_SEND" ? "an email outcome is unknown. Reconcile it from the run below, then resume."
            : s.paused_reason === "PERMISSION_REVOKED" ? "the owner no longer has the Sender role or access to every department." : s.paused_reason}
        </div>
      )}
      <p aria-live="polite" className="meta">{notice}</p>

      <div className="row" style={{ marginBottom: 12 }}>
        <button onClick={() => setEditing((v) => !v)} disabled={busy}>{editing ? "Close editor" : "Edit"}</button>
        <button onClick={toggle} disabled={busy}>{s.active ? "Pause" : "Resume"}</button>
        {s.config.mode === "AUTO_SEND" && !s.auto_send_active && <button className="primary" onClick={() => dialog.current?.showModal()} disabled={busy}>Review and approve automatic sending</button>}
      </div>
      {editing && <ScheduleForm initial={s} busy={busy} issues={issues} onSubmit={save} submitLabel="Save new version" />}

      <div className="grid-2">
        <section className="card" aria-labelledby="s-send">
          <h2 id="s-send">Sending</h2>
          <p>{s.config.mode === "AUTO_SEND" ? (s.auto_send_active ? "Sends automatically from " + (s.policy.sender_mailbox ?? "the connected mailbox") + "." : `Prepares drafts: ${BLOCKED_REASON[s.auto_send_blocked_reason ?? ""] ?? s.auto_send_blocked_reason}`) : "Prepares an email draft for you to review and send."}</p>
          <ul className="filelist">{recipients.length ? recipients.map((r) => <li key={r}>{r}</li>) : <li className="meta">No recipients yet.</li>}</ul>
          <p className="meta">Empty periods: {s.config.empty_policy === "SKIP" ? "skipped" : "empty report and draft, never sent automatically"}.</p>
        </section>
        <section className="card" aria-labelledby="s-next">
          <h2 id="s-next">Next runs</h2>
          {s.next_runs.length === 0 ? <p className="meta">None while paused.</p> : (
            <ul className="filelist">{s.next_runs.map((r) => (
              <li key={r.due_at}><span>{new Date(r.due_at).toLocaleString("en-IN", { timeZone: s.config.timezone, dateStyle: "medium", timeStyle: "short" })}</span><span className="meta">reports {r.period_start === r.period_end ? r.period_start : `${r.period_start} to ${r.period_end}`}</span></li>
            ))}</ul>
          )}
          <form className="stack" onSubmit={(e) => { e.preventDefault(); void runNow(); }} aria-label="Run now">
            <p className="meta" style={{ margin: 0 }}>Run now for a finished period (leave blank for the latest one). Always prepares a draft.</p>
            <div className="row">
              <div><label htmlFor="rn-from">From</label><input id="rn-from" type="date" value={period.start} onChange={(e) => setPeriod((p) => ({ ...p, start: e.target.value }))} /></div>
              <div><label htmlFor="rn-to">To</label><input id="rn-to" type="date" value={period.end} onChange={(e) => setPeriod((p) => ({ ...p, end: e.target.value }))} /></div>
            </div>
            <div className="row"><button type="submit" disabled={busy}>Run now</button></div>
          </form>
        </section>
      </div>

      <section className="card table-scroll" tabIndex={0} aria-labelledby="s-runs">
        <h2 id="s-runs">Runs</h2>
        {!s.runs?.length ? <p className="meta">No runs yet.</p> : (
          <table className="data">
            <caption className="sr-only">Run history, newest first</caption>
            <thead><tr><th scope="col">Period</th><th scope="col">Kind</th><th scope="col">Status</th><th scope="col">Result</th><th scope="col" /></tr></thead>
            <tbody>
              {s.runs.map((r) => {
                const st = runStatus(r.state);
                return (
                  <tr key={r.id}>
                    <td>{r.period_start === r.period_end ? r.period_start : `${r.period_start} to ${r.period_end}`}<div className="meta">version {r.version}</div></td>
                    <td>{r.run_kind === "MANUAL" ? "Run now" : "Scheduled"}</td>
                    <td><span className={`badge tone-${st.tone}`}>{st.label}</span>{(r.note || r.error) && <div className="meta">{r.error?.message ?? r.note}</div>}</td>
                    <td>
                      {r.report_id && <Link href={`/reports/${r.report_id}`}>Report</Link>}
                      {r.draft_id && r.report_id && !r.email_id && <> · <Link href={`/reports/${r.report_id}/email?draft=${r.draft_id}`}>Draft</Link></>}
                      {r.email_id && <> · <Link href={`/emails/${r.email_id}`}>Email</Link></>}
                      {r.excluded_pending ? <div className="meta">{r.excluded_pending} unapproved excluded</div> : null}
                    </td>
                    <td>{["QUEUED", "WAITING", "REPORTING"].includes(r.state) && <button className="danger" onClick={() => cancelRun(r.id)} disabled={busy}>Cancel</button>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      <dialog ref={dialog} className="confirm" aria-labelledby="ap-title">
        <div className="stack">
          <h2 id="ap-title" style={{ margin: 0 }}>Approve automatic sending</h2>
          <p style={{ margin: 0 }}>Every future run of <strong>version {s.version}</strong> will email the report without asking, from {s.policy.sender_mailbox ?? "(no mailbox connected)"}, to:</p>
          <ul className="filelist">{recipients.map((r) => <li key={r}>{r}</li>)}</ul>
          <p className="meta" style={{ margin: 0 }}>{cadenceText(s.config)}. Any change to the schedule, recipients or mailbox cancels this approval. Empty periods are never sent automatically.</p>
          <div className="row">
            <button className="primary" onClick={approve} disabled={busy || !s.policy.sender_mailbox}>Approve for this version</button>
            <button onClick={() => dialog.current?.close()}>Cancel</button>
          </div>
        </div>
      </dialog>
    </>
  );
}
