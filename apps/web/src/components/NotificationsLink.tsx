"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { apiGet } from "@/lib/api";

// Header link to reminders and escalations with the unread count (refreshed every minute).

const REFRESH_MS = 60_000;

export function NotificationsLink() {
  const [unread, setUnread] = useState(0);

  useEffect(() => {
    let active = true;
    const load = () =>
      apiGet<{ unread: number }>("/notifications").then(
        (r) => active && setUnread(r.unread),
        () => undefined,
      );
    void load();
    const timer = setInterval(load, REFRESH_MS);
    window.addEventListener("notifications-read", load);
    return () => {
      active = false;
      clearInterval(timer);
      window.removeEventListener("notifications-read", load);
    };
  }, []);

  return (
    <Link href="/notifications" className="button" aria-label={unread ? `Notifications, ${unread} unread` : "Notifications"}>
      Notifications{unread > 0 && <span className="badge tone-warning" aria-hidden="true">{unread}</span>}
    </Link>
  );
}
