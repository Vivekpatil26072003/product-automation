"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, use, useCallback, useEffect, useRef, useState } from "react";

import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, type Issue, apiGet, apiSend, newIdempotencyKey } from "@/lib/api";
import { emailJsConfig, missingEmailJsConfig, sendWithEmailJs } from "@/lib/emailjs";
import { type Draft, formatBytes, parseRecipients, recipientsText } from "@/lib/reports";

// U7 Email: plain-text draft with To/Cc/Bcc, autosaved. "Preview and send" shows exactly what will be sent;
// "Confirm send" submits that preview's version and content hash. Any later edit makes that confirmation stale.

const AUTOSAVE_MS = 800;
type Form = { to: string; cc: string; bcc: string; subject: string; body: string };

function formOf(d: Draft): Form {
  return { to: recipientsText(d.to), cc: recipientsText(d.cc), bcc: recipientsText(d.bcc), subject: d.subject, body: d.body };
}

export default function EmailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return (
    <Suspense>
      <Composer reportId={id} />
    </Suspense>
  );
}

function Composer({ reportId }: { reportId: string }) {
  const { session } = useSession();
  const router = useRouter();
  const draftId = useSearchParams().get("draft");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [issues, setIssues] = useState<Issue[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<Draft | null>(null);
  const [sending, setSending] = useState(false);
  const [progress, setProgress] = useState("");
  const sendKey = useRef(newIdempotencyKey());
  const dialog = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    if (!draftId) return;
    let active = true;
    apiGet<{ data: Draft }>(`/email-drafts/${draftId}`).then(
      (r) => active && (setDraft(r.data), setForm(formOf(r.data))),
      (e) => active && setMessage(e instanceof ApiError ? e.message : "The draft could not be loaded."),
    );
    return () => {
      active = false;
    };
  }, [draftId]);

  const save = useCallback(async (): Promise<Draft | null> => {
    if (!draft || !form) return null;
    setSaving(true);
    try {
      const r = await apiSend<{ data: Draft }>("PATCH", `/email-drafts/${draft.id}`, {
        to: parseRecipients(form.to), cc: parseRecipients(form.cc), bcc: parseRecipients(form.bcc),
        subject: form.subject, body: form.body,
      }, { ifMatch: `"${draft.version}"` });
      setDraft(r.data);
      setDirty(false);
      setIssues([]);
      setMessage(null);
      return r.data;
    } catch (e) {
      setIssues(e instanceof ApiError ? e.fields : []);
      setMessage(e instanceof ApiError ? (e.status === 412 ? "Someone else changed this draft. Reload to see it." : e.message) : "Not saved.");
      return null;
    } finally {
      setSaving(false);
    }
  }, [draft, form]);

  useEffect(() => {
    if (!dirty) return;
    const t = setTimeout(() => void save(), AUTOSAVE_MS);
    return () => clearTimeout(t);
  }, [dirty, save]);

  function edit(field: keyof Form, value: string) {
    setForm((f) => (f ? { ...f, [field]: value } : f));
    setDirty(true);
  }

  async function preview() {
    const current = dirty ? await save() : draft;
    if (!current) return; // a failed save blocks confirmation
    sendKey.current = newIdempotencyKey();
    setConfirming(current);
    dialog.current?.showModal();
  }

  function cancel() {
    dialog.current?.close();
    setConfirming(null);
  }

  async function confirmSend() {
    if (!confirming) return;
    const viaEmailJs = confirming.channel === "emailjs";
    const config = viaEmailJs ? emailJsConfig() : null;
    if (viaEmailJs && !config) {
      setMessage(`EmailJS is not configured in the web app: set ${missingEmailJsConfig().join(", ")} in apps/web/.env.local and restart it.`);
      return;
    }
    setSending(true);
    try {
      // 1. The API records the confirmed intent (one per draft; outdated reports and stale previews are refused).
      setProgress("Recording the confirmed email…");
      const r = await apiSend<{ data: { email_id: string; channel: string } }>("POST", `/email-drafts/${confirming.id}/send`,
        { version: confirming.version, confirmed_hash: confirming.content_hash, confirmation: true },
        { ifMatch: `"${confirming.version}"`, idempotencyKey: sendKey.current });
      const emailId = r.data.email_id;
      if (r.data.channel === "emailjs" && config) {
        // 2. Claim it once: the API re-checks the report and returns the variables from the latest saved data.
        setProgress("Checking the latest saved report…");
        const claim = await apiSend<{ data: { template_params: Record<string, string> } }>(
          "POST", `/emails/${emailId}/client-send/claim`);
        // 3. Send through EmailJS (your Gmail service), then 4. record exactly what EmailJS answered.
        setProgress("Sending through EmailJS…");
        const result = await sendWithEmailJs(config, claim.data.template_params);
        setProgress("Recording the result…");
        await apiSend("POST", `/emails/${emailId}/client-send/result`,
          { outcome: result.outcome, provider_status: result.status, provider_text: result.text.slice(0, 300) });
        dialog.current?.close();
        const notice = result.outcome === "ACCEPTED" ? "sent" : result.outcome === "FAILED" ? "failed" : "unknown";
        router.push(`/emails/${emailId}?emailjs=${notice}`);
        return;
      }
      dialog.current?.close();
      router.push(`/emails/${emailId}`);
    } catch (e) {
      dialog.current?.close();
      setConfirming(null);
      setMessage(e instanceof ApiError ? e.message : "The email was not sent.");
    } finally {
      setSending(false);
      setProgress("");
    }
  }

  if (!hasRole(session, "SENDER")) return <p>Only Senders can prepare and send email.</p>;
  if (!draftId) return <p>No draft selected. <Link href={`/reports/${reportId}`}>Back to the report</Link></p>;
  if (!draft || !form) return message ? <div className="banner error" role="alert">{message}</div> : <div className="skeleton" aria-busy="true" style={{ height: 160 }} />;

  const locked = draft.state !== "DRAFT";
  const fieldError = (name: string) => issues.filter((i) => i.field === name || i.field?.startsWith(`${name}[`)).map((i) => i.message).join(" ");
  const input = (name: "to" | "cc" | "bcc", label: string, hint?: string) => (
    <div>
      <label htmlFor={`m-${name}`}>{label}</label>
      <textarea id={`m-${name}`} className="wide" rows={2} value={form[name]} disabled={locked}
        aria-invalid={fieldError(name) ? true : undefined} aria-describedby={`m-${name}-help`}
        onChange={(e) => edit(name, e.target.value)} />
      <p id={`m-${name}-help`} className={fieldError(name) ? "reason" : "meta"} style={{ margin: "4px 0 0" }}>
        {fieldError(name) || hint || "Separate addresses with commas or new lines."}
      </p>
    </div>
  );
  const all = confirming ? [...confirming.to, ...confirming.cc, ...confirming.bcc] : [];

  return (
    <>
      <p><Link href={`/reports/${reportId}`}>Back to the report</Link></p>
      <h1>Email report</h1>
      <p className="meta">
        {draft.report.title} · {draft.report.code} version {draft.report.version}
        {draft.resend_of_email_id && " · resend (new, separate email)"}
        {draft.correction_of_email_id && " · correction of an earlier email"}
      </p>
      {locked && (
        <div className="banner info" role="status">
          This draft was confirmed and can no longer change. {draft.email_id && <Link href={`/emails/${draft.email_id}`}>See its status</Link>}
        </div>
      )}
      {message && <div className="banner error" role="alert">{message}</div>}
      <div className="card stack">
        {input("to", "To")}
        {input("cc", "Cc")}
        {input("bcc", "Bcc", "Hidden from other recipients; still listed on the confirmation.")}
        <div>
          <label htmlFor="m-subject">Subject</label>
          <input id="m-subject" type="text" className="wide" maxLength={200} value={form.subject} disabled={locked}
            aria-invalid={fieldError("subject") ? true : undefined} onChange={(e) => edit("subject", e.target.value)} />
          {fieldError("subject") && <p className="reason" style={{ margin: "4px 0 0" }}>{fieldError("subject")}</p>}
        </div>
        <div>
          <label htmlFor="m-body">Message (plain text)</label>
          <textarea id="m-body" className="wide prose-input" rows={12} value={form.body} disabled={locked}
            aria-invalid={fieldError("body") ? true : undefined} onChange={(e) => edit("body", e.target.value)} />
          {fieldError("body") && <p className="reason" style={{ margin: "4px 0 0" }}>{fieldError("body")}</p>}
          <p className="meta" style={{ margin: "4px 0 0" }}>Figures must match the report; changed figures block sending.</p>
        </div>
        {draft.channel === "emailjs" ? (
          <p className="attachment-chip">Sent with EmailJS: the report figures are in the message; the PDF is not attached.</p>
        ) : (
          <p className="attachment-chip">Attachment: {draft.attachment.name} · {formatBytes(draft.attachment.bytes)} (fixed)</p>
        )}
        <p className="meta" aria-live="polite">{saving ? "Saving…" : dirty ? "Unsaved changes" : `Saved · version ${draft.version}`}</p>
        {draft.blocking.filter((b) => b.code !== "DRAFT_LOCKED").length > 0 && !locked && (
          <ul className="issues" aria-label="Before you can send">
            {draft.blocking.map((b) => <li key={b.code} className="reason">{b.message}</li>)}
          </ul>
        )}
        {!locked && (
          <div className="row">
            <button className="primary" disabled={saving} onClick={preview}>Preview and send</button>
            {dirty && <button onClick={() => void save()} disabled={saving}>Save draft</button>}
          </div>
        )}
      </div>

      <dialog ref={dialog} aria-labelledby="c-title" className="confirm" onCancel={cancel}>
        {confirming && (
          <div className="stack">
            <h2 id="c-title" style={{ margin: 0 }}>Confirm send</h2>
            <p style={{ margin: 0 }}>From {confirming.channel === "emailjs" ? "EmailJS (your connected Gmail service)" : confirming.sender_mailbox ?? "(not connected)"}</p>
            <div>
              <strong>{all.length} recipient{all.length === 1 ? "" : "s"}</strong>
              <ul className="filelist">
                {all.map((r) => <li key={r.address}><span>{r.kind === "TO" ? "To" : r.kind === "CC" ? "Cc" : "Bcc"}</span> <span>{r.address}</span></li>)}
              </ul>
            </div>
            {confirming.external_domains.length > 0 && (
              <div className="banner warning" role="alert" style={{ margin: 0 }}>
                Outside your company: {confirming.external_domains.join(", ")}. Check that they may receive this report.
              </div>
            )}
            <p style={{ margin: 0 }}>Subject: {confirming.subject}</p>
            <p style={{ margin: 0 }}>
              Report {confirming.report.code} version {confirming.report.version}
              {confirming.channel === "emailjs" ? " · figures in the message (no attachment)" : ` · ${confirming.attachment.name} · ${formatBytes(confirming.attachment.bytes)}`}
            </p>
            {!confirming.sendable && (
              <ul className="issues">{confirming.blocking.map((b) => <li key={b.code} className="reason">{b.message}</li>)}</ul>
            )}
            <div className="row">
              <button className="primary" disabled={sending || !confirming.sendable} onClick={confirmSend}>{sending ? "Sending…" : "Confirm send"}</button>
              <button onClick={cancel} disabled={sending}>Cancel</button>
            </div>
            {progress && <p className="meta" role="status" style={{ margin: 0 }}>{progress}</p>}
          </div>
        )}
      </dialog>
    </>
  );
}
