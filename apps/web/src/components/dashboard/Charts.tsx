"use client";

import { useState } from "react";

import { formatQty } from "@/lib/filters";

// Dashboard charts (U5). Inline SVG, spec §6 tokens.
// - Department chart: one unit per chart (m, kg and pcs are never on one axis); production is a thin bar
//   from zero, target is an ink tick. Each bar is a real button: hover or keyboard focus shows the
//   tooltip, click drills down to the records behind it.
// - Status chart: counts per status as labelled bars; identity is carried by the label, not by colour.
// - Every chart has a table view with the same numbers (tooltips enhance, never gate).

export type DeptRow = {
  department_id: string;
  department_name: string | null;
  unit: string;
  production_qty: string;
  target_qty: string;
  achievement_pct: string | null;
  variance: string;
  record_count: number;
};

export type UnitMetric = Omit<DeptRow, "department_id" | "department_name">;

export function StatTiles({ metrics }: { metrics: UnitMetric[] }) {
  if (!metrics.length) {
    return (
      <div className="tiles" aria-label="Totals">
        <div className="tile"><div className="tile-label">Records</div><div className="tile-value">0</div></div>
        <div className="tile"><div className="tile-label">Achievement</div><div className="tile-value">N/A</div></div>
      </div>
    );
  }
  return (
    <>
      {metrics.map((m) => (
        <div key={m.unit} className="tiles" aria-label={`Totals in ${m.unit}`}>
          <div className="tile"><div className="tile-label">Production ({m.unit})</div><div className="tile-value">{formatQty(m.production_qty)}</div></div>
          <div className="tile"><div className="tile-label">Target ({m.unit})</div><div className="tile-value">{formatQty(m.target_qty)}</div></div>
          <div className="tile">
            <div className="tile-label">Achievement</div>
            <div className="tile-value">{m.achievement_pct === null ? "N/A" : `${m.achievement_pct}%`}</div>
            {m.achievement_pct === null && <div className="meta">No target set</div>}
          </div>
          <div className="tile"><div className="tile-label">Variance ({m.unit})</div><div className="tile-value">{formatQty(m.variance)}</div></div>
          <div className="tile"><div className="tile-label">Records</div><div className="tile-value">{m.record_count}</div></div>
        </div>
      ))}
    </>
  );
}

export function DepartmentChart(props: { unit: string; rows: DeptRow[]; onSelect: (departmentId: string) => void }) {
  const { unit, rows, onSelect } = props;
  const [active, setActive] = useState<string | null>(null);
  const [table, setTable] = useState(false);
  // One linear scale from zero shared by production bars and target ticks.
  const max = Math.max(1, ...rows.flatMap((r) => [Number(r.production_qty), Number(r.target_qty)]));
  const pct = (v: number) => `${(v / max) * 100}%`;
  const current = rows.find((r) => r.department_id === active);
  const title = `Production against target by department (${unit})`;

  return (
    <figure className="chart" aria-labelledby={`dept-${unit}-title`}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <figcaption id={`dept-${unit}-title`}><strong>{title}</strong></figcaption>
        <button className="linklike" onClick={() => setTable((t) => !t)}>{table ? "Show chart" : "Show as table"}</button>
      </div>
      {table ? (
        <div className="table-scroll" role="region" aria-label={`${title}, table`} tabIndex={0}>
          <DeptTable unit={unit} rows={rows} onSelect={onSelect} />
        </div>
      ) : (
        <>
          <div className="legend">
            <span><span className="key-bar" aria-hidden="true" /> Production</span>
            <span><span className="key-tick" aria-hidden="true" /> Target</span>
          </div>
          <ul className="dept-bars">
            {rows.map((r) => {
              const target = Number(r.target_qty);
              return (
                <li key={r.department_id}>
                  <button
                    className="dept-row"
                    aria-label={`${r.department_name}: ${formatQty(r.production_qty, unit)} of ${formatQty(r.target_qty, unit)}, ${r.achievement_pct ?? "N/A"}${r.achievement_pct ? "%" : ""}. Open records.`}
                    onMouseEnter={() => setActive(r.department_id)}
                    onMouseLeave={() => setActive(null)}
                    onFocus={() => setActive(r.department_id)}
                    onBlur={() => setActive(null)}
                    onClick={() => onSelect(r.department_id)}
                  >
                    <span className="dept-name">{r.department_name}</span>
                    <span className="dept-track">
                      <span className="dept-fill" style={{ width: pct(Number(r.production_qty)) }} />
                      {target > 0 && <span className="dept-target" style={{ left: pct(target) }} />}
                    </span>
                    <span className="dept-value tnum">
                      {formatQty(r.production_qty)} · {r.achievement_pct === null ? "N/A" : `${r.achievement_pct}%`}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          <p className="chart-detail meta" role="status">
            {current
              ? `${current.department_name}: production ${formatQty(current.production_qty, unit)}, target ${formatQty(current.target_qty, unit)}, variance ${formatQty(current.variance, unit)}, ${current.record_count} record${current.record_count === 1 ? "" : "s"}. Click to open.`
              : "Point at or tab to a department for details; click to open its records."}
          </p>
        </>
      )}
    </figure>
  );
}

function DeptTable({ unit, rows, onSelect }: { unit: string; rows: DeptRow[]; onSelect: (id: string) => void }) {
  return (
    <table className="data">
      <caption className="sr-only">Production against target by department ({unit})</caption>
      <thead>
        <tr><th scope="col">Department</th><th scope="col">Production</th><th scope="col">Target</th><th scope="col">Achievement</th><th scope="col">Variance</th><th scope="col">Records</th></tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.department_id}>
            <th scope="row"><button className="linklike" onClick={() => onSelect(r.department_id)}>{r.department_name}</button></th>
            <td className="tnum">{formatQty(r.production_qty, unit)}</td>
            <td className="tnum">{formatQty(r.target_qty, unit)}</td>
            <td className="tnum">{r.achievement_pct === null ? "N/A" : `${r.achievement_pct}%`}</td>
            <td className="tnum">{formatQty(r.variance, unit)}</td>
            <td className="tnum">{r.record_count}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const STATUS_LABEL: Record<string, string> = { RUNNING: "Running", COMPLETED: "Completed", PENDING: "Pending", HOLD: "On hold" };

export function StatusChart(props: { counts: Record<string, number>; shares: Record<string, string | null>; onSelect: (s: string) => void }) {
  const { counts, shares, onSelect } = props;
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const max = Math.max(1, ...Object.values(counts));
  return (
    <figure className="chart" aria-labelledby="status-title">
      <figcaption id="status-title"><strong>Records by end-of-period status</strong></figcaption>
      <ul className="status-bars">
        {Object.entries(counts).map(([status, n]) => (
          <li key={status}>
            <button className="status-row" onClick={() => onSelect(status)} aria-label={`${STATUS_LABEL[status]}: ${n} records${shares[status] ? `, ${shares[status]}%` : ""}. Open records.`}>
              <span className="status-name">{STATUS_LABEL[status] ?? status}</span>
              <span className="status-track"><span className="status-fill" style={{ width: `${(n / max) * 100}%` }} /></span>
              <span className="tnum status-value">{n}{total ? ` · ${shares[status]}%` : ""}</span>
            </button>
          </li>
        ))}
      </ul>
    </figure>
  );
}
