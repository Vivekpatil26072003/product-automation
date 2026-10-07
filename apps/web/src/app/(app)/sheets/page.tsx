"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { SendSheetEmail } from "@/components/sheets/SendSheetEmail";
import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet } from "@/lib/api";
import { dayLabel, EMAIL_STATE_LABEL, fileUrl, fmt, FORMATS, type SheetListItem } from "@/lib/sheets";

// Daily production sheets saved in the database, one row per department and day, with downloads and email.

export default function SheetsPage() {
  const { session } = useSession();
  const [rows, setRows] = useState<SheetListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filters, setFilters] = useState({ from: "", to: "", state: "", supervisor: "" });
  const [applied, setApplied] = useState(filters);
  const ticket = useRef(0);
  const canSend = hasRole(session, "REVIEWER", "SENDER", "ADMIN");

  const load = useCallback(async () => {
    const mine = ++ticket.current;
    const q = new URLSearchParams();
    if (applied.from) q.set("date_from", applied.from);
    if (applied.to) q.set("date_to", applied.to);
    if (applied.state) q.set("state", applied.state);
    if (applied.supervisor.trim()) q.set("supervisor", applied.supervisor.trim());
    try {
      const r = await apiGet<{ data: SheetListItem[] }>(`/sheets${q.toString() ? `?${q}` : ""}`);
      if (mine === ticket.current) {
        setRows(r.data);
        setError(null);
      }
    } catch (e) {
      if (mine === ticket.current) setError(e instanceof ApiError ? e.message : "Sheets could not be loaded.");
    }
  }, [applied]);

  useEffect(() => {
    const t = setTimeout(() => void load(), 0);
    return () => clearTimeout(t);
  }, [load]);

  return (
    <>
      <h1>Daily sheets</h1>
      <p className="meta">Each photographed production report becomes one sheet per day. Values are checked and approved, then saved; totals, efficiencies and to-date are calculated.</p>
      <form className="row" role="search" style={{ marginBottom: 12, alignItems: "end" }} onSubmit={(e) => { e.preventDefault(); setApplied(filters); }}>
        <div><label htmlFor="f-from">From</label><input id="f-from" type="date" value={filters.from} onChange={(e) => setFilters({ ...filters, from: e.target.value })} /></div>
        <div><label htmlFor="f-to">To</label><input id="f-to" type="date" value={filters.to} onChange={(e) => setFilters({ ...filters, to: e.target.value })} /></div>
        <div>
          <label htmlFor="f-state">Status</label>
          <select id="f-state" value={filters.state} onChange={(e) => setFilters({ ...filters, state: e.target.value })}>
            <option value="">All</option><option value="DRAFT">To check</option><option value="APPROVED">Approved</option>
          </select>
        </div>
        <div><label htmlFor="f-sup">Supervisor</label><input id="f-sup" type="text" value={filters.supervisor} onChange={(e) => setFilters({ ...filters, supervisor: e.target.value })} /></div>
        <button type="submit">Show</button>
        {rows && <span className="meta" role="status">{rows.length} sheet{rows.length === 1 ? "" : "s"}</span>}
      </form>
      {error && <div className="banner error" role="alert">{error} <button onClick={() => void load()}>Retry</button></div>}
      {!rows && !error && <div className="skeleton" aria-busy="true" style={{ width: "60%" }} />}
      {rows && rows.length === 0 && (
        <div className="card"><p>No sheets yet. Photograph a daily production report on <Link href="/diary">Diary photos</Link>.</p></div>
      )}
      {rows && rows.length > 0 && (
        <div className="card table-scroll" role="region" aria-label="Daily sheets table" tabIndex={0}>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Date</th><th scope="col">Status</th><th scope="col">Supervisors (I / II / III)</th>
                <th scope="col">Production (m)</th><th scope="col">Production (kg)</th><th scope="col">Picks</th>
                <th scope="col">Total eff. %</th><th scope="col">Downtime (h)</th><th scope="col">Last email</th>
                <th scope="col"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td><Link href={`/sheets/${r.id}`}>{dayLabel(r.report_date)}</Link><div className="meta">{r.department}</div></td>
                  <td>
                    <span className={`badge tone-${r.state === "APPROVED" ? "success" : "warning"}`}>{r.state === "APPROVED" ? "Approved" : "To check"}</span>
                    {r.uncertain > 0 && <div className="meta">{r.uncertain} value{r.uncertain === 1 ? "" : "s"} to check</div>}
                  </td>
                  <td>{["I", "II", "III"].map((sh) => r.supervisors[sh as "I"] || "-").join(" / ")}</td>
                  <td className="tnum">{fmt(r.figures.production_m, 0)}</td>
                  <td className="tnum">{fmt(r.figures.production_kg, 0)}</td>
                  <td className="tnum">{fmt(r.figures.picks, 0)}</td>
                  <td className="tnum">{fmt(r.figures.total_eff_pct)}</td>
                  <td className="tnum">{fmt(r.figures.downtime)}</td>
                  <td>{r.last_email_state ? <span className={`badge tone-${EMAIL_STATE_LABEL[r.last_email_state].tone}`}>{EMAIL_STATE_LABEL[r.last_email_state].label}</span> : <span className="meta">None</span>}</td>
                  <td>
                    <div className="row" style={{ flexWrap: "nowrap" }}>
                      {FORMATS.map((f) => <a key={f.key} className="button" href={fileUrl(r.id, f.key)}>{f.label}</a>)}
                      {canSend && <SendSheetEmail sheet={{ id: r.id, report_date: r.report_date, department: r.department, version: r.version }} onDone={() => void load()} />}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
