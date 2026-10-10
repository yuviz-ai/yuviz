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
    <html lang="en" data-theme="light" suppressHydrationWarning style={{ backgroundColor: 'var(--background, #f7f4ee)' }}>
      <head>
        {/* Applies the saved theme before first paint so a refresh doesn't flash light mode. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `try{if(localStorage.getItem("yuviz.theme")==="dark")document.documentElement.dataset.theme="dark"}catch(e){}`,
          }}
        />
      </head>
      <body className="antialiased">{children}</body>
    </html>
  );
}
