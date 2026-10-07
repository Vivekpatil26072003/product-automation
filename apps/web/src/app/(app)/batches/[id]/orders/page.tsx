"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, use, useCallback, useEffect, useRef, useState } from "react";

import { SourcePane } from "@/components/review/SourcePane";
import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend } from "@/lib/api";
import {
  type ExtraInfo, formatAmount, formatDay, ORDER_DATES, ORDER_FIELDS, ORDER_LABEL, ORDER_NUMBERS, ORDER_REQUIRED,
  type OrderDraft, type OrderField, readingNote, TABLE_FIELDS,
} from "@/lib/orders";

// Customer order review. A table shows every order read from this batch (several customers per page are
// separate rows); the selected order opens in a full form beside its page. Values the reader was not sure of
// are highlighted and must be checked and confirmed (or corrected) before the order can be saved; nothing is
// saved automatically. Every change autosaves; a Reviewer approves to save the order.

const AUTOSAVE_MS = 800;
type SaveState = "idle" | "saving" | "saved" | "failed" | "conflict";
type Patch = { fields?: Record<string, string | null>; confirm?: string[]; extra?: { label: string; value: string }[]; decision?: unknown };

export default function OrderReviewPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense>
      <OrderReview batchId={use(params).id} />
    </Suspense>
  );
}

function readerText(d: OrderDraft): string {
  const r = d.reading ?? {};
  if (d.source === "manual" || r.reader === "manual") return "Entered by hand: nothing could be read automatically.";
  if (r.reader === "claude-orders") {
    const langs = r.languages?.length ? ` · ${r.languages.join(", ")}` : "";
    return `Read by AI (${r.model ?? "Claude"})${langs}. Check every value against the page.`;
  }
  return "Read from \"Label : value\" lines on the page. Check every value against the page.";
}

