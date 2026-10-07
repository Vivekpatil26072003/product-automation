"use client";

import { useEffect, useState } from "react";

import { hasRole, useSession } from "@/components/SessionProvider";
import { ApiError, type Issue, apiGet, apiSend } from "@/lib/api";

// Company automation settings (administrators): optional modules, reminder stages, exception thresholds,
// submission cutoff and working days, internal email domains.

type Settings = {
  version: number; submission_cutoff_local_time: string; working_days: number[]; internal_email_domains: string[];
  feature_flags: { scheduling: boolean; auto_send: boolean; erp_integration: boolean };
  reminders: { enabled: boolean; first_after_minutes: number; second_after_minutes: number; escalate_after_minutes: number; email: boolean };
  exception_rules: { low_achievement_pct: number; high_achievement_pct: number; stop_minutes: number; lookback_days: number };
};
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export default function AutomationSettingsPage() {
  const { session } = useSession();
  const [s, setS] = useState<Settings | null>(null);
  const [etag, setEtag] = useState("");
  const [issues, setIssues] = useState<Issue[]>([]);
  const [message, setMessage] = useState<{ tone: string; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    fetch("/api/v1/settings", { credentials: "same-origin", cache: "no-store" }).then(async (res) => {
      if (!active) return;
      if (!res.ok) return setMessage({ tone: "error", text: "Settings could not be loaded." });
      setEtag(res.headers.get("ETag") ?? "");
      setS((await res.json()).data);
    });
    return () => {
      active = false;
    };
  }, []);

  if (!hasRole(session, "ADMIN")) return <p>Only administrators can change automation settings.</p>;
  if (!s) return message ? <div className="banner error" role="alert">{message.text}</div> : <div className="skeleton" aria-busy="true" style={{ height: 120 }} />;

  const up = <K extends keyof Settings>(k: K, v: Settings[K]) => setS((x) => (x ? { ...x, [k]: v } : x));
  const num = (v: string) => (v === "" ? 0 : Number(v));

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!s) return;
    setBusy(true);
    setIssues([]);
    try {
      await apiSend("PATCH", "/settings", {
        feature_flags: s.feature_flags, reminders: s.reminders, exception_rules: s.exception_rules,
        submission_cutoff_local_time: s.submission_cutoff_local_time, working_days: s.working_days,
        internal_email_domains: s.internal_email_domains,
      }, { ifMatch: etag });
      const res = await apiGet<{ data: Settings }>("/settings");
      setS(res.data);
      setEtag(`"${res.data.version}"`);
      setMessage({ tone: "info", text: "Saved." });
    } catch (err) {
      setIssues(err instanceof ApiError ? err.fields : []);
      setMessage({ tone: "error", text: err instanceof ApiError ? err.message : "Not saved." });
    } finally {
      setBusy(false);
    }
  }

  const bad = (prefix: string) => issues.filter((i) => i.field?.includes(prefix)).map((i) => i.message).join(" ");
  const box = (label: string, checked: boolean, onChange: (v: boolean) => void) => (
    <label style={{ fontWeight: 400, display: "inline-flex", gap: 6, alignItems: "center" }}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} /> {label}
    </label>
  );
  const field = (id: string, label: string, value: number, onChange: (v: number) => void, max: number) => (
    <div><label htmlFor={id}>{label}</label><input id={id} type="number" min={0} max={max} value={value} onChange={(e) => onChange(num(e.target.value))} /></div>
  );

  return (
    <>
      <h1>Automation settings</h1>
      {message && <div className={`banner ${message.tone}`} role={message.tone === "error" ? "alert" : "status"}>{message.text}</div>}
      <form className="stack" onSubmit={save}>
        <section className="card stack" aria-labelledby="st-mod">
          <h2 id="st-mod" style={{ margin: 0 }}>Optional modules</h2>
          {box("Scheduled reports", s.feature_flags.scheduling, (v) => up("feature_flags", { ...s.feature_flags, scheduling: v }))}
          {box("Allow automatic sending (each schedule still needs a Sender's approval)", s.feature_flags.auto_send, (v) => up("feature_flags", { ...s.feature_flags, auto_send: v }))}
        </section>
        <section className="card stack" aria-labelledby="st-sub">
          <h2 id="st-sub" style={{ margin: 0 }}>Daily submissions</h2>
          <div><label htmlFor="st-cut">Submission cutoff (company time)</label><input id="st-cut" type="time" value={s.submission_cutoff_local_time} onChange={(e) => up("submission_cutoff_local_time", e.target.value)} /></div>
          <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
            <legend style={{ fontWeight: 600 }}>Working days</legend>
            <div className="row">{DAYS.map((d, i) => box(d, s.working_days.includes(i + 1), (on) => up("working_days", on ? [...s.working_days, i + 1].sort() : s.working_days.filter((x) => x !== i + 1))))}</div>
          </fieldset>
        </section>
        <section className="card stack" aria-labelledby="st-rem">
          <h2 id="st-rem" style={{ margin: 0 }}>Reminders for missing entries</h2>
          {box("Send reminders", s.reminders.enabled, (v) => up("reminders", { ...s.reminders, enabled: v }))}
          <div className="row">
            {field("st-r1", "First reminder (minutes after cutoff)", s.reminders.first_after_minutes, (v) => up("reminders", { ...s.reminders, first_after_minutes: v }), 1440)}
            {field("st-r2", "Second reminder", s.reminders.second_after_minutes, (v) => up("reminders", { ...s.reminders, second_after_minutes: v }), 1440)}
            {field("st-r3", "Escalate to supervisors", s.reminders.escalate_after_minutes, (v) => up("reminders", { ...s.reminders, escalate_after_minutes: v }), 2880)}
          </div>
          {bad("reminders") && <p className="reason">{bad("reminders")}</p>}
          {box("Also email reminders (uses the connected Microsoft 365 mailbox)", s.reminders.email, (v) => up("reminders", { ...s.reminders, email: v }))}
        </section>
        <section className="card stack" aria-labelledby="st-exc">
          <h2 id="st-exc" style={{ margin: 0 }}>Exception thresholds</h2>
          <div className="row">
            {field("st-lo", "Flag achievement below (%)", s.exception_rules.low_achievement_pct, (v) => up("exception_rules", { ...s.exception_rules, low_achievement_pct: v }), 100)}
            {field("st-hi", "Flag achievement above (%)", s.exception_rules.high_achievement_pct, (v) => up("exception_rules", { ...s.exception_rules, high_achievement_pct: v }), 1000)}
            {field("st-stop", "Flag stop minutes from", s.exception_rules.stop_minutes, (v) => up("exception_rules", { ...s.exception_rules, stop_minutes: v }), 1440)}
            {field("st-days", "Check the last (days)", s.exception_rules.lookback_days, (v) => up("exception_rules", { ...s.exception_rules, lookback_days: v }), 31)}
          </div>
          {bad("exception_rules") && <p className="reason">{bad("exception_rules")}</p>}
        </section>
        <section className="card stack" aria-labelledby="st-dom">
          <h2 id="st-dom" style={{ margin: 0 }}>Company email domains</h2>
          <div>
            <label htmlFor="st-domains">Recipients outside these domains get a warning before sending</label>
            <input id="st-domains" type="text" className="wide" value={s.internal_email_domains.join(", ")}
              onChange={(e) => up("internal_email_domains", e.target.value.split(/[\s,]+/).map((x) => x.trim().toLowerCase()).filter(Boolean))} />
            {bad("internal_email_domains") && <p className="reason">{bad("internal_email_domains")}</p>}
          </div>
        </section>
        <div className="row"><button className="primary" type="submit" disabled={busy}>{busy ? "Saving…" : "Save settings"}</button></div>
      </form>
    </>
  );
}
