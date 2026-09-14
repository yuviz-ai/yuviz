"use client";

// Agents list — create drops you on the canvas; voice/model/tools/number
// live at ./[tenant]/[agent]/settings. URLs stay /workflows/*; labels say agent.

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  AgentWithTenant,
  ApiError,
  Tenant,
  createAgent,
  listAllAgents,
  listTenants,
} from "@/lib/api";
import { Modal } from "@/components/Modal";

type FlowState = "live" | "unpublished" | "draft" | "none";

function flowState(a: AgentWithTenant): FlowState {
  if (a.has_workflow) return a.workflow_diverged ? "unpublished" : "live";
  if (a.has_workflow_draft) return "draft";
  return "none";
}

const STATE_LABEL: Record<FlowState, string> = {
  live: "Live",
  unpublished: "Unpublished changes",
  draft: "Draft — not published",
  none: "Single prompt",
};

const STATE_BADGE: Record<FlowState, string> = {
  live: "green",
  unpublished: "amber",
  draft: "amber",
  none: "gray",
};

function stepCount(a: AgentWithTenant): number | null {
  return a.workflow_node_count ?? null;
}

function slugify(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

// Non-empty global prompt so pipeline date grounding / [[END_CALL]] attach.
const DEFAULT_GREETING = "Hello! How can I help you today?";
const DEFAULT_SYSTEM_PROMPT =
  "You are a helpful voice assistant on a phone call. Answer in at most 2-3 short " +
  "spoken sentences. Plain conversational speech only — no markdown, no lists.";

export default function WorkflowsPage() {
  const router = useRouter();
  const [agents, setAgents] = useState<AgentWithTenant[]>([]);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newTenant, setNewTenant] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("new") !== "1") return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setCreating(true);
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    listTenants()
      .then(async (ts) => {
        setTenants(ts);
        if (ts.length > 0) setNewTenant(ts[0].slug);
        setAgents(await listAllAgents(ts));
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  }, []);

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    const matched = q
      ? agents.filter((a) => `${a.name} ${a.tenantName}`.toLowerCase().includes(q))
      : agents;
    const rank: Record<FlowState, number> = { live: 0, unpublished: 1, draft: 2, none: 3 };
    return [...matched].sort(
      (a, b) => rank[flowState(a)] - rank[flowState(b)] || a.name.localeCompare(b.name),
    );
  }, [agents, search]);

  const open = (a: AgentWithTenant) => router.push(`/workflows/${a.tenantSlug}/${a.slug}`);

  const handleCreate = async () => {
    const slug = slugify(newName);
    if (!slug || !newTenant) return;
    setBusy(true);
    setCreateError(null);
    try {
      const agent = await createAgent(newTenant, {
        slug,
        name: newName.trim(),
        greeting: DEFAULT_GREETING,
        system_prompt: DEFAULT_SYSTEM_PROMPT,
      });
      router.push(`/workflows/${newTenant}/${agent.slug}`);
    } catch (e) {
      setCreateError(e instanceof ApiError ? e.detail : String(e));
      setBusy(false);
    }
  };

  return (
    <>
      <div className="card">
        <div className="card-hdr">
          <span className="card-title">Your Agents</span>
          <input
            className="form-input"
            style={{ width: 200, marginLeft: "auto" }}
            placeholder="Search agents…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <button className="btn btn-primary btn-sm" onClick={() => setCreating(true)}>
            + New agent
          </button>
        </div>

        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : error ? (
          <div className="card-body"><div className="error-banner">{error}</div></div>
        ) : rows.length === 0 ? (
          <div className="empty-state">
            No agents yet. Create one to draw its first conversation flow.
          </div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Agent</th><th>Account</th><th>Flow</th><th>Steps</th><th />
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => {
                const state = flowState(a);
                const steps = stepCount(a);
                return (
                  <tr key={a.id} onClick={() => open(a)}>
                    <td className="bold">{a.name}</td>
                    <td>{a.tenantName}</td>
                    <td>
                      <span className={`badge ${STATE_BADGE[state]}`}>{STATE_LABEL[state]}</span>
                    </td>
                    <td className="mono">{steps === null ? "—" : steps}</td>
                    <td style={{ textAlign: "right" }}>
                      <button className="btn btn-ghost btn-sm" onClick={(e) => { e.stopPropagation(); open(a); }}>
                        {state === "none" ? "Build a flow" : "Open"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="form-hint" style={{ marginTop: 10 }}>
        A flow splits a call into steps, each with its own instructions and its own tools, so the
        agent can&apos;t book before it has verified. Agents marked <strong>Single prompt</strong> run
        one instruction for the whole call — which is the right choice for simple agents. Voice,
        model, tools and phone number are under <strong>Settings</strong> inside each agent.
      </div>

      <Modal
        open={creating}
        title="New agent"
        onClose={() => { if (!busy) setCreating(false); }}
        footer={
          <>
            <button className="btn btn-ghost" disabled={busy} onClick={() => setCreating(false)}>
              Cancel
            </button>
            <button
              className="btn btn-primary"
              disabled={busy || !slugify(newName) || !newTenant}
              onClick={handleCreate}
            >
              {busy ? "Creating…" : "Create"}
            </button>
          </>
        }
      >
        {createError && <div className="error-banner">{createError}</div>}
        <div className="form-group">
          <label className="form-label">Name <span className="required">*</span></label>
          <input
            className="form-input"
            autoFocus
            value={newName}
            placeholder="Booking Bot"
            onChange={(e) => setNewName(e.target.value)}
          />
          {newName.trim() !== "" && (
            <div className="form-hint">
              Address: <span className="mono">{slugify(newName) || "—"}</span>
            </div>
          )}
        </div>
        <div className="form-group" style={{ marginBottom: 0 }}>
          <label className="form-label">Account <span className="required">*</span></label>
          <select
            className="form-select"
            value={newTenant}
            onChange={(e) => setNewTenant(e.target.value)}
          >
            {tenants.map((t) => (
              <option key={t.id} value={t.slug}>{t.name}</option>
            ))}
          </select>
          <div className="form-hint">
            You land on the canvas with a starter flow drawn. Everything else — voice, model,
            tools, number — is under Settings once it exists.
          </div>
        </div>
      </Modal>
    </>
  );
}
