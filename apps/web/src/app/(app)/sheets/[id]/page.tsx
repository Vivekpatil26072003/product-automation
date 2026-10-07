"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState } from "react";

import { SourcePane } from "@/components/review/SourcePane";
import { SendSheetEmail } from "@/components/sheets/SendSheetEmail";
import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend } from "@/lib/api";
import {
  cellKey, dayLabel, EMAIL_STATE_LABEL, fileUrl, fmt, FORMATS, type Sheet, type SheetSection, SOURCE_LABEL, validNumber,
} from "@/lib/sheets";

// One daily sheet: the values read from the photos in the company's layout, highlighted where a person must check,
// calculated columns, approval, downloads and email. Changes are saved together with "Save changes".

const SHIFT_LABEL: Record<string, string> = { I: "I", II: "II", III: "III", D: "Today" };

export default function SheetPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { session } = useSession();
  const [sheet, setSheet] = useState<Sheet | null>(null);
  const [error, setError] = useState<{ status: number; text: string } | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [confirms, setConfirms] = useState<Set<string>>(new Set());
  const [supervisors, setSupervisors] = useState<Record<string, string> | null>(null);
  const [day, setDay] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [group, setGroup] = useState("Weaving");
  const [photo, setPhoto] = useState<{ upload_id: string; page_no: number } | null>(null);
  const [notice, setNotice] = useState<{ tone: string; text: string; list?: string[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const canEdit = hasRole(session, "UPLOADER", "REVIEWER");
  const isReviewer = hasRole(session, "REVIEWER");
  const canSend = hasRole(session, "REVIEWER", "SENDER", "ADMIN");

  const load = useCallback(async () => {
    try {
      const { data } = await apiGet<{ data: Sheet }>(`/sheets/${id}`);
      setSheet(data);
      setError(null);
    } catch (e) {
      setError({ status: e instanceof ApiError ? e.status : 0, text: e instanceof ApiError ? e.message : "The sheet could not be loaded." });
    }
  }, [id]);

  useEffect(() => {
    const t = setTimeout(() => void load(), 0);
    return () => clearTimeout(t);
  }, [load]);

  const groups = useMemo(() => Array.from(new Set((sheet?.sections ?? []).map((s) => s.group))), [sheet]);
  const approved = sheet?.state === "APPROVED";
  const editable = !!sheet && canEdit && (!approved || isReviewer);
  const changed = Object.keys(edits).length + confirms.size + (supervisors ? 1 : 0) + (day ? 1 : 0);
  const invalid = Object.entries(edits).filter(([, v]) => !validNumber(v)).map(([k]) => k);

  function resetBuffers() {
    setEdits({});
    setConfirms(new Set());
    setSupervisors(null);
    setDay(null);
    setReason("");
  }

  async function save() {
    if (!sheet || !changed || invalid.length) return;
    setBusy(true);
    setNotice(null);
    const body: Record<string, unknown> = {
      values: Object.entries(edits).map(([k, value]) => {
        const [section, metric, shift] = k.split("|");
        return { section, metric, shift, value: value.trim() === "" ? null : value.trim().replace(/,/g, "") };
      }),
      confirm: Array.from(confirms).filter((k) => !(k in edits)).map((k) => {
        const [section, metric, shift] = k.split("|");
        return { section, metric, shift };
      }),
    };
    if (supervisors) body.shifts = Object.fromEntries(Object.entries(supervisors).map(([sh, name]) => [sh, { supervisor: name }]));
    if (day) body.report_date = day;
    if (approved) body.reason = reason;
    try {
      const { data } = await apiSend<{ data: Sheet }>("PATCH", `/sheets/${sheet.id}`, body, { ifMatch: `"${sheet.version}"` });
      setSheet(data);
      resetBuffers();
      setNotice({ tone: "success", text: "Saved." });
    } catch (e) {
      setNotice({ tone: "error", text: e instanceof ApiError ? (e.status === 412 ? "Someone else changed this sheet. Reload to see the latest values." : e.message) : "Not saved. Try again." });
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (!sheet) return;
    setBusy(true);
    setNotice(null);
    try {
      const { data } = await apiSend<{ data: Sheet }>("POST", `/sheets/${sheet.id}/approve`, undefined, { ifMatch: `"${sheet.version}"` });
      setSheet(data);
      setNotice({ tone: "success", text: "Sheet approved and saved. Downloads and emails now show it as approved." });
    } catch (e) {
      setNotice({ tone: "error", text: e instanceof ApiError ? e.message : "Not approved.", list: e instanceof ApiError ? e.fields.map((f) => f.message) : [] });
    } finally {
      setBusy(false);
    }
  }

  if (error?.status === 404) return <p>This sheet does not exist or you do not have access to it. <Link href="/sheets">All sheets</Link></p>;
  if (!sheet) return error ? <div className="banner error" role="alert">{error.text}</div> : <div className="skeleton" aria-busy="true" style={{ height: 160 }} />;

  const sups = supervisors ?? Object.fromEntries((["I", "II", "III"] as const).map((sh) => [sh, sheet.shifts[sh]?.supervisor ?? ""]));

  return (
    <>
      <p><Link href="/sheets">All daily sheets</Link></p>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>Daily sheet · {dayLabel(sheet.report_date)}</h1>
        <div className="row">
          {FORMATS.map((f) => <a key={f.key} className="button" href={fileUrl(sheet.id, f.key)}>Download {f.label}</a>)}
          {canSend && <SendSheetEmail sheet={sheet} onDone={() => void load()} />}
        </div>
      </div>
      <p className="meta">
        {sheet.department} · <span className={`badge tone-${approved ? "success" : "warning"}`}>{approved ? "Approved" : "To check"}</span>
        {approved && sheet.approved_by ? ` by ${sheet.approved_by}` : ""} · {sheet.sources.length} photo{sheet.sources.length === 1 ? "" : "s"}
      </p>

      {notice && (
        <div className={`banner ${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>
          {notice.text}
          {notice.list && notice.list.length > 0 && <ul className="issues">{notice.list.slice(0, 12).map((x) => <li key={x}>{x}</li>)}</ul>}
        </div>
      )}
      {!approved && (sheet.uncertain > 0 || !sheet.date_confirmed) && (
        <div className="banner warning" role="status">
          {sheet.uncertain > 0 && `${sheet.uncertain} value${sheet.uncertain === 1 ? " was" : "s were"} hard to read: they are highlighted below. Correct them or press OK. `}
          {!sheet.date_confirmed && "The date was not found on the page: confirm it below."}
        </div>
      )}

      <section className="card stack" aria-labelledby="s-head">
        <h2 id="s-head" style={{ margin: 0 }}>Day and shifts</h2>
        <div className="form-grid">
          <div>
            <label htmlFor="s-date">Date</label>
            <input id="s-date" type="date" value={day ?? sheet.report_date} disabled={!editable}
              aria-invalid={!sheet.date_confirmed && !day ? true : undefined} onChange={(e) => setDay(e.target.value)} />
            {!sheet.date_confirmed && <p className="reason" style={{ margin: "4px 0 0" }}>Not found on the page; the upload day is shown. Set the right date.</p>}
          </div>
          {(["I", "II", "III"] as const).map((sh) => (
            <div key={sh}>
              <label htmlFor={`s-sup-${sh}`}>Shift {sh} supervisor</label>
              <input id={`s-sup-${sh}`} type="text" value={sups[sh] ?? ""} disabled={!editable} maxLength={80}
                onChange={(e) => setSupervisors({ ...sups, [sh]: e.target.value })} />
            </div>
          ))}
        </div>
        {sheet.sources.length > 0 && (
          <div className="row">
            <span className="meta">Photos:</span>
            {sheet.sources.map((s) => (
              <button key={`${s.upload_id}-${s.page_no}`} type="button" aria-pressed={photo?.upload_id === s.upload_id && photo.page_no === s.page_no}
                onClick={() => setPhoto(photo?.upload_id === s.upload_id && photo.page_no === s.page_no ? null : s)}>
                {s.file}{s.page_no > 1 ? ` p${s.page_no}` : ""} ({s.values_read} values{s.conflicts ? `, ${s.conflicts} different` : ""})
              </button>
            ))}
          </div>
        )}
        {photo && <SourcePane uploadId={photo.upload_id} page={photo.page_no} highlight={[]} onPage={(p) => setPhoto({ ...photo, page_no: p })} />}
      </section>

      <div className="row" role="tablist" aria-label="Sheet part" style={{ margin: "12px 0" }}>
        {groups.map((g) => (
          <button key={g} role="tab" aria-selected={g === group} className={g === group ? "primary" : undefined} onClick={() => setGroup(g)}>{g}</button>
        ))}
      </div>

      {sheet.sections.filter((s) => s.group === group).map((s) => (
        <SectionTable key={s.key} section={s} editable={editable} edits={edits} confirms={confirms}
          onEdit={(k, v) => setEdits((e) => ({ ...e, [k]: v }))}
          onConfirm={(k) => setConfirms((c) => new Set(c).add(k))} />
      ))}

      {editable && (
        <div className="card stack save-bar">
          {approved && (
            <div>
              <label htmlFor="s-reason">Reason for changing an approved sheet</label>
              <input id="s-reason" type="text" className="wide" minLength={5} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
            </div>
          )}
          {invalid.length > 0 && <p className="reason" role="alert" style={{ margin: 0 }}>{invalid.length} value{invalid.length === 1 ? " is" : "s are"} not a number (up to 4 decimals).</p>}
          <div className="row">
            <button className="primary" disabled={busy || !changed || invalid.length > 0 || (approved && reason.trim().length < 5)} onClick={save}>
              {busy ? "Saving…" : `Save changes${changed ? ` (${changed})` : ""}`}
            </button>
            {changed > 0 && <button disabled={busy} onClick={resetBuffers}>Discard</button>}
            {isReviewer && !approved && (
              <button className="primary" disabled={busy || changed > 0 || !sheet.approvable} onClick={approve}
                title={changed ? "Save your changes first" : undefined}>Approve and save sheet</button>
            )}
            {!isReviewer && !approved && <span className="meta">A Reviewer approves the sheet after the values are checked.</span>}
          </div>
        </div>
      )}

      {sheet.notes.length > 0 && (
        <section className="card" aria-labelledby="s-notes">
          <h2 id="s-notes">Notes from the page</h2>
          <dl className="kv">{sheet.notes.map((n, i) => <div key={i}><dt className="meta">{n.label}</dt><dd style={{ overflowWrap: "anywhere" }}>{n.text}</dd></div>)}</dl>
        </section>
      )}

      <section className="card" aria-labelledby="s-mail">
        <h2 id="s-mail">Emails</h2>
        {sheet.emails.length === 0 ? <p className="meta">Not emailed yet.</p> : (
          <ul className="filelist">
            {sheet.emails.map((e) => (
              <li key={e.id}>
                <div>
                  <div style={{ overflowWrap: "anywhere" }}>{e.to_email} · {e.attachment}</div>
                  <div className="meta">{new Date(e.at).toLocaleString("en-IN", { timeZone: session.timezone })}{e.by ? ` · ${e.by}` : ""}{e.error?.message ? ` · ${e.error.message}` : ""}</div>
                </div>
                <span className={`badge tone-${EMAIL_STATE_LABEL[e.state].tone}`}>{EMAIL_STATE_LABEL[e.state].label}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {sheet.changes.length > 0 && (
        <section className="card" aria-labelledby="s-hist">
          <h2 id="s-hist">Change history</h2>
          <ol>
            {sheet.changes.map((c, i) => {
              const sec = sheet.sections.find((s) => s.key === c.section);
              const label = sec?.rows.find((r) => r.metric === c.metric)?.label ?? c.metric;
              return (
                <li key={i}>
                  {new Date(c.at).toLocaleString("en-IN", { timeZone: session.timezone })} · {sec?.title}: {label} ({SHIFT_LABEL[c.shift]}) {fmt(c.old) || "empty"} → {fmt(c.new) || "empty"}
                  {c.by ? ` · ${c.by}` : ""}{c.reason ? ` · ${c.reason}` : ""}
                </li>
              );
            })}
          </ol>
        </section>
      )}
    </>
  );
}

function SectionTable({ section, editable, edits, confirms, onEdit, onConfirm }: {
  section: SheetSection; editable: boolean; edits: Record<string, string>; confirms: Set<string>;
  onEdit: (key: string, value: string) => void; onConfirm: (key: string) => void;
}) {
  const day = section.shifts.length === 1;
  const id = `sec-${section.key}`;
  return (
    <section className="card table-scroll" role="region" aria-labelledby={id} tabIndex={0}>
      <h2 id={id} style={{ marginTop: 0 }}>{section.title}</h2>
      {Object.keys(section.params).length > 0 && (
        <p className="meta" style={{ marginTop: 0 }}>
          Installed looms {section.params.installed} · {section.params.rate} picks/hour × 8 h (efficiency) · theoretical picks row uses {section.params.theo_rate} picks/hour
        </p>
      )}
      <table className="data sheet-table">
        <thead>
          <tr>
            <th scope="col">Row</th><th scope="col">Target</th>
            {section.shifts.map((sh) => <th key={sh} scope="col">{SHIFT_LABEL[sh]}</th>)}
            {!day && <th scope="col">Total</th>}<th scope="col">To date</th>
          </tr>
        </thead>
        <tbody>
          {section.rows.map((r) => (
            <tr key={r.metric} className={r.kind === "derived" ? "calc-row" : undefined}>
              <th scope="row" style={{ fontWeight: r.kind === "derived" ? 400 : 600 }}>
                {r.label}{r.unit ? <span className="meta"> ({r.unit})</span> : null}
                {r.kind === "derived" && <span className="meta"> · calculated</span>}
              </th>
              <td className="tnum meta">{fmt(r.target)}</td>
              {section.shifts.map((sh) => {
                const cell = r.cells[sh];
                const key = cellKey(section.key, r.metric, sh);
                if (r.kind === "derived") return <td key={sh} className="tnum">{fmt(cell?.value)}</td>;
                const value = key in edits ? edits[key] : cell?.value ?? "";
                const check = !!cell?.uncertain && !confirms.has(key) && !(key in edits);
                const help = `${key}-help`;
                return (
                  <td key={sh} className={check ? "cell-check" : undefined}>
                    <label className="sr-only" htmlFor={key}>{`${section.title}: ${r.label}, shift ${SHIFT_LABEL[sh]}`}</label>
                    <input id={key} className="cell-input tnum" inputMode="decimal" value={value} disabled={!editable}
                      aria-invalid={check || (key in edits && !validNumber(edits[key] ?? "")) ? true : undefined}
                      aria-describedby={check || cell?.source ? help : undefined}
                      onChange={(e) => onEdit(key, e.target.value)} />
                    {check ? (
                      <div id={help} className="reason" style={{ fontSize: 12 }}>
                        {cell?.note ?? "Check against the photo."}
                        {editable && <> <button type="button" className="linklike" onClick={() => onConfirm(key)}>OK</button></>}
                      </div>
                    ) : cell?.source ? (
                      <span id={help} className="sr-only">{SOURCE_LABEL[cell.source]}{cell.raw ? `: ${cell.raw}` : ""}</span>
                    ) : null}
                  </td>
                );
              })}
              {!day && <td className="tnum"><strong>{fmt(r.total)}</strong></td>}
              <td className="tnum">{fmt(r.to_date)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
