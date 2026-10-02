"use client";

// Full-page call-flow editor for one agent; keyed by agent because the graph lives on agents.workflow.

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { Agent, ApiError, getAgent } from "@/lib/api";
import { WorkflowPanel } from "@/components/workflow/WorkflowPanel";

export default function WorkflowEditorPage() {
  const { tenantSlug, agentSlug } = useParams<{ tenantSlug: string; agentSlug: string }>();
  const [agent, setAgent] = useState<Agent | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getAgent(tenantSlug, agentSlug)
      .then(setAgent)
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)));
  }, [tenantSlug, agentSlug]);

  if (error) return <div className="error-banner">{error}</div>;
  if (!agent) return <div className="empty-state">Loading…</div>;

  return (
    <div className="wf-page">
      <WorkflowPanel
        tenantSlug={tenantSlug}
        agentId={agent.id}
        agentSlug={agentSlug}
        greeting={agent.greeting}
        systemPrompt={agent.system_prompt}
        header={{
          title: agent.name,
          backHref: "/workflows",
          settingsHref: `/agents/${tenantSlug}/${agentSlug}`,
        }}
      />
    </div>
  );
}
