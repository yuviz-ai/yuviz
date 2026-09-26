"use client";

// One call flow's canvas. Keyed by the flow's own id — not by an agent —
// because a call flow is its own object and may front several agents.

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ApiError, Tenant, listTenants } from "@/lib/api";
import { CallFlow, getCallFlow } from "@/lib/callFlowApi";
import { CallFlowPanel } from "@/components/callflow/CallFlowPanel";

export default function CallFlowEditorPage() {
  const { callFlowId } = useParams<{ callFlowId: string }>();
  const [flow, setFlow] = useState<CallFlow | null>(null);
  // The canvas needs a tenant *slug* to list that tenant's agents for the
  // "AI agent" step, but a flow row only carries tenant_id — resolve it once
  // here rather than adding a slug to every call-flow response.
  const [tenantSlug, setTenantSlug] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([getCallFlow(callFlowId), listTenants()])
      .then(([f, tenants]: [CallFlow, Tenant[]]) => {
        setFlow(f);
        setTenantSlug(tenants.find((t) => t.id === f.tenant_id)?.slug ?? null);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)));
  }, [callFlowId]);

  if (error) return <div className="error-banner">{error}</div>;
  if (!flow || !tenantSlug) return <div className="empty-state">Loading…</div>;

  return (
    <div className="wf-page">
      <CallFlowPanel flow={flow} tenantSlug={tenantSlug} />
    </div>
  );
}
