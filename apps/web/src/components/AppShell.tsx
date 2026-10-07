"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";

import { apiSend, setCsrfToken } from "@/lib/api";

import { NotificationsLink } from "./NotificationsLink";
import { hasRole, useSession } from "./SessionProvider";

// Only screens that exist are listed. Navigation hides by role; the server enforces access anyway.
const ALL = ["UPLOADER", "REVIEWER", "SENDER", "ADMIN", "VIEWER"];
const NAV = [
  { href: "/dashboard", label: "Overview", roles: ALL },
  { href: "/diary", label: "Diary photos", roles: ["UPLOADER", "REVIEWER"] },
  { href: "/uploads/new", label: "Upload notes", roles: ["UPLOADER", "REVIEWER"] },
  { href: "/records", label: "Production records", roles: ALL },
  { href: "/orders", label: "Customer orders", roles: ["REVIEWER", "SENDER", "VIEWER"] },
  { href: "/sheets", label: "Daily sheets", roles: ALL },
  { href: "/registers", label: "Pick registers", roles: ALL },
  { href: "/reports", label: "Reports", roles: ["REVIEWER", "SENDER"] },
  { href: "/history", label: "History", roles: ["UPLOADER", "REVIEWER", "SENDER", "ADMIN", "VIEWER"] },
  { href: "/automation", label: "Automation", roles: ["SENDER"] },
  { href: "/exceptions", label: "Exceptions", roles: ["REVIEWER", "SENDER", "ADMIN"] },
  { href: "/control-tower", label: "Control tower", roles: ALL },
  { href: "/settings/integrations", label: "Integrations", roles: ["ADMIN"] },
  { href: "/settings/automation", label: "Automation settings", roles: ["ADMIN"] },
  { href: "/settings/operations", label: "Operations", roles: ["ADMIN"] },
  { href: "/settings/owner-report", label: "Owner report & email", roles: ["ADMIN"] },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const { session } = useSession();
  const pathname = usePathname();
  const router = useRouter();
  const [menuOpen, setMenuOpen] = useState(false);
  const items = NAV.filter((n) => hasRole(session, ...n.roles));

  async function signOut() {
    try {
      await apiSend("POST", "/auth/logout");
    } finally {
      setCsrfToken(null);
      router.replace("/login");
    }
  }

  return (
    <div className="shell">
      <aside className={`sidebar${menuOpen ? " open" : ""}`} id="sidebar">
        <div className="brand">Production Automation</div>
        <nav aria-label="Main">
          <ul>
            {items.map((n) => (
              <li key={n.href}>
                <Link
                  href={n.href}
                  aria-current={pathname === n.href || pathname.startsWith(`${n.href}/`) ? "page" : undefined}
                  onClick={() => setMenuOpen(false)}
                >
                  {n.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      </aside>
      <div>
        <header className="header">
          <button
            className="menu-toggle"
            aria-expanded={menuOpen}
            aria-controls="sidebar"
            onClick={() => setMenuOpen((o) => !o)}
          >
            Menu
          </button>
          <span className="meta">
            {session.display_name ?? session.email} · {session.roles.join(", ").toLowerCase()}
          </span>
          <div className="row" style={{ gap: 8 }}>
            <NotificationsLink />
            <button onClick={signOut}>Sign out</button>
          </div>
        </header>
        <main className="main" id="main">
          {items.length === 0 && !pathname.startsWith("/records/") ? (
            <p>Your role has no screens in this release yet.</p>
          ) : (
            children
          )}
        </main>
      </div>
    </div>
  );
}
