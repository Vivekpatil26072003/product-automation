"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { useSession } from "@/components/SessionProvider";
import { ApiError, apiGet, apiSend } from "@/lib/api";
import type { Notification } from "@/lib/automation";

// A3 reminders and escalations for the signed-in user.

const KIND: Record<string, string> = { REMINDER_FIRST: "Reminder", REMINDER_SECOND: "Second reminder", ESCALATION: "Escalation" };

export default function NotificationsPage() {
  const { session } = useSession();
  const [items, setItems] = useState<Notification[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    apiGet<{ data: Notification[] }>("/notifications").then(
      (r) => active && setItems(r.data),
      (e) => active && setError(e instanceof ApiError ? e.message : "Notifications could not be loaded."),
    );
    return () => {
      active = false;
    };
  }, []);

  async function readAll() {
    try {
      await apiSend("POST", "/notifications/read", { ids: [] });
      setItems((xs) => xs?.map((x) => ({ ...x, read: true })) ?? null);
      window.dispatchEvent(new Event("notifications-read"));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not mark as read.");
    }
  }

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>Notifications</h1>
        {items?.some((x) => !x.read) && <button onClick={readAll}>Mark all read</button>}
      </div>
      {error && <div className="banner error" role="alert">{error}</div>}
      {!items && !error && <div className="skeleton" aria-busy="true" style={{ height: 80 }} />}
      {items && items.length === 0 && <div className="card"><p>No notifications.</p></div>}
      {items && items.length > 0 && (
        <ul className="stack" style={{ listStyle: "none", padding: 0 }}>
          {items.map((n) => (
            <li key={n.id} className="card stack" style={{ gap: 4, borderLeft: n.read ? undefined : "4px solid var(--primary)" }}>
              <div className="row">
                <span className={`badge tone-${n.kind === "ESCALATION" ? "warning" : "neutral"}`}>{KIND[n.kind] ?? n.kind}</span>
                <strong>{n.title}</strong>
                {!n.read && <span className="sr-only">Unread</span>}
              </div>
              <p style={{ margin: 0 }}>{n.body}</p>
              <span className="meta">
                {new Date(n.created_at).toLocaleString("en-IN", { timeZone: session.timezone })}
                {n.email_state === "ACCEPTED" ? " · also emailed (accepted by provider)" : n.email_state === "UNKNOWN" ? " · email outcome unknown" : ""}
              </span>
              {n.link && <Link href={n.link}>Go to upload</Link>}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
