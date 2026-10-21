import type { ReactNode } from "react";

export function ConnectedAppsHeading({ children }: { children?: ReactNode }) {
  return <div className="connected-apps-heading">
    <div><h1>Connected Apps</h1><p>Link the phone lines and work tools your agents use.</p></div>
    {children}
  </div>;
}
