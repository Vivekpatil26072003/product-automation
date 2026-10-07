"use client";

import { useEffect, useRef, useState } from "react";

import { ApiError, apiGet, apiSend, newIdempotencyKey } from "@/lib/api";
import { emailPending, isEmail, type Order, type OrderEmail } from "@/lib/orders";

// "Send Email" for one saved order. The user types only the recipient. The server checks the address, renders
// the PDF of the order's latest revision and queues the email; the worker sends it through EmailJS (no need to
// keep this page open) and this dialog shows what EmailJS answered. Nothing about the order is changed.

type Props = {
  order: { id: string; order_ref: string; revision: number; customer_name: string | null; pdf_name: string };
  onDone?: () => void;
};
type Status = { tone: "success" | "error" | "warning" | "info"; text: string } | null;
const POLL_MS = 1500;
const POLL_LIMIT = 80; // two minutes; the worker keeps going after that and the order page shows the result

export function SendOrderEmail({ order, onDone }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const [to, setTo] = useState("");
  const [invalid, setInvalid] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [status, setStatus] = useState<Status>(null);
  const alive = useRef(true);
  const inputId = `send-to-${order.id}`;

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  function open() {
    setStatus(null);
    setInvalid(null);
    dialog.current?.showModal();
  }

  function close() {
    if (sending) return;
    dialog.current?.close();
    trigger.current?.focus();
  }

  async function follow(emailId: string): Promise<OrderEmail | null> {
    for (let i = 0; i < POLL_LIMIT && alive.current; i++) {
      await new Promise((r) => setTimeout(r, POLL_MS));
      const { data } = await apiGet<{ data: Order }>(`/orders/${order.id}`);
      const e = data.emails.find((x) => x.id === emailId);
      if (e && !emailPending(e.state)) return e;
    }
    return null;
  }

  async function send(e: React.FormEvent) {
    e.preventDefault();
    if (sending) return; // one send per click
    setStatus(null);
    if (!isEmail(to)) {
      setInvalid("Enter one valid email address, for example name@company.com.");
      return; // nothing is requested for an invalid address
    }
    setInvalid(null);
    setSending(true);
    try {
      const queued = await apiSend<{ data: OrderEmail }>("POST", `/orders/${order.id}/emails`,
        { to_email: to.trim(), revision: order.revision }, { idempotencyKey: newIdempotencyKey() });
      setStatus({ tone: "info", text: "Sending…" });
      const done = await follow(queued.data.id);
      if (!done) {
        setStatus({ tone: "info", text: "Still sending. You can close this; the result appears on the order." });
      } else if (done.state === "ACCEPTED") {
        setStatus({ tone: "success", text: `Email sent successfully to ${done.to_email} with ${done.attachment.name}.` });
        setTo("");
      } else if (done.state === "FAILED") {
        setStatus({ tone: "error", text: `Not sent: ${done.error?.message ?? "EmailJS refused it."} The order is unchanged; you can try again.` });
      } else {
        setStatus({ tone: "warning", text: `${done.error?.message ?? "No answer from EmailJS."} Check before sending again.` });
      }
      onDone?.();
    } catch (err) {
      setStatus({ tone: "error", text: err instanceof ApiError ? err.message : "The email could not be queued. Check your connection and try again." });
      if (err instanceof ApiError && err.code === "ORDER_CHANGED") onDone?.();
    } finally {
      setSending(false);
    }
  }

  return (
    <>
      <button ref={trigger} type="button" onClick={open}>Send Email</button>
      <dialog ref={dialog} className="confirm" aria-labelledby={`${inputId}-title`} onCancel={(e) => { e.preventDefault(); close(); }}>
        <form className="stack" onSubmit={send} noValidate>
          <h2 id={`${inputId}-title`} style={{ margin: 0 }}>Send Report</h2>
          <p style={{ margin: 0 }}>Customer: <strong>{order.customer_name || "(not recorded)"}</strong> · {order.order_ref}</p>
          <div>
            <label htmlFor={inputId}>Email Address</label>
            <input id={inputId} type="email" className="wide" autoComplete="email" inputMode="email" value={to}
              disabled={sending} aria-invalid={invalid ? true : undefined} aria-describedby={`${inputId}-help`}
              onChange={(e) => { setTo(e.target.value); setInvalid(null); }} />
            <p id={`${inputId}-help`} className={invalid ? "reason" : "meta"} style={{ margin: "4px 0 0" }}>
              {invalid ?? "One address. The order's latest PDF is attached automatically."}
            </p>
          </div>
          <p className="attachment-chip" style={{ margin: 0 }}>Attachment: ✓ {order.pdf_name}</p>
          {status && (
            <div className={`banner ${status.tone}`} role={status.tone === "error" || status.tone === "warning" ? "alert" : "status"} style={{ margin: 0 }}>
              {status.text}
            </div>
          )}
          <div className="row">
            <button type="button" onClick={close} disabled={sending}>{status?.tone === "success" ? "Close" : "Cancel"}</button>
            <button type="submit" className="primary" disabled={sending}>{sending ? "Sending…" : "Send Email"}</button>
          </div>
        </form>
      </dialog>
    </>
  );
}
