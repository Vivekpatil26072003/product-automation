"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend } from "@/lib/api";

// Operations (M8, administrators): health and alerts, retention (dry run, purge, holds) and ROI measurement.

type Counts = Record<string, { eligible: number; held: number }>;
type Status = {
  sends_paused: boolean;
  queue: Record<string, number>;
  company: Record<string, number>;
  alerts: { code: string; severity: string; message: string; value: number }[];
  retention: {
    eligible_now: Counts;
    runs: { id: string; dry_run: boolean; purged: number; held: number; failed: number; started_at: string }[];
    holds: { id: string; object_type: string; object_id: string; reason: string; created_at: string }[];
  };
};
type Roi = {
  period: { from: string; to: string };
  pilot: Record<string, number | null>;
  baseline: Record<string, number | string | null> | null;
  comparison: Record<string, number | string> | null;
  comparison_unavailable: string[];
};
const CATEGORY: Record<string, string> = {
  REJECTED_UPLOAD: "Rejected files (7 days)", SOURCE: "Original and derived files",
  REPORT_FILE: "Report PDFs", EXPORT_FILE: "Excel exports",
};
const PILOT: [string, string][] = [
  ["reports_ready", "Reports produced"], ["report_generation_minutes_median", "Report generation (median minutes)"],
  ["email_preparation_minutes_median", "Email preparation (median minutes)"], ["extraction_minutes_median", "Upload to extracted entries (median minutes)"],
  ["entries_approved", "Entries approved"], ["data_entry_rows_avoided", "Entries not typed by hand"],
  ["correction_rate_pct", "Entries corrected before approval (%)"], ["exception_rate_pct", "Exceptions per record (%)"],
  ["followups_per_week", "Reminders per week"],
];
const BASE: [string, string][] = [
  ["manual_minutes_per_report", "Minutes to prepare one report by hand"], ["manual_minutes_per_entry", "Minutes to type one entry"],
  ["manual_minutes_per_email", "Minutes to prepare one email"], ["manual_followups_per_week", "Manual follow-ups per week"],
  ["manual_correction_rate_pct", "Correction rate before (%)"],
];

