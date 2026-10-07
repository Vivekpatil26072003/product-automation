"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, use, useCallback, useEffect, useState } from "react";

import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend } from "@/lib/api";
import { type Email, emailStatus, formatBytes } from "@/lib/reports";

// Email status (FR20, U7/U8): provider acceptance is shown as such, never as delivery. An unknown outcome
// blocks any resend until a Sender reconciles it with evidence (for example the Sent Items entry).

const POLL_MS = 2000;
const RECIPIENT_STATE: Record<string, string> = {
  PENDING: "Waiting", ACCEPTED: "Accepted by provider (delivery not confirmed)", UNKNOWN: "Unknown",
  FAILED: "Not sent", DELIVERED: "Delivered (confirmed)", BOUNCED: "Bounced",
};

export default function EmailStatusPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return (
    <Suspense>
      <EmailStatus id={id} />
    </Suspense>
  );
}

function EmailStatus({ id }: { id: string }) {
  const { session } = useSession();
  const router = useRouter();
  const [email, setEmail] = useState<Email | null>(null);
  const [error, setError] = useState<{ status: number; text: string } | null>(null);
  const [notice, setNotice] = useState("");
  const [outcome, setOutcome] = useState<"ACCEPTED" | "NOT_ACCEPTED">("ACCEPTED");
  const [evidence, setEvidence] = useState("");
  const [reason, setReason] = useState("");
  const [resendReason, setResendReason] = useState("");
  const sentVia = useSearchParams().get("emailjs"); // set by the composer after an EmailJS send: sent | failed | unknown

  const load = useCallback(async () => {
    try {
      const { data } = await apiGet<{ data: Email }>(`/emails/${id}`);
      setEmail(data);
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
      if (cancelled || (data && data.state !== "QUEUED" && data.state !== "SENDING")) return;
      timer = setTimeout(tick, POLL_MS);
    };
    timer = setTimeout(tick, 0);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [load]);

  async function reconcile(e: React.FormEvent) {
    e.preventDefault();
    try {
      const r = await apiSend<{ data: Email }>("POST", `/emails/${id}/reconcile`, { outcome, evidence_ref: evidence, reason });
      setEmail(r.data);
      setNotice("Outcome recorded.");
    } catch (err) {
      setNotice(err instanceof ApiError ? err.message : "Could not record the outcome.");
    }
  }

  async function resend(e: React.FormEvent) {
    e.preventDefault();
    try {
      const r = await apiSend<{ data: { id: string; report_id: string } }>("POST", `/emails/${id}/resend-draft`, { reason: resendReason });
      router.push(`/reports/${r.data.report_id}/email?draft=${r.data.id}`);
    } catch (err) {
      setNotice(err instanceof ApiError ? err.message : "Could not prepare a resend.");
    }
  }

  if (!hasRole(session, "SENDER")) return <p>Only Senders can see email status.</p>;
  if (error?.status === 404) return <p>This email does not exist or you do not have access to it.</p>;
  if (!email) return error ? <div className="banner error" role="alert">{error.text}</div> : <div className="skeleton" aria-busy="true" style={{ height: 120 }} />;

  const st = emailStatus(email.state);
  const viaEmailJs = email.channel === "emailjs";
  const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString("en-IN", { timeZone: session.timezone }) : "");
  return (
    <>
      <p><Link href={`/reports/${email.report_id}`}>Report {email.report_code} version {email.report_version}</Link></p>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>{email.subject}</h1>
        <span className={`badge tone-${st.tone}`} role="status">{st.label}</span>
      </div>
      {viaEmailJs && sentVia === "sent" && email.state === "ACCEPTED" && (
        <div className="banner success" role="status">Email sent. EmailJS accepted it for delivery through your Gmail service.</div>
      )}
      {viaEmailJs && sentVia === "failed" && email.state === "FAILED" && (
        <div className="banner error" role="alert">
          EmailJS did not send this email{email.error ? `: ${email.error.message}` : "."} Fix the cause (for example the template ID, public key or a recipient address), then use &quot;Send again&quot;.
        </div>
      )}
      {viaEmailJs && sentVia === "unknown" && email.state === "UNKNOWN" && (
        <div className="banner warning" role="alert">
          No answer from EmailJS, so it is not known whether the email went out. Check EmailJS → Email History, then record the outcome below. It is not resent automatically.
        </div>
      )}
      <p>{email.state_text}</p>
      {email.error && <p className="reason">{email.error.message}</p>}
      <p aria-live="polite" className="meta">{notice}</p>
      <dl className="kv card">
        <div><dt className="meta">From</dt><dd>{viaEmailJs ? "EmailJS (connected Gmail service)" : email.sender_mailbox}</dd></div>
        <div><dt className="meta">Confirmed</dt><dd>{when(email.created_at)}</dd></div>
        {email.accepted_at && <div><dt className="meta">Accepted by provider</dt><dd>{when(email.accepted_at)}</dd></div>}
        <div><dt className="meta">Attachment</dt><dd>{viaEmailJs || !email.attachment.name ? "None: report figures are in the message" : `${email.attachment.name} · ${formatBytes(email.attachment.bytes)}`}</dd></div>
        {email.provider_id && <div><dt className="meta">Provider reference</dt><dd style={{ overflowWrap: "anywhere" }}>{email.provider_id}</dd></div>}
      </dl>

      <section className="card table-scroll" tabIndex={0} aria-labelledby="e-rcpt">
        <h2 id="e-rcpt">Recipients</h2>
        <table className="data">
          <thead><tr><th scope="col">Recipient</th><th scope="col">Type</th><th scope="col">State</th><th scope="col">Observed</th></tr></thead>
          <tbody>
            {email.recipients.map((r) => (
              <tr key={r.address}>
                <td style={{ overflowWrap: "anywhere" }}>{r.address}</td><td>{r.kind}</td><td>{RECIPIENT_STATE[r.state] ?? r.state}</td>
                <td className="meta">{r.observation_source ? `${r.observation_source.replace("_", " ")} · ${when(r.observed_at)}` : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card" aria-labelledby="e-att">
        <h2 id="e-att">Attempts</h2>
        <ol>
          {email.attempts.map((a) => (
            <li key={a.attempt_no}>
              {when(a.started_at)} · {a.outcome.replace("_", " ").toLowerCase()}
              {a.http_status ? ` (HTTP ${a.http_status})` : ""}{a.error_message ? ` · ${a.error_message}` : ""}
            </li>
          ))}
        </ol>
        {email.reconciliation && (
          <p className="meta">Reconciled {when(email.reconciliation.at)}: {email.reconciliation.outcome === "ACCEPTED" ? "accepted" : "not accepted"} · evidence {email.reconciliation.evidence_ref} · {email.reconciliation.reason}</p>
        )}
      </section>

      {email.state === "UNKNOWN" && (
        <form className="card stack" onSubmit={reconcile} aria-labelledby="e-rec">
          <h2 id="e-rec" style={{ margin: 0 }}>Reconcile the outcome</h2>
          <p className="meta" style={{ margin: 0 }}>
            {viaEmailJs
              ? "Check EmailJS → Email History (and the Gmail Sent folder) first. Nothing is resent automatically."
              : "Check the sender's Sent Items or the provider's message trace first. Nothing is resent automatically."}
          </p>
          <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
            <legend style={{ fontWeight: 600 }}>What did you find?</legend>
            <label style={{ fontWeight: 400 }}><input type="radio" name="outcome" checked={outcome === "ACCEPTED"} onChange={() => setOutcome("ACCEPTED")} /> The provider accepted it</label>
            <label style={{ fontWeight: 400 }}><input type="radio" name="outcome" checked={outcome === "NOT_ACCEPTED"} onChange={() => setOutcome("NOT_ACCEPTED")} /> It was not accepted</label>
          </fieldset>
          <div><label htmlFor="e-ev">Evidence reference</label><input id="e-ev" type="text" className="wide" value={evidence} minLength={3} maxLength={300} required onChange={(e) => setEvidence(e.target.value)} /></div>
          <div><label htmlFor="e-rs">Reason</label><input id="e-rs" type="text" className="wide" value={reason} minLength={3} maxLength={500} required onChange={(e) => setReason(e.target.value)} /></div>
          <div className="row"><button type="submit" className="primary">Record outcome</button></div>
        </form>
      )}

      {(email.state === "ACCEPTED" || email.state === "FAILED") && (
        <form className="card stack" onSubmit={resend} aria-labelledby="e-rsd">
          <h2 id="e-rsd" style={{ margin: 0 }}>Send again</h2>
          <p className="meta" style={{ margin: 0 }}>Creates a new, separate draft with the same recipients. The original stays as recorded.</p>
          <div><label htmlFor="e-why">Reason</label><input id="e-why" type="text" className="wide" value={resendReason} minLength={3} maxLength={500} required onChange={(e) => setResendReason(e.target.value)} /></div>
          <div className="row"><button type="submit">Prepare new draft</button></div>
        </form>
      )}
    </>
  );
}
