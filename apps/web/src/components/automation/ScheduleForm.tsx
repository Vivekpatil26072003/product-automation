"use client";

import { useEffect, useState } from "react";

import { useSession } from "@/components/SessionProvider";
import { ApiError, type Issue, apiGet } from "@/lib/api";
import { type Occurrence, type Schedule, cadenceText } from "@/lib/automation";
import { parseRecipients } from "@/lib/reports";

// U9 schedule editor. Save stays disabled until the cadence is valid; the next three runs come from the server
// (the same code that claims them), so the preview matches what will actually happen.

export type ScheduleBody = Record<string, unknown>;
type Form = {
  name: string; cadence: "DAILY" | "WEEKLY" | "MONTHLY"; local_time: string; weekday: string; monthday: string;
  timezone: string; departments: string[]; unit: string; title: string; include_detail: boolean;
  to: string; cc: string; bcc: string; subject: string; mode: "DRAFT_ONLY" | "AUTO_SEND"; empty_policy: "SKIP" | "DRAFT";
};

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function formOf(s: Schedule | null, tz: string): Form {
  const c = s?.config;
  return {
    name: s?.name ?? "Daily production report", cadence: c?.cadence ?? "DAILY", local_time: c?.local_time ?? "08:00",
    weekday: String(c?.weekday ?? 1), monthday: String(c?.monthday ?? 1), timezone: c?.timezone ?? tz,
    departments: c?.department_ids ?? [], unit: c?.units[0] ?? "", title: c?.title ?? "Daily Production Report",
    include_detail: c?.include_detail ?? true, to: (s?.to ?? []).join(", "), cc: (s?.cc ?? []).join(", "),
    bcc: (s?.bcc ?? []).join(", "), subject: c?.subject ?? "", mode: c?.mode ?? "DRAFT_ONLY", empty_policy: c?.empty_policy ?? "SKIP",
  };
}

