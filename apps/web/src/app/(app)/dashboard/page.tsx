"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";

import { DepartmentChart, type DeptRow, StatTiles, StatusChart, type UnitMetric } from "@/components/dashboard/Charts";
import { FilterBar } from "@/components/FilterBar";
import { hasRole, useSession } from "@/components/SessionProvider";
import { OrdersSummary } from "@/components/orders/OrdersSummary";
import { ApiError, apiGet, type Master } from "@/lib/api";
import { formatQty, readFilter, toQuery } from "@/lib/filters";
import { type PowerBiStatus, powerBiLabel } from "@/lib/integrations";

// U5 Overview: every panel comes from one aggregate response for the filters in the URL.

type Dashboard = {
  record_count: number;
  metrics: UnitMetric[];
  departments: DeptRow[];
  status_counts: Record<string, number>;
  status_shares: Record<string, string | null>;
  stop_total_minutes: number;
  recent: { id: string; production_date: string; department: { name: string }; machine: { code: string };
            production_qty: string; unit: string; achievement_pct: string | null; status: string }[];
  data_version: number;
  computed_at: string;
  power_bi: PowerBiStatus;
};

export default function DashboardPage() {
  return (
    <Suspense>
      <Overview />
    </Suspense>
  );
}

function Overview() {
  const { session } = useSession();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const filter = useMemo(() => readFilter(new URLSearchParams(params.toString())), [params]);
  const query = toQuery(filter);
  const [data, setData] = useState<{ query: string; value: Dashboard } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [departments, setDepartments] = useState<Master[]>([]);
  const [reload, setReload] = useState(0);
  const latest = useRef(0);

  useEffect(() => {
    apiGet<{ data: Master[] }>("/masters/departments?active=true").then((r) => setDepartments(r.data), () => undefined);
  }, []);

  useEffect(() => {
    const ticket = ++latest.current; // older responses are ignored if the filters change meanwhile
    apiGet<{ data: Dashboard }>(`/dashboard?${query}`).then(
      (r) => ticket === latest.current && (setData({ query, value: r.data }), setError(null)),
      (e) => ticket === latest.current && setError(e instanceof ApiError ? e.message : "The dashboard could not be loaded."),
    );
  }, [query, reload]);

  const apply = (next: typeof filter) => router.push(`${pathname}?${toQuery(next)}`);
  const drill = (extra: Record<string, string>) => router.push(`/records?${toQuery(filter, extra)}`);
  const d = data?.value;
  const stale = data && data.query !== query;
  const units = Array.from(new Set((d?.departments ?? []).map((x) => x.unit)));

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>Overview</h1>
        <div className="row">
          {d && <span className="meta">As of {new Date(d.computed_at).toLocaleString("en-IN", { timeZone: session.timezone })} · data version {d.data_version}</span>}
          {d && d.power_bi.state !== "NOT_CONFIGURED" && (
            <span className="meta">
              Power BI: <span className={`badge tone-${powerBiLabel(d.power_bi).tone}`}>{powerBiLabel(d.power_bi).label}</span>
            </span>
          )}
          <button onClick={() => setReload((n) => n + 1)}>Refresh</button>
        </div>
      </div>
      <FilterBar key={query} value={filter} departments={departments} onApply={apply} />
      {hasRole(session, "REVIEWER", "SENDER", "VIEWER") && <OrdersSummary refresh={reload} />}
      {error && (
        <div className="banner error" role="alert">
          {error} <button onClick={() => setReload((n) => n + 1)}>Retry</button>
        </div>
      )}
      {!d && !error && <div className="skeleton" aria-busy="true" style={{ width: "60%", height: 80 }} />}
      {d && (
        <div style={{ opacity: stale ? 0.6 : 1 }} aria-busy={stale || undefined}>
          {d.record_count === 0 ? (
            <div className="card">
              <p>No approved records match these filters. Achievement: N/A.</p>
              {hasRole(session, "UPLOADER", "REVIEWER") && <Link className="button primary" href="/uploads/new">Upload notes</Link>}
            </div>
          ) : (
            <>
              <StatTiles metrics={d.metrics} />
              {units.length > 1 && (
                <p className="meta">Quantities in different units are shown separately; they are never added together.</p>
              )}
              <div className="grid-2">
                <div className="card">
                  {units.map((u) => (
                    <DepartmentChart key={u} unit={u} rows={d.departments.filter((x) => x.unit === u)}
                                     onSelect={(id) => drill({ department_id: id, unit: u })} />
                  ))}
                </div>
                <div className="card">
                  <StatusChart counts={d.status_counts} shares={d.status_shares} onSelect={(s) => drill({ status: s })} />
                  <p className="meta" style={{ marginTop: 12 }}>
                    Record downtime: {d.stop_total_minutes.toLocaleString("en-IN")} minutes (summed per record; not plant downtime).
                  </p>
                </div>
              </div>
              <section className="card" aria-labelledby="recent-title">
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <h2 id="recent-title" style={{ margin: 0 }}>Recent records</h2>
                  <Link href={`/records?${query}`}>All {d.record_count} records</Link>
                </div>
                <ul className="filelist">
                  {d.recent.map((r) => (
                    <li key={r.id}>
                      <Link href={`/records/${r.id}`}>{r.production_date} · {r.department.name} · {r.machine.code}</Link>
                      <span className="meta tnum">{formatQty(r.production_qty, r.unit)} · {r.achievement_pct ?? "N/A"}{r.achievement_pct ? "%" : ""} · {r.status}</span>
                    </li>
                  ))}
                </ul>
              </section>
              <p className="meta">Power BI is not connected yet; this dashboard reads approved records directly.</p>
            </>
          )}
        </div>
      )}
    </>
  );
}
