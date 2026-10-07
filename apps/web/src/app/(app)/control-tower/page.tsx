"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { type UnitMetric, StatTiles } from "@/components/dashboard/Charts";
import { useSession } from "@/components/SessionProvider";
import { ApiError, apiGet } from "@/lib/api";
import { type PowerBiStatus, powerBiLabel } from "@/lib/integrations";

// Control tower (A2, A9): who has submitted today, what is waiting, and which pieces are not connected yet.

type Dept = {
  department_id: string; name: string; code: string; status: string; approved_records: number;
  pending_review: number; pending_blocked: number; processing: number; failed: number; rejected: number; sync_failed: number;
};
type Tower = {
  date: string; timezone: string; working_day: boolean; cutoff_local_time: string; past_cutoff: boolean;
  computed_at: string; totals: { record_count: number; metrics: UnitMetric[] }; status_counts: Record<string, number>;
  departments: Dept[]; review_queue: { entries: number; with_problems: number };
  exceptions: { CRITICAL: number; WARNING: number; INFO: number };
  reminders: { enabled: boolean; sent: Record<string, number> };
  integrations: Record<string, { state: string; available_from: string; stale?: boolean; error_code?: string | null;
    sync?: { PENDING: number; SYNCED: number; FAILED: number; CONFLICT: number }; counts?: Record<string, number> }>;
};

const STATUS: Record<string, { label: string; tone: string }> = {
  SUBMITTED: { label: "Submitted", tone: "success" },
  REVIEW_PENDING: { label: "Waiting for review", tone: "warning" },
  PROCESSING: { label: "Processing", tone: "progress" },
  MISSING: { label: "Missing", tone: "error" },
  AWAITING: { label: "Not yet (before cutoff)", tone: "neutral" },
  NOT_EXPECTED: { label: "Not expected", tone: "neutral" },
};
const INTEGRATION: Record<string, string> = {
  google_sheets: "Google Sheets sync", power_bi: "Power BI", reports: "PDF reports", email: "Email",
};

function integrationText(v: Tower["integrations"][string]): string {
  if (v.state === "NOT_AVAILABLE") return `Not available yet (arrives in ${v.available_from})`;
  if (v.state === "NOT_CONFIGURED") return "Not set up";
  if (v.state === "NOT_PERMITTED") return "Not shown for your role";
  if (v.counts && "READY" in v.counts) {
    const c = v.counts;
    return v.state === "NONE" ? "No reports for this day"
      : `${c.READY} ready, ${c.OUTDATED} outdated, ${c.FAILED} failed${c.IN_PROGRESS ? `, ${c.IN_PROGRESS} being created` : ""}`;
  }
  if (v.counts && "ACCEPTED" in v.counts) {
    const c = v.counts;
    return v.state === "NONE" ? "No emails sent this day"
      : `${c.ACCEPTED} accepted by provider, ${c.UNKNOWN} unknown, ${c.FAILED} not sent${(c.QUEUED ?? 0) + (c.SENDING ?? 0) ? `, ${(c.QUEUED ?? 0) + (c.SENDING ?? 0)} sending` : ""}`;
  }
  if (v.sync) {
    const s = v.sync;
    const base = `${connectionLabel(v.state)} · ${s.SYNCED} sent, ${s.PENDING} waiting`;
    return s.FAILED || s.CONFLICT ? `${base}, ${s.FAILED} failed, ${s.CONFLICT} conflicts` : base;
  }
  return powerBiLabel({ state: v.state as PowerBiStatus["state"] }).label;
}

function connectionLabel(state: string): string {
  return ({ NEEDS_TEST: "Testing", CONNECTED: "Connected", TEST_FAILED: "Test failed",
    RECONNECT_REQUIRED: "Reconnect required", CONFLICT: "Stopped: conflict" } as Record<string, string>)[state] ?? state;
}

export default function ControlTowerPage() {
  return (
    <Suspense>
      <ControlTower />
    </Suspense>
  );
}

