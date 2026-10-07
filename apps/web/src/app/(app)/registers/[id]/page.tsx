"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState } from "react";

import { SourcePane } from "@/components/review/SourcePane";
import { SendSheetEmail } from "@/components/sheets/SendSheetEmail";
import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend } from "@/lib/api";
import {
  cellText, compareMachines, parseCell, REG_FORMATS, registerFileUrl, regKey, type Register, type RegShift,
  type RegShiftView,
} from "@/lib/registers";
import { dayLabel, EMAIL_STATE_LABEL, fmt, SOURCE_LABEL, validNumber } from "@/lib/sheets";

// One pick reading register (WGS-02) laid out like the page: machine rows, the start reading and "reading picks" per
// time, the calculated totals under the worker's written totals. Cells a person must check are highlighted with the
// reason; changes are saved together with "Save changes".


export default function RegisterPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { session } = useSession();
  const [reg, setReg] = useState<Register | null>(null);
  const [error, setError] = useState<{ status: number; text: string } | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [totals, setTotals] = useState<Record<string, string>>({});
  const [confirms, setConfirms] = useState<Set<string>>(new Set());
  const [added, setAdded] = useState<Record<RegShift, string[]>>({ I: [], II: [], III: [] });
  const [newMachine, setNewMachine] = useState("");
  const [day, setDay] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [tab, setTab] = useState<RegShift | null>(null);
  const [photo, setPhoto] = useState<{ upload_id: string; page_no: number } | null>(null);
  const [notice, setNotice] = useState<{ tone: string; text: string; list?: string[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const canEdit = hasRole(session, "UPLOADER", "REVIEWER");
  const isReviewer = hasRole(session, "REVIEWER");
  const canSend = hasRole(session, "REVIEWER", "SENDER", "ADMIN");

  const load = useCallback(async () => {
    try {
      const { data } = await apiGet<{ data: Register }>(`/registers/${id}`);
      setReg(data);
      setError(null);
    } catch (e) {
      setError({ status: e instanceof ApiError ? e.status : 0, text: e instanceof ApiError ? e.message : "The register could not be loaded." });
    }
  }, [id]);

  useEffect(() => {
    const t = setTimeout(() => void load(), 0);
    return () => clearTimeout(t);
  }, [load]);

  const shift = useMemo<RegShift>(() => {
    if (tab) return tab;
    return reg?.shifts.find((s) => s.machines.length > 0)?.shift ?? "I";
  }, [tab, reg]);
  const approved = reg?.state === "APPROVED";
  const editable = !!reg && canEdit && (!approved || isReviewer);
  const parsed = Object.entries(edits).map(([k, v]) => [k, parseCell(v, Number(k.split("|")[2]))] as const);
  const invalid = parsed.filter(([, p]) => !p.ok).map(([k]) => k);
  const badTotals = Object.entries(totals).filter(([, v]) => !validNumber(v)).map(([k]) => k);
  const changed = Object.keys(edits).length + Object.keys(totals).length + confirms.size + (day ? 1 : 0);

  function resetBuffers() {
    setEdits({});
    setTotals({});
    setConfirms(new Set());
    setAdded({ I: [], II: [], III: [] });
    setDay(null);
    setReason("");
  }

  async function save() {
    if (!reg || !changed || invalid.length || badTotals.length) return;
    setBusy(true);
    setNotice(null);
    const body: Record<string, unknown> = {
      values: parsed.map(([k, p]) => {
        const [sh, machine, slot] = k.split("|");
        const v = p.ok ? p.value : { reading: null, picks: null, status: null };
        return { shift: sh, machine, slot: Number(slot), reading: v.reading, picks: v.picks, status: v.status };
      }),
      totals: Object.entries(totals).map(([k, value]) => {
        const [sh, slot] = k.split("|");
        return { shift: sh, slot: Number(slot), value: value.trim() === "" ? null : value.trim().replace(/,/g, "") };
      }),
      confirm: Array.from(confirms).filter((k) => !(k in edits)).map((k) => {
        const [sh, machine, slot] = k.split("|");
        return machine === "TOTAL" ? { shift: sh, slot: Number(slot), total: true } : { shift: sh, machine, slot: Number(slot) };
      }),
    };
    if (day) body.register_date = day;
    if (approved) body.reason = reason;
    try {
      const { data } = await apiSend<{ data: Register }>("PATCH", `/registers/${reg.id}`, body, { ifMatch: `"${reg.version}"` });
      setReg(data);
      resetBuffers();
      setNotice({ tone: "success", text: "Saved." });
    } catch (e) {
      setNotice({ tone: "error", text: e instanceof ApiError ? (e.status === 412 ? "Someone else changed this register. Reload to see the latest values." : e.message) : "Not saved. Try again." });
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (!reg) return;
    setBusy(true);
    setNotice(null);
    try {
      const { data } = await apiSend<{ data: Register }>("POST", `/registers/${reg.id}/approve`, undefined, { ifMatch: `"${reg.version}"` });
      setReg(data);
      setNotice({ tone: "success", text: "Register approved and saved. Downloads and emails now show it as approved." });
    } catch (e) {
      setNotice({ tone: "error", text: e instanceof ApiError ? e.message : "Not approved.", list: e instanceof ApiError ? e.fields.map((f) => f.message) : [] });
    } finally {
      setBusy(false);
    }
  }

  function addMachine(ev: React.FormEvent) {
    ev.preventDefault();
    const m = newMachine.trim().replace(/^0+(?=\d)/, "").toUpperCase();
    if (!/^\d{1,4}[A-Z]?$/.test(m)) {
      setNotice({ tone: "error", text: "Machine number: up to 4 digits, optionally one letter (e.g. 27 or 12A)." });
      return;
    }
    const view = reg?.shifts.find((s) => s.shift === shift);
    if (!view?.machines.some((x) => x.machine === m) && !added[shift].includes(m)) setAdded({ ...added, [shift]: [...added[shift], m] });
    setNewMachine("");
  }

  if (error?.status === 404) return <p>This register does not exist or you do not have access to it. <Link href="/registers">All registers</Link></p>;
  if (!reg) return error ? <div className="banner error" role="alert">{error.text}</div> : <div className="skeleton" aria-busy="true" style={{ height: 160 }} />;
  const view = reg.shifts.find((s) => s.shift === shift)!;
  const toCheck = reg.uncertain + reg.checks;

  return (
    <>
      <p><Link href="/registers">All pick registers</Link></p>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>Pick register · {dayLabel(reg.register_date)}</h1>
        <div className="row">
          {REG_FORMATS.map((f) => <a key={f.key} className="button" href={registerFileUrl(reg.id, f.key)}>Download {f.label}</a>)}
          {canSend && <SendSheetEmail kind="register" sheet={{ id: reg.id, report_date: reg.register_date, department: reg.department, version: reg.version }} onDone={() => void load()} />}
        </div>
      </div>
      <p className="meta">
        {reg.department} · Hourly production reading register (WGS-02) ·{" "}
        <span className={`badge tone-${approved ? "success" : "warning"}`}>{approved ? "Approved" : "To check"}</span>
        {approved && reg.approved_by ? ` by ${reg.approved_by}` : ""} · {reg.sources.length} photo{reg.sources.length === 1 ? "" : "s"} · day total{" "}
        <strong>{fmt(reg.day_total, 0) || "-"}</strong> picks
      </p>

      {notice && (
        <div className={`banner ${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>
          {notice.text}
          {notice.list && notice.list.length > 0 && <ul className="issues">{notice.list.slice(0, 12).map((x) => <li key={x}>{x}</li>)}</ul>}
        </div>
      )}
      {!approved && (toCheck > 0 || !reg.date_confirmed) && (
        <div className="banner warning" role="status">
          {reg.uncertain > 0 && `${reg.uncertain} number${reg.uncertain === 1 ? " was" : "s were"} hard to read. `}
          {reg.checks > 0 && `${reg.checks} cell${reg.checks === 1 ? " does" : "s do"} not add up (picks = reading − previous reading, column totals, shift start = previous shift end). `}
          {toCheck > 0 && "They are highlighted below: correct them or press OK if the page is right. "}
          {!reg.date_confirmed && "The date was not found on the page: confirm it below."}
        </div>
      )}

      <section className="card stack" aria-labelledby="r-head">
        <h2 id="r-head" style={{ margin: 0 }}>Day</h2>
        <div className="form-grid">
          <div>
            <label htmlFor="r-date">Date</label>
            <input id="r-date" type="date" value={day ?? reg.register_date} disabled={!editable}
              aria-invalid={!reg.date_confirmed && !day ? true : undefined} onChange={(e) => setDay(e.target.value)} />
            {!reg.date_confirmed && <p className="reason" style={{ margin: "4px 0 0" }}>Not found on the page; the upload day is shown. Set the right date.</p>}
          </div>
        </div>
        {reg.sources.length > 0 && (
          <div className="row">
            <span className="meta">Photos:</span>
            {reg.sources.map((s) => (
              <button key={`${s.upload_id}-${s.page_no}`} type="button" aria-pressed={photo?.upload_id === s.upload_id && photo.page_no === s.page_no}
                onClick={() => setPhoto(photo?.upload_id === s.upload_id && photo.page_no === s.page_no ? null : s)}>
                Shift {s.shift}: {s.file}{s.page_no > 1 ? ` p${s.page_no}` : ""} ({s.values_read} cells{s.conflicts ? `, ${s.conflicts} different` : ""})
              </button>
            ))}
          </div>
        )}
        {photo && <SourcePane uploadId={photo.upload_id} page={photo.page_no} highlight={[]} onPage={(p) => setPhoto({ ...photo, page_no: p })} />}
      </section>

      <div className="row" role="tablist" aria-label="Shift" style={{ margin: "12px 0" }}>
        {reg.shifts.map((s) => (
          <button key={s.shift} role="tab" aria-selected={s.shift === shift} className={s.shift === shift ? "primary" : undefined} onClick={() => setTab(s.shift)}>
            Shift {s.shift} ({s.hours}){s.machines.length === 0 ? " · no page" : ""}
          </button>
        ))}
      </div>

      <ShiftTable view={view} extra={added[shift]} editable={editable} edits={edits} totals={totals} confirms={confirms}
        onEdit={(k, v) => setEdits((e) => ({ ...e, [k]: v }))} onTotal={(k, v) => setTotals((t) => ({ ...t, [k]: v }))}
        onConfirm={(k) => setConfirms((c) => new Set(c).add(k))} />

      {editable && (
        <form className="row" onSubmit={addMachine} style={{ margin: "8px 0", alignItems: "end" }}>
          <div>
            <label htmlFor="r-add">Add a machine row to shift {shift}</label>
            <input id="r-add" type="text" inputMode="numeric" maxLength={6} value={newMachine} onChange={(e) => setNewMachine(e.target.value)} />
          </div>
          <button type="submit">Add row</button>
        </form>
      )}

      {editable && (
        <div className="card stack save-bar">
          {approved && (
            <div>
              <label htmlFor="r-reason">Reason for changing an approved register</label>
              <input id="r-reason" type="text" className="wide" minLength={5} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
            </div>
          )}
          {(invalid.length > 0 || badTotals.length > 0) && (
            <p className="reason" role="alert" style={{ margin: 0 }}>
              {invalid.length + badTotals.length} cell{invalid.length + badTotals.length === 1 ? " is" : "s are"} not valid: write the reading, then the picks, then a mark (e.g. &quot;2282 24&quot; or &quot;1815 S/C&quot;).
            </p>
          )}
          <div className="row">
            <button className="primary" disabled={busy || !changed || invalid.length > 0 || badTotals.length > 0 || (approved && reason.trim().length < 5)} onClick={save}>
              {busy ? "Saving…" : `Save changes${changed ? ` (${changed})` : ""}`}
            </button>
            {changed > 0 && <button disabled={busy} onClick={resetBuffers}>Discard</button>}
            {isReviewer && !approved && (
              <button className="primary" disabled={busy || changed > 0 || !reg.approvable} onClick={approve}
                title={changed ? "Save your changes first" : undefined}>Approve and save register</button>
            )}
            {!isReviewer && !approved && <span className="meta">A Reviewer approves the register after the values are checked.</span>}
          </div>
        </div>
      )}

      {(reg.findings.length > 0 || reg.notes.length > 0) && (
        <section className="card" aria-labelledby="r-notes">
          <h2 id="r-notes">Notes</h2>
          <dl className="kv">
            {[...reg.findings, ...reg.notes].map((n, i) => <div key={i}><dt className="meta">{n.label}</dt><dd style={{ overflowWrap: "anywhere" }}>{n.text}</dd></div>)}
          </dl>
        </section>
      )}

      <section className="card" aria-labelledby="r-mail">
        <h2 id="r-mail">Emails</h2>
        {reg.emails.length === 0 ? <p className="meta">Not emailed yet.</p> : (
          <ul className="filelist">
            {reg.emails.map((e) => (
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

      {reg.changes.length > 0 && (
        <section className="card" aria-labelledby="r-hist">
          <h2 id="r-hist">Change history</h2>
          <ol>
            {reg.changes.map((c, i) => (
              <li key={i}>
                {new Date(c.at).toLocaleString("en-IN", { timeZone: session.timezone })} · Shift {c.shift}
                {c.machine === "TOTAL" ? ` total ${c.time}` : `, machine ${c.machine}, ${c.time} ${c.field}`}: {c.old ?? "empty"} → {c.new ?? "empty"}
                {c.by ? ` · ${c.by}` : ""}{c.reason ? ` · ${c.reason}` : ""}
              </li>
            ))}
          </ol>
        </section>
      )}
    </>
  );
}

function ShiftTable({ view, extra, editable, edits, totals, confirms, onEdit, onTotal, onConfirm }: {
  view: RegShiftView; extra: string[]; editable: boolean; edits: Record<string, string>; totals: Record<string, string>;
  confirms: Set<string>; onEdit: (key: string, value: string) => void; onTotal: (key: string, value: string) => void;
  onConfirm: (key: string) => void;
}) {
  const id = `shift-${view.shift}`;
  const machines = [...view.machines.map((m) => m.machine), ...extra].sort(compareMachines);
  const byMachine = new Map(view.machines.map((m) => [m.machine, m]));
  return (
    <section className="card table-scroll" role="region" aria-labelledby={id} tabIndex={0}>
      <h2 id={id} style={{ marginTop: 0 }}>Shift {view.shift} · {view.hours}</h2>
      <p className="meta" style={{ marginTop: 0 }}>
        Each cell: meter reading, then the picks written under it, then a mark if any (e.g. &quot;2282 24&quot;, &quot;B.FALL&quot;).
        {" "}{view.times[0]} is the start reading carried over from the previous shift.
      </p>
      {machines.length === 0 && <p>No page of this shift yet. Photograph it, or add machine rows below and type the values.</p>}
      {machines.length > 0 && (
        <table className="data sheet-table">
          <thead>
            <tr>
              <th scope="col">M/c</th>
              {view.times.map((t, k) => <th key={t + k} scope="col">{t}{k === 0 ? " (start)" : ""}</th>)}
              <th scope="col">Total picks</th>
            </tr>
          </thead>
          <tbody>
            {machines.map((m) => {
              const row = byMachine.get(m);
              return (
                <tr key={m}>
                  <th scope="row">{m}</th>
                  {view.times.map((t, k) => {
                    const cell = row?.cells[k];
                    const key = regKey(view.shift, m, k);
                    const value = key in edits ? edits[key] : cellText(cell);
                    const reasons = [...(cell?.uncertain ? [cell.note ?? "Check against the photo."] : []), ...(cell?.checks ?? []).map((c) => c.text)];
                    const check = reasons.length > 0 && !confirms.has(key) && !(key in edits);
                    const parsedOk = !(key in edits) || parseCell(edits[key] ?? "", k).ok;
                    const help = `${key}-help`;
                    return (
                      <td key={t + k} className={check ? "cell-check" : undefined}>
                        <label className="sr-only" htmlFor={key}>{`Shift ${view.shift}, machine ${m}, ${t}${k === 0 ? " start reading" : ": reading and picks"}`}</label>
                        <input id={key} className="cell-input tnum" value={value} disabled={!editable} autoComplete="off"
                          aria-invalid={check || !parsedOk ? true : undefined}
                          aria-describedby={check || cell?.source ? help : undefined}
                          onChange={(e) => onEdit(key, e.target.value)} />
                        {check ? (
                          <div id={help} className="reason" style={{ fontSize: 12 }}>
                            {reasons.join(" ")}
                            {editable && <> <button type="button" className="linklike" onClick={() => onConfirm(key)}>OK</button></>}
                          </div>
                        ) : cell?.source ? (
                          <span id={help} className="sr-only">{SOURCE_LABEL[cell.source]}{cell.status_label ? `; ${cell.status_label}` : ""}{cell.accepted.length ? "; checked and accepted" : ""}</span>
                        ) : null}
                      </td>
                    );
                  })}
                  <td className="tnum"><strong>{fmt(row?.total, 0)}</strong></td>
                </tr>
              );
            })}
          </tbody>
          <tfoot>
            <tr className="calc-row">
              <th scope="row">Total (calculated)</th>
              {view.columns.map((c) => <td key={c.slot} className="tnum">{c.slot === 0 ? "" : fmt(c.calculated, 0)}</td>)}
              <td className="tnum"><strong>{fmt(view.total, 0)}</strong></td>
            </tr>
            <tr>
              <th scope="row">Total (written)</th>
              {view.columns.map((c) => {
                const key = `${view.shift}|${c.slot}`;
                const value = key in totals ? totals[key] : c.written ?? "";
                const confirmKey = regKey(view.shift, "TOTAL", c.slot);
                const mismatch = c.match === false && !(key in totals) && (c.check !== null || c.slot === 0) && !confirms.has(confirmKey);
                return (
                  <td key={c.slot} className={mismatch ? "cell-check" : undefined}>
                    <label className="sr-only" htmlFor={`tot-${key}`}>{`Shift ${view.shift}, total written under ${c.time}${c.slot === 0 ? " (shift or day total)" : ""}`}</label>
                    <input id={`tot-${key}`} className="cell-input tnum" inputMode="decimal" value={value} disabled={!editable}
                      aria-invalid={mismatch || (key in totals && !validNumber(totals[key] ?? "")) ? true : undefined}
                      aria-describedby={c.written !== null ? `tot-${key}-m` : undefined} onChange={(e) => onTotal(key, e.target.value)} />
                    {c.written !== null && c.match !== null && (
                      <div id={`tot-${key}-m`} className={c.match || !mismatch ? "meta" : "reason"} style={{ fontSize: 12 }}>
                        {c.match ? "✓ matches" : `≠ ${c.slot === 0 ? "shift total" : "calculated"} ${fmt(c.slot === 0 ? view.total : c.calculated, 0)}`}
                        {!c.match && c.accepted && " · checked"}
                        {c.check && !confirms.has(confirmKey) && !(key in totals) && (
                          <>
                            {" "}{c.check}
                            {editable && <> <button type="button" className="linklike" onClick={() => onConfirm(confirmKey)}>OK</button></>}
                          </>
                        )}
                      </div>
                    )}
                  </td>
                );
              })}
              <td />
            </tr>
            <tr className="calc-row">
              <th scope="row">M/c stop</th>
              {view.columns.map((c) => <td key={c.slot} className="tnum">{c.stopped}</td>)}
              <td />
            </tr>
          </tfoot>
        </table>
      )}
    </section>
  );
}
