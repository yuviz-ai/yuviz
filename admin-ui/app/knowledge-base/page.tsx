"use client";

import { useEffect, useState } from "react";
import { getCurrentUser, listTenants, Tenant, User } from "@/lib/api";
import { KnowledgeBaseTabs } from "@/components/KnowledgeBaseTabs";
import { ACTIVE_TENANT_STORAGE_KEY } from "@/components/AppShell";

export default function KnowledgeBasePage() {
  const [user, setUser] = useState<User | null>(null);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  // null covers both "not superadmin, irrelevant" and "superadmin, nothing
  // picked in the header switcher yet" (same shape as live-calls's page).
  const [activeTenantId, setActiveTenantId] = useState<string | null>(null);

  useEffect(() => {
    getCurrentUser().then(setUser).catch(() => {});
  }, []);

  useEffect(() => {
    if (!user) return;
    listTenants().then((ts) => {
      setTenants(ts);
      if (user.role !== "superadmin") {
        setActiveTenantId(ts[0]?.id ?? null);
        return;
      }
      const storedSlug = typeof window !== "undefined" ? window.localStorage.getItem(ACTIVE_TENANT_STORAGE_KEY) : null;
      setActiveTenantId(ts.find((t) => t.slug === storedSlug)?.id ?? null);
    });
  }, [user]);

  if (!user) return null;

  if (user.role === "superadmin" && activeTenantId === null) {
    return (
      <div className="card">
        <div className="card-hdr">
          <div className="card-title">Knowledge Base</div>
        </div>
        <div style={{ padding: 24, color: "var(--text-2)", fontSize: ".82rem" }}>
          {tenants.length === 0
            ? "No tenants yet."
            : "Pick a tenant from the switcher in the header above to manage its knowledge base."}
        </div>
      </div>
    );
  }

  return <KnowledgeBaseTabs tenantId={activeTenantId!} />;
}
