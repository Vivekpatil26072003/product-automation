"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, type Batch } from "@/lib/api";

type Page = { data: Batch[]; next_cursor: string | null; total: number };

export default function BatchesPage() {
  const { session } = useSession();
  const [rows, setRows] = useState<Batch[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  function fetchPage(next?: string) {
    return apiGet<Page>(`/batches?size=25${next ? `&cursor=${encodeURIComponent(next)}` : ""}`).then(
      (page) => {
        setRows((r) => (next && r ? [...r, ...page.data] : page.data));
        setCursor(page.next_cursor);
        setError(null);
      },
      (e) => setError(e instanceof ApiError ? e.message : "Could not load batches."),
    );
  }
  const load = (next?: string) => void fetchPage(next);

  useEffect(() => {
    let active = true;
    apiGet<Page>("/batches?size=25").then(
      (page) => {
        if (!active) return;
        setRows(page.data);
        setCursor(page.next_cursor);
      },
      (e) => active && setError(e instanceof ApiError ? e.message : "Could not load batches."),
    );
    return () => {
      active = false;
    };
  }, []);

  return (
    <>
      <h1>Processing history</h1>
      {error && (
        <div className="banner error" role="alert">
          {error} <button onClick={() => void load()}>Retry</button>
        </div>
      )}
      {rows === null && !error && <div className="skeleton" aria-busy="true" style={{ width: "50%" }} />}
      {rows?.length === 0 && (
        <div className="card">
          <p>No uploads yet.</p>
          <Link className="button primary" href="/uploads/new">
            Upload notes
          </Link>
        </div>
      )}
      {rows && rows.length > 0 && (
        <section className="card">
          <ul className="filelist">
            {rows.map((b) => (
              <li key={b.id}>
                <div>
                  <Link className="filename" href={`/batches/${b.id}`}>
                    {b.files.map((f) => f.name).slice(0, 3).join(", ")}
                    {b.files.length > 3 ? ` and ${b.files.length - 3} more` : ""}
                  </Link>
                  <div className="meta">
                    {b.department.name} ·{" "}
                    {new Date(b.created_at).toLocaleString("en-IN", { timeZone: session.timezone })} ·{" "}
                    {b.summary.pages_processed} of {b.summary.pages_total} pages processed
                    {b.summary.rejected ? ` · ${b.summary.rejected} not processed` : ""}
                  </div>
                </div>
                <span className={`badge ${b.summary.in_progress ? "tone-progress" : "tone-neutral"}`}>
                  {b.summary.in_progress ? "In progress" : "Finished"}
                </span>
              </li>
            ))}
          </ul>
          {cursor && (
            <button style={{ marginTop: 12 }} onClick={() => void load(cursor)}>
              Load more
            </button>
          )}
        </section>
      )}
    </>
  );
}
