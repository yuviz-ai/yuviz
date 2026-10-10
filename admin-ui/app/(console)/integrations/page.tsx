"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { ConnectorsPanel } from "@/components/ConnectorsPanel";
import { useActiveTenant } from "@/lib/useActiveTenant";

export default function IntegrationsPage() {
  const { tenant, isAllTenants, loading, error } = useActiveTenant();
  const searchParams = useSearchParams();
  const router = useRouter();

  return (
    <>
      {error && <div className="error-banner">{error}</div>}

      <div style={{ marginBottom: 18 }}>
        <h1 style={{ fontSize: "1.5rem", fontWeight: 600, letterSpacing: "-.025em", margin: 0, color: "var(--text)" }}>
          Connected Apps
        </h1>
        <div className="form-hint" style={{ marginTop: 4 }}>
          {tenant ? `Connected accounts and ready-made tools for ${tenant.name}.` : "Connected accounts and ready-made tools."}
        </div>
      </div>

      {loading ? (
        <div className="card">
          <div className="empty-state">Loading integrations…</div>
        </div>
      ) : tenant ? (
        <ConnectorsPanel
          key={tenant.id}
          tenantId={tenant.id}
          connectOpen={searchParams.get("add") === "1"}
          onConnectClose={() => router.replace("/integrations")}
        />
      ) : (
        <div className="empty-state">
          {isAllTenants
            ? "Pick a single tenant from the header switcher to manage its integrations."
            : "No account selected."}
        </div>
      )}
    </>
  );
}
