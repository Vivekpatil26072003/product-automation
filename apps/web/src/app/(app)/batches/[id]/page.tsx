"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";

import { BatchPipeline, type Stage } from "@/components/orders/BatchPipeline";
import { DiaryDataTable } from "@/components/orders/DiaryDataTable";
import { OwnerReports } from "@/components/orders/OwnerReports";
import { SheetLinks } from "@/components/sheets/SheetLinks";
import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend, type Batch, type BatchFile, type Job } from "@/lib/api";
import { formatBytes } from "@/lib/files";
import { fileStatus, scanLabel } from "@/lib/status";

// U2 Processing: counts, never invented percentages. Polls every 2 s, slowing to 15 s, and stops
// when nothing is in progress. A refresh simply resumes polling from server state.

const FIRST_POLL_MS = 2000;
const MAX_POLL_MS = 15000;

export default function BatchPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { session } = useSession();
  const [batch, setBatch] = useState<Batch | null>(null);
  const [error, setError] = useState<{ status: number; text: string } | null>(null);
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [pollRun, setPollRun] = useState(0); // bump to restart polling at the fast interval
  const [stages, setStages] = useState<Stage[] | null>(null);

  const loadStages = useCallback(async () => {
    try {
      const { data } = await apiGet<{ data: { stages: Stage[] } }>(`/batches/${id}/pipeline`);
      setStages(data.stages);
    } catch {
      setStages(null); // the file list below still shows the details
    }
  }, [id]);

  const load = useCallback(async () => {
    try {
      const { data } = await apiGet<{ data: Batch }>(`/batches/${id}`);
      void loadStages();
      setBatch(data);
      setError(null);
      return data;
    } catch (e) {
      setError({ status: e instanceof ApiError ? e.status : 0, text: e instanceof ApiError ? e.message : "Could not load." });
      return null;
    }
  }, [id, loadStages]);

  useEffect(() => {
    let cancelled = false;
    let delay = FIRST_POLL_MS;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const data = await load();
      if (cancelled || (data && !data.summary.in_progress)) return; // settled: stop polling
      delay = Math.min(delay * 1.5, MAX_POLL_MS);
      timer = setTimeout(tick, delay);
    };
    timer = setTimeout(tick, pollRun === 0 ? 0 : FIRST_POLL_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [load, pollRun]);

  const restartPolling = () => setPollRun((n) => n + 1);

  async function act(job: Job, action: "retry" | "cancel") {
    setPending(job.id);
    try {
      await apiSend("POST", `/jobs/${job.id}/${action}`, action === "retry" ? { failed_only: true } : undefined);
      setNotice(action === "retry" ? "Retrying failed pages." : "Cancellation requested.");
      await load();
      restartPolling();
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : "The action failed.");
    } finally {
      setPending(null);
    }
  }

  async function openSource(f: BatchFile) {
    try {
      const { data } = await apiGet<{ data: { url: string } }>(`/sources/${f.id}/file`);
      window.open(data.url, "_blank", "noopener,noreferrer");
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : "The file could not be opened.");
    }
  }

  if (error?.status === 404) {
    return (
      <>
        <h1>Batch not found</h1>
        <p>It does not exist or you do not have access to it.</p>
        <Link href="/batches">Back to processing history</Link>
      </>
    );
  }
  if (!batch) {
    return (
      <div aria-busy="true">
        <h1>Processing</h1>
        {error ? (
          <div className="banner error" role="alert">
            {error.text} <button onClick={() => void load()}>Retry</button>
          </div>
        ) : (
          <div className="skeleton" style={{ width: "60%" }} />
        )}
      </div>
    );
  }

  const s = batch.summary;
  const partial = batch.files.filter((f) => f.pages.failed.length > 0);
  const isOwner = batch.owner_id === session.user_id;
  const canRetry = hasRole(session, "UPLOADER", "REVIEWER");

  return (
    <>
      <h1>Processing</h1>
      {stages && <BatchPipeline stages={stages} />}
      <SheetLinks batchId={id} refresh={JSON.stringify(stages)} />
      <DiaryDataTable batchId={id} refresh={JSON.stringify(stages)} />
      <p className="meta">
        {batch.department.name} · {new Date(batch.created_at).toLocaleString("en-IN", { timeZone: session.timezone })}
      </p>
      <p className="sr-only" aria-live="polite">
        {notice || (s.in_progress ? `${s.pages_processed} of ${s.pages_total} pages processed` : "Processing finished.")}
      </p>

      {error && (
        <div className="banner error" role="alert">
          Latest status could not be loaded: {error.text} Showing the last known state.
        </div>
      )}
      {notice && <div className="banner info">{notice}</div>}
      {partial.length > 0 && (
        <div className="banner warning" role="status">
          Some pages could not be read:{" "}
          {partial.map((f) => `${f.name} (page${f.pages.failed.length > 1 ? "s" : ""} ${f.pages.failed.join(", ")})`).join("; ")}.
          Completed pages are kept.
        </div>
      )}

      <div className="card row" style={{ justifyContent: "space-between" }}>
        <div>
          <strong>
            {s.pages_processed} of {s.pages_total} page{s.pages_total === 1 ? "" : "s"} processed
          </strong>
          <div className="meta">
            {s.files} files · {s.parsed} fully read · {s.partial} with problems · {s.rejected} not processed
            {s.waiting_for_upload ? ` · ${s.waiting_for_upload} waiting for upload` : ""}
          </div>
        </div>
        <div className="row">
          <span className={`badge ${s.in_progress ? "tone-progress" : "tone-neutral"}`}>
            {s.in_progress ? "In progress" : "Finished"}
          </span>
          {(s.to_review > 0 || batch.files.some((f) => {
            const done = (j: Job | null) => !!j && !["QUEUED", "RUNNING", "RETRY_WAIT"].includes(j.state);
            // Unreadable files (e.g. a photo with no OCR reader) also lead to review, where they can be entered by hand.
            return done(f.jobs.extract) || (f.state === "READY" && !f.jobs.extract && done(f.jobs.parse) && f.pages.succeeded.length === 0);
          })) && (
            <Link className="button primary" href={`/batches/${batch.id}/review`}>
              Review entries{s.to_review ? ` (${s.to_review})` : ""}
            </Link>
          )}
        </div>
      </div>

      <section className="card" aria-labelledby="files-title">
        <h2 id="files-title">Files</h2>
        <ul className="filelist">
          {batch.files.map((f) => {
            const st = fileStatus(f);
            const scan = scanLabel(f);
            const parse = f.jobs.parse;
            const running = [f.jobs.scan, parse].find((j) => j && ["QUEUED", "RUNNING", "RETRY_WAIT"].includes(j.state));
            return (
              <li key={f.id}>
                <div>
                  <div className="filename">{f.name}</div>
                  <div className="meta">
                    {formatBytes(f.bytes)}
                    {scan ? ` · ${scan}` : ""}
                  </div>
                  <div className="row" style={{ marginTop: 4 }}>
                    <span className={`badge tone-${st.tone}`}>{st.label}</span>
                    {st.detail && <span className="meta">{st.detail}</span>}
                  </div>
                  {f.duplicate_of && <div className="meta">This exact file was uploaded before; review will flag it.</div>}
                </div>
                <div className="row">
                  {f.state === "READY" && <button onClick={() => openSource(f)}>View file</button>}
                  {canRetry && parse?.retryable && (
                    <button disabled={pending === parse.id} onClick={() => act(parse, "retry")}>
                      Retry failed pages
                    </button>
                  )}
                  {isOwner && running && !running.cancel_requested && (
                    <button className="danger" disabled={pending === running.id} onClick={() => act(running, "cancel")}>
                      Cancel
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      </section>

      <OwnerReports batchId={batch.id} canCreate onChange={() => void loadStages()} />

      <div className="row">
        <Link className="button" href="/uploads/new">
          Back to uploads
        </Link>
        <button onClick={() => { void load(); restartPolling(); }}>Refresh status</button>
      </div>
    </>
  );
}
