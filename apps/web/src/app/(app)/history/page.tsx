"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet } from "@/lib/api";
import { emailStatus, reportStatus } from "@/lib/reports";

// U8 History: one tab per kind of work, each limited by role and scope on the server. Failed attempts stay
// visible; a success badge names the stage that succeeded.

type Row = {
  kind: string; object_id: string; state: string; at: string; summary: string; detail_url: string;
  outdated?: boolean; state_text?: string; attempts?: number; max_attempts?: number;
  last_error?: { code: string; message: string } | null; next_retry_at?: string | null;
};
const TABS = [
  { kind: "uploads", label: "Uploads", roles: ["UPLOADER", "REVIEWER"] },
  { kind: "reports", label: "Reports", roles: ["REVIEWER", "SENDER"] },
  { kind: "emails", label: "Emails", roles: ["SENDER"] },
  { kind: "sync", label: "Sync", roles: ["ADMIN"] },
  { kind: "orders", label: "Orders", roles: ["REVIEWER", "SENDER", "VIEWER"] },
  { kind: "order_emails", label: "Order emails", roles: ["REVIEWER", "SENDER"] },
  { kind: "owner_reports", label: "Owner reports", roles: ["REVIEWER", "SENDER", "ADMIN"] },
] as const;
const JOB_LABEL: Record<string, string> = {
  "upload.scan": "Safety scan", "upload.parse": "Reading", "upload.extract": "Extraction",
  "sheets.sync": "Google Sheets sync", "erp.sync": "ERP sync", "powerbi.refresh": "Power BI refresh",
  "integration.test": "Connection test",
};
const JOB_STATE: Record<string, string> = {
  QUEUED: "Waiting", RUNNING: "Running", RETRY_WAIT: "Retrying soon", SUCCEEDED: "Succeeded", PARTIAL: "Partly done",
  FAILED: "Failed", CANCELLED: "Cancelled",
};

export default function HistoryPage() {
  return (
    <Suspense>
      <History />
    </Suspense>
  );
}

function History() {
  const { session } = useSession();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const tabs = TABS.filter((t) => hasRole(session, ...t.roles));
  const kind = tabs.find((t) => t.kind === params.get("kind"))?.kind ?? tabs[0]?.kind;
  const [rows, setRows] = useState<{ kind: string; data: Row[]; next: string | null } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!kind) return;
    let active = true;
    apiGet<{ data: Row[]; next_cursor: string | null }>(`/history?kind=${kind}`).then(
      (r) => active && (setRows({ kind, data: r.data, next: r.next_cursor }), setError(null)),
      (e) => active && setError(e instanceof ApiError ? e.message : "History could not be loaded."),
    );
    return () => {
      active = false;
    };
  }, [kind]);

  async function more() {
    if (!rows?.next || !kind) return;
    try {
      const r = await apiGet<{ data: Row[]; next_cursor: string | null }>(`/history?kind=${kind}&cursor=${encodeURIComponent(rows.next)}`);
      setRows({ kind, data: [...rows.data, ...r.data], next: r.next_cursor });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "More history could not be loaded.");
    }
  }

  if (!kind) return <p>Your role has no history to show.</p>;
  const current = rows?.kind === kind ? rows : null;

  function badge(r: Row) {
    if (r.kind === "reports") {
      const s = reportStatus({ state: r.state, outdated: !!r.outdated });
      return <span className={`badge tone-${s.tone}`}>{s.label}</span>;
    }
    if (r.kind === "orders") {
      return <span className={`badge tone-${r.state === "SAVED" ? "success" : "progress"}`}>{r.state === "SAVED" ? "Saved" : "Corrected"}</span>;
    }
    if (r.kind === "order_emails" || r.kind === "owner_reports") {
      const tone = { ACCEPTED: "success", FAILED: "error", UNKNOWN: "warning", SENDING: "progress", QUEUED: "progress" }[r.state] ?? "neutral";
      const label = { ACCEPTED: "Sent", FAILED: "Not sent", UNKNOWN: "Unknown", SENDING: "Sending", QUEUED: "Waiting" }[r.state] ?? r.state;
      return <span className={`badge tone-${tone}`} title={r.state_text}>{label}</span>;
    }
    if (r.kind === "emails") {
      const s = emailStatus(r.state);
      return <span className={`badge tone-${s.tone}`} title={r.state_text}>{s.label}</span>;
    }
    const tone = r.state === "SUCCEEDED" ? "success" : r.state === "FAILED" ? "error" : r.state === "PARTIAL" || r.state === "RETRY_WAIT" ? "warning" : "progress";
    const stage = JOB_LABEL[r.summary.split(" · ")[0] ?? ""] ?? r.summary;
    return <span className={`badge tone-${tone}`}>{stage}: {JOB_STATE[r.state] ?? r.state}</span>;
  }

  return (
    <>
      <h1>History</h1>
      <div className="row" role="tablist" aria-label="History type" style={{ marginBottom: 12 }}>
        {tabs.map((t) => (
          <button key={t.kind} role="tab" aria-selected={t.kind === kind} className={t.kind === kind ? "primary" : undefined}
            onClick={() => router.push(`${pathname}?kind=${t.kind}`)}>
            {t.label}
          </button>
        ))}
      </div>
      {kind === "uploads" && <p className="meta"><Link href="/batches">All upload batches</Link></p>}
      {error && <div className="banner error" role="alert">{error}</div>}
      {!current && !error && <div className="skeleton" aria-busy="true" style={{ height: 80 }} />}
      {current && current.data.length === 0 && <div className="card"><p>Nothing here yet.</p></div>}
      {current && current.data.length > 0 && (
        <div className="card" role="tabpanel">
          <ul className="filelist">
            {current.data.map((r) => (
              <li key={`${r.object_id}-${r.at}`} className="stack" style={{ gap: 4 }}>
                <div className="row">
                  {badge(r)}
                  <Link href={r.detail_url}>{r.kind === "uploads" ? r.summary.split(" · ").slice(1).join(" · ") : r.summary}</Link>
                </div>
                <span className="meta">
                  {new Date(r.at).toLocaleString("en-IN", { timeZone: session.timezone })}
                  {r.attempts ? ` · attempt ${r.attempts} of ${r.max_attempts}` : ""}
                  {r.next_retry_at ? ` · next try ${new Date(r.next_retry_at).toLocaleTimeString("en-IN", { timeZone: session.timezone })}` : ""}
                </span>
                {r.last_error && <span className="reason">{r.last_error.message}</span>}
              </li>
            ))}
          </ul>
          {current.next && <button onClick={more}>Load more</button>}
        </div>
      )}
    </>
  );
}
