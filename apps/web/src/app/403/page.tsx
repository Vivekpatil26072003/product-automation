import Link from "next/link";

const REASONS: Record<string, string> = {
  no_membership: "Your company account is not set up for this application. Ask an administrator to add you.",
  multi_tenant_unsupported: "Your account belongs to more than one company, which this release does not support.",
};

export default async function Forbidden({ searchParams }: { searchParams: Promise<{ reason?: string }> }) {
  const { reason } = await searchParams;
  return (
    <main className="main" style={{ maxWidth: 560, margin: "10vh auto" }}>
      <div className="card">
        <h1>Access not available</h1>
        <p>{(reason && REASONS[reason]) ?? "You do not have permission to open this page."}</p>
        <Link href="/login">Back to sign in</Link>
      </div>
    </main>
  );
}
