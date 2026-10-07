"use client";

import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useState } from "react";

import { ApiError, apiGet, setCsrfToken, type Session } from "@/lib/api";

type Ctx = { session: Session; refresh: () => Promise<void> };
const SessionContext = createContext<Ctx | null>(null);

export function useSession(): Ctx {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession outside SessionProvider");
  return ctx;
}

export function hasRole(session: Session, ...roles: string[]): boolean {
  return roles.some((r) => session.roles.includes(r));
}

/** Loads the server session before rendering anything restricted, so restricted content never flashes. */
export function SessionProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchSession = useCallback(
    () =>
      apiGet<{ data: Session }>("/session").then(
        ({ data }) => {
          setCsrfToken(data.csrf_token);
          setSession(data);
        },
        (e) => {
          if (e instanceof ApiError && e.status === 401) {
            router.replace(`/login?return_to=${encodeURIComponent(pathname)}`);
          } else {
            setError(e instanceof ApiError ? e.message : "The service is unavailable. Try again shortly.");
          }
        },
      ),
    [pathname, router],
  );
  const refresh = fetchSession;

  useEffect(() => {
    void fetchSession();
  }, [fetchSession]);

  if (error) {
    return (
      <main className="main" role="alert">
        <h1>Something went wrong</h1>
        <p>{error}</p>
        <button onClick={() => { setError(null); void refresh(); }}>Try again</button>
      </main>
    );
  }
  if (!session) {
    return (
      <main className="main" aria-busy="true">
        <span className="sr-only">Loading your session…</span>
        <div className="skeleton" style={{ width: 240 }} />
      </main>
    );
  }
  return <SessionContext.Provider value={{ session, refresh }}>{children}</SessionContext.Provider>;
}
