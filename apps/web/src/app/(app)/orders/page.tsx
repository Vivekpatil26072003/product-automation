"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";

import { SendOrderEmail } from "@/components/orders/SendOrderEmail";
import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet } from "@/lib/api";
import { EMAIL_STATE, formatAmount, formatCount, formatDay, type OrderListItem, pdfUrl } from "@/lib/orders";

// Customer orders: saved (approved) orders in scope, always showing each order's latest saved revision.

export default function OrdersPage() {
  return (
    <Suspense>
      <Orders />
    </Suspense>
  );
}

function Orders() {
  const { session } = useSession();
  const customer = useSearchParams().get("customer");
  const [rows, setRows] = useState<OrderListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [applied, setApplied] = useState("");
  const latest = useRef(0);
  const canSend = hasRole(session, "REVIEWER", "SENDER");
  const canRead = hasRole(session, "REVIEWER", "SENDER", "VIEWER");

  const load = useCallback(async () => {
    const ticket = ++latest.current;
    try {
      const q = new URLSearchParams();
      if (applied) q.set("q", applied);
      if (customer) q.set("customer_id", customer);
      const r = await apiGet<{ data: OrderListItem[] }>(`/orders${q.toString() ? `?${q.toString()}` : ""}`);
      if (ticket === latest.current) {
        setRows(r.data);
        setError(null);
      }
    } catch (e) {
      if (ticket === latest.current) setError(e instanceof ApiError ? e.message : "Orders could not be loaded.");
    }
  }, [applied, customer]);

  useEffect(() => {
    if (!canRead) return;
    const t = setTimeout(() => void load(), 0);
    return () => clearTimeout(t);
  }, [load, canRead]);

  if (!canRead) return <p>Customer orders are available to Reviewers, Senders and Viewers.</p>;

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>Customer orders</h1>
        <Link className="button" href="/records">Production records</Link>
      </div>
      {customer && <p><Link href="/orders">Show all customers</Link> · showing one customer&apos;s orders</p>}
      <p className="meta">Orders are added here when a Reviewer approves an order form. Corrections show immediately; earlier versions stay in each order&apos;s history.</p>
      <form className="row" role="search" style={{ marginBottom: 12 }} onSubmit={(e) => { e.preventDefault(); setApplied(q.trim()); }}>
        <label htmlFor="order-search" className="sr-only">Search orders</label>
        <input id="order-search" type="search" placeholder="Customer, order number or mobile" value={q} onChange={(e) => setQ(e.target.value)} />
        <button type="submit">Search</button>
        {rows && <span className="meta" role="status">{rows.length} order{rows.length === 1 ? "" : "s"}</span>}
      </form>
      {error && <div className="banner error" role="alert">{error} <button onClick={() => void load()}>Retry</button></div>}
      {!rows && !error && <div className="skeleton" aria-busy="true" style={{ width: "60%" }} />}
      {rows && rows.length === 0 && (
        <div className="card"><p>No saved orders{applied ? " match this search" : " yet"}. Upload an order note, then review and approve it.</p></div>
      )}
      {rows && rows.length > 0 && (
        <div className="card table-scroll" role="region" aria-label="Orders table" tabIndex={0}>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Order</th><th scope="col">Customer</th><th scope="col">Mobile</th><th scope="col">Order date</th>
                <th scope="col">Delivery</th><th scope="col">Quantity</th><th scope="col">Total</th><th scope="col">Last email</th>
                <th scope="col"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td><Link href={`/orders/${r.id}`}>{r.order_ref}</Link> <span className="meta">r{r.revision}</span></td>
                  <td>
                    {r.customer_id ? <Link href={`/orders?customer=${r.customer_id}`}>{r.values.customer_name}</Link> : r.values.customer_name}
                    {r.attention > 0 && <span className="badge tone-warning" title="Missing or uncertain information was noted when saved"> {r.attention} note{r.attention === 1 ? "" : "s"}</span>}
                  </td>
                  <td className="tnum">{r.values.mobile}</td>
                  <td>{formatDay(r.values.order_date)}</td>
                  <td>{formatDay(r.values.delivery_date)}</td>
                  <td className="tnum">{formatCount(r.values.quantity)}</td>
                  <td className="tnum">{formatAmount(r.values.total)}</td>
                  <td>{r.last_email_state ? <span className={`badge tone-${EMAIL_STATE[r.last_email_state].tone}`}>{EMAIL_STATE[r.last_email_state].label}</span> : <span className="meta">None</span>}</td>
                  <td>
                    <div className="row" style={{ flexWrap: "nowrap" }}>
                      <a className="button" href={pdfUrl(r.id)} target="_blank" rel="noopener">View PDF</a>
                      {canSend && (
                        <SendOrderEmail
                          order={{ id: r.id, order_ref: r.order_ref, revision: r.revision, customer_name: r.values.customer_name, pdf_name: r.pdf_name }}
                          onDone={() => void load()}
                        />
                      )}
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
