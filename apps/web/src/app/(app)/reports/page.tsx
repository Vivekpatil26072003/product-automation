"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet } from "@/lib/api";
import { formatQty } from "@/lib/filters";
import { type ReportListItem, periodText, reportStatus } from "@/lib/reports";

// U6 Reports: every version is kept; outdated ones stay readable but cannot be sent.

export default function ReportsPage() {
  const { session } = useSession();
  const [rows, setRows] = useState<ReportListItem[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    apiGet<{ data: ReportListItem[]; next_cursor: string | null }>("/reports").then(
      (r) => active && (setRows(r.data), setNext(r.next_cursor), setError(null)),
      (e) => active && setError(e instanceof ApiError ? e.message : "Reports could not be loaded."),
    );
    return () => {
      active = false;
    };
  }, []);

  async function more() {
    if (!next) return;
    try {
      const r = await apiGet<{ data: ReportListItem[]; next_cursor: string | null }>(`/reports?cursor=${encodeURIComponent(next)}`);
      setRows((old) => [...(old ?? []), ...r.data]);
      setNext(r.next_cursor);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "More reports could not be loaded.");
    }
  }

  if (!hasRole(session, "REVIEWER", "SENDER")) return <p>Reports are available to Reviewers and Senders.</p>;

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>Reports</h1>
        <Link className="button primary" href="/reports/new">New report</Link>
      </div>
      {error && <div className="banner error" role="alert">{error}</div>}
      {!rows && !error && <div className="skeleton" aria-busy="true" style={{ height: 80 }} />}
      {rows && rows.length === 0 && <div className="card"><p>No reports yet. Create one from approved records.</p></div>}
      {rows && rows.length > 0 && (
        <div className="card table-scroll" role="region" aria-label="Reports" tabIndex={0}>
          <table className="data">
            <caption className="sr-only">Reports, newest first</caption>
            <thead>
              <tr><th scope="col">Report</th><th scope="col">Period</th><th scope="col">Status</th><th scope="col">Records</th><th scope="col">Production</th><th scope="col">Created</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const st = reportStatus(r);
                return (
                  <tr key={r.id}>
                    <td><Link href={`/reports/${r.id}`}>{r.title}</Link><div className="meta">{r.code} · version {r.version}</div></td>
                    <td>{periodText(r.filter)}</td>
                    <td><span className={`badge tone-${st.tone}`}>{st.label}</span></td>
                    <td className="tnum">{r.record_count}</td>
                    <td className="tnum">{r.metrics.map((m) => formatQty(m.production_qty, m.unit)).join(" · ") || "-"}</td>
                    <td className="meta">{new Date(r.created_at).toLocaleString("en-IN", { timeZone: session.timezone })}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {next && <button onClick={more}>Load more</button>}
        </div>
      )}
    </>
  );
}
