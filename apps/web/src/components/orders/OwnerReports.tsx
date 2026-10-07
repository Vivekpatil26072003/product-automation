"use client";

import { useCallback, useEffect, useState } from "react";

import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend } from "@/lib/api";
import { formatAmount } from "@/lib/orders";

// The owner's report for one batch: the consolidated PDF of what was approved, and every email of it to the
// owner (automatic or by hand). A failed or unknown email is shown as such, with retry / check-and-record.

type Delivery = {
  id: string; to_email: string; trigger: string; state: "QUEUED" | "SENDING" | "ACCEPTED" | "FAILED" | "UNKNOWN";
  error: { code: string; message: string | null } | null; reconcile_note: string | null; at: string;
};
type Report = {
  id: string; version: number; trigger: "AUTO" | "MANUAL"; state: "QUEUED" | "GENERATING" | "READY" | "FAILED";
  orders: number; records: number; file_name: string | null;
  summary: { orders?: number; customers?: number; total?: string | null; attention?: number };
  error: { code: string; message: string | null } | null; created_at: string; deliveries: Delivery[];
};

const DELIVERY: Record<Delivery["state"], { label: string; tone: string }> = {
  QUEUED: { label: "Waiting to send", tone: "progress" }, SENDING: { label: "Sending", tone: "progress" },
  ACCEPTED: { label: "Sent to owner", tone: "success" }, FAILED: { label: "Not sent", tone: "error" },
  UNKNOWN: { label: "Result unknown", tone: "warning" },
};

