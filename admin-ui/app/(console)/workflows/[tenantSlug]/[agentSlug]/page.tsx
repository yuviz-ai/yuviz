"use client";

// Full-page call-flow editor for one agent. An agent no longer *is* its
// flow (that was the 2026-08-30 model): opening an agent now shows its
// configuration under /agents, and this canvas is the separate, optional
// surface for splitting a call into steps. The flow graph is still stored
// on the agent row (agents.workflow), so this route stays keyed by agent.
//
// No header row of its own: the back link, the title and the config entry
// are passed into WorkflowPanel's toolbar so the canvas starts one row down
// instead of two.

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
