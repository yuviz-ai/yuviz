"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { ConnectorsPanel } from "@/components/ConnectorsPanel";
import { ConnectedAppsHeading } from "@/components/ConnectedAppsHeading";
import { useActiveTenant } from "@/lib/useActiveTenant";

export default function IntegrationsPage() {
  return (
    <Suspense>
      <Integrations />
    </Suspense>
  );
}

function Integrations() {
  const { tenant, isAllTenants, loading, error } = useActiveTenant();
  const searchParams = useSearchParams();

  return (
    <>
      {error && <div className="error-banner">{error}</div>}

      {(!tenant || loading) && <ConnectedAppsHeading />}

      {loading ? (
        <div className="card">
          <div className="empty-state">Loading apps…</div>
        </div>
      ) : tenant ? (
        <ConnectorsPanel
          key={tenant.id}
          tenantId={tenant.id}
          manageApp={searchParams.get("manage") ?? undefined}
          connectOpen={searchParams.get("add") === "1"}
          onConnectClose={() => window.history.replaceState(null, "", "/integrations")}
          onManageChange={(app) => {
            const params = new URLSearchParams(window.location.search);
            params.delete("add");
            if (app) params.set("manage", app);
            else params.delete("manage");
            window.history.replaceState(null, "", `/integrations${params.size ? `?${params}` : ""}`);
          }}
        />
      ) : (
        <div className="empty-state">
          {isAllTenants
            ? searchParams.get("add") === "1"
              ? "Pick a single tenant from the header switcher before connecting a calling app to add a phone number."
              : "Pick a single tenant from the header switcher to manage its integrations."
            : searchParams.get("add") === "1"
              ? "Select an account before connecting a calling app to add a phone number."
              : "No account selected."}
        </div>
      )}
    </>
  );
}