function OrderReview({ batchId }: { batchId: string }) {
  const { session } = useSession();
  const router = useRouter();
  const pathname = usePathname();
  const search = useSearchParams();
  const isReviewer = hasRole(session, "REVIEWER");
  const [drafts, setDrafts] = useState<OrderDraft[] | null>(null);
  const [edits, setEdits] = useState<Record<string, Partial<Record<OrderField, string>>>>({});
  const [saveState, setSaveState] = useState<Record<string, SaveState>>({});
  const [notice, setNotice] = useState<{ tone: "info" | "error" | "success"; text: string; orderId?: string } | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [focus, setFocus] = useState<OrderField | null>(null);
  const [tab, setTab] = useState<"source" | "fields">("fields");
  const [busy, setBusy] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [extraDraft, setExtraDraft] = useState<{ id: string; items: ExtraInfo[] } | null>(null);
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const latest = useRef<Record<string, OrderDraft>>({});

  const load = useCallback(async () => {
    try {
      const r = await apiGet<{ data: OrderDraft[] }>(`/batches/${batchId}/order-drafts`);
      setDrafts(r.data);
    } catch (e) {
      setNotice({ tone: "error", text: e instanceof ApiError ? e.message : "The order forms could not be loaded." });
    }
  }, [batchId]);

  useEffect(() => {
    const t = setTimeout(() => void load(), 0);
    return () => clearTimeout(t);
  }, [load]);

  useEffect(() => {
    for (const d of drafts ?? []) latest.current[d.id] = d;
  }, [drafts]);

  const selectedId = search.get("draft") ?? drafts?.[0]?.id ?? null;
  const selected = drafts?.find((d) => d.id === selectedId) ?? null;
  const dirty = Object.values(edits).some((e) => Object.keys(e).length > 0);

  useEffect(() => {
    const onLeave = (e: BeforeUnloadEvent) => {
      if (dirty) e.preventDefault();
    };
    window.addEventListener("beforeunload", onLeave);
    return () => window.removeEventListener("beforeunload", onLeave);
  }, [dirty]);

  function choose(id: string) {
    const p = new URLSearchParams(search.toString());
    p.set("draft", id);
    router.replace(`${pathname}?${p.toString()}`, { scroll: false });
    setProblems([]);
    setFocus(null);
    setRejecting(false);
  }

  async function send(id: string, body: Patch, clearFields: Partial<Record<OrderField, string>> = {}): Promise<OrderDraft | null> {
    const d = latest.current[id];
    if (!d) return null;
    setSaveState((s) => ({ ...s, [id]: "saving" }));
    try {
      const r = await apiSend<{ data: OrderDraft }>("PATCH", `/order-drafts/${id}`, { fields: {}, ...body }, { ifMatch: `"${d.version}"` });
      latest.current[id] = r.data;
      setDrafts((list) => list?.map((x) => (x.id === id ? r.data : x)) ?? null);
      setEdits((all) => {
        const rest = { ...(all[id] ?? {}) };
        for (const k of Object.keys(clearFields) as OrderField[]) if (rest[k] === clearFields[k]) delete rest[k];
        return { ...all, [id]: rest };
      });
      setSaveState((s) => ({ ...s, [id]: "saved" }));
      return r.data;
    } catch (e) {
      setSaveState((s) => ({ ...s, [id]: e instanceof ApiError && e.status === 412 ? "conflict" : "failed" }));
      if (!(e instanceof ApiError && e.status === 412)) setNotice({ tone: "error", text: e instanceof ApiError ? e.message : "Saving failed." });
      return null;
    }
  }

  function edit(d: OrderDraft, field: OrderField, value: string) {
    const change = { ...(edits[d.id] ?? {}), [field]: value };
    setEdits((all) => ({ ...all, [d.id]: change }));
    setSaveState((s) => ({ ...s, [d.id]: "idle" }));
    clearTimeout(timers.current[d.id]);
    timers.current[d.id] = setTimeout(() => {
      const fields: Record<string, string | null> = {};
      for (const [k, v] of Object.entries(change)) fields[k] = (v ?? "").trim() || null;
      void send(d.id, { fields }, change);
    }, AUTOSAVE_MS);
  }

  async function flush(d: OrderDraft): Promise<OrderDraft | null> {
    clearTimeout(timers.current[d.id]);
    const pending = edits[d.id] ?? {};
    if (!Object.keys(pending).length) return latest.current[d.id] ?? d;
    const fields: Record<string, string | null> = {};
    for (const [k, v] of Object.entries(pending)) fields[k] = (v ?? "").trim() || null;
    return send(d.id, { fields }, pending);
  }

  async function confirm(d: OrderDraft, fields: OrderField[]) {
    const current = await flush(d);
    if (current) await send(d.id, { confirm: fields });
  }

  async function approve() {
    if (!selected) return;
    setBusy(true);
    setProblems([]);
    try {
      const current = await flush(selected);
      if (!current) return; // a failed save blocks approval
      const r = await apiSend<{ data: { id: string; order_ref: string; revision: number } }>("POST", `/order-drafts/${current.id}/approve`, undefined,
        { ifMatch: `"${current.version}"` });
      const what = r.data.revision > 1 ? `Order ${r.data.order_ref} updated (revision ${r.data.revision}).` : `Order ${r.data.order_ref} saved.`;
      setNotice({ tone: "success", text: `${what} It is now in Customer orders.`, orderId: r.data.id });
      await load();
    } catch (e) {
      if (e instanceof ApiError) {
        setProblems(e.fields.length ? e.fields.map((f) => f.message) : [e.message]);
        setNotice({ tone: "error", text: `${e.status === 412 ? "This form changed since you opened it. Reloaded." : e.message}` });
        await load();
      } else setNotice({ tone: "error", text: "Nothing was saved. Check your connection and try again." });
    } finally {
      setBusy(false);
    }
  }

  async function reject(e: React.FormEvent) {
    e.preventDefault();
    if (!selected) return;
    try {
      await apiSend("POST", `/order-drafts/${selected.id}/reject`, { reason });
      setRejecting(false);
      setReason("");
      setNotice({ tone: "info", text: "Order form rejected. Nothing was saved." });
      await load();
    } catch (err) {
      setNotice({ tone: "error", text: err instanceof ApiError ? err.message : "Rejecting failed." });
    }
  }

  if (!drafts) {
    return (
      <div aria-busy="true">
        <h1>Review customer orders</h1>
        {notice ? <div className="banner error" role="alert">{notice.text}</div> : <div className="skeleton" style={{ width: "50%" }} />}
      </div>
    );
  }

  const valueOf = (d: OrderDraft, f: OrderField) => {
    const local = edits[d.id] ?? {};
    return f in local ? local[f] ?? "" : d.fields[f]?.value ?? "";
  };
  const issuesFor = (d: OrderDraft, f: OrderField | null) => d.issues.filter((i) => i.field === f);
  const needsCheck = (d: OrderDraft, f: OrderField) => issuesFor(d, f).some((i) => i.severity === "error");
  const toConfirm = (d: OrderDraft) => [...new Set(d.issues.filter((i) => i.code === "CONFIRM_VALUE" && i.field).map((i) => i.field as OrderField))];
  const state = selected ? saveState[selected.id] ?? "idle" : "idle";
  const highlight = focus && selected ? selected.fields[focus].evidence.map((e) => e.id) : [];
  const extraItems = selected ? (extraDraft?.id === selected.id ? extraDraft.items : selected.extra) : [];

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>Review customer orders</h1>
        <div className="row">
          <Link className="button" href={`/batches/${batchId}/review`}>All entries</Link>
          <Link className="button" href={`/batches/${batchId}`}>Back to processing</Link>
        </div>
      </div>
      <p className="meta">Every order read from the diary is listed below. Highlighted values were hard to read or are missing: check them against the page, correct or confirm them, then approve.</p>

      {notice && (
        <div className={`banner ${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>
          {notice.text} {notice.orderId && <><Link href={`/orders/${notice.orderId}`}>Open the order</Link> · <Link href="/orders">Customer orders</Link></>}
        </div>
      )}

      {drafts.length === 0 ? (
        <div className="card"><p>No order forms are waiting for review in this batch. <Link href={`/batches/${batchId}`}>See the batch status</Link></p></div>
      ) : (
        <>
          <section className="card table-scroll" role="region" aria-labelledby="o-table" tabIndex={0}>
            <h2 id="o-table">Orders read from this batch ({drafts.length})</h2>
            <table className="data">
              <thead>
                <tr>
                  <th scope="col">Page</th>
                  {TABLE_FIELDS.map((f) => <th key={f} scope="col">{ORDER_LABEL[f]}</th>)}
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {drafts.map((d) => {
                  const checks = d.issues.filter((i) => i.severity === "error").length;
                  return (
                    <tr key={d.id} aria-current={d.id === selectedId ? "true" : undefined}>
                      <td><button className="linklike" onClick={() => choose(d.id)}>{d.file_name} p{d.page_no}</button></td>
                      {TABLE_FIELDS.map((f) => (
                        <td key={f} className={needsCheck(d, f) ? "cell-check" : undefined}>
                          <label className="sr-only" htmlFor={`t-${d.id}-${f}`}>{ORDER_LABEL[f]} ({d.fields.customer_name.value ?? "order"})</label>
                          <input id={`t-${d.id}-${f}`} className={ORDER_NUMBERS.has(f) ? "cell-input tnum" : "cell-input"}
                            type={ORDER_DATES.has(f) ? "date" : "text"} inputMode={ORDER_NUMBERS.has(f) ? "decimal" : undefined}
                            value={valueOf(d, f)} aria-invalid={needsCheck(d, f) ? true : undefined}
                            disabled={d.state !== "NEEDS_REVIEW"} onChange={(e) => edit(d, f, e.target.value)} />
                        </td>
                      ))}
                      <td>
                        <span className={`badge tone-${d.approvable ? "success" : "warning"}`}>
                          {d.approvable ? "Ready" : `${checks} to check`}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </section>

          {selected && (
            <>
              <div className="tabs" role="tablist" aria-label="Show">
                <button role="tab" aria-selected={tab === "fields"} onClick={() => setTab("fields")}>Order form</button>
                <button role="tab" aria-selected={tab === "source"} onClick={() => setTab("source")}>Diary page</button>
              </div>
              <div className="review-split">
                <div className={`pane-source${tab === "source" ? " active" : ""}`}>
                  <SourcePane uploadId={selected.upload_id} page={selected.page_no} highlight={highlight} onPage={() => undefined} />
                </div>
                <div className={`pane-fields card stack${tab === "fields" ? " active" : ""}`}>
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <h2 style={{ margin: 0 }}>{selected.fields.customer_name.value || "Order"} · page {selected.page_no}</h2>
                    <span className="meta" aria-live="polite">
                      {{ idle: Object.keys(edits[selected.id] ?? {}).length ? "Unsaved changes" : "", saving: "Saving…", saved: "Saved", failed: "Not saved", conflict: "Changed elsewhere" }[state]}
                      {state === "conflict" && <> <button className="linklike" onClick={() => void load()}>Reload</button></>}
                    </span>
                  </div>
                  <p className="meta" style={{ margin: 0 }}>{readerText(selected)}</p>
                  {issuesFor(selected, null).filter((i) => i.code !== "DUPLICATE_DECISION").map((i) => (
                    <div key={i.code} className={`banner ${i.severity === "error" ? "error" : "warning"}`}>{i.message}</div>
                  ))}
                  {(selected.reading?.warnings ?? []).map((w) => <p key={w} className="meta" style={{ margin: 0 }}>Note from the reader: {w}</p>)}

                  {selected.matches.length > 0 && selected.state === "NEEDS_REVIEW" && (
                    <fieldset className="stack" style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 12 }}>
                      <legend style={{ fontWeight: 600 }}>This looks like an order that is already saved</legend>
                      {selected.matches.map((m) => (
                        <label key={m.order_id} style={{ fontWeight: 400 }}>
                          <input type="radio" name={`decision-${selected.id}`} disabled={!isReviewer}
                            checked={selected.decision?.mode === "update" && selected.decision.order_id === m.order_id}
                            onChange={() => void send(selected.id, { decision: { mode: "update", order_id: m.order_id } })} />{" "}
                          Update {m.order_ref} ({m.customer_name}, {formatDay(m.order_date)}, qty {m.quantity ?? "?"}, total {formatAmount(m.total)})
                        </label>
                      ))}
                      <label style={{ fontWeight: 400 }}>
                        <input type="radio" name={`decision-${selected.id}`} disabled={!isReviewer} checked={selected.decision?.mode === "new"}
                          onChange={() => void send(selected.id, { decision: { mode: "new" } })} /> Save as a new, separate order
                      </label>
                      {!isReviewer && <p className="meta" style={{ margin: 0 }}>A Reviewer makes this choice.</p>}
                    </fieldset>
                  )}

                  {toConfirm(selected).length > 0 && selected.state === "NEEDS_REVIEW" && (
                    <div className="banner warning" role="status">
                      {toConfirm(selected).length} value(s) were hard to read. Check each against the page, correct it if needed, and confirm it.
                    </div>
                  )}

                  <div className="form-grid">
                    {ORDER_FIELDS.map((f) => {
                      const fi = selected.fields[f];
                      const iss = issuesFor(selected, f);
                      const err = iss.find((i) => i.severity === "error");
                      const id = `of-${f}`;
                      const canConfirm = iss.some((i) => i.code === "CONFIRM_VALUE") && selected.state === "NEEDS_REVIEW";
                      return (
                        <div key={f} className={err ? "field-check" : undefined} style={f === "remarks" ? { gridColumn: "1 / -1" } : undefined}>
                          <label htmlFor={id}>{ORDER_LABEL[f]}{ORDER_REQUIRED.has(f) && <span aria-hidden="true"> *</span>}</label>
                          {f === "remarks" ? (
                            <textarea id={id} className="wide" rows={3} value={valueOf(selected, f)} aria-invalid={err ? true : undefined}
                              aria-describedby={`${id}-help`} onFocus={() => setFocus(f)} onChange={(e) => edit(selected, f, e.target.value)} />
                          ) : (
                            <input id={id} className="wide" required={ORDER_REQUIRED.has(f)}
                              type={ORDER_DATES.has(f) ? "date" : f === "customer_email" ? "email" : "text"}
                              inputMode={ORDER_NUMBERS.has(f) ? "decimal" : f === "mobile" ? "tel" : undefined}
                              value={valueOf(selected, f)} aria-invalid={err ? true : undefined} aria-describedby={`${id}-help`}
                              onFocus={() => setFocus(f)} onChange={(e) => edit(selected, f, e.target.value)} />
                          )}
                          <p id={`${id}-help`} className={err ? "reason" : "meta"} style={{ margin: "4px 0 0" }}>
                            {iss.map((i) => i.message).join(" ") || readingNote(fi) || (fi.value ? "" : "Not found on the page. Enter it if known.")}
                            {fi.corrected_from && !iss.length ? ` Crossed out on the page: "${fi.corrected_from}".` : ""}
                          </p>
                          {canConfirm && (
                            <button type="button" style={{ marginTop: 4 }} onClick={() => void confirm(selected, [f])}>
                              Confirm {ORDER_LABEL[f].toLowerCase()} as shown
                            </button>
                          )}
                        </div>
                      );
                    })}
                  </div>

                  <fieldset className="stack" style={{ border: 0, padding: 0, margin: 0 }}>
                    <legend style={{ fontWeight: 600 }}>Other information on the page</legend>
                    {extraItems.length === 0 && <p className="meta" style={{ margin: 0 }}>None. Add anything else written for this order that has no field above.</p>}
                    {extraItems.map((x, i) => (
                      <div key={i} className="row">
                        <label className="sr-only" htmlFor={`x-l-${i}`}>Label</label>
                        <input id={`x-l-${i}`} placeholder="Label" value={x.label} disabled={selected.state !== "NEEDS_REVIEW"}
                          onChange={(e) => setExtraDraft({ id: selected.id, items: extraItems.map((y, j) => (j === i ? { ...y, label: e.target.value } : y)) })} />
                        <label className="sr-only" htmlFor={`x-v-${i}`}>Value</label>
                        <input id={`x-v-${i}`} placeholder="Value" value={x.value} disabled={selected.state !== "NEEDS_REVIEW"}
                          onChange={(e) => setExtraDraft({ id: selected.id, items: extraItems.map((y, j) => (j === i ? { ...y, value: e.target.value } : y)) })} />
                        <button type="button" onClick={() => setExtraDraft({ id: selected.id, items: extraItems.filter((_, j) => j !== i) })}>Remove</button>
                      </div>
                    ))}
                    {selected.state === "NEEDS_REVIEW" && (
                      <div className="row">
                        <button type="button" onClick={() => setExtraDraft({ id: selected.id, items: [...extraItems, { label: "", value: "" }] })}>Add information</button>
                        {extraDraft?.id === selected.id && (
                          <button type="button" onClick={async () => {
                            const saved = await send(selected.id, { extra: extraItems.map(({ label, value }) => ({ label, value })) });
                            if (saved) setExtraDraft(null);
                          }}>Save information</button>
                        )}
                      </div>
                    )}
                  </fieldset>

                  {problems.length > 0 && <ul className="issues" aria-label="Problems">{problems.map((p) => <li key={p} className="reason">{p}</li>)}</ul>}
                  {selected.state === "NEEDS_REVIEW" && (
                    <div className="row">
                      {isReviewer ? (
                        <button className="primary" disabled={busy || !selected.approvable || state === "saving"} onClick={approve}>
                          {busy ? "Saving…" : selected.decision?.mode === "update" ? `Approve and update ${selected.decision.order_ref ?? "order"}` : "Approve and save order"}
                        </button>
                      ) : (
                        <span className="meta">A Reviewer approves this order. Your corrections are saved.</span>
                      )}
                      {isReviewer && <button className="danger" onClick={() => setRejecting(true)} disabled={busy}>Reject</button>}
                      {!selected.approvable && <span className="reason">{selected.issues.filter((i) => i.severity === "error").length} item(s) must be checked before approval.</span>}
                    </div>
                  )}
                  {rejecting && (
                    <form className="stack" onSubmit={reject}>
                      <label htmlFor="o-reject">Reason for rejecting</label>
                      <input id="o-reject" className="wide" minLength={5} maxLength={500} required value={reason} onChange={(e) => setReason(e.target.value)} />
                      <div className="row">
                        <button type="submit" className="danger" disabled={reason.trim().length < 5}>Reject order form</button>
                        <button type="button" onClick={() => setRejecting(false)}>Cancel</button>
                      </div>
                    </form>
                  )}
                </div>
              </div>
            </>
          )}
        </>
      )}
    </>
  );
}
