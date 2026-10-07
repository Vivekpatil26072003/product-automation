"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, use, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { SourcePane } from "@/components/review/SourcePane";
import { hasRole, useSession } from "@/components/SessionProvider";
import {
  ApiError,
  apiGet,
  apiSend,
  type Batch,
  type Candidate,
  type CandidateIssue,
  type ExtractionInfo,
  FIELD_NAMES,
  type FieldName,
  type Master,
} from "@/lib/api";
import type { OrderDraft } from "@/lib/orders";
import { SheetLinks } from "@/components/sheets/SheetLinks";

// U3 Review extracted data. Every entry needs a person's approval (A8); nothing here approves by itself.

const AUTOSAVE_MS = 800;
const LABELS: Record<FieldName, string> = {
  production_date: "Production date", department_id: "Department", machine_id: "Machine",
  operator_name: "Operator", production_qty: "Production quantity", target_qty: "Target quantity",
  unit: "Unit", status: "Status", stop_minutes: "Stop time (minutes)", remarks: "Remarks",
};
const REQUIRED = new Set<FieldName>(FIELD_NAMES.filter((f) => f !== "remarks"));
const CONFIDENCE: Record<Candidate["confidence"], string> = {
  OK: "Read clearly", ATTENTION: "Needs attention", UNASSESSED: "Reading confidence unknown",
};

type Draft = Partial<Record<FieldName, string>>;
type SaveState = "idle" | "saving" | "saved" | "failed" | "conflict";

function asText(v: Candidate["fields"][FieldName]["value"]): string {
  return v === null || v === undefined ? "" : String(v);
}

function toPayload(field: FieldName, text: string): string | number | null {
  const t = text.trim();
  if (!t) return field === "remarks" ? "" : null;
  if (field === "stop_minutes" && /^\d+$/.test(t)) return Number(t);
  return t;
}

export default function ReviewPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense>
      <Review batchId={use(params).id} />
    </Suspense>
  );
}

