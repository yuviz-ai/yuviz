"use client";

// Agent Studio. Opening an agent goes to its config tabs; its conversation-steps canvas lives under /workflows.
// Cards show only stored values — no invented metrics like containment rate.

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Bot, MoreVertical, PauseCircle, Pencil, Play, PlayCircle, Plus, Workflow } from "lucide-react";
import { Agent, AgentCallStats, ApiError, getAgentCallStats, listAgents, listPhoneNumbers, updateAgent } from "@/lib/api";
import { AgentStatusDialog } from "@/components/AgentStatusDialog";
import { useMenuKeys } from "@/lib/useMenuKeys";
import { AgentTestPopup } from "@/components/AgentTestPopup";
import { CallStatsGrid, NO_CALLS } from "@/components/CallStatsGrid";
import { useActiveTenant } from "@/lib/useActiveTenant";
import { listAgentKnowledgeBases } from "@/lib/knowledgeApi";
import { listAgentCustomApis } from "@/lib/toolexecApi";
import { AGENT_TEMPLATES, agentIcon, agentTemplate } from "@/lib/agentTemplates";
import { LANGUAGES } from "@/lib/engineCatalog";
import { AgentDraft, clearAgentDraft, draftSavedLabel, loadAgentDraft } from "@/lib/agentDraft";
import { hasInboundNumber, isChatAgent } from "@/lib/agentReadiness";

interface AgentRow extends Agent {
  tenantName: string;
  tenantSlug: string;
}

interface Attachments {
  sources: number | null; // null = the lookup failed; render "—", never 0
  tools: number | null;
  calls: AgentCallStats | null;
  numbers: string[] | null;
  inboundNumber: boolean | null;
}

