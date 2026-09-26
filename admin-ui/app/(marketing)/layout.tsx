import './landing.css'

export default function MarketingLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <div className="bg-background min-h-screen text-foreground">{children}</div>;
}
