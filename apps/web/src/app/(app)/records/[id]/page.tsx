"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useRef, useState } from "react";

import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend, type Master, type RecordFields, type RecordView } from "@/lib/api";

// U4 record detail: approved values, revision history, corrections and archive (FR10).
// Approved revisions never change; a correction is a new revision that someone approves.

const LABELS: Record<keyof RecordFields, string> = {
  production_date: "Production date", department_id: "Department", machine_id: "Machine",
  operator_name: "Operator", production_qty: "Production", target_qty: "Target", unit: "Unit",
  status: "Status", stop_minutes: "Stop time (min)", remarks: "Remarks",
};
const EDITABLE: (keyof RecordFields)[] = ["production_qty", "target_qty", "status", "stop_minutes", "operator_name", "remarks"];

export default function RecordPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { session } = useSession();
  const isReviewer = hasRole(session, "REVIEWER");
  const [rec, setRec] = useState<RecordView | null>(null);
  const [names, setNames] = useState<{ depts: Map<string, string>; machines: Map<string, string> }>({ depts: new Map(), machines: new Map() });
  const [error, setError] = useState<{ status: number; text: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [correcting, setCorrecting] = useState(false);
  const [archiving, setArchiving] = useState(false);

  const apply = useCallback((p: Promise<{ data: RecordView }>) => p.then(
    (r) => { setRec(r.data); setError(null); },
    (e) => setError({ status: e instanceof ApiError ? e.status : 0, text: e instanceof ApiError ? e.message : "Could not load." }),
  ), []);

  useEffect(() => {
    void apply(apiGet<{ data: RecordView }>(`/records/${id}`));
    Promise.all([apiGet<{ data: Master[] }>("/masters/departments"), apiGet<{ data: Master[] }>("/masters/machines")]).then(
      ([d, m]) => setNames({ depts: new Map(d.data.map((x) => [x.id, x.name ?? x.code])), machines: new Map(m.data.map((x) => [x.id, x.code])) }),
      () => undefined,
    );
  }, [id, apply]);

  const show = (f: keyof RecordFields, v: RecordFields) =>
    f === "department_id" ? names.depts.get(v[f]) ?? "—" : f === "machine_id" ? names.machines.get(v[f]) ?? "—" : String(v[f] ?? "") || "—";

  async function act(fn: () => Promise<unknown>, done: string) {
    try {
      await fn();
      setNotice(done);
      await apply(apiGet<{ data: RecordView }>(`/records/${id}`));
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : "The action failed.");
    }
  }

  if (error?.status === 404) {
    return (<><h1>Record not found</h1><p>It does not exist or you do not have access to it.</p></>);
  }
  if (!rec) return <div className="skeleton" aria-busy="true" style={{ width: "40%" }} />;

  const pending = rec.revisions.find((r) => r.state === "PENDING");
  return (
    <>
      <h1>Production record</h1>
      <p className="meta">
        Revision {rec.current_revision} · {rec.state === "ARCHIVED" ? `Archived: ${rec.archive_reason}` : "Active"}
      </p>
      {notice && <div className="banner info" role="status">{notice}</div>}
      {rec.state === "ARCHIVED" && (
        <div className="banner warning">This record is archived and excluded from future totals. Its history is kept.</div>
      )}

      <section className="card">
        <h2>Current values</h2>
        <dl className="kv">
          {(Object.keys(LABELS) as (keyof RecordFields)[]).map((f) => (
            <div key={f}><dt>{LABELS[f]}</dt><dd>{show(f, rec.fields)}</dd></div>
          ))}
        </dl>
        {isReviewer && rec.state === "ACTIVE" && (
          <div className="row">
            <button disabled={!!pending} onClick={() => setCorrecting(true)}>Correct values</button>
            <button className="danger" onClick={() => setArchiving(true)}>Archive</button>
          </div>
        )}
      </section>

      {pending && (
        <section className="card" aria-labelledby="pending-title">
          <h2 id="pending-title">Correction waiting for approval</h2>
          <p>Reason: {pending.reason}</p>
          <Changes before={rec.fields} after={pending.fields} show={show} />
          {isReviewer && (
            <div className="row">
              <button className="primary" onClick={() => act(() => apiSend("POST", `/records/${id}/revisions/${pending.id}/approve`), "Correction approved.")}>
                Approve correction
              </button>
              <button onClick={() => act(() => apiSend("POST", `/records/${id}/revisions/${pending.id}/reject`, { reason: "Correction not confirmed" }), "Correction rejected.")}>
                Reject correction
              </button>
            </div>
          )}
        </section>
      )}

      <section className="card" aria-labelledby="history-title">
        <h2 id="history-title">History</h2>
        <ol className="filelist">
          {rec.revisions.map((r, i) => (
            <li key={r.id}>
              <div>
                <strong>Revision {r.number}</strong> · {r.state.toLowerCase()} ·{" "}
                {new Date(r.approved_at ?? r.created_at).toLocaleString("en-IN", { timeZone: session.timezone })}
                {r.reason && <div className="meta">Reason: {r.reason}</div>}
                {i > 0 && <Changes before={rec.revisions[i - 1]!.fields} after={r.fields} show={show} />}
                {r.provenance && "upload_id" in r.provenance && (
                  <div className="meta">From an uploaded note (entry {String((r.provenance as { candidate_id?: string }).candidate_id).slice(0, 8)})</div>
                )}
              </div>
            </li>
          ))}
        </ol>
      </section>

      <Link className="button" href="/batches">Back to processing history</Link>

      {correcting && (
        <CorrectionDialog
          fields={rec.fields}
          onCancel={() => setCorrecting(false)}
          onSubmit={(changes, reason) => {
            setCorrecting(false);
            void act(() => apiSend("POST", `/records/${id}/revisions`, { fields: changes, reason }, { ifMatch: `"${rec.version}"` }),
              "Correction saved. It takes effect once approved.");
          }}
        />
      )}
      {archiving && (
        <ReasonDialog
          title="Archive this record"
          action="Archive"
          onCancel={() => setArchiving(false)}
          onSubmit={(reason) => {
            setArchiving(false);
            void act(() => apiSend("POST", `/records/${id}/archive`, { reason }), "Record archived.");
          }}
        />
      )}
    </>
  );
}