export default function OperationsPage() {
  const { session } = useSession();
  const [status, setStatus] = useState<Status | null>(null);
  const [roi, setRoi] = useState<Roi | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [hold, setHold] = useState({ object_type: "report", object_id: "", reason: "" });
  const [base, setBase] = useState<Record<string, string>>({});
  const confirm = useRef<HTMLDialogElement>(null);

  const load = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([apiGet<{ data: Status }>("/ops/status"), apiGet<{ data: Roi }>("/roi")]);
      setStatus(s.data);
      setRoi(r.data);
      setBase(Object.fromEntries(BASE.map(([k]) => [k, r.data.baseline?.[k] == null ? "" : String(r.data.baseline[k])])));
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : "Could not load operations data.");
    }
  }, []);

  useEffect(() => {
    let active = true;
    Promise.all([apiGet<{ data: Status }>("/ops/status"), apiGet<{ data: Roi }>("/roi")]).then(
      ([s, r]) => {
        if (!active) return;
        setStatus(s.data);
        setRoi(r.data);
        setBase(Object.fromEntries(BASE.map(([k]) => [k, r.data.baseline?.[k] == null ? "" : String(r.data.baseline[k])])));
      },
      (e) => active && setNotice(e instanceof ApiError ? e.message : "Could not load operations data."),
    );
    return () => {
      active = false;
    };
  }, []);

  async function act(label: string, fn: () => Promise<string>) {
    setBusy(true);
    try {
      setNotice(await fn());
      await load();
    } catch (e) {
      setNotice(e instanceof ApiError ? (e.fields[0]?.message ?? e.message) : `${label} failed.`);
    } finally {
      setBusy(false);
    }
  }

  const retention = (dry: boolean) => act("Retention", async () => {
    confirm.current?.close();
    const r = await apiSend<{ data: { purged: number; held: number; failed: number } }>("POST", "/retention/run", { dry_run: dry });
    return dry ? "Dry run recorded; nothing was deleted." : `Purged ${r.data.purged}, held ${r.data.held}, failed ${r.data.failed} (failures are retried on the next run).`;
  });
  const addHold = () => act("Hold", async () => {
    await apiSend("POST", "/retention/holds", hold);
    setHold({ ...hold, object_id: "", reason: "" });
    return "Hold placed.";
  });
  const release = (id: string) => act("Release", async () => {
    await apiSend("POST", `/retention/holds/${id}/release`);
    return "Hold released.";
  });
  const saveBaseline = () => act("Baseline", async () => {
    const version = Number(roi?.baseline?.version ?? 0);
    const body = Object.fromEntries(BASE.map(([k]) => [k, base[k] === "" ? null : Number(base[k])]));
    await apiSend("PUT", "/roi/baseline", body, { ifMatch: `"${version}"` });
    return "Baseline saved.";
  });

  if (!hasRole(session, "ADMIN")) return <p>Only administrators can see operations.</p>;
  if (!status || !roi) return notice ? <div className="banner error" role="alert">{notice}</div> : <div className="skeleton" aria-busy="true" style={{ height: 120 }} />;

  const fmt = (n: number | null | undefined) => (n == null ? "–" : n.toLocaleString("en-IN", { maximumFractionDigits: 2 }));
  return (
    <>
      <h1>Operations</h1>
      <p aria-live="polite" className="meta">{notice}</p>
      <section className="card stack" aria-labelledby="op-health">
        <h2 id="op-health" style={{ margin: 0 }}>Health</h2>
        {status.sends_paused && <div className="banner warning" role="status" style={{ margin: 0 }}>Outbound email is paused (SENDS_PAUSED). Emails wait in the queue.</div>}
        {status.alerts.length === 0 ? <p style={{ margin: 0 }}><span className="badge tone-success">No alerts</span></p> : (
          <ul className="issues">{status.alerts.map((a) => <li key={a.code} className={a.severity === "critical" ? "reason" : undefined}>{a.message}</li>)}</ul>
        )}
        <dl className="kv">
          <div><dt className="meta">Oldest waiting job</dt><dd>{fmt(status.queue.jobs_oldest_due_seconds)} s</dd></div>
          <div><dt className="meta">Jobs failed (15 min)</dt><dd>{fmt(status.queue.jobs_failed_15m)} of {fmt(status.queue.jobs_finished_15m)}</dd></div>
          <div><dt className="meta">Emails with unknown outcome</dt><dd>{fmt(status.company.email_unknown)}</dd></div>
          <div><dt className="meta">Oldest pending sync</dt><dd>{fmt(status.company.sync_oldest_pending_seconds)} s</dd></div>
        </dl>
      </section>

      <section className="card stack" aria-labelledby="op-ret">
        <h2 id="op-ret" style={{ margin: 0 }}>Retention</h2>
        <p className="meta" style={{ margin: 0 }}>Deletes file bytes past their retention period; records, figures, hashes and audit remain. Held items are never purged. A daily purge also runs automatically.</p>
        <table className="data">
          <caption className="sr-only">Items past their retention period now</caption>
          <thead><tr><th scope="col">Category</th><th scope="col">Eligible now</th><th scope="col">On hold</th></tr></thead>
          <tbody>{Object.entries(status.retention.eligible_now).map(([k, v]) => (
            <tr key={k}><td>{CATEGORY[k] ?? k}</td><td className="tnum">{v.eligible}</td><td className="tnum">{v.held}</td></tr>
          ))}</tbody>
        </table>
        <div className="row">
          <button onClick={() => retention(true)} disabled={busy}>Dry run</button>
          <button className="danger" onClick={() => confirm.current?.showModal()} disabled={busy}>Purge now</button>
        </div>
        {status.retention.runs.length > 0 && (
          <ul className="filelist" aria-label="Recent retention runs">{status.retention.runs.map((r) => (
            <li key={r.id}><span>{new Date(r.started_at).toLocaleString("en-IN", { timeZone: session.timezone })} · {r.dry_run ? "dry run" : "purge"}</span>
              <span className="meta">purged {r.purged} · held {r.held} · failed {r.failed}</span></li>
          ))}</ul>
        )}
        <h3 style={{ margin: "8px 0 0" }}>Holds</h3>
        {status.retention.holds.length === 0 ? <p className="meta" style={{ margin: 0 }}>No active holds.</p> : (
          <ul className="filelist">{status.retention.holds.map((h) => (
            <li key={h.id}><span>{h.object_type} {h.object_id.slice(0, 8)}… · {h.reason}</span><button onClick={() => release(h.id)} disabled={busy}>Release</button></li>
          ))}</ul>
        )}
        <form className="row" onSubmit={(e) => { e.preventDefault(); void addHold(); }} aria-label="Place a hold">
          <div><label htmlFor="h-type">Item</label>
            <select id="h-type" value={hold.object_type} onChange={(e) => setHold({ ...hold, object_type: e.target.value })}>
              <option value="report">Report</option><option value="batch">Upload batch</option><option value="upload">Uploaded file</option>
            </select></div>
          <div><label htmlFor="h-id">ID</label><input id="h-id" type="text" value={hold.object_id} onChange={(e) => setHold({ ...hold, object_id: e.target.value.trim() })} /></div>
          <div style={{ flex: "1 1 200px" }}><label htmlFor="h-reason">Reason</label><input id="h-reason" type="text" className="wide" value={hold.reason} onChange={(e) => setHold({ ...hold, reason: e.target.value })} /></div>
          <button type="submit" disabled={busy || !hold.object_id || hold.reason.trim().length < 3}>Place hold</button>
        </form>
      </section>

      <section className="card stack" aria-labelledby="op-roi">
        <h2 id="op-roi" style={{ margin: 0 }}>Time saved (ROI)</h2>
        <p className="meta" style={{ margin: 0 }}>Pilot figures are measured from {roi.period.from} to {roi.period.to}. Savings appear only once a measured manual baseline exists and there is enough pilot data.</p>
        <table className="data">
          <caption className="sr-only">Measured pilot figures</caption>
          <tbody>{PILOT.map(([k, label]) => <tr key={k}><th scope="row">{label}</th><td className="tnum">{fmt(roi.pilot[k] as number | null)}</td></tr>)}</tbody>
        </table>
        {roi.comparison ? (
          <div className="banner info" role="status" style={{ margin: 0 }}>
            About {fmt(roi.comparison.minutes_saved_per_report as number)} minutes saved per report; {fmt(roi.comparison.report_minutes_saved_in_period as number)} report minutes and {fmt(roi.comparison.entry_minutes_saved_in_period as number)} data-entry minutes in the period. {roi.comparison.note}
          </div>
        ) : (
          <ul className="issues" aria-label="Why savings are not shown yet">{roi.comparison_unavailable.map((r) => <li key={r}>{r}</li>)}</ul>
        )}
        <form className="stack" onSubmit={(e) => { e.preventDefault(); void saveBaseline(); }} aria-label="Measured manual baseline">
          <h3 style={{ margin: 0 }}>Measured manual baseline</h3>
          <div className="row">{BASE.map(([k, label]) => (
            <div key={k}><label htmlFor={`b-${k}`}>{label}</label><input id={`b-${k}`} type="number" min={0} step="0.01" value={base[k] ?? ""} onChange={(e) => setBase({ ...base, [k]: e.target.value })} /></div>
          ))}</div>
          <div className="row"><button type="submit" disabled={busy}>Save baseline</button></div>
        </form>
      </section>

      <dialog ref={confirm} className="confirm" aria-labelledby="pg-title">
        <div className="stack">
          <h2 id="pg-title" style={{ margin: 0 }}>Purge files now?</h2>
          <p style={{ margin: 0 }}>File bytes past their retention period are deleted and cannot be recovered from this system. Records, report figures and audit remain; held items are skipped.</p>
          <div className="row"><button className="danger" onClick={() => retention(false)} disabled={busy}>Purge</button><button onClick={() => confirm.current?.close()}>Cancel</button></div>
        </div>
      </dialog>
    </>
  );
}
