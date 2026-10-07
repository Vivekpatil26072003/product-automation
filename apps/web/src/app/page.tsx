import { redirect } from "next/navigation";

// Overview is the native dashboard (spec §5: root redirects to /dashboard).
export default function Home() {
  redirect("/dashboard");
}
