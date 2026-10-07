"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { apiGet } from "@/lib/api";
import { formatAmount } from "@/lib/orders";

// Overview card: customer orders waiting for review and saved in the last 30 days (latest saved values).

type Summary = { awaiting_review: number; saved: number; saved_total: string; emails_accepted: number };

export function OrdersSummary({ refresh }: { refresh: number }) {
  const [s, setS] = useState<Summary | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    apiGet<{ data: Summary }>("/orders/summary").then(
      (r) => active && (setS(r.data), setFailed(false)),
      () => active && setFailed(true),
    );
    return () => {
      active = false;
    };
  }, [refresh]);

  if (failed) return null; // the production overview stays usable if order figures cannot load
  return (
    <section className="card" aria-labelledby="orders-summary">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 id="orders-summary" style={{ margin: 0 }}>Customer orders (last 30 days)</h2>
        <Link href="/orders">All orders</Link>
      </div>
      {!s ? (
        <div className="skeleton" aria-busy="true" style={{ width: "40%" }} />
      ) : (
        <div className="tiles" style={{ margin: "12px 0 0" }}>
          <div className="tile"><div className="tile-label">Waiting for review</div><div className="tile-value tnum">{s.awaiting_review}</div></div>
          <div className="tile"><div className="tile-label">Orders saved</div><div className="tile-value tnum">{s.saved}</div></div>
          <div className="tile"><div className="tile-label">Order value</div><div className="tile-value tnum">{formatAmount(s.saved_total)}</div></div>
          <div className="tile"><div className="tile-label">PDFs emailed</div><div className="tile-value tnum">{s.emails_accepted}</div></div>
        </div>
      )}
    </section>
  );
}
