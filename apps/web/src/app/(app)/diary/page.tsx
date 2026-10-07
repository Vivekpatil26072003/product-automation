"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { BatchPipeline, type Stage } from "@/components/orders/BatchPipeline";
import { DiaryDataTable } from "@/components/orders/DiaryDataTable";
import { SheetLinks } from "@/components/sheets/SheetLinks";
import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend, newIdempotencyKey, sha256Hex, type UploadSlot } from "@/lib/api";

// Worker screen: take photos of several pages one after the other (they wait in a list) and send them together, or
// pick images from the gallery (sent at once). The status of each upload is shown in plain steps. No settings, no
// recipients: reading, review, the owner report and its email happen after this, in the background.

type MyBatch = { id: string; created_at: string; files: number; stages: Stage[]; counts: { orders_waiting: number; entries_waiting: number } };
const DEPT_KEY = "diary.department";
const MAX_PHOTOS = 20; // one upload batch (server limit)
const PARALLEL = 3; // photos sent at the same time

/** Runs the jobs with at most `limit` at a time; resolves with each job's result in order. */
async function pooled<T>(jobs: (() => Promise<T>)[], limit: number): Promise<T[]> {
  const out: T[] = new Array(jobs.length);
  let next = 0;
  const lanes = Array.from({ length: Math.min(limit, jobs.length) }, async () => {
    while (next < jobs.length) {
      const i = next++;
      out[i] = await jobs[i]!();
    }
  });
  await Promise.all(lanes);
  return out;
}

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
  const [tray, setTray] = useState<{ file: File; url: string }[]>([]);
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

  function addToTray(list: FileList | null) {
    const files = Array.from(list ?? []);
    if (camera.current) camera.current.value = "";
    if (!files.length) return;
    setTray((t) => {
      const room = MAX_PHOTOS - t.length;
      if (files.length > room) setMessage({ tone: "error", text: `At most ${MAX_PHOTOS} photos at once: send these first.` });
      return [...t, ...files.slice(0, Math.max(room, 0)).map((file) => ({ file, url: URL.createObjectURL(file) }))];
    });
  }

  function removeFromTray(i: number) {
    setTray((t) => {
      URL.revokeObjectURL(t[i]!.url);
      return t.filter((_, j) => j !== i);
    });
  }

  async function sendTray() {
    const files = tray.map((x) => x.file);
    if (await upload(files)) {
      tray.forEach((x) => URL.revokeObjectURL(x.url));
      setTray([]);
    }
  }

  async function upload(list: FileList | File[] | null): Promise<boolean> {
    const files = Array.from(list ?? []);
    if (!files.length || busy) return false;
    setBusy(true);
    setMessage({ tone: "info", text: `Uploading ${files.length} photo${files.length === 1 ? "" : "s"}…` });
    try {
      const hashes = await Promise.all(files.map((f) => sha256Hex(f)));
      const manifest = files.map((f, i) => ({ name: f.name || `diary-${Date.now()}.jpg`, bytes: f.size, sha256: hashes[i], mime: f.type }));
      const { data } = await apiSend<{ data: { batch_id: string; uploads: UploadSlot[] } }>(
        "POST", "/batches", { department_id: department, files: manifest }, { idempotencyKey: newIdempotencyKey() });
      const sent = await pooled(files.map((file, i) => async () => {
        const slot = data.uploads[i];
        if (!slot?.put_url || !slot.headers) return false;
        try {
          const put = await fetch(slot.put_url, { method: "PUT", body: file, headers: slot.headers });
          if (!put.ok) return false;
          await apiSend("POST", `/uploads/${slot.id}/complete`, { sha256: hashes[i], bytes: file.size });
          return true;
        } catch {
          return false;
        }
      }), PARALLEL);
      const ok = sent.filter(Boolean).length;
      setMessage(ok === files.length
        ? { tone: "success", text: `Uploaded${files.length > 1 ? ` ${files.length} photos` : ""}. The pages are being read now; you can take the next photo.` }
        : { tone: "error", text: `${ok} of ${files.length} uploaded. Try the others again.` });
      await load();
      return ok === files.length;
    } catch (e) {
      setMessage({ tone: "error", text: e instanceof ApiError ? (e.fields.map((f) => f.message).join(" ") || e.message) : "Upload failed. Check the connection and try again." });
      return false;
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
      <p className="meta">Take a clear photo of each page, flat and in good light. Take all the pages, then press Send.</p>
      {session.departments.length > 1 && (
        <div style={{ marginBottom: 12 }}>
          <label htmlFor="d-dept">Department</label>
          <select id="d-dept" value={department} onChange={(e) => chooseDepartment(e.target.value)} disabled={busy}>
            {session.departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
        </div>
      )}
      <div className="diary-actions">
        <button className={`${tray.length ? "" : "primary "}big`} disabled={busy || !department || tray.length >= MAX_PHOTOS} onClick={() => camera.current?.click()}>
          📷 {tray.length ? "Take another photo" : "Take photo"}
        </button>
        <button className="big" disabled={busy || !department} onClick={() => picker.current?.click()}>🖼 Upload diary image</button>
      </div>
      <input ref={camera} type="file" accept="image/*" capture="environment" hidden aria-label="Take photo" onChange={(e) => addToTray(e.target.files)} />
      <input ref={picker} type="file" accept="image/jpeg,image/png,application/pdf" multiple hidden aria-label="Upload diary image" onChange={(e) => void upload(e.target.files)} />
      {tray.length > 0 && (
        <section className="card stack" aria-labelledby="tray-title" style={{ marginBottom: 12 }}>
          <h2 id="tray-title" style={{ margin: 0 }}>{tray.length} photo{tray.length === 1 ? "" : "s"} ready to send</h2>
          <ul className="photo-tray" aria-label="Photos ready to send">
            {tray.map((x, i) => (
              <li key={x.url}>
                {/* eslint-disable-next-line @next/next/no-img-element -- local preview of the photo just taken */}
                <img src={x.url} alt={`Photo ${i + 1}`} />
                <button type="button" disabled={busy} onClick={() => removeFromTray(i)} aria-label={`Remove photo ${i + 1}`}>Remove</button>
              </li>
            ))}
          </ul>
          <button className="primary big" disabled={busy || !department} onClick={() => void sendTray()}>
            {busy ? "Sending…" : `Send ${tray.length} photo${tray.length === 1 ? "" : "s"}`}
          </button>
        </section>
      )}
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
