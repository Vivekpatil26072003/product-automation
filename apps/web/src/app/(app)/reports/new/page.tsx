"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useRef, useState } from "react";

import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, type Issue, apiSend, newIdempotencyKey } from "@/lib/api";

// U6 Create report: period, scope, units, title and detail toggle. The snapshot is taken on the server when this
// is submitted; later record changes never alter it. No records → explicit confirmation for an empty report.

function todayIn(tz: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: tz }).format(new Date());
}

export default function NewReportPage() {
  return (
    <Suspense>
      <NewReport />
    </Suspense>
  );
}

function NewReport() {
  const { session } = useSession();
  const router = useRouter();
  const params = useSearchParams();
  const today = todayIn(session.timezone);
  const [dateFrom, setDateFrom] = useState(params.get("date_from") ?? today);
  const [dateTo, setDateTo] = useState(params.get("date_to") ?? today);
  const [departments, setDepartments] = useState<string[]>([]);
  const [unit, setUnit] = useState(params.get("unit") ?? "");
  const [title, setTitle] = useState("Daily Production Report");
  const [detail, setDetail] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [issues, setIssues] = useState<Issue[]>([]);
  const [confirmEmpty, setConfirmEmpty] = useState(false);
  const key = useRef(newIdempotencyKey());

  if (!hasRole(session, "REVIEWER", "SENDER")) return <p>Reports are available to Reviewers and Senders.</p>;

  async function submit(allowEmpty: boolean) {
    setBusy(true);
    setError(null);
    setIssues([]);
    if (allowEmpty) key.current = newIdempotencyKey(); // a different request than the refused one
    try {
      const r = await apiSend<{ data: { report_id: string } }>("POST", "/reports", {
        filter: { date_from: dateFrom, date_to: dateTo, department_ids: departments, units: unit ? [unit] : [] },
        title, include_detail: detail, allow_empty: allowEmpty,
      }, { idempotencyKey: key.current });
      router.push(`/reports/${r.data.report_id}`);
    } catch (e) {
      if (e instanceof ApiError && e.code === "EMPTY_PERIOD") {
        setConfirmEmpty(true);
      } else {
        setError(e instanceof ApiError ? e.message : "The report could not be created.");
        setIssues(e instanceof ApiError ? e.fields : []);
      }
      key.current = newIdempotencyKey();
    } finally {
      setBusy(false);
    }
  }

  const fieldError = (f: string) => issues.find((i) => i.field === f)?.message;

  return (
    <>
      <h1>New report</h1>
      <form className="card stack" onSubmit={(e) => { e.preventDefault(); void submit(false); }} aria-label="Report settings">
        <div className="row">
          <div>
            <label htmlFor="r-from">From</label>
            <input id="r-from" type="date" value={dateFrom} max={dateTo} onChange={(e) => setDateFrom(e.target.value)} required />
          </div>
          <div>
            <label htmlFor="r-to">To</label>
            <input id="r-to" type="date" value={dateTo} min={dateFrom} onChange={(e) => setDateTo(e.target.value)} required />
          </div>
          <div>
            <label htmlFor="r-unit">Unit</label>
            <select id="r-unit" value={unit} onChange={(e) => setUnit(e.target.value)}>
              <option value="">All units (shown separately)</option>
              <option value="m">m</option>
              <option value="kg">kg</option>
              <option value="pcs">pcs</option>
            </select>
          </div>
        </div>
        {(fieldError("date_from") || fieldError("date_to")) && <p className="reason">{fieldError("date_from") ?? fieldError("date_to")}</p>}
        <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
          <legend style={{ fontWeight: 600, marginBottom: 4 }}>Departments</legend>
          <p className="meta" style={{ margin: "0 0 4px" }}>None selected means all departments you can see.</p>
          <div className="row">
            {session.departments.map((d) => (
              <label key={d.id} style={{ fontWeight: 400, display: "inline-flex", gap: 6, alignItems: "center" }}>
                <input type="checkbox" checked={departments.includes(d.id)}
                  onChange={(e) => setDepartments((cur) => (e.target.checked ? [...cur, d.id] : cur.filter((x) => x !== d.id)))} />
                {d.name}
              </label>
            ))}
          </div>
        </fieldset>
        <div>
          <label htmlFor="r-title">Title</label>
          <input id="r-title" type="text" className="wide" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} required />
        </div>
        <label style={{ fontWeight: 400, display: "inline-flex", gap: 6, alignItems: "center" }}>
          <input type="checkbox" checked={detail} onChange={(e) => setDetail(e.target.checked)} />
          Include record detail (machine, operator, remarks) in the PDF
        </label>
        {error && <div className="banner error" role="alert">{error}</div>}
        {confirmEmpty ? (
          <div className="banner warning" role="alert">
            <p style={{ margin: "0 0 8px" }}>No approved records match this period and scope. An empty report shows 0 and N/A.</p>
            <div className="row">
              <button type="button" className="primary" disabled={busy} onClick={() => submit(true)}>Create empty report</button>
              <button type="button" onClick={() => setConfirmEmpty(false)}>Change the period</button>
            </div>
          </div>
        ) : (
          <div className="row">
            <button type="submit" className="primary" disabled={busy}>{busy ? "Creating…" : "Create report"}</button>
          </div>
        )}
      </form>
    </>
  );
}
