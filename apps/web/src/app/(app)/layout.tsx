import { AppShell } from "@/components/AppShell";
import { SessionProvider } from "@/components/SessionProvider";

export default function AuthenticatedLayout({ children }: { children: React.ReactNode }) {
  return (
    <SessionProvider>
      <AppShell>{children}</AppShell>
    </SessionProvider>
  );
}
