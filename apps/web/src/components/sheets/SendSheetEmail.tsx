"use client";

import { useEffect, useRef, useState } from "react";

import { ApiError, apiGet, apiSend, newIdempotencyKey } from "@/lib/api";
import { REG_FORMATS } from "@/lib/registers";
import { dayLabel, FORMATS, isEmail, type SheetEmail } from "@/lib/sheets";

// "Send Email" for one daily sheet or pick register: the user types the address and picks the file; the server
// builds the file from the saved values, the worker sends it through EmailJS, and this dialog shows EmailJS's answer.

type Props = {
  sheet: { id: string; report_date: string; department: string; version: number };
  kind?: "sheet" | "register";
  onDone?: () => void;
};
const POLL_MS = 1500;

export function SendSheetEmail({ sheet, kind = "sheet", onDone }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const alive = useRef(true);
  const [to, setTo] = useState("");
  const [format, setFormat] = useState<string>("pdf");
  const [invalid, setInvalid] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [status, setStatus] = useState<{ tone: string; text: string } | null>(null);
  const id = `${kind}-mail-${sheet.id}`;
  const base = kind === "register" ? "/registers" : "/sheets";
  const formats: readonly { key: string; label: string }[] = kind === "register" ? REG_FORMATS : FORMATS;

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  async function follow(emailId: string): Promise<SheetEmail | null> {
    for (let i = 0; i < 80 && alive.current; i++) {
      await new Promise((r) => setTimeout(r, POLL_MS));
      const { data } = await apiGet<{ data: { emails: SheetEmail[] } }>(`${base}/${sheet.id}`);
      const e = data.emails.find((x) => x.id === emailId);
      if (e && e.state !== "QUEUED" && e.state !== "SENDING") return e;
    }
    return null;
  }

  async function send(ev: React.FormEvent) {
    ev.preventDefault();
    if (sending) return;
    setStatus(null);
    if (!isEmail(to)) {
      setInvalid("Enter one valid email address, for example owner@company.com.");
      return;
    }
    setInvalid(null);
    setSending(true);
    try {
      const { data } = await apiSend<{ data: SheetEmail }>("POST", `${base}/${sheet.id}/emails`,
        { to_email: to.trim(), format, version: sheet.version }, { idempotencyKey: newIdempotencyKey() });
      setStatus({ tone: "info", text: "Sending…" });
      const done = await follow(data.id);
      if (!done) setStatus({ tone: "info", text: `Still sending. You can close this; the result appears on the ${kind === "register" ? "register" : "sheet"}.` });
      else if (done.state === "ACCEPTED") setStatus({ tone: "success", text: `Email sent successfully to ${done.to_email} with ${done.attachment}.` });
      else if (done.state === "FAILED") setStatus({ tone: "error", text: `Not sent: ${done.error?.message ?? "EmailJS refused it."} You can try again.` });
      else setStatus({ tone: "warning", text: `${done.error?.message ?? "No answer from EmailJS."} Check before sending again.` });
      onDone?.();
    } catch (err) {
      setStatus({ tone: "error", text: err instanceof ApiError ? err.message : "The email could not be queued. Try again." });
    } finally {
      setSending(false);
    }
  }

  return (
    <>
      <button ref={trigger} type="button" onClick={() => { setStatus(null); dialog.current?.showModal(); }}>Send Email</button>
      <dialog ref={dialog} className="confirm" aria-labelledby={`${id}-t`}
        onCancel={(e) => { e.preventDefault(); if (!sending) { dialog.current?.close(); trigger.current?.focus(); } }}>
        <form className="stack" onSubmit={send} noValidate>
          <h2 id={`${id}-t`} style={{ margin: 0 }}>{kind === "register" ? "Send pick register" : "Send daily sheet"}</h2>
          <p style={{ margin: 0 }}>{dayLabel(sheet.report_date)} · {sheet.department}</p>
          <div>
            <label htmlFor={`${id}-to`}>Email address</label>
            <input id={`${id}-to`} type="email" className="wide" value={to} disabled={sending} autoComplete="email"
              aria-invalid={invalid ? true : undefined} aria-describedby={`${id}-help`} onChange={(e) => { setTo(e.target.value); setInvalid(null); }} />
            <p id={`${id}-help`} className={invalid ? "reason" : "meta"} style={{ margin: "4px 0 0" }}>
              {invalid ?? (kind === "register" ? "The picks per shift are written in the email; the full register is attached." : "The key figures are written in the email; the full sheet is attached.")}
            </p>
          </div>
          <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
            <legend style={{ fontWeight: 600 }}>Attach as</legend>
            <div className="row">
              {formats.map((f) => (
                <label key={f.key} style={{ fontWeight: 400 }}>
                  <input type="radio" name={`${id}-fmt`} checked={format === f.key} disabled={sending} onChange={() => setFormat(f.key)} /> {f.label}
                </label>
              ))}
            </div>
            {format !== "pdf" && (
              <p className="meta" style={{ margin: "4px 0 0" }}>{kind === "register" ? "Excel, CSV and SQL" : "Excel and CSV"} need the second attachment slot in the EmailJS template (parameter sheet_file).</p>
            )}
          </fieldset>
          {status && <div className={`banner ${status.tone}`} role={status.tone === "error" || status.tone === "warning" ? "alert" : "status"} style={{ margin: 0 }}>{status.text}</div>}
          <div className="row">
            <button type="button" disabled={sending} onClick={() => { dialog.current?.close(); trigger.current?.focus(); }}>
              {status?.tone === "success" ? "Close" : "Cancel"}
            </button>
            <button type="submit" className="primary" disabled={sending}>{sending ? "Sending…" : "Send Email"}</button>
          </div>
        </form>
      </dialog>
    </>
  );
}