export function ScheduleForm({ initial, busy, issues, onSubmit, submitLabel }: {
  initial: Schedule | null;
  busy: boolean;
  issues: Issue[];
  onSubmit: (body: ScheduleBody) => void;
  submitLabel: string;
}) {
  const { session } = useSession();
  const [f, setF] = useState<Form>(() => formOf(initial, session.timezone));
  const [preview, setPreview] = useState<{ key: string; runs: Occurrence[] | null; error: string | null } | null>(null);
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF((x) => ({ ...x, [k]: v }));
  const err = (name: string) => issues.filter((i) => i.field === name || i.field?.startsWith(`${name}[`)).map((i) => i.message).join(" ");

  const query = new URLSearchParams({ cadence: f.cadence, local_time: f.local_time, timezone: f.timezone });
  if (f.cadence === "WEEKLY") query.set("weekday", f.weekday);
  if (f.cadence === "MONTHLY") query.set("monthday", f.monthday);
  const key = query.toString();

  useEffect(() => {
    let active = true;
    const timer = setTimeout(() => {
      apiGet<{ data: Occurrence[] }>(`/schedules/preview?${key}`).then(
        (r) => active && setPreview({ key, runs: r.data, error: null }),
        (e) => active && setPreview({ key, runs: null, error: e instanceof ApiError ? (e.fields[0]?.message ?? e.message) : "Invalid schedule." }),
      );
    }, 300);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [key]);

  const current = preview?.key === key ? preview : null;
  const valid = !!current?.runs && f.name.trim() !== "" && (f.mode === "DRAFT_ONLY" || parseRecipients(f.to).length > 0);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    onSubmit({
      name: f.name, cadence: f.cadence, local_time: f.local_time, timezone: f.timezone,
      weekday: f.cadence === "WEEKLY" ? Number(f.weekday) : null, monthday: f.cadence === "MONTHLY" ? Number(f.monthday) : null,
      department_ids: f.departments, units: f.unit ? [f.unit] : [], title: f.title, include_detail: f.include_detail,
      to: parseRecipients(f.to), cc: parseRecipients(f.cc), bcc: parseRecipients(f.bcc), subject: f.subject || null,
      mode: f.mode, empty_policy: f.empty_policy,
    });
  }

  const check = (label: string, checked: boolean, onChange: (v: boolean) => void) => (
    <label style={{ fontWeight: 400, display: "inline-flex", gap: 6, alignItems: "center" }}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} /> {label}
    </label>
  );

  return (
    <form className="card stack" onSubmit={submit} aria-label="Schedule">
      <div><label htmlFor="s-name">Name</label><input id="s-name" type="text" className="wide" value={f.name} maxLength={120} onChange={(e) => set("name", e.target.value)} /></div>
      <div className="row">
        <div>
          <label htmlFor="s-cad">Repeat</label>
          <select id="s-cad" value={f.cadence} onChange={(e) => set("cadence", e.target.value as Form["cadence"])}>
            <option value="DAILY">Daily</option><option value="WEEKLY">Weekly</option><option value="MONTHLY">Monthly</option>
          </select>
        </div>
        {f.cadence === "WEEKLY" && (
          <div>
            <label htmlFor="s-wd">Day</label>
            <select id="s-wd" value={f.weekday} onChange={(e) => set("weekday", e.target.value)}>
              {WEEKDAYS.map((d, i) => <option key={d} value={i + 1}>{d}</option>)}
            </select>
          </div>
        )}
        {f.cadence === "MONTHLY" && (
          <div><label htmlFor="s-md">Day of month</label><input id="s-md" type="number" min={1} max={31} value={f.monthday} onChange={(e) => set("monthday", e.target.value)} /></div>
        )}
        <div><label htmlFor="s-time">Time</label><input id="s-time" type="time" value={f.local_time} onChange={(e) => set("local_time", e.target.value)} /></div>
        <div><label htmlFor="s-tz">Time zone</label><input id="s-tz" type="text" value={f.timezone} onChange={(e) => set("timezone", e.target.value)} /></div>
      </div>
      <div className="banner info" role="status" style={{ margin: 0 }}>
        {current?.error ? <span className="reason">{current.error}</span> : (
          <>
            <strong>{cadenceText({ cadence: f.cadence, local_time: f.local_time, weekday: Number(f.weekday), monthday: Number(f.monthday), timezone: f.timezone })}.</strong>
            {current?.runs && (
              <ul style={{ margin: "6px 0 0", paddingLeft: 18 }} aria-label="Next three runs">
                {current.runs.map((r) => (
                  <li key={r.due_at}>
                    {new Date(r.due_at).toLocaleString("en-IN", { timeZone: f.timezone, dateStyle: "medium", timeStyle: "short" })} · reports {r.period_start === r.period_end ? r.period_start : `${r.period_start} to ${r.period_end}`}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
      <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
        <legend style={{ fontWeight: 600 }}>Departments</legend>
        <p className="meta" style={{ margin: "0 0 4px" }}>None selected means all departments you can see now.</p>
        <div className="row">
          {session.departments.map((d) => check(d.name, f.departments.includes(d.id), (on) => set("departments", on ? [...f.departments, d.id] : f.departments.filter((x) => x !== d.id))))}
        </div>
        {err("department_ids") && <p className="reason">{err("department_ids")}</p>}
      </fieldset>
      <div className="row">
        <div>
          <label htmlFor="s-unit">Unit</label>
          <select id="s-unit" value={f.unit} onChange={(e) => set("unit", e.target.value)}>
            <option value="">All units (shown separately)</option><option value="m">m</option><option value="kg">kg</option><option value="pcs">pcs</option>
          </select>
        </div>
        <div>
          <label htmlFor="s-empty">When there are no approved records</label>
          <select id="s-empty" value={f.empty_policy} onChange={(e) => set("empty_policy", e.target.value as Form["empty_policy"])}>
            <option value="SKIP">Skip the run (recommended)</option><option value="DRAFT">Create an empty report and draft</option>
          </select>
        </div>
      </div>
      <div><label htmlFor="s-title">Report title</label><input id="s-title" type="text" className="wide" value={f.title} maxLength={200} onChange={(e) => set("title", e.target.value)} /></div>
      {check("Include record detail in the PDF", f.include_detail, (v) => set("include_detail", v))}
      {(["to", "cc", "bcc"] as const).map((k) => (
        <div key={k}>
          <label htmlFor={`s-${k}`}>{k === "to" ? "To" : k === "cc" ? "Cc" : "Bcc"}</label>
          <textarea id={`s-${k}`} className="wide" rows={2} value={f[k]} aria-invalid={err(k) ? true : undefined} onChange={(e) => set(k, e.target.value)} />
          <p className={err(k) ? "reason" : "meta"} style={{ margin: "4px 0 0" }}>{err(k) || "Separate addresses with commas or new lines."}</p>
        </div>
      ))}
      <div><label htmlFor="s-subj">Subject (optional)</label><input id="s-subj" type="text" className="wide" maxLength={200} value={f.subject} onChange={(e) => set("subject", e.target.value)} /></div>
      <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
        <legend style={{ fontWeight: 600 }}>After the report is ready</legend>
        <label style={{ fontWeight: 400, display: "block" }}><input type="radio" name="mode" checked={f.mode === "DRAFT_ONLY"} onChange={() => set("mode", "DRAFT_ONLY")} /> Prepare an email draft for me to review and send</label>
        <label style={{ fontWeight: 400, display: "block" }}><input type="radio" name="mode" checked={f.mode === "AUTO_SEND"} onChange={() => set("mode", "AUTO_SEND")} /> Send automatically, only after I approve the recipients and settings</label>
      </fieldset>
      <div className="row">
        <button type="submit" className="primary" disabled={busy || !valid}>{busy ? "Saving…" : submitLabel}</button>
        {!valid && <span className="meta">{f.mode === "AUTO_SEND" && !parseRecipients(f.to).length ? "Automatic sending needs a To recipient." : "Check the schedule timing."}</span>}
      </div>
    </form>
  );
}
