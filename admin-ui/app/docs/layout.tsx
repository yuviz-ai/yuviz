import "@/app/globals.css";
import Link from "next/link";

// Outside app/(console)/ so it renders even when auth/API/AppShell is broken;
// hence it imports the stylesheet itself and the theme follows the OS.
export default function DocsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div style={{ minHeight: "100vh", background: "var(--bg)", color: "var(--text)" }}>
      <div style={{ padding: "12px 20px", borderBottom: "1px solid var(--border)" }}>
        <Link href="/dashboard" className="btn btn-ghost btn-sm">
          ← Console
        </Link>
      </div>
      {children}
    </div>
  );
}