function Review({ batchId }: { batchId: string }) {
  const { session } = useSession();
  const router = useRouter();
  const pathname = usePathname();
  const search = useSearchParams();
  const isReviewer = hasRole(session, "REVIEWER");

  const [cands, setCands] = useState<Candidate[] | null>(null);
  const [extractions, setExtractions] = useState<ExtractionInfo[]>([]);
  const [batch, setBatch] = useState<Batch | null>(null);
  const [departments, setDepartments] = useState<Master[]>([]);
  const [machines, setMachines] = useState<Master[]>([]);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [saveState, setSaveState] = useState<Record<string, SaveState>>({});
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [focus, setFocus] = useState<{ field: FieldName | null; page: number }>({ field: null, page: 1 });
  const [tab, setTab] = useState<"source" | "fields">("fields");
  const [notice, setNotice] = useState<{ tone: "info" | "error" | "warning"; text: string; records?: string[] } | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [ack, setAck] = useState<{ excluded: { upload_id: string; unselected_entries: number; failed_pages: boolean }[] } | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [orderDrafts, setOrderDrafts] = useState<OrderDraft[]>([]);
  const [sheetUploads, setSheetUploads] = useState<string[]>([]);
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});

  const selectedId = search.get("candidate") ?? cands?.[0]?.id ?? null;
  const selected = cands?.find((c) => c.id === selectedId) ?? null;

  const reload = useCallback(async () => {
    const [c, b, o, sh] = await Promise.all([
      apiGet<{ data: { candidates: Candidate[]; extractions: ExtractionInfo[] } }>(`/batches/${batchId}/candidates`),
      apiGet<{ data: Batch }>(`/batches/${batchId}`),
      apiGet<{ data: OrderDraft[] }>(`/batches/${batchId}/order-drafts`).catch(() => ({ data: [] as OrderDraft[] })), // orders never block entry review
      apiGet<{ data: { upload_ids: string[] }[] }>(`/batches/${batchId}/sheets`).catch(() => ({ data: [] })),
    ]);
    setSheetUploads(sh.data.flatMap((x) => x.upload_ids));
    setCands(c.data.candidates);
    setExtractions(c.data.extractions);
    setBatch(b.data);
    setOrderDrafts(o.data);
  }, [batchId]);

  useEffect(() => {
    let active = true;
    Promise.all([
      apiGet<{ data: { candidates: Candidate[]; extractions: ExtractionInfo[] } }>(`/batches/${batchId}/candidates`),
      apiGet<{ data: Batch }>(`/batches/${batchId}`),
      apiGet<{ data: Master[] }>("/masters/departments?active=true"),
      apiGet<{ data: Master[] }>("/masters/machines?active=true"),
      apiGet<{ data: OrderDraft[] }>(`/batches/${batchId}/order-drafts`).catch(() => ({ data: [] as OrderDraft[] })), // orders never block entry review
      apiGet<{ data: { upload_ids: string[] }[] }>(`/batches/${batchId}/sheets`).catch(() => ({ data: [] })),
    ]).then(
      ([c, b, d, m, o, sh]) => {
        if (!active) return;
        setSheetUploads(sh.data.flatMap((x) => x.upload_ids));
        setCands(c.data.candidates);
        setExtractions(c.data.extractions);
        setBatch(b.data);
        setOrderDrafts(o.data);
        setDepartments(d.data);
        setMachines(m.data);
      },
      (e) => active && setNotice({ tone: "error", text: e instanceof ApiError ? e.message : "Could not load entries." }),
    );
    return () => {
      active = false;
    };
  }, [batchId]);

  // Unsaved local edits must not be lost silently.
  const dirty = Object.values(drafts).some((d) => Object.keys(d).length > 0);
  useEffect(() => {
    const onLeave = (e: BeforeUnloadEvent) => {
      if (dirty) e.preventDefault();
    };
    window.addEventListener("beforeunload", onLeave);
    return () => window.removeEventListener("beforeunload", onLeave);
  }, [dirty]);

  function select(id: string) {
    const params = new URLSearchParams(search.toString());
    params.set("candidate", id);
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
    setFocus({ field: null, page: 1 });
    setProblems([]);
  }

  function replaceCandidate(next: Candidate) {
    setCands((list) => list?.map((c) => (c.id === next.id ? next : c)) ?? null);
  }

  async function save(c: Candidate, draft: Draft, decision?: Candidate["duplicate_decision"]) {
    const fields: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(draft)) fields[k] = toPayload(k as FieldName, v ?? "");
    const body: Record<string, unknown> = { fields };
    if (decision !== undefined) body.duplicate_decision = decision;
    setSaveState((s) => ({ ...s, [c.id]: "saving" }));
    try {
      const { data } = await apiSend<{ data: Candidate }>("PATCH", `/candidates/${c.id}`, body, { ifMatch: `"${c.version}"` });
      replaceCandidate(data);
      setDrafts((d) => {
        const rest = { ...(d[c.id] ?? {}) };
        for (const k of Object.keys(draft)) if (rest[k as FieldName] === draft[k as FieldName]) delete rest[k as FieldName];
        return { ...d, [c.id]: rest };
      });
      setSaveState((s) => ({ ...s, [c.id]: "saved" }));
    } catch (e) {
      if (e instanceof ApiError && e.status === 412) {
        setSaveState((s) => ({ ...s, [c.id]: "conflict" }));
      } else {
        setSaveState((s) => ({ ...s, [c.id]: "failed" }));
        setNotice({ tone: "error", text: e instanceof ApiError ? e.message : "Saving failed." });
      }
    }
  }

  function edit(field: FieldName, value: string) {
    if (!selected) return;
    const c = selected;
    const draft = { ...(drafts[c.id] ?? {}), [field]: value };
    setDrafts((d) => ({ ...d, [c.id]: draft }));
    setSaveState((s) => ({ ...s, [c.id]: "idle" }));
    clearTimeout(timers.current[c.id]);
    timers.current[c.id] = setTimeout(() => void save(c, draft), AUTOSAVE_MS);
  }

  async function loadServerVersion() {
    if (!selected) return;
    const { data } = await apiGet<{ data: Candidate }>(`/candidates/${selected.id}`);
    replaceCandidate(data);
    setDrafts((d) => ({ ...d, [selected.id]: {} }));
    setSaveState((s) => ({ ...s, [selected.id]: "idle" }));
  }

  async function approve(ackPartial = false) {
    const items = (cands ?? []).filter((c) => checked.has(c.id)).map((c) => ({ candidate_id: c.id, version: c.version }));
    setBusy(true);
    setProblems([]);
    try {
      const { data } = await apiSend<{ data: { record_ids: string[] } }>("POST", "/approvals", { items, ack_partial: ackPartial });
      setAck(null);
      setChecked(new Set());
      setNotice({ tone: "info", text: `${data.record_ids.length} record(s) approved.`, records: data.record_ids });
      await reload();
    } catch (e) {
      if (e instanceof ApiError && e.code === "PARTIAL_ACK_REQUIRED") {
        setAck({ excluded: (e.details.excluded as NonNullable<typeof ack>["excluded"]) ?? [] });
      } else if (e instanceof ApiError) {
        setProblems(e.fields.length ? e.fields.map((f) => f.message) : [e.message]);
        setNotice({ tone: "error", text: `${e.message} (nothing was approved)` });
        await reload(); // show the current problems, e.g. a duplicate that appeared since the last save
      }
    } finally {
      setBusy(false);
    }
  }

  async function reject(reason: string) {
    if (!selected) return;
    try {
      await apiSend("POST", `/candidates/${selected.id}/reject`, { reason });
      setRejecting(false);
      setNotice({ tone: "info", text: "Entry rejected. It will not count in any totals." });
      await reload();
    } catch (e) {
      setNotice({ tone: "error", text: e instanceof ApiError ? e.message : "Rejecting failed." });
    }
  }

  async function uploadAction(uploadId: string, kind: "manual" | "reprocess" | "order") {
    try {
      if (kind === "order") {
        const { data } = await apiSend<{ data: OrderDraft }>("POST", `/uploads/${uploadId}/order-drafts`);
        router.push(`/batches/${batchId}/orders?draft=${data.id}`);
      } else if (kind === "manual") {
        const { data } = await apiSend<{ data: Candidate }>("POST", `/uploads/${uploadId}/candidates`);
        await reload();
        select(data.id);
      } else {
        await apiSend("POST", `/batches/${batchId}/reprocess`, { upload_ids: [uploadId], preserve_edits: true });
        setNotice({ tone: "info", text: "Extracting again. Entries you edited are kept; refresh in a moment." });
      }
    } catch (e) {
      setNotice({ tone: "error", text: e instanceof ApiError ? e.message : "The action failed." });
    }
  }

  const fileName = useMemo(() => new Map(batch?.files.map((f) => [f.id, f.name]) ?? []), [batch]);
  const running = (j: Batch["files"][number]["jobs"]["parse"]) => !!j && ["QUEUED", "RUNNING", "RETRY_WAIT"].includes(j.state);
  // A file counts as read once extraction finished, or once reading failed on every page (for example a
  // handwritten photo with no OCR reader configured): no extraction runs then, but the file needs a way forward.
  const withoutEntries = (batch?.files ?? []).filter(
    (f) => f.state === "READY" &&
      ((f.jobs.extract && !running(f.jobs.extract)) || (!f.jobs.extract && f.jobs.parse && !running(f.jobs.parse) && f.pages.succeeded.length === 0)) &&
      !(cands ?? []).some((c) => c.upload_id === f.id) && !orderDrafts.some((o) => o.upload_id === f.id) &&
      !sheetUploads.includes(f.id),
  );
  const unreadMessage = (f: Batch["files"][number]) => {
    const x = extractions.find((e) => e.upload_id === f.id);
    if (!f.jobs.extract && f.jobs.parse?.error?.code === "OCR_NOT_CONFIGURED") {
      return "This is a photo or scan and no OCR reader is set up, so nothing could be read. Enter the values yourself " +
        "beside the photo, or ask an administrator to set up OCR and then use \"Retry failed pages\" on the processing screen.";
    }
    if (!f.jobs.extract) return f.jobs.parse?.error?.message ?? "The file could not be read.";
    return x?.error_message ?? (x?.warnings.includes("AI_NOT_CONFIGURED")
      ? "No entries could be read automatically (AI extraction is not configured)."
      : "No entries could be read automatically.");
  };

  if (!cands) {
    return (
      <div aria-busy="true">
        <h1>Review entries</h1>
        {notice ? <div className="banner error" role="alert">{notice.text}</div> : <div className="skeleton" style={{ width: "50%" }} />}
      </div>
    );
  }

  const draft = (selected && drafts[selected.id]) || {};
  const value = (f: FieldName) => (f in draft ? (draft[f] ?? "") : asText(selected?.fields[f].value ?? null));
  const issuesFor = (f: FieldName | null) => selected?.issues.filter((i) => i.field === f) ?? [];
  const blocking = selected?.issues.filter((i) => i.severity === "error") ?? [];
  const deptId = value("department_id");
  const state = selected ? saveState[selected.id] ?? "idle" : "idle";
  const highlight = focus.field && selected ? selected.fields[focus.field].evidence.map((e) => e.id) : [];

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>Review entries</h1>
        <Link className="button" href={`/batches/${batchId}`}>Back to processing</Link>
      </div>
      <p className="meta">Every entry needs your check. Nothing is added to production totals until you approve it.</p>

      {notice && (
        <div className={`banner ${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>
          {notice.text}{" "}
          {notice.records?.map((r) => <Link key={r} href={`/records/${r}`} style={{ marginRight: 8 }}>Open record</Link>)}
        </div>
      )}
      {extractions.filter((x) => x.release_state === "UNEVALUATED").length > 0 && (
        <div className="banner warning">Some entries were read by an AI model version that has not passed evaluation. Check every value.</div>
      )}

      <SheetLinks batchId={batchId} refresh={String(sheetUploads.length)} />

      {orderDrafts.length > 0 && (
        <section className="card row" style={{ justifyContent: "space-between" }} aria-labelledby="orders-title">
          <div>
            <h2 id="orders-title" style={{ margin: 0 }}>Customer orders ({orderDrafts.length})</h2>
            <p className="meta" style={{ margin: 0 }}>
              {orderDrafts.map((o) => o.fields.customer_name.value || "customer not read").join(", ")}: read from order notes, waiting for review.
            </p>
          </div>
          <Link className="button primary" href={`/batches/${batchId}/orders`}>Review orders</Link>
        </section>
      )}

      {withoutEntries.length > 0 && (
        <section className="card">
          <h2>Files without entries</h2>
          <ul className="filelist">
            {withoutEntries.map((f) => {
              return (
                <li key={f.id}>
                  <div>
                    <div className="filename">{f.name}</div>
                    <div className="meta">{unreadMessage(f)}</div>
                  </div>
                  <div className="row">
                    <button onClick={() => uploadAction(f.id, "order")}>Enter customer order</button>
                    <button onClick={() => uploadAction(f.id, "manual")}>Enter production entry</button>
                    {f.jobs.extract && <button onClick={() => uploadAction(f.id, "reprocess")}>Extract again</button>}
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {cands.length === 0 ? (
        <div className="card"><p>No production entries are waiting for review in this batch.</p></div>
      ) : (
        <>
          <section className="card" aria-labelledby="entries-title">
            <h2 id="entries-title">Entries ({cands.length})</h2>
            <ul className="filelist">
              {cands.map((c) => (
                <li key={c.id} aria-current={c.id === selectedId ? "true" : undefined}>
                  <div className="row">
                    {isReviewer && (
                      <input
                        type="checkbox"
                        aria-label={`Select ${c.source_record_key} for approval`}
                        disabled={!c.approvable}
                        checked={checked.has(c.id)}
                        onChange={(e) => setChecked((s) => {
                          const n = new Set(s);
                          if (e.target.checked) n.add(c.id); else n.delete(c.id);
                          return n;
                        })}
                      />
                    )}
                    <button className="linklike" onClick={() => select(c.id)}>
                      {c.fields.machine_id.display ?? c.fields.machine_id.raw ?? "Unknown machine"} ·{" "}
                      {c.fields.production_date.value ?? c.fields.production_date.raw ?? "no date"} ·{" "}
                      {fileName.get(c.upload_id)}
                    </button>
                  </div>
                  <div className="row">
                    <span className={`badge ${c.approvable ? "tone-success" : "tone-warning"}`}>
                      {c.approvable ? "Ready to approve" : `${c.issues.filter((i) => i.severity === "error").length} to fix`}
                    </span>
                    <span className="badge tone-neutral">{CONFIDENCE[c.confidence]}</span>
                  </div>
                </li>
              ))}
            </ul>
            {isReviewer && (
              <div className="row" style={{ marginTop: 12 }}>
                <button className="primary" disabled={busy || checked.size === 0} onClick={() => approve(false)}>
                  Approve selected ({checked.size})
                </button>
                <span className="meta">Only entries without problems can be selected.</span>
              </div>
            )}
            {problems.length > 0 && (
              <div className="banner error" role="alert" style={{ marginTop: 12 }}>
                <ul>{problems.map((p) => <li key={p}>{p}</li>)}</ul>
              </div>
            )}
          </section>

          {selected && (
            <>
              <div className="tabs" role="tablist" aria-label="Review panes">
                <button role="tab" aria-selected={tab === "source"} onClick={() => setTab("source")}>Source</button>
                <button role="tab" aria-selected={tab === "fields"} onClick={() => setTab("fields")}>Fields</button>
              </div>
              <div className="review-split">
                <div className={`pane-source${tab === "source" ? " active" : ""}`}>
                  <SourcePane
                    uploadId={selected.upload_id}
                    page={focus.page}
                    highlight={highlight}
                    onPage={(p) => setFocus((f) => ({ ...f, page: p }))}
                  />
                </div>
                <div className={`pane-fields${tab === "fields" ? " active" : ""}`}>
                  <section className="card" aria-labelledby="fields-title">
                    <div className="row" style={{ justifyContent: "space-between" }}>
                      <h2 id="fields-title" style={{ margin: 0 }}>Fields</h2>
                      <span className="meta" role="status" aria-live="polite">
                        {{ idle: "", saving: "Saving…", saved: "Saved", failed: "Save failed", conflict: "Not saved" }[state]}
                        {state === "failed" && (
                          <button style={{ marginLeft: 8 }} onClick={() => save(selected, draft)}>Retry</button>
                        )}
                      </span>
                    </div>

                    {state === "conflict" && (
                      <div className="banner warning" role="alert" style={{ marginTop: 12 }}>
                        Someone else changed this entry. Your edits were not saved.{" "}
                        <button onClick={loadServerVersion}>Load their version</button>
                      </div>
                    )}

                    {blocking.length > 0 && (
                      <div className="banner error" style={{ marginTop: 12 }} role="group" aria-label="Problems to fix">
                        <strong>Fix before approving:</strong>
                        <ul>
                          {blocking.map((i) => (
                            <li key={`${i.field}-${i.code}`}>
                              {i.field ? <a href={`#field-${i.field}`}>{LABELS[i.field]}</a> : "Entry"}: {i.message}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    <div className="stack" style={{ marginTop: 12 }}>
                      {FIELD_NAMES.map((f) => (
                        <FieldRow
                          key={f}
                          name={f}
                          label={LABELS[f]}
                          value={value(f)}
                          field={selected.fields[f]}
                          issues={issuesFor(f)}
                          departments={departments}
                          machines={machines.filter((m) => !deptId || m.department_id === deptId)}
                          disabled={selected.state !== "NEEDS_REVIEW"}
                          onChange={(v) => edit(f, v)}
                          onShow={() => {
                            const page = selected.fields[f].evidence[0]?.page ?? 1;
                            setFocus({ field: f, page });
                            setTab("source");
                          }}
                        />
                      ))}
                    </div>

                    <DuplicatePanel candidate={selected} onDecide={(d) => save(selected, draft, d)} />

                    {isReviewer && selected.state === "NEEDS_REVIEW" && (
                      <div className="row" style={{ marginTop: 16 }}>
                        <button className="danger" onClick={() => setRejecting(true)}>Reject entry</button>
                        <button onClick={() => uploadAction(selected.upload_id, "reprocess")}>Extract this file again</button>
                      </div>
                    )}
                  </section>
                </div>
              </div>
            </>
          )}
        </>
      )}

      {ack && (
        <ConfirmDialog
          title="Some pages or entries are not included"
          confirm="Approve the selected entries"
          onCancel={() => setAck(null)}
          onConfirm={() => approve(true)}
        >
          <ul>
            {ack.excluded.map((x) => (
              <li key={x.upload_id}>
                {fileName.get(x.upload_id)}:{" "}
                {[x.unselected_entries ? `${x.unselected_entries} other entr${x.unselected_entries === 1 ? "y" : "ies"} not selected` : "",
                  x.failed_pages ? "some pages could not be read" : ""].filter(Boolean).join("; ")}
              </li>
            ))}
          </ul>
          <p>Those parts stay out of the production records unless you approve them later.</p>
        </ConfirmDialog>
      )}
      {rejecting && <RejectDialog onCancel={() => setRejecting(false)} onReject={reject} />}
    </>
  );
}

