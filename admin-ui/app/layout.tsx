import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Yuviz AI — Let AI handle the conversation",
  description: "AI voice agents that answer calls, understand customers, and take action — 24/7.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
