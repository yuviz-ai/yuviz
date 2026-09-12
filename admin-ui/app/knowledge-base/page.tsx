"use client";

import { useEffect, useState } from "react";
import { listTenants, Tenant } from "@/lib/api";
import { KnowledgeBaseTabs } from "@/components/KnowledgeBaseTabs";

export default function KnowledgeBasePage() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantId, setTenantId] = useState<string>("");

  useEffect(() => {
    listTenants().then((ts) => {
      setTenants(ts);
      if (ts.length > 0) setTenantId(ts[0].id);
    });
  }, []);

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 14, gap: 10 }}>
        <select className="form-select" style={{ width: 240 }} value={tenantId} onChange={(e) => setTenantId(e.target.value)}>
          {tenants.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
      </div>

      {tenantId && <KnowledgeBaseTabs tenantId={tenantId} />}
    </>
  );
}
