"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useCallback, useEffect, useState } from "react";

import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend } from "@/lib/api";
import { formatQty } from "@/lib/filters";
import { type Report, emailStatus, formatBytes, periodText, reportStatus } from "@/lib/reports";

// U6 Report: snapshot metadata, the PDF (preview and download use the same stored bytes), Excel snapshot,
// compose email (Sender), regenerate (new version) and retry (failed render, same snapshot).

const POLL_MS = 2000;
const STATUS_WORDS: Record<string, string> = { RUNNING: "Running", COMPLETED: "Completed", PENDING: "Pending", HOLD: "Hold" };

type FileLink = { url: string; download_url: string; sha256: string; bytes: number; name: string; expires_at: string };

export default function ReportPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { session } = useSession();
  const router = useRouter();
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<{ status: number; text: string } | null>(null);
  const [file, setFile] = useState<FileLink | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [pollRun, setPollRun] = useState(0);

  const load = useCallback(async () => {
    try {
      const { data } = await apiGet<{ data: Report }>(`/reports/${id}`);
      setReport(data);
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
      if (cancelled || (data && (data.state === "READY" || data.state === "FAILED") &&
        (!data.excel_export || data.excel_export.state !== "QUEUED"))) return;
      timer = setTimeout(tick, POLL_MS);
    };
    timer = setTimeout(tick, pollRun === 0 ? 0 : POLL_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [load, pollRun]);

  // A fresh short-lived link whenever the report becomes READY (links expire after 5 minutes).
  useEffect(() => {
    if (report?.state !== "READY") return;
    let active = true;
    apiGet<{ data: FileLink }>(`/reports/${id}/file`).then((r) => active && setFile(r.data), () => undefined);
    return () => {
      active = false;
    };
  }, [id, report?.state]);

  async function act(label: string, fn: () => Promise<void>) {
    setBusy(true);
    setNotice("");
    try {
      await fn();
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : `${label} failed.`);
    } finally {
      setBusy(false);
    }
  }

  const regenerate = () => act("Regenerate", async () => {
    const r = await apiSend<{ data: { report_id: string } }>("POST", "/reports", { supersedes: id });
    router.push(`/reports/${r.data.report_id}`);
  });
  const retry = () => act("Retry", async () => {
    await apiSend("POST", `/reports/${id}/retry`);
    setNotice("Creating the PDF again from the same snapshot.");
    setPollRun((n) => n + 1);
  });
  const compose = () => act("Compose", async () => {
    const r = await apiSend<{ data: { id: string } }>("POST", "/email-drafts", { report_id: id });
    router.push(`/reports/${id}/email?draft=${r.data.id}`);
  });
  const excel = () => act("Excel export", async () => {
    const r = await apiSend<{ data: { export_id: string } }>("POST", "/exports", { report_id: id });
    const x = await apiGet<{ data: { state: string; url: string | null } }>(`/exports/${r.data.export_id}`);
    if (x.data.url) window.location.assign(x.data.url);
    else {
      setNotice("The Excel snapshot is being prepared. Select Download Excel again in a moment.");
      setPollRun((n) => n + 1);
    }
  });
  const openPdf = (download: boolean) => act("Download", async () => {
    const r = await apiGet<{ data: FileLink }>(`/reports/${id}/file`); // renew: links are short-lived
    setFile(r.data);
    window.open(download ? r.data.download_url : r.data.url, "_blank", "noopener,noreferrer");
  });

  if (error?.status === 404) return <p>This report does not exist or you do not have access to it.</p>;
  if (error && !report) return <div className="banner error" role="alert">{error.text}</div>;
  if (!report) return <div className="skeleton" aria-busy="true" style={{ height: 120 }} />;

  const st = reportStatus(report);
  const sender = hasRole(session, "SENDER");
  const m = report.metrics;
  return (
    <>
      <p><Link href="/reports">All reports</Link></p>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>{report.title}</h1>
        <span className={`badge tone-${st.tone}`} role="status">{st.label}</span>
      </div>
      <p className="meta">
        {report.code} · version {report.version} · {report.facts.period.label} ({report.timezone}) · snapshot
        {" "}{new Date(report.created_at).toLocaleString("en-IN", { timeZone: session.timezone })} · data version {report.data_version}
      </p>
      {report.outdated && (
        <div className="banner warning" role="alert">
          Records in this period changed after this snapshot. This version stays as it was and cannot be sent. Generate a new version for current figures.
        </div>
      )}
      {report.state === "FAILED" && (
        <div className="banner error" role="alert">{report.error?.message ?? "The PDF could not be created."} No file was produced.</div>
      )}
      {(report.state === "QUEUED" || report.state === "GENERATING") && (
        <div className="banner info" role="status">Creating the PDF from the snapshot…</div>
      )}
      <p aria-live="polite" className="meta">{notice}</p>

      <div className="row" style={{ marginBottom: 16 }}>
        {report.state === "READY" && <button className="primary" disabled={busy} onClick={() => openPdf(true)}>Download PDF</button>}
        {report.state === "READY" && <button disabled={busy} onClick={() => openPdf(false)}>Open PDF</button>}
        {report.state === "READY" && <button disabled={busy} onClick={excel}>Download Excel snapshot</button>}
        {report.state === "READY" && sender && !report.outdated && <button disabled={busy} onClick={compose}>Compose email</button>}
        {report.state === "FAILED" && <button className="primary" disabled={busy} onClick={retry}>Retry PDF</button>}
        {report.state !== "QUEUED" && report.state !== "GENERATING" && (
          <button disabled={busy} onClick={regenerate}>{report.outdated ? "Generate new version" : "Regenerate"}</button>
        )}
      </div>

      <div className="grid-2">
        <section className="card" aria-labelledby="r-sum">
          <h2 id="r-sum">Summary</h2>
          {report.summary.length === 0 ? <p className="meta">The summary appears when the PDF is ready.</p> : report.summary.map((s, i) => <p key={i}>{s.text}</p>)}
          {report.summary_source && (
            <p className="meta">{report.summary_source === "AI" ? "AI-assisted wording, every number checked against the snapshot." : "Written from the snapshot by a fixed template."}</p>
          )}
          <dl className="kv">
            <div><dt className="meta">Records included</dt><dd>{report.record_count} approved</dd></div>
            <div><dt className="meta">Waiting for review (excluded)</dt><dd>{report.facts.excluded_pending}</dd></div>
            <div><dt className="meta">Period</dt><dd>{periodText(report.filter)}</dd></div>
            {report.file && <div><dt className="meta">PDF</dt><dd>{report.attachment_name} · {formatBytes(report.file.bytes)}</dd></div>}
            {report.file && <div><dt className="meta">Checksum (SHA-256)</dt><dd style={{ overflowWrap: "anywhere", fontSize: 12 }}>{report.file.sha256}</dd></div>}
          </dl>
        </section>
        <section className="card table-scroll" tabIndex={0} aria-labelledby="r-fig">
          <h2 id="r-fig">Figures</h2>
          {m.metrics.length === 0 ? <p>No approved records. Production 0, target 0, achievement N/A.</p> : (
            <table className="data">
              <caption className="sr-only">Production against target per unit</caption>
              <thead><tr><th scope="col">Unit</th><th scope="col">Production</th><th scope="col">Target</th><th scope="col">Achievement</th><th scope="col">Variance</th></tr></thead>
              <tbody>
                {m.metrics.map((u) => (
                  <tr key={u.unit}>
                    <td>{u.unit}</td><td className="tnum">{formatQty(u.production_qty)}</td><td className="tnum">{formatQty(u.target_qty)}</td>
                    <td className="tnum">{u.achievement_pct === null ? "N/A" : `${u.achievement_pct}%`}</td><td className="tnum">{formatQty(u.variance)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="meta">
            Status: {Object.keys(STATUS_WORDS).map((s) => `${STATUS_WORDS[s]} ${m.status_counts[s] ?? 0}`).join(" · ")} · record downtime {m.stop_total_minutes} minutes
          </p>
        </section>
      </div>

      {report.state === "READY" && file && (
        <section className="card pdf-preview" aria-labelledby="r-prev">
          <h2 id="r-prev">Preview</h2>
          <iframe title={`PDF preview of ${report.title}`} src={file.url} className="pdf-frame" />
          <p className="meta">Preview and download are the same file (SHA-256 {file.sha256.slice(0, 16)}…). If the preview is blank on this device, use Open PDF.</p>
        </section>
      )}

      {sender && report.emails.length > 0 && (
        <section className="card" aria-labelledby="r-mail">
          <h2 id="r-mail">Emails for this report</h2>
          <ul className="filelist">
            {report.emails.map((e) => (
              <li key={e.id}>
                <Link href={`/emails/${e.id}`}>{e.subject}</Link>{" "}
                <span className={`badge tone-${emailStatus(e.state).tone}`}>{emailStatus(e.state).label}</span>
                <span className="meta"> · {e.recipient_count} recipient{e.recipient_count === 1 ? "" : "s"}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {report.versions.length > 1 && (
        <section className="card" aria-labelledby="r-ver">
          <h2 id="r-ver">Versions</h2>
          <ul className="filelist">
            {report.versions.map((v) => (
              <li key={v.id}>
                {v.id === report.id ? <strong>Version {v.version} (this one)</strong> : <Link href={`/reports/${v.id}`}>Version {v.version}</Link>}
                <span className="meta"> · {reportStatus(v).label} · {new Date(v.created_at).toLocaleString("en-IN", { timeZone: session.timezone })}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