export function OwnerReports({ batchId, canCreate, onChange }: { batchId: string; canCreate: boolean; onChange?: () => void }) {
  const { session } = useSession();
  const [reports, setReports] = useState<Report[] | null>(null);
  const [notice, setNotice] = useState<{ tone: string; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const canSend = hasRole(session, "REVIEWER", "SENDER", "ADMIN");

  const load = useCallback(async () => {
    try {
      const r = await apiGet<{ data: Report[] }>(`/batches/${batchId}/reports`);
      setReports(r.data);
      return r.data;
    } catch (e) {
      setNotice({ tone: "error", text: e instanceof ApiError ? e.message : "Reports could not be loaded." });
      return null;
    }
  }, [batchId]);

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const data = await load();
      const moving = data?.some((r) => r.state === "QUEUED" || r.state === "GENERATING" || r.deliveries.some((d) => d.state === "QUEUED" || d.state === "SENDING"));
      if (!stop && moving) timer = setTimeout(tick, 2000);
    };
    timer = setTimeout(tick, 0);
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [load]);

  async function act(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    setNotice(null);
    try {
      await fn();
      setNotice({ tone: "info", text: ok });
      await load();
      onChange?.();
      // follow the worker for a short while
      for (let i = 0; i < 20; i++) {
        await new Promise((r) => setTimeout(r, 1500));
        const data = await load();
        if (!data?.some((r) => r.state === "QUEUED" || r.state === "GENERATING" || r.deliveries.some((d) => d.state === "QUEUED" || d.state === "SENDING"))) break;
      }
      onChange?.();
    } catch (e) {
      setNotice({ tone: "error", text: e instanceof ApiError ? e.message : "The action failed." });
    } finally {
      setBusy(false);
    }
  }

  const create = (emailOwner: boolean) =>
    act(() => apiSend("POST", `/batches/${batchId}/reports`, { email_owner: emailOwner }),
      emailOwner ? "Creating the report; it is emailed to the owner when ready." : "Creating the report.");
  const deliver = (id: string, resend: boolean) =>
    act(() => apiSend("POST", `/batch-reports/${id}/deliveries`, { resend }), "Emailing the report to the owner.");
  const reconcile = (id: string, outcome: "ACCEPTED" | "FAILED") =>
    act(() => apiSend("POST", `/report-deliveries/${id}/reconcile`, { outcome, note }), "Result recorded.");

  if (!reports) return <div className="skeleton" aria-busy="true" style={{ width: "40%" }} />;
  const latest = reports[0];
  const last = latest?.deliveries[0];

  return (
    <section className="card stack" aria-labelledby="owner-report">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 id="owner-report" style={{ margin: 0 }}>Owner report</h2>
        {canCreate && canSend && (
          <div className="row">
            <button disabled={busy} onClick={() => void create(false)}>{latest ? "Create new report" : "Create report"}</button>
            <button className="primary" disabled={busy} onClick={() => void create(true)}>Create and email to owner</button>
          </div>
        )}
      </div>
      {notice && <div className={`banner ${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>{notice.text}</div>}
      {!latest ? (
        <p className="meta" style={{ margin: 0 }}>No report yet. When automatic owner email is on, it is created and sent as soon as every entry of this batch is approved or rejected.</p>
      ) : (
        <>
          <p style={{ margin: 0 }}>
            <strong>{latest.file_name ?? `Report version ${latest.version}`}</strong>{" "}
            <span className={`badge tone-${latest.state === "READY" ? "success" : latest.state === "FAILED" ? "error" : "progress"}`}>
              {{ QUEUED: "Waiting", GENERATING: "Creating PDF", READY: "PDF ready", FAILED: "PDF failed" }[latest.state]}
            </span>
            <span className="meta"> · {latest.trigger === "AUTO" ? "automatic" : "by hand"} · {latest.orders} order(s){latest.records ? `, ${latest.records} production entr${latest.records === 1 ? "y" : "ies"}` : ""}
              {latest.summary.total ? ` · total ${formatAmount(latest.summary.total)}` : ""}
              {latest.summary.attention ? ` · ${latest.summary.attention} item(s) need attention` : ""}</span>
          </p>
          {latest.error?.message && <p className="reason" style={{ margin: 0 }}>{latest.error.message}</p>}
          {latest.state === "READY" && (
            <div className="row">
              <a className="button" href={`/api/v1/batch-reports/${latest.id}/pdf`} target="_blank" rel="noopener">View PDF</a>
              <a className="button" href={`/api/v1/batch-reports/${latest.id}/pdf?download=true`}>Download PDF</a>
              {canSend && !last && <button disabled={busy} onClick={() => void deliver(latest.id, false)}>Email to owner</button>}
              {canSend && last?.state === "FAILED" && <button className="primary" disabled={busy} onClick={() => void deliver(latest.id, false)}>Retry email</button>}
              {canSend && last?.state === "ACCEPTED" && <button disabled={busy} onClick={() => void deliver(latest.id, true)}>Send again</button>}
            </div>
          )}
          {latest.deliveries.length > 0 && (
            <ul className="filelist" aria-label="Emails of this report">
              {latest.deliveries.map((d) => (
                <li key={d.id}>
                  <div>
                    <div style={{ overflowWrap: "anywhere" }}>To {d.to_email} · {d.trigger === "AUTO" ? "automatic" : d.trigger === "RESEND" ? "sent again" : "by hand"}</div>
                    <div className="meta">
                      {new Date(d.at).toLocaleString("en-IN", { timeZone: session.timezone })}
                      {d.error?.message ? ` · ${d.error.message}` : ""}{d.reconcile_note ? ` · checked: ${d.reconcile_note}` : ""}
                    </div>
                  </div>
                  <span className={`badge tone-${DELIVERY[d.state].tone}`}>{DELIVERY[d.state].label}</span>
                </li>
              ))}
            </ul>
          )}
          {canSend && last?.state === "UNKNOWN" && (
            <div className="stack">
              <p className="meta" style={{ margin: 0 }}>Check EmailJS → Email History (and the Gmail Sent folder), then record what happened. It is not resent automatically.</p>
              <label htmlFor="rec-note">What you found</label>
              <input id="rec-note" className="wide" minLength={5} maxLength={300} value={note} onChange={(e) => setNote(e.target.value)} />
              <div className="row">
                <button disabled={busy || note.trim().length < 5} onClick={() => void reconcile(last.id, "ACCEPTED")}>It was sent</button>
                <button disabled={busy || note.trim().length < 5} onClick={() => void reconcile(last.id, "FAILED")}>It was not sent</button>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
