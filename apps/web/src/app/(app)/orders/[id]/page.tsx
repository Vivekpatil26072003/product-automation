"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";

import { SendOrderEmail } from "@/components/orders/SendOrderEmail";
import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend } from "@/lib/api";
import {
  EMAIL_STATE, formatAmount, formatCount, formatDay, type Order, ORDER_DATES, ORDER_FIELDS, ORDER_LABEL,
  ORDER_NUMBERS, type OrderField, pdfUrl,
} from "@/lib/orders";

// One saved order: its latest values, corrections (a new revision with a reason), PDF, emails and history.

function shown(field: OrderField, v: string | null): string {
  if (v === null || v === "") return "";
  if (ORDER_DATES.has(field)) return formatDay(v);
  if (field === "quantity") return formatCount(v);
  if (ORDER_NUMBERS.has(field)) return formatAmount(v);
  return v;
}

export default function OrderPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { session } = useSession();
  const [order, setOrder] = useState<Order | null>(null);
  const [error, setError] = useState<{ status: number; text: string } | null>(null);
  const [editing, setEditing] = useState<Partial<Record<OrderField, string>> | null>(null);
  const [reason, setReason] = useState("");
  const [extraText, setExtraText] = useState("");
  const [checkNote, setCheckNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [problems, setProblems] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const canEdit = hasRole(session, "REVIEWER");
  const canSend = hasRole(session, "REVIEWER", "SENDER");

  const load = useCallback(async () => {
    try {
      const r = await apiGet<{ data: Order }>(`/orders/${id}`);
      setOrder(r.data);
      setError(null);
    } catch (e) {
      setError({ status: e instanceof ApiError ? e.status : 0, text: e instanceof ApiError ? e.message : "The order could not be loaded." });
    }
  }, [id]);

  useEffect(() => {
    const t = setTimeout(() => void load(), 0);
    return () => clearTimeout(t);
  }, [load]);

  function startEdit() {
    if (!order) return;
    setEditing(Object.fromEntries(ORDER_FIELDS.map((f) => [f, order.values[f] ?? ""])));
    setExtraText(order.extra.map((x) => `${x.label}: ${x.value}`).join("\n"));
    setReason("");
    setProblems({});
    setNotice(null);
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!order || !editing) return;
    const changes: Record<string, string | null> = {};
    for (const f of ORDER_FIELDS) {
      const next = (editing[f] ?? "").trim();
      if (next !== (order.values[f] ?? "")) changes[f] = next || null;
    }
    const extra = extraText.split("\n").map((line) => line.split(/:(.*)/s)).filter(([l, v]) => l?.trim() && v?.trim())
      .map(([l, v]) => ({ label: (l ?? "").trim(), value: (v ?? "").trim() }));
    const extraChanged = JSON.stringify(extra) !== JSON.stringify(order.extra.map(({ label, value }) => ({ label, value })));
    if (Object.keys(changes).length === 0 && !extraChanged) {
      setProblems({ form: "Nothing was changed." });
      return;
    }
    setSaving(true);
    try {
      const body: Record<string, unknown> = { fields: changes, reason };
      if (extraChanged) body.extra = extra;
      const r = await apiSend<{ data: Order }>("POST", `/orders/${order.id}/revisions`, body,
        { ifMatch: `"${order.revision}"` });
      setOrder(r.data);
      setEditing(null);
      setNotice(`Saved as revision ${r.data.revision}. The PDF and emails now use these values.`);
    } catch (err) {
      if (err instanceof ApiError) {
        const map: Record<string, string> = {};
        for (const f of err.fields) map[(f.field ?? "form").replace(/^fields\./, "")] = f.message;
        if (!err.fields.length) map.form = err.status === 412 ? "Someone else changed this order. Reload to see the latest values." : err.message;
        setProblems(map);
      } else setProblems({ form: "Not saved. Check your connection and try again." });
    } finally {
      setSaving(false);
    }
  }

  async function reconcile(emailId: string, outcome: "ACCEPTED" | "FAILED") {
    if (!order) return;
    try {
      await apiSend("POST", `/orders/${order.id}/emails/${emailId}/reconcile`, { outcome, note: checkNote });
      setCheckNote("");
      await load();
    } catch (err) {
      setProblems({ form: err instanceof ApiError ? err.message : "Could not record the result." });
    }
  }

  if (error?.status === 404) return <p>This order does not exist or you do not have access to it. <Link href="/orders">All orders</Link></p>;
  if (!order) return error ? <div className="banner error" role="alert">{error.text}</div> : <div className="skeleton" aria-busy="true" style={{ height: 160 }} />;

  return (
    <>
      <p><Link href="/orders">All customer orders</Link></p>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>Order {order.order_ref}</h1>
        <div className="row">
          <a className="button" href={pdfUrl(order.id)} target="_blank" rel="noopener">View PDF</a>
          <a className="button" href={pdfUrl(order.id, undefined, true)}>Download PDF</a>
          {canSend && (
            <SendOrderEmail
              order={{ id: order.id, order_ref: order.order_ref, revision: order.revision, customer_name: order.values.customer_name, pdf_name: order.pdf_name }}
              onDone={() => void load()}
            />
          )}
        </div>
      </div>
      <p className="meta">
        {order.customer && <><Link href={`/orders?customer=${order.customer.id}`}>All orders of {order.customer.name}</Link> · </>}
        {order.department} · revision {order.revision} · PDF {order.pdf_name}
      </p>
      {notice && <div className="banner success" role="status">{notice}</div>}
      {order.attention.length > 0 && (
        <div className="banner warning" role="status">
          Noted when saved: {order.attention.map((a) => a.message).join(" ")}
        </div>
      )}

      {!editing ? (
        <section className="card" aria-labelledby="o-values">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <h2 id="o-values" style={{ margin: 0 }}>Saved values</h2>
            {canEdit && <button onClick={startEdit}>Correct values</button>}
          </div>
          <dl className="kv">
            {ORDER_FIELDS.map((f) => (
              <div key={f}><dt className="meta">{ORDER_LABEL[f]}</dt><dd style={{ overflowWrap: "anywhere" }}>{shown(f, order.values[f]) || <span className="meta">Not recorded</span>}</dd></div>
            ))}
          </dl>
          {order.extra.length > 0 && (
            <>
              <h3 style={{ margin: "8px 0 4px" }}>Other information from the page</h3>
              <dl className="kv">
                {order.extra.map((x, i) => <div key={i}><dt className="meta">{x.label}</dt><dd style={{ overflowWrap: "anywhere" }}>{x.value}</dd></div>)}
              </dl>
            </>
          )}
        </section>
      ) : (
        <form className="card stack" onSubmit={save} aria-labelledby="o-edit" noValidate>
          <h2 id="o-edit" style={{ margin: 0 }}>Correct values</h2>
          <p className="meta" style={{ margin: 0 }}>Saving creates revision {order.revision + 1}. The current values stay in the history.</p>
          {problems.form && <div className="banner error" role="alert">{problems.form}</div>}
          <div className="form-grid">
            {ORDER_FIELDS.map((f) => (
              <div key={f}>
                <label htmlFor={`e-${f}`}>{ORDER_LABEL[f]}</label>
                {f === "remarks" ? (
                  <textarea id={`e-${f}`} className="wide" rows={3} value={editing[f] ?? ""} aria-invalid={problems[f] ? true : undefined}
                    onChange={(e) => setEditing({ ...editing, [f]: e.target.value })} />
                ) : (
                  <input id={`e-${f}`} className="wide" type={ORDER_DATES.has(f) ? "date" : f === "customer_email" ? "email" : "text"}
                    inputMode={ORDER_NUMBERS.has(f) ? "decimal" : undefined} value={editing[f] ?? ""} aria-invalid={problems[f] ? true : undefined}
                    onChange={(e) => setEditing({ ...editing, [f]: e.target.value })} />
                )}
                {problems[f] && <p className="reason" style={{ margin: "4px 0 0" }}>{problems[f]}</p>}
              </div>
            ))}
          </div>
          <div>
            <label htmlFor="e-extra">Other information (one per line, Label: value)</label>
            <textarea id="e-extra" className="wide" rows={3} value={extraText} onChange={(e) => setExtraText(e.target.value)} />
          </div>
          <div>
            <label htmlFor="e-reason">Reason for the correction</label>
            <input id="e-reason" className="wide" type="text" minLength={5} maxLength={500} required value={reason}
              aria-invalid={problems.reason ? true : undefined} onChange={(e) => setReason(e.target.value)} />
            {problems.reason && <p className="reason" style={{ margin: "4px 0 0" }}>{problems.reason}</p>}
          </div>
          <div className="row">
            <button type="submit" className="primary" disabled={saving || reason.trim().length < 5}>{saving ? "Saving…" : "Save correction"}</button>
            <button type="button" onClick={() => setEditing(null)} disabled={saving}>Cancel</button>
          </div>
        </form>
      )}

      <section className="card" aria-labelledby="o-emails">
        <h2 id="o-emails">Emails</h2>
        {order.emails.length === 0 ? <p className="meta">Not emailed yet.</p> : (
          <ul className="filelist">
            {order.emails.map((e) => (
              <li key={e.id}>
                <div>
                  <div style={{ overflowWrap: "anywhere" }}>{e.to_email} · {e.attachment.name}</div>
                  <div className="meta">
                    {new Date(e.at).toLocaleString("en-IN", { timeZone: session.timezone })}{e.by ? ` · ${e.by}` : ""}
                    {e.error?.message ? ` · ${e.error.message}` : ""}
                  </div>
                </div>
                <div className="row">
                  <span className={`badge tone-${EMAIL_STATE[e.state].tone}`}>{EMAIL_STATE[e.state].label}</span>
                  {canSend && e.state === "UNKNOWN" && (
                    <>
                      <label className="sr-only" htmlFor={`chk-${e.id}`}>What you found in EmailJS history</label>
                      <input id={`chk-${e.id}`} placeholder="What you found in EmailJS history" value={checkNote} onChange={(ev) => setCheckNote(ev.target.value)} />
                      <button disabled={checkNote.trim().length < 5} onClick={() => void reconcile(e.id, "ACCEPTED")}>It was sent</button>
                      <button disabled={checkNote.trim().length < 5} onClick={() => void reconcile(e.id, "FAILED")}>Not sent</button>
                    </>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="card" aria-labelledby="o-history">
        <h2 id="o-history">History</h2>
        <ol>
          {order.revisions.map((r) => (
            <li key={r.number}>
              Revision {r.number} · {new Date(r.at).toLocaleString("en-IN", { timeZone: session.timezone })}{r.by ? ` · ${r.by}` : ""}
              {r.number === 1 ? " · approved from the order form" : ` · ${r.reason}${r.changed.length ? ` (changed: ${r.changed.map((f) => ORDER_LABEL[f].toLowerCase()).join(", ")})` : ""}`}
              {" · "}<a href={pdfUrl(order.id, r.number)} target="_blank" rel="noopener">PDF r{r.number}</a>
            </li>
          ))}
        </ol>
        <p className="meta">Source: <Link href={`/batches/${order.source.batch_id}`}>uploaded file</Link></p>
      </section>
    </>
  );
}
