"use client";

import { useEffect, useRef, useState } from "react";

import { ApiError, apiGet, type SourcePage } from "@/lib/api";

// Source evidence beside the fields (U3). Text pages render every span; the spans behind the focused
// field are highlighted and scrolled into view. Scans and photos show the page image; OCR evidence
// there is page-level (no invented boxes), so the matching transcribed lines are highlighted instead.

type Props = { uploadId: string; page: number; highlight: string[]; onPage: (page: number) => void };

type Loaded = { key: string; page: SourcePage | null; error: string | null };

export function SourcePane({ uploadId, page, highlight, onPage }: Props) {
  const key = `${uploadId}:${page}`;
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const markRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    let active = true;
    apiGet<{ data: SourcePage }>(`/uploads/${uploadId}/pages/${page}`).then(
      (r) => active && setLoaded({ key, page: r.data, error: null }),
      (e) => active && setLoaded({ key, page: null, error: e instanceof ApiError ? e.message : "Could not load the page." }),
    );
    return () => {
      active = false;
    };
  }, [uploadId, page, key]);

  const highlightKey = highlight.join(",");
  useEffect(() => {
    const mark = markRef.current;
    // Only when the pane is on screen: on phones it is a hidden tab, and scrolling would move the whole page.
    if (mark && mark.offsetParent !== null) mark.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [highlightKey, loaded]);

  const current = loaded?.key === key ? loaded : null;
  const doc = current?.page;
  const hl = new Set(highlight);
  const firstMarkedId = doc?.spans.find((s) => hl.has(s.id))?.id;

  return (
    <section className="card source-pane" aria-labelledby="source-title">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 id="source-title" style={{ margin: 0 }}>
          Source{doc ? `: ${doc.file_name}` : ""}
        </h2>
        {doc?.page_count && doc.page_count > 1 && (
          <div className="row" role="group" aria-label="Page">
            <button disabled={page <= 1} onClick={() => onPage(page - 1)} aria-label="Previous page">
              ‹
            </button>
            <span className="meta">
              Page {page} of {doc.page_count}
            </span>
            <button disabled={page >= doc.page_count} onClick={() => onPage(page + 1)} aria-label="Next page">
              ›
            </button>
          </div>
        )}
      </div>
      {!current && <div className="skeleton" aria-busy="true" style={{ marginTop: 12 }} />}
      {current?.error && (
        <div className="banner error" role="alert" style={{ marginTop: 12 }}>
          {current.error}
        </div>
      )}
      {doc?.state === "FAILED" && (
        <div className="banner warning" style={{ marginTop: 12 }}>
          This page could not be read ({doc.error?.message}). Enter its values from the image or the original file.
        </div>
      )}
      {doc?.image_url && (
        // eslint-disable-next-line @next/next/no-img-element
        <img className="source-image" src={doc.image_url} alt={`Page ${page} of ${doc.file_name}`} />
      )}
      {doc && doc.spans.length > 0 && (
        <div className="source-text" tabIndex={0} aria-label="Text read from the page">
          {doc.spans.map((s) => {
            const marked = hl.has(s.id);
            const ref = s.id === firstMarkedId ? (el: HTMLElement | null) => void (markRef.current = el) : undefined;
            return marked ? (
              <mark key={s.id} ref={ref} className="span marked" data-span={s.id}>
                {s.text}
              </mark>
            ) : (
              <span key={s.id} className="span" data-span={s.id}>
                {s.text}
              </span>
            );
          })}
        </div>
      )}
      {doc?.parser.startsWith("ocr:") && (
        <p className="meta" style={{ marginTop: 8 }}>
          Text transcribed by {doc.parser.slice(4)}. Check values against the image.
        </p>
      )}
    </section>
  );
}
