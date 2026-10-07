"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { ApiError, apiSend, setCsrfToken, type Session } from "@/lib/api";

const DEV_AUTH = process.env.NEXT_PUBLIC_DEV_AUTH === "true";
const DEV_USERS = [
  ["dev-reviewer", "Reviewer (all departments)"],
  ["dev-uploader", "Uploader (Tapeline only)"],
  ["dev-viewer", "Viewer (Tapeline, Warping)"],
  ["dev-sender", "Sender"],
  ["dev-admin", "Administrator"],
] as const;

function safeReturnTo(value: string | null): string {
  return value && value.startsWith("/") && !value.startsWith("//") && !value.includes("\\") ? value : "/";
}

function LoginForm() {
  const params = useSearchParams();
  const router = useRouter();
  const returnTo = safeReturnTo(params.get("return_to"));
  const [error, setError] = useState<string | null>(
    params.get("error") ? "Sign-in was cancelled or could not be completed. Try again." : null,
  );
  const [busy, setBusy] = useState(false);

  async function devLogin(subject: string) {
    setBusy(true);
    setError(null);
    try {
      const { data } = await apiSend<{ data: Session }>("POST", "/auth/dev-login", { subject });
      setCsrfToken(data.csrf_token);
      router.replace(returnTo);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Sign-in failed.");
      setBusy(false);
    }
  }

  return (
    <main className="main" style={{ maxWidth: 480, margin: "10vh auto" }}>
      <div className="card stack">
        <h1>Sign in</h1>
        <p className="muted">Use your company account. There is no public registration.</p>
        {error && (
          <div className="banner error" role="alert">
            {error}
          </div>
        )}
        <a className="button primary" href={`/api/v1/auth/login?return_to=${encodeURIComponent(returnTo)}`}>
          Sign in with company SSO
        </a>
        {DEV_AUTH && (
          <fieldset className="stack" style={{ border: "1px dashed var(--border)", borderRadius: 8, padding: 12 }}>
            <legend className="meta">Development only: seeded demo users</legend>
            {DEV_USERS.map(([subject, label]) => (
              <button key={subject} disabled={busy} onClick={() => devLogin(subject)}>
                {label}
              </button>
            ))}
          </fieldset>
        )}
      </div>
    </main>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}
