"use client";

import { useCallback, useEffect, useState } from "react";

import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend } from "@/lib/api";

// Administrators: who receives the owner report, whether it is sent automatically, the company name on the
// PDFs, and the EmailJS account that sends PDFs from the server. The private key is write-only: it is stored
// encrypted and never shown again; leave the box empty to keep the stored key. Values set in the server
// environment (EMAILJS_* in the backend .env) take precedence and are shown read-only.

type View = {
  owner_email: string | null; auto_send: boolean; company_name: string;
  emailjs: {
    service_id: string | null; template_id: string | null; public_key: string | null; private_key_set: boolean; max_request_kb: number;
    sources: Record<"emailjs_service_id" | "emailjs_template_id" | "emailjs_public_key" | "emailjs_private_key", "environment" | "settings" | null>;
  };
  email_ready: boolean; missing: string[]; encryption_ready: boolean; version: number; updated_at: string | null;
};
type Form = {
  owner_email: string; auto_send: boolean; company_name: string; emailjs_service_id: string; emailjs_template_id: string;
  emailjs_public_key: string; emailjs_private_key: string; max_request_kb: string;
};
const MISSING: Record<string, string> = {
  owner_email: "owner email", emailjs_service_id: "EmailJS service ID", emailjs_template_id: "EmailJS template ID",
  emailjs_public_key: "EmailJS public key", emailjs_private_key: "EmailJS private key",
};

function formOf(v: View): Form {
  return {
    owner_email: v.owner_email ?? "", auto_send: v.auto_send, company_name: v.company_name,
    emailjs_service_id: v.emailjs.service_id ?? "", emailjs_template_id: v.emailjs.template_id ?? "",
    emailjs_public_key: v.emailjs.public_key ?? "", emailjs_private_key: "", max_request_kb: String(v.emailjs.max_request_kb),
  };
}

