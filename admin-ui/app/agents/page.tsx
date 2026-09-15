"use client";

// Agent Studio — the agents and their configuration. Opening an agent goes
// to its config tabs, NOT to the call-flow canvas: identity/voice/knowledge/
// limits are what you edit day to day, and the flow is a separate surface
// under /workflows. Before this split, /workflows was the agent list, the
// canvas and the settings page all at once.
//
// Every figure on a card is read from something this system actually
// stores: config_version, status, language, the three assigned
// provider_configs, and the real count of attached knowledge bases and
// custom APIs. There is deliberately no containment rate or cost-per-call
// here — nothing in this repo measures either, and a plausible-looking
// number nobody computed is worse than no number.

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  AgentWithTenant,
  ApiError,
  ProviderConfig,
  Tenant,
  listAllAgents,
  listProviders,
  listTenants,
} from "@/lib/api";
import { listAgentKnowledgeBases } from "@/lib/knowledgeApi";
import { listAgentCustomApis } from "@/lib/toolexecApi";
import { AGENT_TEMPLATES } from "@/lib/agentTemplates";
import { TestAgentPanel } from "@/components/TestAgentPanel";

interface Attachments {
  sources: number | null; // null = the lookup failed; render "—", never 0
  tools: number | null;
}

export default function AgentsPage() {
  const router = useRouter();
  const [agents, setAgents] = useState<AgentWithTenant[]>([]);
  const [providersById, setProvidersById] = useState<Record<string, ProviderConfig>>({});
  const [attachments, setAttachments] = useState<Record<string, Attachments>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [testing, setTesting] = useState<AgentWithTenant | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    listTenants()
      .then(async (ts: Tenant[]) => {
        const list = await listAllAgents(ts);
        setAgents(list);
        setLoading(false);

        // Provider names: one request per tenant, not per agent.
        const provs = await Promise.all(
          ts.map((t) => listProviders(t.id).catch(() => [] as ProviderConfig[])),
        );
        setProvidersById(Object.fromEntries(provs.flat().map((p) => [p.id, p])));

        // Attachment counts are per-agent by necessity (both junction tables
        // are keyed by agent_id with no bulk endpoint). Failures degrade to
        // "—" per agent rather than failing the page (lesson 21).
        const entries = await Promise.all(
          list.map(async (a) => {
            const [kbs, apis] = await Promise.all([
              listAgentKnowledgeBases(a.id).then((r) => r.length).catch(() => null),
              listAgentCustomApis(a.id).then((r) => r.length).catch(() => null),
            ]);
            return [a.id, { sources: kbs, tools: apis }] as const;
          }),
        );
        setAttachments(Object.fromEntries(entries));
      })
      .catch((e) => {
        setError(e instanceof ApiError ? e.detail : String(e));
        setLoading(false);
      });
  }, []);

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    const matched = q
      ? agents.filter((a) => `${a.name} ${a.tenantName}`.toLowerCase().includes(q))
      : agents;
    return [...matched].sort(
      (a, b) =>
        Number(b.status === "active") - Number(a.status === "active") || a.name.localeCompare(b.name),
    );
  }, [agents, search]);

  const providerLabel = (id: string | null) => {
    if (!id) return "—";
    const p = providersById[id];
    if (!p) return "—";
    return p.model || p.voice || p.engine;
  };

  const accountLine =
    agents.length > 0
      ? `${agents.length} agent${agents.length === 1 ? "" : "s"} across ${
          new Set(agents.map((a) => a.tenantSlug)).size
        } account${new Set(agents.map((a) => a.tenantSlug)).size === 1 ? "" : "s"}. Each carries its own voice stack, knowledge and guardrails.`
      : "Each agent carries its own voice stack, knowledge and guardrails.";

  return (
    <>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12, marginBottom: 18 }}>
        <div>
          <h1 style={{ fontSize: "1.5rem", fontWeight: 600, margin: 0 }}>AI agents</h1>
          <div className="form-hint" style={{ marginTop: 4 }}>{accountLine}</div>
        </div>
        <input
          className="form-input"
          style={{ width: 200, marginLeft: "auto" }}
          placeholder="Search agents…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <button className="btn btn-primary btn-sm" onClick={() => router.push("/agents/new")}>
          New agent
        </button>
      </div>

      <div className="form-label" style={{ letterSpacing: ".06em", textTransform: "uppercase", fontSize: ".68rem" }}>
        Start from a template
      </div>
      <div className="agent-template-row">
        {AGENT_TEMPLATES.map((t) => (
          <button
            key={t.key}
            type="button"
            className="agent-template-card"
            onClick={() => router.push(`/agents/new?template=${t.key}`)}
          >
            <div className="agent-template-title">{t.label}</div>
            <div className="agent-template-blurb">{t.blurb}</div>
          </button>
        ))}
      </div>

      {error && <div className="error-banner">{error}</div>}

      {loading ? (
        <div className="empty-state">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="empty-state">
          No agents yet. Start from a template above, or create one from scratch.
        </div>
      ) : (
        <div className="agent-card-grid">
          {rows.map((a) => {
            const att = attachments[a.id];
            return (
              <div key={a.id} className="agent-card">
                <div className="agent-card-top">
                  <span className={`badge ${a.status === "active" ? "green" : "gray"}`}>
                    {a.status === "active" ? "Live" : "Paused"}
                  </span>
                  <span className="mono agent-card-meta">
                    v{a.config_version} {a.language || ""}
                  </span>
                </div>

                <div className="agent-card-name">{a.name}</div>
                <div className="agent-card-blurb">
                  {a.system_prompt?.trim() || "No system prompt set yet."}
                </div>

                <dl className="agent-card-stack">
                  <div><dt>STT</dt><dd className="mono">{providerLabel(a.stt_config_id)}</dd></div>
                  <div><dt>LLM</dt><dd className="mono">{providerLabel(a.llm_config_id)}</dd></div>
                  <div><dt>TTS</dt><dd className="mono">{providerLabel(a.tts_config_id)}</dd></div>
                </dl>

                <div className="agent-card-counts">
                  {att?.sources ?? "—"} source{att?.sources === 1 ? "" : "s"} ·{" "}
                  {att?.tools ?? "—"} tool{att?.tools === 1 ? "" : "s"} · {a.tenantName}
                </div>

                <div className="agent-card-actions">
                  <button
                    className="btn btn-ghost btn-sm"
                    onClick={() => router.push(`/agents/${a.tenantSlug}/${a.slug}`)}
                  >
                    View &amp; edit
                  </button>
                  <button className="btn btn-primary btn-sm" onClick={() => setTesting(a)}>
                    Test
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {testing && (
        <TestAgentPanel
          open
          onClose={() => setTesting(null)}
          tenantSlug={testing.tenantSlug}
          agentSlug={testing.slug}
        />
      )}
    </>
  );
}