function AgentCardMenu({ agent, editUrl, stepsUrl, onStatus }: {
  agent: AgentRow; editUrl: string; stepsUrl: string; onStatus: (agent: AgentRow) => void;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const { triggerRef, menuRef, onMenuKey } = useMenuKeys(open, () => setOpen(false));
  return (
    <div className="ed2-menu">
      <button
        ref={triggerRef}
        className="agent-card-icon-btn"
        aria-label="More actions"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <MoreVertical size={15} />
      </button>
      {open && (
        <>
          <div className="ed2-menu-backdrop" onClick={() => setOpen(false)} />
          <div ref={menuRef} className="ed2-menu-pop" role="menu" onKeyDown={onMenuKey}>
            <button role="menuitem" onClick={() => router.push(editUrl)}>
              <Pencil size={13} /> Edit agent
            </button>
            <button role="menuitem" onClick={() => router.push(stepsUrl)}>
              <Workflow size={13} /> Conversation steps
            </button>
            <button role="menuitem" onClick={() => { setOpen(false); onStatus(agent); }}>
              {agent.status === "active" ? <PauseCircle size={13} /> : <PlayCircle size={13} />}
              {agent.status === "active" ? "Pause agent" : agent.activated_at ? "Resume agent" : "Go live"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

export default function AgentsPage() {
  const router = useRouter();
  const { tenant, allTenants, isPlatformScoped, isAllTenants, loading: tenantLoading } = useActiveTenant();
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [attachments, setAttachments] = useState<Record<string, Attachments>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [draft, setDraft] = useState<AgentDraft | null>(null);
  const [testing, setTesting] = useState<AgentRow | null>(null);
  const [statusTarget, setStatusTarget] = useState<AgentRow | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setDraft(loadAgentDraft());
  }, []);

  // allSettled: one failing tenant must not blank the others.
  useEffect(() => {
    if (tenantLoading) return;
    const targets = isAllTenants ? allTenants : tenant ? [tenant] : [];
    if (targets.length === 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setAgents([]);
      setLoading(false);
      return;
    }
     
    setLoading(true);
    (async () => {
      const results = await Promise.allSettled(
        targets.map((t) =>
          listAgents(t.slug).then((found) =>
            found.map((a): AgentRow => ({ ...a, tenantName: t.name, tenantSlug: t.slug })),
          ),
        ),
      );
      const list: AgentRow[] = [];
      const errs: string[] = [];
      results.forEach((r, i) => {
        if (r.status === "fulfilled") list.push(...r.value);
        else errs.push(`${targets[i].name}: ${r.reason instanceof ApiError ? r.reason.detail : String(r.reason)}`);
      });
      setAgents(list);
      setError(errs.length > 0 ? errs.join("; ") : null);
      setLoading(false);

      // No bulk endpoint for attachment counts; failures degrade to "—" per agent.
      const [tenantNumbers, tenantStats] = await Promise.all([
        Promise.all(targets.map(async (t) => [t.id, await listPhoneNumbers(t.id).catch(() => null)] as const)),
        Promise.all(targets.map(async (t) => [t.id, await getAgentCallStats(t.slug).catch(() => null)] as const)),
      ]).then(([n, s]) => [new Map(n), new Map(s)] as const);
      const entries = await Promise.all(
        list.map(async (a) => {
          const [kbs, apis] = await Promise.all([
            listAgentKnowledgeBases(a.id).then((r) => r.length).catch(() => null),
            listAgentCustomApis(a.id).then((r) => r.length).catch(() => null),
          ]);
          const nums = tenantNumbers.get(a.tenant_id);
          const numbers = nums ? nums.filter((n) => n.agent_id === a.id && n.status === "active").map((n) => n.did) : null;
          const inboundNumber = nums ? hasInboundNumber(a, nums) : null;
          const stats = tenantStats.get(a.tenant_id);
          const calls = stats ? (stats.find((s) => s.agent_id === a.id) ?? { agent_id: a.id, ...NO_CALLS }) : null;
          return [a.id, { sources: kbs, tools: apis, calls, numbers, inboundNumber }] as const;
        }),
      );
      setAttachments(Object.fromEntries(entries));
    })();
  }, [tenant, allTenants, isAllTenants, tenantLoading]);

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

  const accountLine = isAllTenants
    ? `${agents.length} agent${agents.length === 1 ? "" : "s"} across ${allTenants.length} account${allTenants.length === 1 ? "" : "s"}.` +
      " Each has its own voice, knowledge and rules."
    : tenant
      ? `${agents.length} agent${agents.length === 1 ? "" : "s"} in ${tenant.name}.` +
        (isPlatformScoped ? " Switch accounts from the header." : "") +
        " Each has its own voice, knowledge and rules."
      : "Each agent has its own voice, knowledge and rules.";

  return (
    <>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", gap: 12, marginBottom: 18 }}>
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
      </div>

      {draft && (
        <div className="draft-banner">
          <span>
            You have an unfinished agent{draft.name.trim() ? <> called <b>{draft.name.trim()}</b></> : ""}, saved{" "}
            {draftSavedLabel(draft.savedAt)}.
          </span>
          <button className="btn btn-primary btn-sm" onClick={() => router.push("/agents/new?mode=advanced")}>Continue</button>
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => {
              clearAgentDraft();
              setDraft(null);
            }}
          >
            Discard
          </button>
        </div>
      )}

      {error && <div className="error-banner">{error}</div>}

      {loading ? (
        <div className="empty-state">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="agent-empty">
          <div className="agent-empty-ico"><Bot size={22} /></div>
          <h2>{search.trim() ? "No agents match your search" : "Create your first agent"}</h2>
          <p>
            {search.trim()
              ? "Try a different name, or clear the search."
              : "An agent answers or places phone calls for you. Pick a template below to get going in a minute."}
          </p>
        </div>
      ) : (
        <div className="agent-card-grid">
          {rows.map((a) => {
            const att = attachments[a.id];
            const language = LANGUAGES.find((l) => l.value === a.language)?.label ?? a.language;
            const Icon = agentIcon(a);
            const tpl = agentTemplate(a);
            const direction = a.direction;
            const unreachable = a.status === "active" && direction !== "outbound" &&
              !isChatAgent(a) && att?.inboundNumber === false;
            const editUrl = `/agents/${a.tenantSlug}/${a.slug}`;
            return (
              <div key={a.id} className={`agent-card${a.status === "active" ? "" : " paused"}`}>
                <div className="agent-card-top">
                  <div className="agent-card-avatar"><Icon size={22} /></div>
                  <button className="agent-card-icon-btn" aria-label={`Test ${a.name}`} title="Test it" onClick={() => setTesting(a)}>
                    <Play size={15} />
                  </button>
                  <AgentCardMenu agent={a} editUrl={editUrl}
                    stepsUrl={`/workflows/${a.tenantSlug}/${a.slug}`} onStatus={setStatusTarget} />
                </div>

                <Link href={editUrl} className="agent-card-name agent-card-link">{a.name}</Link>
                {isAllTenants && <div className="agent-card-acct">{a.tenantName}</div>}

                <div className="agent-card-meta">
                  {direction && (
                    <span className={`badge ${direction === "inbound" ? "cyan" : "indigo"}`}>
                      {direction === "inbound" ? "Inbound" : direction === "outbound" ? "Outbound" : "Inbound + outbound"}
                    </span>
                  )}
                  <span className="badge gray">{tpl?.label ?? "Custom"}</span>
                  {att?.numbers != null && (att.numbers.length === 0
                    ? direction !== "outbound" && !isChatAgent(a) && <span className="badge gray">No number</span>
                    : att.numbers.map((n) => <span key={n} className="badge green">{n}</span>))}
                </div>

                <div className="agent-card-facts">
                  {language && <span>{language}</span>}
                  {att?.sources != null && (
                    <span className={att.sources === 0 ? "muted" : ""}>
                      {att.sources === 0 ? "No knowledge added" : `${att.sources} knowledge source${att.sources === 1 ? "" : "s"}`}
                    </span>
                  )}
                  {!!att?.tools && <span>{att.tools} connection{att.tools === 1 ? "" : "s"}</span>}
                  {a.transfer_type !== "none" && <span>Can transfer to a person</span>}
                </div>

                {att?.calls && <CallStatsGrid stats={att.calls} />}

                <div className="agent-card-foot">
                  <span className={`agent-card-status${unreachable ? " unreachable" : a.status === "active" ? " on" : ""}`}>
                    <i />{unreachable ? "Not reachable – no number" : a.status === "active" ? "Live" : "Paused"}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {testing && (
        <AgentTestPopup
          key={testing.id}
          tenantSlug={testing.tenantSlug}
          agentSlug={testing.slug}
          name={testing.name}
          icon={agentIcon(testing)}
          onClose={() => setTesting(null)}
        />
      )}

      {!loading && (
        <section className="agent-templates">
          <div className="agent-templates-hdr">
            <h2>{rows.length === 0 && !search.trim() ? "Start from a template" : "Create another agent"}</h2>
            <span>Pre-filled for you. Change anything later.</span>
          </div>
          <div className="agent-template-row">
            {AGENT_TEMPLATES.map((t) => (
              <button
                key={t.key}
                type="button"
                className="agent-template-card"
                onClick={() => router.push(`/agents/new?template=${t.key}`)}
              >
                <i className="tpl-ico"><t.icon size={15} /></i>
                <div className="agent-template-title">{t.label}</div>
                <div className="agent-template-blurb">{t.blurb}</div>
              </button>
            ))}
            <button type="button" className="agent-template-card blank" onClick={() => router.push("/agents/new")}>
              <i className="tpl-ico"><Plus size={15} /></i>
              <div className="agent-template-title">Start blank</div>
              <div className="agent-template-blurb">Describe the job in your own words.</div>
            </button>
          </div>
        </section>
      )}
      <AgentStatusDialog key={statusTarget?.id ?? "closed"} agent={statusTarget} tenantSlug={statusTarget?.tenantSlug ?? ""}
        onClose={() => setStatusTarget(null)} onConfirm={async (target) => {
          const row = statusTarget;
          if (!row) return;
          const updated = await updateAgent(row.tenantSlug, target.id, {
            status: target.status === "active" ? "inactive" : "active",
          });
          setAgents((current) => current.map((item) => item.id === target.id ? { ...item, ...updated } : item));
        }} />
    </>
  );
}