function Changes({ before, after, show }: { before: RecordFields; after: RecordFields; show: (f: keyof RecordFields, v: RecordFields) => string }) {
  const changed = (Object.keys(LABELS) as (keyof RecordFields)[]).filter((f) => String(before[f]) !== String(after[f]));
  if (!changed.length) return null;
  return (
    <ul className="meta">
      {changed.map((f) => <li key={f}>{LABELS[f]}: {show(f, before)} → {show(f, after)}</li>)}
    </ul>
  );
}

function useModal() {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return ref;
}

function CorrectionDialog({ fields, onCancel, onSubmit }: { fields: RecordFields; onCancel: () => void; onSubmit: (c: Partial<RecordFields>, reason: string) => void }) {
  const ref = useModal();
  const [draft, setDraft] = useState<Record<string, string>>(Object.fromEntries(EDITABLE.map((f) => [f, String(fields[f])])));
  const [reason, setReason] = useState("");
  const changes = Object.fromEntries(
    EDITABLE.filter((f) => draft[f] !== String(fields[f])).map((f) => [f, f === "stop_minutes" ? Number(draft[f]) : draft[f]]),
  ) as Partial<RecordFields>;
  return (
    <dialog ref={ref} aria-labelledby="correct-title" onCancel={onCancel}>
      <h2 id="correct-title">Correct values</h2>
      <div className="stack">
        {EDITABLE.map((f) => (
          <div key={f}>
            <label htmlFor={`c-${f}`}>{LABELS[f]}</label>
            <input id={`c-${f}`} type="text" value={draft[f]} onChange={(e) => setDraft((d) => ({ ...d, [f]: e.target.value }))} />
          </div>
        ))}
        <div>
          <label htmlFor="c-reason">Reason (5–500 characters)</label>
          <textarea id="c-reason" rows={2} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>
      </div>
      <div className="row" style={{ marginTop: 12 }}>
        <button className="primary" disabled={!Object.keys(changes).length || reason.trim().length < 5} onClick={() => onSubmit(changes, reason.trim())}>
          Save correction
        </button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </dialog>
  );
}

function ReasonDialog({ title, action, onCancel, onSubmit }: { title: string; action: string; onCancel: () => void; onSubmit: (r: string) => void }) {
  const ref = useModal();
  const [reason, setReason] = useState("");
  return (
    <dialog ref={ref} aria-labelledby="reason-title" onCancel={onCancel}>
      <h2 id="reason-title">{title}</h2>
      <label htmlFor="reason">Reason (5–500 characters)</label>
      <textarea id="reason" rows={2} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
      <div className="row" style={{ marginTop: 12 }}>
        <button className="danger" disabled={reason.trim().length < 5} onClick={() => onSubmit(reason.trim())}>{action}</button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </dialog>
  );
}
