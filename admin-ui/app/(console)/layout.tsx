import "@/app/globals.css";
import { AppShell } from "@/components/AppShell";

export default function ConsoleLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <AppShell>{children}</AppShell>;
}