export default function OwnerReportSettings() {
  const { session } = useSession();
  const [view, setView] = useState<View | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [notice, setNotice] = useState<{ tone: string; text: string } | null>(null);
  const [problems, setProblems] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await apiGet<{ data: View }>("/settings/owner-report");
      setView(r.data);
      setForm(formOf(r.data));
    } catch (e) {
      setNotice({ tone: "error", text: e instanceof ApiError ? e.message : "Settings could not be loaded." });
    }
  }, []);

  useEffect(() => {
    const t = setTimeout(() => void load(), 0);
    return () => clearTimeout(t);
  }, [load]);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!form || !view) return;
    setBusy(true);
    setProblems({});
    setNotice(null);
    const body: Record<string, unknown> = {
      owner_email: form.owner_email, auto_send: form.auto_send, company_name: form.company_name,
      max_request_kb: Number(form.max_request_kb) || 50,
    };
    for (const f of ["emailjs_service_id", "emailjs_template_id", "emailjs_public_key"] as const) {
      if (view.emailjs.sources[f] !== "environment") body[f] = form[f]; // environment values are read-only
    }
    if (form.emailjs_private_key.trim() && view.emailjs.sources.emailjs_private_key !== "environment") {
      body.emailjs_private_key = form.emailjs_private_key.trim();
    }
    try {
      const r = await apiSend<{ data: View }>("PUT", "/settings/owner-report", body, { ifMatch: `"${view.version}"` });
      setView(r.data);
      setForm(formOf(r.data));
      setNotice({ tone: "success", text: "Saved." });
    } catch (err) {
      if (err instanceof ApiError) {
        const map: Record<string, string> = {};
        for (const f of err.fields) map[f.field ?? "form"] = f.message;
        setProblems(map);
        setNotice({ tone: "error", text: err.status === 412 ? "Someone else changed these settings. Reloaded." : err.message });
        if (err.status === 412) await load();
      } else setNotice({ tone: "error", text: "Not saved. Check the connection and try again." });
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setBusy(true);
    setNotice(null);
    try {
      const r = await apiSend<{ data: { outcome: string; message: string } }>("POST", "/settings/owner-report/test");
      setNotice({ tone: r.data.outcome === "ACCEPTED" ? "success" : r.data.outcome === "FAILED" ? "error" : "warning", text: r.data.message });
    } catch (err) {
      setNotice({ tone: "error", text: err instanceof ApiError ? err.message : "The test could not be sent." });
    } finally {
      setBusy(false);
    }
  }

  if (!hasRole(session, "ADMIN")) return <p>Only administrators manage owner reports.</p>;
  if (!view || !form) return notice ? <div className="banner error" role="alert">{notice.text}</div> : <div className="skeleton" aria-busy="true" style={{ height: 160 }} />;

  const fromEnv = (name: keyof Form) => (view.emailjs.sources as Record<string, string | null>)[name] === "environment";
  const field = (name: keyof Form, label: string, hint?: string, type = "text") => (
    <div>
      <label htmlFor={`s-${name}`}>{label}</label>
      <input id={`s-${name}`} className="wide" type={type} autoComplete="off" value={form[name] as string}
        readOnly={fromEnv(name)} aria-invalid={problems[name] ? true : undefined} aria-describedby={`s-${name}-help`}
        onChange={(e) => setForm({ ...form, [name]: e.target.value })} />
      <p id={`s-${name}-help`} className={problems[name] ? "reason" : "meta"} style={{ margin: "4px 0 0" }}>
        {problems[name] ?? (fromEnv(name) ? "From the server environment (.env); change it there." : hint ?? "")}
      </p>
    </div>
  );

  return (
    <>
      <h1>Owner report &amp; email</h1>
      <p className="meta">After a batch of diary pages is reviewed, a PDF summary is created from the saved records and emailed to the owner by the server. The same EmailJS account sends single order PDFs.</p>
      {notice && <div className={`banner ${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>{notice.text}</div>}
      <div className={`banner ${view.email_ready && view.owner_email ? "success" : "warning"}`} role="status">
        {view.email_ready && view.owner_email
          ? `Ready: reports go to ${view.owner_email}${view.auto_send ? " automatically" : " when sent by hand"}.`
          : `Not ready yet. Missing: ${view.missing.map((m) => MISSING[m] ?? m).join(", ")}.`}
      </div>
      {!view.encryption_ready && (
        <div className="banner error" role="alert">INTEGRATION_KEYS is not set on the server, so the EmailJS private key cannot be stored safely.</div>
      )}
      <form className="card stack" onSubmit={save} noValidate>
        <h2 style={{ margin: 0 }}>Owner</h2>
        <div className="form-grid">
          {field("owner_email", "Owner email address", "Reports are sent only to this address.", "email")}
          {field("company_name", "Company name", "Printed on reports and order PDFs.")}
        </div>
        <label style={{ fontWeight: 400 }}>
          <input type="checkbox" checked={form.auto_send} onChange={(e) => setForm({ ...form, auto_send: e.target.checked })} />{" "}
          Email the owner automatically when every entry of a batch has been approved or rejected
        </label>
        {problems.auto_send && <p className="reason" style={{ margin: 0 }}>{problems.auto_send}</p>}

        <h2 style={{ margin: "12px 0 0" }}>EmailJS (connected Gmail service)</h2>
        <p className="meta" style={{ margin: 0 }}>
          From the EmailJS dashboard: Email Services (service ID), Email Templates (the one shared document template), Account → General
          (public and private key). In Account → Security, allow API requests for non-browser applications.
        </p>
        <div className="form-grid">
          {field("emailjs_service_id", "Service ID", "For example service_75drj1q.")}
          {field("emailjs_template_id", "Template ID", "The shared template with the PDF as a variable attachment.")}
          {field("emailjs_public_key", "Public key")}
          {view.emailjs.sources.emailjs_private_key === "environment" ? (
            <div>
              <p style={{ margin: 0, fontWeight: 600 }}>Private key</p>
              <p className="meta" style={{ margin: "4px 0 0" }}>Set in the server environment (EMAILJS_PRIVATE_KEY). Never shown or sent to browsers.</p>
            </div>
          ) : (
            <div>
              <label htmlFor="s-private">Private key</label>
              <input id="s-private" className="wide" type="password" autoComplete="new-password" value={form.emailjs_private_key}
                placeholder={view.emailjs.private_key_set ? "Stored (leave empty to keep it)" : ""}
                onChange={(e) => setForm({ ...form, emailjs_private_key: e.target.value })} />
              <p className="meta" style={{ margin: "4px 0 0" }}>Better: put it in the server .env as EMAILJS_PRIVATE_KEY. Stored here it is encrypted and never shown again.</p>
            </div>
          )}
          {field("max_request_kb", "Request size limit (KB)", "50 on the EmailJS free plan; raise only if your plan allows it.")}
        </div>
        <div className="row">
          <button type="submit" className="primary" disabled={busy}>{busy ? "Saving…" : "Save settings"}</button>
          <button type="button" disabled={busy || !view.email_ready || !view.owner_email} onClick={() => void test()}>Send test email to owner</button>
        </div>
      </form>
    </>
  );
}
