"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { BatchPipeline, type Stage } from "@/components/orders/BatchPipeline";
import { DiaryDataTable } from "@/components/orders/DiaryDataTable";
import { SheetLinks } from "@/components/sheets/SheetLinks";
import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend, newIdempotencyKey, sha256Hex, type UploadSlot } from "@/lib/api";

// Worker screen: take a photo of a diary page (or pick images), it uploads at once, and the status of each
// upload is shown in plain steps. No settings, no recipients: reading, review, the owner report and its email
// happen after this, in the background.

type MyBatch = { id: string; created_at: string; files: number; stages: Stage[]; counts: { orders_waiting: number; entries_waiting: number } };
const DEPT_KEY = "diary.department";

function remembered(): string {
  try {
    return localStorage.getItem(DEPT_KEY) ?? "";
  } catch {
    return "";
  }
}

export default function DiaryPage() {
  const { session } = useSession();
  const camera = useRef<HTMLInputElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const [department, setDepartment] = useState(() => {
    const saved = remembered();
    return session.departments.some((d) => d.id === saved) ? saved : session.departments[0]?.id ?? "";
  });
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "info" | "error" | "success"; text: string } | null>(null);
  const [batches, setBatches] = useState<MyBatch[] | null>(null);
  const canReview = hasRole(session, "REVIEWER");

  const load = useCallback(async () => {
    try {
      const r = await apiGet<{ data: MyBatch[] }>("/diary/batches?limit=8");
      setBatches(r.data);
      return r.data;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const data = await load();
      const moving = data?.some((b) => b.stages.some((s) => s.state === "current" && s.key !== "review"));
      if (!stop) timer = setTimeout(tick, moving ? 3000 : 20000);
    };
    timer = setTimeout(tick, 0);
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [load]);

  function chooseDepartment(id: string) {
    setDepartment(id);
    try {
      localStorage.setItem(DEPT_KEY, id);
    } catch {
      /* remembered for this visit only */
    }
  }

  async function upload(list: FileList | null) {
    const files = Array.from(list ?? []);
    if (!files.length || busy) return;
    setBusy(true);
    setMessage({ tone: "info", text: `Uploading ${files.length} photo${files.length === 1 ? "" : "s"}…` });
    try {
      const hashes = await Promise.all(files.map((f) => sha256Hex(f)));
      const manifest = files.map((f, i) => ({ name: f.name || `diary-${Date.now()}.jpg`, bytes: f.size, sha256: hashes[i], mime: f.type }));
      const { data } = await apiSend<{ data: { batch_id: string; uploads: UploadSlot[] } }>(
        "POST", "/batches", { department_id: department, files: manifest }, { idempotencyKey: newIdempotencyKey() });
      let ok = 0;
      for (const [i, file] of files.entries()) {
        const slot = data.uploads[i];
        if (!slot?.put_url || !slot.headers) continue;
        const put = await fetch(slot.put_url, { method: "PUT", body: file, headers: slot.headers });
        if (!put.ok) continue;
        await apiSend("POST", `/uploads/${slot.id}/complete`, { sha256: hashes[i], bytes: file.size });
        ok += 1;
      }
      setMessage(ok === files.length
        ? { tone: "success", text: "Uploaded. The pages are being read now; you can take the next photo." }
        : { tone: "error", text: `${ok} of ${files.length} uploaded. Try the others again.` });
      await load();
    } catch (e) {
      setMessage({ tone: "error", text: e instanceof ApiError ? (e.fields.map((f) => f.message).join(" ") || e.message) : "Upload failed. Check the connection and try again." });
    } finally {
      setBusy(false);
      if (camera.current) camera.current.value = "";
      if (picker.current) picker.current.value = "";
    }
  }

  if (!hasRole(session, "UPLOADER", "REVIEWER")) return <p>Only workers who upload diary pages use this screen.</p>;
  if (session.departments.length === 0) return <p>You are not assigned to a department yet. Ask an administrator.</p>;

  return (
    <div className="diary">
      <h1>Diary photos</h1>
      <p className="meta">Take a clear photo of one diary page at a time, flat and in good light.</p>
      {session.departments.length > 1 && (
        <div style={{ marginBottom: 12 }}>
          <label htmlFor="d-dept">Department</label>
          <select id="d-dept" value={department} onChange={(e) => chooseDepartment(e.target.value)} disabled={busy}>
            {session.departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
        </div>
      )}
      <div className="diary-actions">
        <button className="primary big" disabled={busy || !department} onClick={() => camera.current?.click()}>📷 Take photo</button>
        <button className="big" disabled={busy || !department} onClick={() => picker.current?.click()}>🖼 Upload diary image</button>
      </div>
      <input ref={camera} type="file" accept="image/*" capture="environment" hidden aria-label="Take photo" onChange={(e) => void upload(e.target.files)} />
      <input ref={picker} type="file" accept="image/jpeg,image/png,application/pdf" multiple hidden aria-label="Upload diary image" onChange={(e) => void upload(e.target.files)} />
      {message && <div className={`banner ${message.tone}`} role={message.tone === "error" ? "alert" : "status"}>{message.text}</div>}

      <h2>My recent pages</h2>
      {!batches ? <div className="skeleton" aria-busy="true" style={{ width: "60%" }} /> : batches.length === 0 ? (
        <p className="meta">Nothing uploaded yet.</p>
      ) : (
        <ul className="diary-list">
          {batches.map((b) => {
            const waiting = b.counts.orders_waiting + b.counts.entries_waiting;
            const unread = b.stages.find((s) => s.key === "reading")?.state === "failed";
            return (
              <li key={b.id} className="card stack">
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <strong>{new Date(b.created_at).toLocaleString("en-IN", { timeZone: session.timezone, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} · {b.files} file{b.files === 1 ? "" : "s"}</strong>
                  <Link href={`/batches/${b.id}`}>Details</Link>
                </div>
                <BatchPipeline stages={b.stages} compact />
                <SheetLinks batchId={b.id} refresh={JSON.stringify(b.stages)} />
                <DiaryDataTable batchId={b.id} refresh={JSON.stringify(b.stages)} compact />
                {(waiting > 0 || unread) && (
                  <div className="row">
                    <Link className="button primary big" href={`/batches/${b.id}/review`}>
                      {unread && !waiting ? "Enter what could not be read" : `Review ${waiting} entr${waiting === 1 ? "y" : "ies"}`}
                    </Link>
                    {!canReview && <span className="meta">A reviewer approves after you check the values.</span>}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
