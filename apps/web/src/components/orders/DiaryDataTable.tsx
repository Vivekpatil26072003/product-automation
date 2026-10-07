"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { apiGet } from "@/lib/api";

// The diary data read from one upload, as a table, with its PDF. Saved orders show their latest saved values;
// orders still waiting for review show the values as read and say so ("To review"), so the preview is never
// mistaken for checked records.

type Row = {
  draft_id: string; order_id: string | null; order_ref: string | null; page: number; customer: string; mobile: string;
  order_date: string; delivery_date: string; package: string; quantity: string; rate: string; total: string;
  details: string; status: string;
};
type Data = { batch_id: string; batch_ref: string; rows: Row[]; reviewed: boolean };

export function DiaryDataTable({ batchId, refresh = "", compact = false }: { batchId: string; refresh?: string; compact?: boolean }) {
  const [data, setData] = useState<Data | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    apiGet<{ data: Data }>(`/batches/${batchId}/diary-data`).then(
      (r) => active && (setData(r.data), setFailed(false)),
      () => active && setFailed(true),
    );
    return () => {
      active = false;
    };
  }, [batchId, refresh]);

  if (failed) return null;
  if (!data) return <div className="skeleton" aria-busy="true" style={{ width: "50%" }} />;
  if (data.rows.length === 0) return compact ? null : <p className="meta">No orders have been read from this upload yet.</p>;
  const toReview = data.rows.filter((r) => r.status !== "Saved").length;
  const id = `diary-${batchId}`;

  return (
    <section className={compact ? "stack" : "card stack"} aria-labelledby={id}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 id={id} style={{ margin: 0, fontSize: compact ? 16 : undefined }}>Diary data ({data.rows.length} order{data.rows.length === 1 ? "" : "s"})</h2>
        <div className="row">
          <a className="button" href={`/api/v1/batches/${batchId}/diary-data/pdf`}>
            {data.reviewed ? "Download PDF" : "Download PDF (draft)"}
          </a>
          {toReview > 0 && <Link className="button primary" href={`/batches/${batchId}/orders`}>Review {toReview}</Link>}
        </div>
      </div>
      {!data.reviewed && (
        <p className="meta" style={{ margin: 0 }}>Rows marked “To review” show what was read from the diary and have not been checked yet.</p>
      )}
      <div className="table-scroll" role="region" aria-label={`Diary data table ${data.batch_ref}`} tabIndex={0}>
        <table className="data">
          <thead>
            <tr>
              <th scope="col">Order</th><th scope="col">Customer</th><th scope="col">Mobile</th><th scope="col">Order date</th>
              <th scope="col">Delivery</th><th scope="col">Package</th><th scope="col">Qty</th><th scope="col">Rate</th>
              <th scope="col">Total</th><th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {data.rows.map((r) => (
              <tr key={r.draft_id}>
                <td>{r.order_id ? <Link href={`/orders/${r.order_id}`}>{r.order_ref}</Link> : `Page ${r.page}`}</td>
                <td>{r.customer || <span className="meta">not read</span>}{r.details && <div className="meta">{r.details}</div>}</td>
                <td className="tnum">{r.mobile}</td>
                <td>{r.order_date}</td>
                <td>{r.delivery_date}</td>
                <td>{r.package}</td>
                <td className="tnum">{r.quantity}</td>
                <td className="tnum">{r.rate}</td>
                <td className="tnum">{r.total}</td>
                <td><span className={`badge tone-${r.status === "Saved" ? "success" : "warning"}`}>{r.status}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