function ControlTower() {
  const { session } = useSession();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const day = params.get("date") ?? "";
  const [data, setData] = useState<Tower | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    apiGet<{ data: Tower }>(`/control-tower${day ? `?date=${day}` : ""}`).then(
      (r) => active && (setData(r.data), setError(null)),
      (e) => active && setError(e instanceof ApiError ? e.message : "The control tower could not be loaded."),
    );
    return () => {
      active = false;
    };
  }, [day]);

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>Control tower</h1>
        <div className="row">
          <label htmlFor="ct-date">Day</label>
          <input id="ct-date" type="date" value={day || data?.date || ""} onChange={(e) => router.push(`${pathname}?date=${e.target.value}`)} />
        </div>
      </div>
      {error && <div className="banner error" role="alert">{error}</div>}
      {!data && !error && <div className="skeleton" aria-busy="true" style={{ width: "50%" }} />}
      {data && (
        <>
          <p className="meta">
            {data.date} ({data.timezone}) · {data.working_day ? "working day" : "not a working day"} · submission cutoff {data.cutoff_local_time}
            {data.past_cutoff ? " (passed)" : ""} · as of {new Date(data.computed_at).toLocaleTimeString("en-IN", { timeZone: session.timezone })}
          </p>
          <div className="tiles" aria-label="Departments by status">
            {Object.entries(data.status_counts).filter(([, n]) => n > 0).map(([s, n]) => (
              <div key={s} className="tile"><div className="tile-label">{STATUS[s]?.label ?? s}</div><div className="tile-value">{n}</div></div>
            ))}
            <div className="tile"><div className="tile-label">Entries waiting for review</div><div className="tile-value">{data.review_queue.entries}</div>
              {data.review_queue.with_problems > 0 && <div className="meta">{data.review_queue.with_problems} with problems to fix</div>}</div>
            <div className="tile"><div className="tile-label">Open exceptions</div>
              <div className="tile-value"><Link href="/exceptions">{data.exceptions.CRITICAL + data.exceptions.WARNING + data.exceptions.INFO}</Link></div>
              {data.exceptions.CRITICAL > 0 && <div className="meta">{data.exceptions.CRITICAL} critical</div>}</div>
          </div>
          <p className="meta">
            Reminders: {data.reminders.enabled
              ? `on · sent for this day: ${data.reminders.sent.REMINDER_FIRST ?? 0} first, ${data.reminders.sent.REMINDER_SECOND ?? 0} second, ${data.reminders.sent.ESCALATION ?? 0} escalations`
              : "off"}
          </p>
          <StatTiles metrics={data.totals.metrics} />
          <section className="card table-scroll" tabIndex={0} aria-labelledby="ct-depts">
            <h2 id="ct-depts">Departments</h2>
            <table className="data">
              <thead>
                <tr><th scope="col">Department</th><th scope="col">Status</th><th scope="col">Approved records</th>
                  <th scope="col">Waiting for review</th><th scope="col">Processing</th><th scope="col">Failed or rejected files</th>
                  <th scope="col">Rejected entries</th><th scope="col">Not synced</th></tr>
              </thead>
              <tbody>
                {data.departments.map((d) => (
                  <tr key={d.department_id}>
                    <th scope="row">{d.name}</th>
                    <td><span className={`badge tone-${STATUS[d.status]?.tone ?? "neutral"}`}>{STATUS[d.status]?.label ?? d.status}</span></td>
                    <td className="tnum">
                      {d.approved_records > 0 ? <Link href={`/records?date_from=${data.date}&date_to=${data.date}&department_id=${d.department_id}`}>{d.approved_records}</Link> : 0}
                    </td>
                    <td className="tnum">{d.pending_review}{d.pending_blocked ? ` (${d.pending_blocked} with problems)` : ""}</td>
                    <td className="tnum">{d.processing}</td>
                    <td className="tnum">{d.failed}</td>
                    <td className="tnum">{d.rejected}</td>
                    <td className="tnum">{d.sync_failed}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
          <section className="card" aria-labelledby="ct-int">
            <h2 id="ct-int">Connections</h2>
            <ul className="filelist">
              {Object.entries(data.integrations).map(([k, v]) => (
                <li key={k}><span>{INTEGRATION[k] ?? k}</span><span className="meta">{integrationText(v)}</span></li>
              ))}
            </ul>
          </section>
        </>
      )}
    </>
  );
}
