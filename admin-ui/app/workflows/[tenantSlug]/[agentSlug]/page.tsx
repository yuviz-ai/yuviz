"use client";

// Full-page workflow editor — an agent IS its flow (2026-08-30), so this is
// what opening an agent shows. Its voice, model, tools and number are one
// level down at ./settings, reached from the editor's ⋮ menu.
//
// No header row of its own: the back link, the title and the settings entry
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
          settingsHref: `/workflows/${tenantSlug}/${agentSlug}/settings`,
        }}
      />
    </div>
  );
}
