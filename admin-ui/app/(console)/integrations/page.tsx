"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { CustomApisPanel } from "@/components/CustomApisPanel";
import { ConnectorsPanel } from "@/components/ConnectorsPanel";
import { ConnectedAppsHeading } from "@/components/ConnectedAppsHeading";
import { useActiveTenant } from "@/lib/useActiveTenant";
import { APP_CATEGORIES } from "@/lib/connectedApps";

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
  const category = APP_CATEGORIES
    .find((value) => value.toLowerCase() === searchParams.get("category")?.toLowerCase()) ?? "All";
  const updateParams = (update: (params: URLSearchParams) => void) => {
    const params = new URLSearchParams(window.location.search);
    update(params);
    window.history.replaceState(null, "", `/integrations${params.size ? `?${params}` : ""}`);
  };

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
          category={category}
          onCategoryChange={(value) => updateParams((params) => {
            if (value === "All") params.delete("category");
            else params.set("category", value.toLowerCase());
          })}
          manageApp={searchParams.get("manage") ?? undefined}
          connectOpen={searchParams.get("add") === "1"}
          onConnectClose={() => updateParams((params) => params.delete("add"))}
          onManageChange={(app) => updateParams((params) => {
            params.delete("add");
            if (app) params.set("manage", app);
            else params.delete("manage");
          })}
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
      {tenant && !loading && (
        <section id="custom-tools" className="connected-api-tools" aria-labelledby="custom-api-tools-title">
          <CustomApisPanel key={tenant.id} tenantId={tenant.id} />
        </section>
      )}
    </>
  );
}
