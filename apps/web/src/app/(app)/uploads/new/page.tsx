"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { CameraCapture } from "@/components/CameraCapture";
import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend, newIdempotencyKey, sha256Hex, type Limits, type UploadSlot } from "@/lib/api";
import { checkQueue, formatBytes, limitsSentence } from "@/lib/files";

// U1 Upload and camera. No send button and no recipients: this screen only captures notes.

type Queued = { key: string; file: File; status: "queued" | "hashing" | "uploading" | "done" | "failed"; error?: string };

export default function UploadPage() {
  const { session } = useSession();
  const router = useRouter();
  const [limits, setLimits] = useState<Limits | null>(null);
  const [department, setDepartment] = useState(session.departments[0]?.id ?? "");
  const [queue, setQueue] = useState<Queued[]>([]);
  const [dragging, setDragging] = useState(false);
  const [camera, setCamera] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState<{ text: string; requestId?: string } | null>(null);
  const [batchId, setBatchId] = useState<string | null>(null);
  const idemKey = useRef(newIdempotencyKey());
  const pickerRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    apiGet<{ data: Limits }>("/uploads/limits")
      .then((r) => setLimits(r.data))
      .catch(() => setError({ text: "Upload limits could not be loaded. Reload the page." }));
  }, []);

  // Leaving with files queued but not sent loses them; warn first.
  useEffect(() => {
    const pending = queue.some((q) => q.status !== "done");
    const onLeave = (e: BeforeUnloadEvent) => {
      if (pending) e.preventDefault();
    };
    window.addEventListener("beforeunload", onLeave);
    return () => window.removeEventListener("beforeunload", onLeave);
  }, [queue]);

  const checks = useMemo(
    () => (limits ? checkQueue(queue.map((q) => ({ name: q.file.name, size: q.file.size })), limits) : []),
    [queue, limits],
  );
  const invalid = checks.filter((c) => c.reason).length;
  const canProcess = !busy && !batchId && queue.length > 0 && invalid === 0 && !!department;

  function add(files: FileList | File[]) {
    const added = Array.from(files).map((file) => ({ key: crypto.randomUUID(), file, status: "queued" as const }));
    setQueue((q) => [...q, ...added]);
    setMessage(`${added.length} file${added.length === 1 ? "" : "s"} added.`);
    idemKey.current = newIdempotencyKey(); // a changed selection is a new request
  }

  function remove(key: string) {
    setQueue((q) => q.filter((x) => x.key !== key));
    idemKey.current = newIdempotencyKey();
  }

  function setStatus(key: string, status: Queued["status"], err?: string) {
    setQueue((q) => q.map((x) => (x.key === key ? { ...x, status, error: err } : x)));
  }

  async function uploadOne(item: Queued, slot: UploadSlot, sha: string) {
    setStatus(item.key, "uploading");
    try {
      if (!slot.put_url || !slot.headers) throw new Error("No upload slot is available for this file.");
      const put = await fetch(slot.put_url, { method: "PUT", body: item.file, headers: slot.headers });
      if (!put.ok) throw new Error("The storage service refused the file. Try again.");
      await apiSend("POST", `/uploads/${slot.id}/complete`, { sha256: sha, bytes: item.file.size });
      setStatus(item.key, "done");
      return true;
    } catch (e) {
      setStatus(item.key, "failed", e instanceof Error ? e.message : "Upload failed.");
      return false;
    }
  }

  async function process() {
    setBusy(true);
    setError(null);
    try {
      const hashes: string[] = [];
      for (const [i, item] of queue.entries()) {
        setMessage(`Preparing file ${i + 1} of ${queue.length}…`);
        setStatus(item.key, "hashing");
        hashes.push(await sha256Hex(item.file));
      }
      const files = queue.map((q, i) => ({ name: q.file.name, bytes: q.file.size, sha256: hashes[i], mime: q.file.type }));
      const { data } = await apiSend<{ data: { batch_id: string; uploads: UploadSlot[] } }>(
        "POST", "/batches", { department_id: department, files }, { idempotencyKey: idemKey.current },
      );
      setBatchId(data.batch_id);
      let ok = 0;
      for (const [i, item] of queue.entries()) {
        setMessage(`Uploading file ${i + 1} of ${queue.length}…`);
        const slot = data.uploads[i];
        if (slot && (await uploadOne(item, slot, hashes[i] ?? ""))) ok += 1;
      }
      if (ok === queue.length) {
        setMessage("All files uploaded. Opening processing status.");
        setQueue([]);
        router.push(`/batches/${data.batch_id}`);
      } else {
        setMessage(`${ok} of ${queue.length} files uploaded. Retry the failed files or continue.`);
      }
    } catch (e) {
      if (e instanceof ApiError && e.fields.length) {
        setError({ text: e.fields.map((f) => f.message).join(" "), requestId: e.requestId });
      } else {
        setError({ text: e instanceof Error ? e.message : "Upload failed.", requestId: (e as ApiError).requestId });
      }
      setQueue((q) => q.map((x) => (x.status === "hashing" ? { ...x, status: "queued" } : x)));
    } finally {
      setBusy(false);
    }
  }

  async function retry(item: Queued) {
    if (!batchId) return;
    const { data } = await apiGet<{ data: UploadSlot[] }>(`/batches/${batchId}/upload-slots`);
    const index = queue.findIndex((q) => q.key === item.key);
    const slot = data[index];
    if (!slot) return;
    await uploadOne(item, slot, await sha256Hex(item.file));
  }

  if (!hasRole(session, "UPLOADER", "REVIEWER")) {
    return (
      <>
        <h1>Upload notes</h1>
        <p>Uploading notes needs the Uploader or Reviewer role. Ask an administrator if you need it.</p>
      </>
    );
  }

  if (session.departments.length === 0) {
    return (
      <>
        <h1>Upload notes</h1>
        <p>You are not assigned to any department yet. Ask an administrator for access.</p>
      </>
    );
  }

  return (
    <>
      <h1>Upload notes</h1>
      <p className="sr-only" aria-live="polite">
        {message}
      </p>

      {error && (
        <div className="banner error" role="alert">
          {error.text}
          {error.requestId && <div className="meta">Reference: {error.requestId}</div>}
        </div>
      )}

      <div className="card stack">
        <div>
          <label htmlFor="department">Department</label>
          {session.departments.length === 1 ? (
            <p id="department">{session.departments[0]?.name}</p>
          ) : (
            <select id="department" value={department} disabled={!!batchId} onChange={(e) => setDepartment(e.target.value)}>
              {session.departments.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          )}
        </div>

        <div
          className={`dropzone${dragging ? " active" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            if (!batchId) add(e.dataTransfer.files);
          }}
        >
          <p>
            <strong>Drop production notes here</strong>
          </p>
          <div className="row" style={{ justifyContent: "center" }}>
            <button className="primary" onClick={() => setCamera(true)} disabled={!!batchId}>
              Capture photo
            </button>
            <button onClick={() => pickerRef.current?.click()} disabled={!!batchId}>
              Add files
            </button>
          </div>
          <input
            ref={pickerRef}
            type="file"
            multiple
            className="sr-only"
            accept={limits?.accepted_extensions.join(",")}
            aria-label="Add files"
            onChange={(e) => {
              if (e.target.files) add(e.target.files);
              e.target.value = "";
            }}
          />
          {limits && <p className="meta" style={{ marginTop: 12 }}>{limitsSentence(limits)}</p>}
        </div>
      </div>

      {queue.length > 0 && (
        <section className="card" aria-labelledby="queue-title">
          <h2 id="queue-title">
            Selected files ({queue.length})
          </h2>
          {invalid > 0 && (
            <div className="banner warning" role="status">
              {invalid} file{invalid === 1 ? " cannot" : "s cannot"} be uploaded. Remove {invalid === 1 ? "it" : "them"} to
              continue; only the files you keep will be processed.
            </div>
          )}
          <ul className="filelist">
            {queue.map((q, i) => {
              const reason = checks[i]?.reason;
              return (
                <li key={q.key}>
                  <div>
                    <div className="filename">{q.file.name}</div>
                    <div className="meta">
                      {formatBytes(q.file.size)} · {statusText(q)}
                    </div>
                    {reason && <div className="reason">{reason}</div>}
                    {q.error && <div className="reason">{q.error}</div>}
                  </div>
                  <div className="row">
                    {q.status === "failed" && <button onClick={() => retry(q)}>Retry</button>}
                    {!batchId && (
                      <button onClick={() => remove(q.key)} aria-label={`Remove ${q.file.name}`}>
                        Remove
                      </button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
          <div className="row" style={{ marginTop: 16 }}>
            {!batchId ? (
              <button className="primary" disabled={!canProcess} onClick={process}>
                {busy ? "Working…" : "Process notes"}
              </button>
            ) : (
              <button className="primary" onClick={() => router.push(`/batches/${batchId}`)}>
                Continue to processing
              </button>
            )}
          </div>
        </section>
      )}

      <CameraCapture open={camera} onClose={() => setCamera(false)} onUse={(f) => add([f])} />
    </>
  );
}

function statusText(q: Queued): string {
  return { queued: "Ready to send", hashing: "Preparing", uploading: "Uploading", done: "Uploaded", failed: "Upload failed" }[
    q.status
  ];
}