function FieldRow(props: {
  name: FieldName;
  label: string;
  value: string;
  field: Candidate["fields"][FieldName];
  issues: CandidateIssue[];
  departments: Master[];
  machines: Master[];
  disabled: boolean;
  onChange: (v: string) => void;
  onShow: () => void;
}) {
  const { name, label, value, field, issues, disabled, onChange } = props;
  const id = `field-${name}`;
  const describedBy = issues.length ? `${id}-issues` : undefined;
  const invalid = issues.some((i) => i.severity === "error");
  const common = { id, disabled, "aria-invalid": invalid || undefined, "aria-describedby": describedBy };
  let control: React.ReactNode;
  if (name === "department_id" || name === "machine_id") {
    const options = name === "department_id" ? props.departments : props.machines;
    control = (
      <select {...common} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">Choose…</option>
        {options.map((o) => <option key={o.id} value={o.id}>{name === "machine_id" ? o.code : o.name}</option>)}
      </select>
    );
  } else if (name === "unit" || name === "status") {
    const options = name === "unit" ? ["m", "kg", "pcs"] : ["RUNNING", "COMPLETED", "PENDING", "HOLD"];
    control = (
      <select {...common} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">Choose…</option>
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    );
  } else if (name === "remarks") {
    control = <textarea {...common} rows={2} maxLength={2000} value={value} onChange={(e) => onChange(e.target.value)} />;
  } else {
    const type = name === "production_date" ? "date" : "text";
    const inputMode = name === "stop_minutes" ? "numeric" : name.endsWith("_qty") ? "decimal" : undefined;
    control = <input {...common} type={type} inputMode={inputMode} value={value} onChange={(e) => onChange(e.target.value)} />;
  }
  return (
    <div className="field">
      <label htmlFor={id}>
        {label}
        {REQUIRED.has(name) && <span className="meta"> (required)</span>}
      </label>
      {control}
      <div className="meta row">
        {field.raw ? <span>From note: “{field.raw}”</span> : field.source === "upload_context" ? <span>Proposed from the upload</span> : <span>Not found in the note</span>}
        {field.source === "reviewer" && <span className="badge tone-neutral">Edited</span>}
        {field.evidence.length > 0 && (
          <button className="linklike" onClick={props.onShow}>Show in source</button>
        )}
      </div>
      {issues.length > 0 && (
        <ul id={`${id}-issues`} className="issues">
          {issues.map((i) => (
            <li key={i.code} className={i.severity === "error" ? "reason" : "meta"}>
              {i.severity === "error" ? "Problem: " : "Note: "}
              {i.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DuplicatePanel({ candidate, onDecide }: { candidate: Candidate; onDecide: (d: Candidate["duplicate_decision"]) => void }) {
  const [reason, setReason] = useState(candidate.duplicate_decision?.reason ?? "");
  const blocking = candidate.duplicates.filter((d) => d.kind !== "PENDING_CANDIDATE");
  if (!blocking.length || candidate.state !== "NEEDS_REVIEW") return null;
  const kinds: Record<string, string> = {
    EXACT_FILE: "the same file was uploaded before",
    SAME_RECORD: "an approved record has the same date, machine, quantity and operator",
    NEAR_RECORD: "an approved record has the same date and machine",
  };
  return (
    <fieldset className="card" style={{ marginTop: 16 }}>
      <legend>Possible duplicate</legend>
      <ul>
        {blocking.map((d, i) => (
          <li key={i}>
            {kinds[d.kind] ?? d.kind}
            {d.record_id && <> (<Link href={`/records/${d.record_id}`}>open record</Link>)</>}
          </li>
        ))}
      </ul>
      <label htmlFor="dup-reason">Why is this a separate event?</label>
      <input id="dup-reason" type="text" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
      <div className="row" style={{ marginTop: 8 }}>
        <button disabled={reason.trim().length < 5} onClick={() => onDecide({ action: "KEEP", reason: reason.trim() })}>
          Keep as a separate event
        </button>
        <button onClick={() => onDecide({ action: "SKIP", reason: null })}>It is a duplicate</button>
      </div>
      {candidate.duplicate_decision && (
        <p className="meta">
          Decision: {candidate.duplicate_decision.action === "KEEP" ? `kept (${candidate.duplicate_decision.reason})` : "duplicate — reject it"}
        </p>
      )}
    </fieldset>
  );
}

function ConfirmDialog(props: { title: string; confirm: string; children: React.ReactNode; onCancel: () => void; onConfirm: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog ref={ref} aria-labelledby="confirm-title" onCancel={props.onCancel}>
      <h2 id="confirm-title">{props.title}</h2>
      {props.children}
      <div className="row">
        <button className="primary" onClick={props.onConfirm}>{props.confirm}</button>
        <button onClick={props.onCancel}>Go back</button>
      </div>
    </dialog>
  );
}

function RejectDialog({ onCancel, onReject }: { onCancel: () => void; onReject: (reason: string) => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [reason, setReason] = useState("");
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog ref={ref} aria-labelledby="reject-title" onCancel={onCancel}>
      <h2 id="reject-title">Reject this entry</h2>
      <label htmlFor="reject-reason">Reason (5–500 characters)</label>
      <textarea id="reject-reason" rows={3} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
      <div className="row" style={{ marginTop: 8 }}>
        <button className="danger" disabled={reason.trim().length < 5} onClick={() => onReject(reason.trim())}>Reject entry</button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </dialog>
  );
}
