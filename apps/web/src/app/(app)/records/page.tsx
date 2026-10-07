"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";

import { FilterBar } from "@/components/FilterBar";
import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend, type Master } from "@/lib/api";
import { formatQty, readFilter, toExportFilter, toQuery } from "@/lib/filters";
import { syncStateLabel } from "@/lib/integrations";

// U4 Production records: approved records in scope, filters in the URL, stable cursor paging, Excel export.

type Row = {
  id: string; state: string; revision: number; production_date: string;
  department: { id: string; name: string }; machine: { id: string; code: string }; operator_name: string;
  production_qty: string; target_qty: string; unit: string; achievement_pct: string | null; status: string;
  stop_minutes: number; sync_state: string;
};
type Page = { data: Row[]; next_cursor: string | null; total: number; data_version: number };
type ExportState = { id: string; state: string; row_count: number; url: string | null; error_code: string | null };

const SORTS = [
  ["date_desc", "Newest first"], ["date_asc", "Oldest first"], ["department", "Department"],
  ["production_desc", "Largest production (one unit)"],
] as const;

export default function RecordsPage() {
  return (
    <Suspense>
      <Records />
    </Suspense>
  );
}

function Records() {
  const { session } = useSession();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const filter = useMemo(() => readFilter(new URLSearchParams(params.toString())), [params]);
  const sort = params.get("sort") ?? "date_desc";
  const query = toQuery(filter, { sort: sort === "date_desc" ? "" : sort });
  const [page, setPage] = useState<{ query: string; rows: Row[]; next: string | null; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [departments, setDepartments] = useState<Master[]>([]);
  const [exp, setExp] = useState<ExportState | null>(null);
  const latest = useRef(0);
  const canExport = hasRole(session, "REVIEWER", "SENDER");

  useEffect(() => {
    apiGet<{ data: Master[] }>("/masters/departments?active=true").then((r) => setDepartments(r.data), () => undefined);
  }, []);

  useEffect(() => {
    const ticket = ++latest.current;
    apiGet<Page>(`/records?${query}&size=25`).then(
      (r) => ticket === latest.current && (setPage({ query, rows: r.data, next: r.next_cursor, total: r.total }), setError(null)),
      (e) => ticket === latest.current && setError(e instanceof ApiError ? e.message : "Records could not be loaded."),
    );
  }, [query]);

  async function more() {
    if (!page?.next) return;
    try {
      const r = await apiGet<Page>(`/records?${query}&size=25&cursor=${encodeURIComponent(page.next)}`);
      setPage((p) => p && { ...p, rows: [...p.rows, ...r.data], next: r.next_cursor });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "More records could not be loaded.");
    }
  }

  async function startExport() {
    try {
      const { data } = await apiSend<{ data: { export_id: string; row_count: number } }>("POST", "/exports", { filter: toExportFilter(filter), format: "xlsx" });
      setExp({ id: data.export_id, state: "QUEUED", row_count: data.row_count, url: null, error_code: null });
      for (let i = 0; i < 60; i++) {
        await new Promise((res) => setTimeout(res, Math.min(1000 * (i + 1), 5000)));
        const r = await apiGet<{ data: ExportState }>(`/exports/${data.export_id}`);
        setExp(r.data);
        if (r.data.state === "READY" || r.data.state === "FAILED") return;
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "The export could not be started.");
    }
  }

  const apply = (next: typeof filter) => router.push(`${pathname}?${toQuery(next, { sort: sort === "date_desc" ? "" : sort })}`);
  const setSort = (s: string) => router.push(`${pathname}?${toQuery(filter, { sort: s === "date_desc" ? "" : s })}`);
  const stale = page && page.query !== query;

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>Production records</h1>
        {hasRole(session, "REVIEWER", "SENDER", "VIEWER") && <Link className="button" href="/orders">Customer orders</Link>}
      </div>
      <FilterBar key={toQuery(filter)} value={filter} departments={departments} onApply={apply} showSearch />
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
        <div className="row">
          <label htmlFor="sort" className="sr-only">Sort</label>
          <select id="sort" value={sort} onChange={(e) => setSort(e.target.value)}>
            {SORTS.map(([v, l]) => <option key={v} value={v} disabled={v === "production_desc" && !filter.unit}>{l}</option>)}
          </select>
          {page && <span className="meta" role="status">{page.total} record{page.total === 1 ? "" : "s"}</span>}
        </div>
        {canExport && (
          <div className="row">
            <button onClick={startExport} disabled={!!exp && !["READY", "FAILED"].includes(exp.state)}>Export to Excel</button>
            {exp && (
              <span role="status" className="meta">
                {exp.state === "READY" && exp.url ? (
                  <a href={exp.url}>Download ({exp.row_count} records)</a>
                ) : exp.state === "FAILED" ? (
                  "The export failed. Try again."
                ) : (
                  `Preparing ${exp.row_count} records…`
                )}
              </span>
            )}
          </div>
        )}
      </div>
      {error && <div className="banner error" role="alert">{error}</div>}
      {!page && !error && <div className="skeleton" aria-busy="true" style={{ width: "60%" }} />}
      {page && page.rows.length === 0 && (
        <div className="card"><p>No approved records match these filters.</p></div>
      )}
      {page && page.rows.length > 0 && (
        <div className="card table-scroll" role="region" aria-label="Records table" tabIndex={0} style={{ opacity: stale ? 0.6 : 1 }}>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Date</th><th scope="col">Department</th><th scope="col">Machine</th><th scope="col">Operator</th>
                <th scope="col">Production</th><th scope="col">Target</th><th scope="col">Achievement</th><th scope="col">Status</th>
                <th scope="col">Sheets sync</th>
              </tr>
            </thead>
            <tbody>
              {page.rows.map((r) => (
                <tr key={r.id} className={r.state === "ARCHIVED" ? "archived" : undefined}>
                  <td><Link href={`/records/${r.id}`}>{r.production_date}</Link>{r.state === "ARCHIVED" && <span className="badge tone-neutral">Archived</span>}</td>
                  <td>{r.department.name}</td>
                  <td>{r.machine.code}</td>
                  <td>{r.operator_name}</td>
                  <td className="tnum">{formatQty(r.production_qty, r.unit)}</td>
                  <td className="tnum">{formatQty(r.target_qty, r.unit)}</td>
                  <td className="tnum">{r.achievement_pct === null ? "N/A" : `${r.achievement_pct}%`}</td>
                  <td>{r.status}</td>
                  <td className="meta">{syncStateLabel(r.sync_state)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {page.next && <button style={{ marginTop: 12 }} onClick={more}>Load more</button>}
        </div>
      )}
    </>
  );
}
