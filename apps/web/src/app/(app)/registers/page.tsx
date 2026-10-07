"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { SendSheetEmail } from "@/components/sheets/SendSheetEmail";
import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet } from "@/lib/api";
import { REG_FORMATS, registerFileUrl, type RegisterListItem } from "@/lib/registers";
import { dayLabel, EMAIL_STATE_LABEL, fmt } from "@/lib/sheets";

// Pick reading registers (WGS-02) saved in the database, one row per department and day, with downloads and email.

export default function RegistersPage() {
  const { session } = useSession();
  const [rows, setRows] = useState<RegisterListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filters, setFilters] = useState({ from: "", to: "", state: "" });
  const [applied, setApplied] = useState(filters);
  const ticket = useRef(0);
  const canSend = hasRole(session, "REVIEWER", "SENDER", "ADMIN");

  const load = useCallback(async () => {
    const mine = ++ticket.current;
    const q = new URLSearchParams();
    if (applied.from) q.set("date_from", applied.from);
    if (applied.to) q.set("date_to", applied.to);
    if (applied.state) q.set("state", applied.state);
    try {
      const r = await apiGet<{ data: RegisterListItem[] }>(`/registers${q.toString() ? `?${q}` : ""}`);
      if (mine === ticket.current) {
        setRows(r.data);
        setError(null);
      }
    } catch (e) {
      if (mine === ticket.current) setError(e instanceof ApiError ? e.message : "Registers could not be loaded.");
    }
  }, [applied]);

  useEffect(() => {
    const t = setTimeout(() => void load(), 0);
    return () => clearTimeout(t);
  }, [load]);

  return (
    <>
      <h1>Pick registers</h1>
      <p className="meta">
        Hourly production reading register (WGS-02). Each photographed shift page goes into the register of its day: meter
        readings and the picks of every two hours per machine. Totals and stopped machines are calculated and checked against
        what the worker wrote.
      </p>
      <form className="row" role="search" style={{ marginBottom: 12, alignItems: "end" }} onSubmit={(e) => { e.preventDefault(); setApplied(filters); }}>
        <div><label htmlFor="r-from">From</label><input id="r-from" type="date" value={filters.from} onChange={(e) => setFilters({ ...filters, from: e.target.value })} /></div>
        <div><label htmlFor="r-to">To</label><input id="r-to" type="date" value={filters.to} onChange={(e) => setFilters({ ...filters, to: e.target.value })} /></div>
        <div>
          <label htmlFor="r-state">Status</label>
          <select id="r-state" value={filters.state} onChange={(e) => setFilters({ ...filters, state: e.target.value })}>
            <option value="">All</option><option value="DRAFT">To check</option><option value="APPROVED">Approved</option>
          </select>
        </div>
        <button type="submit">Show</button>
        {rows && <span className="meta" role="status">{rows.length} register{rows.length === 1 ? "" : "s"}</span>}
      </form>
      {error && <div className="banner error" role="alert">{error} <button onClick={() => void load()}>Retry</button></div>}
      {!rows && !error && <div className="skeleton" aria-busy="true" style={{ width: "60%" }} />}
      {rows && rows.length === 0 && (
        <div className="card"><p>No registers yet. Photograph a pick reading register page on <Link href="/diary">Diary photos</Link>.</p></div>
      )}
      {rows && rows.length > 0 && (
        <div className="card table-scroll" role="region" aria-label="Pick registers table" tabIndex={0}>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Date</th><th scope="col">Status</th><th scope="col">Shift I picks</th><th scope="col">Shift II picks</th>
                <th scope="col">Shift III picks</th><th scope="col">Day picks</th><th scope="col">Machines</th><th scope="col">Last email</th>
                <th scope="col"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const toCheck = r.uncertain + r.checks;
                return (
                  <tr key={r.id}>
                    <td><Link href={`/registers/${r.id}`}>{dayLabel(r.register_date)}</Link><div className="meta">{r.department}</div></td>
                    <td>
                      <span className={`badge tone-${r.state === "APPROVED" ? "success" : "warning"}`}>{r.state === "APPROVED" ? "Approved" : "To check"}</span>
                      {toCheck > 0 && <div className="meta">{toCheck} value{toCheck === 1 ? "" : "s"} to check</div>}
                    </td>
                    {(["I", "II", "III"] as const).map((sh) => (
                      <td key={sh} className="tnum">{r.present.includes(sh) ? fmt(r.shifts[sh], 0) : <span className="meta">No page</span>}</td>
                    ))}
                    <td className="tnum"><strong>{fmt(r.day_total, 0)}</strong></td>
                    <td className="tnum">{r.machines}</td>
                    <td>{r.last_email_state ? <span className={`badge tone-${EMAIL_STATE_LABEL[r.last_email_state].tone}`}>{EMAIL_STATE_LABEL[r.last_email_state].label}</span> : <span className="meta">None</span>}</td>
                    <td>
                      <div className="row" style={{ flexWrap: "nowrap" }}>
                        {REG_FORMATS.map((f) => <a key={f.key} className="button" href={registerFileUrl(r.id, f.key)}>{f.label}</a>)}
                        {canSend && <SendSheetEmail kind="register" sheet={{ id: r.id, report_date: r.register_date, department: r.department, version: r.version }} onDone={() => void load()} />}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
