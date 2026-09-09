"use client";

import { useState } from "react";
import { KnowledgeBasePanel } from "@/components/KnowledgeBasePanel";
import { CustomApisPanel } from "@/components/CustomApisPanel";
import { AgentCustomApisPanel } from "@/components/AgentCustomApisPanel";

type SubTab = "documents" | "apis";

export function KnowledgeBaseTabs({ tenantId, agentId }: { tenantId: string; agentId?: string }) {
  const [subTab, setSubTab] = useState<SubTab>("documents");

  return (
    <div>
      <div className="tabs">
        <button className={`tab${subTab === "documents" ? " active" : ""}`} onClick={() => setSubTab("documents")}>
          Documents
        </button>
        <button className={`tab${subTab === "apis" ? " active" : ""}`} onClick={() => setSubTab("apis")}>
          APIs
        </button>
      </div>

      {subTab === "documents" && <KnowledgeBasePanel tenantId={tenantId} agentId={agentId} />}
      {subTab === "apis" &&
        (agentId ? (
          <AgentCustomApisPanel tenantId={tenantId} agentId={agentId} />
        ) : (
          <CustomApisPanel tenantId={tenantId} />
        ))}
    </div>
  );
}
