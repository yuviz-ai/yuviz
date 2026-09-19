"use client";

// Knowledge — what the agents can look things up in. Two kinds of source
// live here, because both answer the same question at call time:
//
//   Sources  — documents, chunked and embedded into this tenant's namespace
//   APIs     — custom HTTP calls, including ones whose parameters come from
//              another API's response (custom_api_params.upstream_api_id),
//              which is what makes a chain multi-level
//
// The APIs tab is the tenant-level builder. The agent's own Knowledge &
// Tools tab renders the *attach* view instead (AgentCustomApisPanel) — you
// define an API once here and tick it on per agent, the same relationship
// knowledge bases already have.
//
// Every figure in the stat row is counted from something this system
// actually stores. There is deliberately no storage-used or retrieval-calls
// tile: no column records document bytes and nothing meters retrievals, and
// an invented number on a page about grounding facts would be its own joke.

import { useEffect, useMemo, useState } from "react";
import { ApiError, getCurrentUser } from "@/lib/api";
import { useActiveTenant } from "@/lib/useActiveTenant";
import {
  KbAgent,
  KbDocument,
  KnowledgeBase,
  deleteKnowledgeBase,
  listDocuments,
  listKbAgents,
  listKnowledgeBases,
} from "@/lib/knowledgeApi";
import { CustomApi, listCustomApis } from "@/lib/toolexecApi";
import { AddSourceModal } from "@/components/AddSourceModal";
import { CustomApisPanel } from "@/components/CustomApisPanel";

interface KbRow extends KnowledgeBase {
  tenantName: string;
  tenantSlug: string;
}

interface SourceRow extends KbDocument {
  kbName: string;
  tenantName: string;
  agents: KbAgent[];
}

type Tab = "sources" | "apis";

const STATUS_BADGE: Record<string, string> = {
  ready: "green",
  processing: "amber",
  pending: "amber",
  failed: "red",
};

function timeAgo(iso: string): string {
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/** "Upload" for a file, otherwise the content type — what the source IS,
 *  not the MIME string, which reads as noise in a table. */
function sourceType(doc: KbDocument): string {
  const ct = (doc.content_type || "").toLowerCase();
  if (ct.includes("pdf")) return "PDF";
  if (ct.includes("markdown") || ct.includes("md")) return "Markdown";
  if (ct.includes("plain") || ct.includes("text")) return "Text";
  if (ct.includes("csv")) return "CSV";
  if (ct.includes("word") || ct.includes("docx")) return "Word";
  return ct ? ct.split("/").pop()! : "Upload";
}

export default function KnowledgeBasesPage() {
  const { tenant, allTenants, loading: tenantLoading } = useActiveTenant();
  const [kbs, setKbs] = useState<KbRow[]>([]);
  const [sources, setSources] = useState<SourceRow[]>([]);
  const [emptyKbs, setEmptyKbs] = useState<KbRow[]>([]);
  const [removingKb, setRemovingKb] = useState<string | null>(null);
  const [apis, setApis] = useState<CustomApi[]>([]);
  const [tenantErrors, setTenantErrors] = useState<string[]>([]);
  const [canManage, setCanManage] = useState(false);
  const [loading, setLoading] = useState(true);
  // Fatal only for listTenants() — every fetch downstream of it depends on
  // the tenant list, so its failure alone renders a page-level error
  // (lesson 21 for everything below it).
  const [pageError, setPageError] = useState<string | null>(null);
  const [addSourceOpen, setAddSourceOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("sources");

  const refresh = async () => {
    setLoading(true);
    setPageError(null);
    setTenantErrors([]);
    if (!tenant) {
      setLoading(false);
      return;
    }
    // One account at a time (the header switcher picks it). This used to
    // query every tenant for knowledge bases AND custom APIs, which on a
    // platform with hundreds of accounts is thousands of requests and a
    // page full of "Failed to fetch".
    const fetchedTenants = [tenant];
    try {
      const kbResults = await Promise.allSettled(fetchedTenants.map((t) => listKnowledgeBases(t.id)));
      const nextKbs: KbRow[] = [];
      const nextTenantErrors: string[] = [];
      kbResults.forEach((result, i) => {
        const t = fetchedTenants[i];
        if (result.status === "fulfilled") {
          nextKbs.push(...result.value.map((kb) => ({ ...kb, tenantName: t.name, tenantSlug: t.slug })));
        } else {
          const reason = result.reason;
          nextTenantErrors.push(
            `Couldn't load knowledge bases for ${t.name}: ${reason instanceof ApiError ? reason.detail : String(reason)}`,
          );
        }
      });
      setKbs(nextKbs);
      setTenantErrors(nextTenantErrors);

      const [docResults, agentResults, apiResults] = await Promise.all([
        Promise.allSettled(nextKbs.map((kb) => listDocuments(kb.id))),
        Promise.allSettled(nextKbs.map((kb) => listKbAgents(kb.id))),
        Promise.allSettled(fetchedTenants.map((t) => listCustomApis(t.id))),
      ]);

      const nextSources: SourceRow[] = [];
      docResults.forEach((result, i) => {
        if (result.status !== "fulfilled") return;
        const kb = nextKbs[i];
        const agents = agentResults[i].status === "fulfilled"
          ? (agentResults[i] as PromiseFulfilledResult<KbAgent[]>).value
          : [];
        nextSources.push(
          ...result.value.map((d) => ({ ...d, kbName: kb.name, tenantName: kb.tenantName, agents })),
        );
      });
      setSources(nextSources);
      // A knowledge base with no documents is a container someone started
      // and never filled — usually an upload that failed. Surfaced as a row
      // rather than only as a count, so it can be seen and removed instead
      // of quietly inflating the tile.
      setEmptyKbs(
        nextKbs.filter((kb) => !nextSources.some((src) => src.kb_id === kb.id)),
      );

      setApis(
        apiResults.flatMap((r) => (r.status === "fulfilled" ? r.value : [])),
      );
    } catch (e) {
      setPageError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (tenantLoading) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    getCurrentUser()
      .then((me) => setCanManage(me.role === "superadmin" || me.role === "admin"))
      .catch(() => {
        // Leave canManage false — same "fail closed on write controls" as
        // every other page's canManage check.
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenant, tenantLoading]);

  const stats = useMemo(() => {
    const ready = sources.filter((s) => s.status === "ready").length;
    const chunks = sources.reduce((n, s) => n + (s.chunk_count ?? 0), 0);
    // An API is "chained" when one of its parameters is filled from another
    // API's response — that is the dependency, not chain_levels, which is
    // only the depth budget.
    const chained = apis.filter((a) => (a.params ?? []).some((p) => p.source === "upstream")).length;
    return { ready, chunks, chained };
  }, [sources, apis]);

  const removeEmptyKb = async (kb: KbRow) => {
    if (!window.confirm(`Delete the empty knowledge base "${kb.name}"?`)) return;
    setRemovingKb(kb.id);
    try {
      await deleteKnowledgeBase(kb.id);
      await refresh();
    } catch (e) {
      setTenantErrors((errs) => [...errs, e instanceof ApiError ? e.detail : String(e)]);
    } finally {
      setRemovingKb(null);
    }
  };

  if (pageError) {
    return (
      <div className="error-banner">
        Couldn&apos;t load accounts: {pageError}{" "}
        <button className="btn btn-ghost btn-sm" onClick={refresh}>Retry</button>
      </div>
    );
  }

  return (
    <>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12, marginBottom: 18 }}>
        <div>
          <h1 style={{ fontSize: "1.5rem", fontWeight: 600, margin: 0 }}>Knowledge</h1>
          <div className="form-hint" style={{ marginTop: 4 }}>
            Added once for the account and attached to as many agents as you like — one ingestion,
            one index.
          </div>
        </div>
        {canManage && tab === "sources" && (
          <button
            className="btn btn-primary btn-sm"
            style={{ marginLeft: "auto" }}
            onClick={() => setAddSourceOpen(true)}
          >
            Add source
          </button>
        )}
      </div>

      <div className="kb-stat-row">
        <div className="kb-stat">
          <div className="kb-stat-label">Sources</div>
          <div className="kb-stat-value">{sources.length}</div>
          <div className="kb-stat-sub">{stats.ready} ready to serve</div>
        </div>
        <div className="kb-stat">
          <div className="kb-stat-label">Indexed chunks</div>
          <div className="kb-stat-value">{stats.chunks.toLocaleString()}</div>
          <div className="kb-stat-sub">across all sources</div>
        </div>
        <div className="kb-stat">
          <div className="kb-stat-label">Knowledge bases</div>
          <div className="kb-stat-value">{kbs.length}</div>
          <div className="kb-stat-sub">in {tenant?.name ?? "this account"}</div>
        </div>
        <div className="kb-stat">
          <div className="kb-stat-label">APIs</div>
          <div className="kb-stat-value">{apis.length}</div>
          <div className="kb-stat-sub">
            {stats.chained} fed by another API
          </div>
        </div>
      </div>

      {tenantErrors.map((msg, i) => (
        <div key={i} className="error-banner">{msg}</div>
      ))}

      <div className="tabs">
        <button className={`tab${tab === "sources" ? " active" : ""}`} onClick={() => setTab("sources")}>
          Sources
        </button>
        <button className={`tab${tab === "apis" ? " active" : ""}`} onClick={() => setTab("apis")}>
          APIs
        </button>
      </div>

      {tab === "sources" && (
        <div className="card">
          {loading ? (
            <div className="empty-state">Loading…</div>
          ) : sources.length === 0 && emptyKbs.length === 0 ? (
            <div className="empty-state">
              No sources yet. Add one and it&apos;s chunked, embedded, and available to every agent
              you attach it to.
            </div>
          ) : (
            <table className="tbl">
              <thead>
                <tr>
                  <th>Source</th><th>Type</th><th>Status</th><th>Indexed</th><th>Used by</th>
                </tr>
              </thead>
              <tbody>
                {emptyKbs.map((kb) => (
                  <tr key={kb.id}>
                    <td>
                      <div className="bold">{kb.name}</div>
                      <div className="kb-source-sub">
                        Knowledge base · created {timeAgo(kb.created_at)}
                      </div>
                    </td>
                    <td>—</td>
                    <td><span className="badge gray">empty</span></td>
                    <td className="kb-source-sub">No documents in it yet</td>
                    <td>
                      {canManage && (
                        <button
                          className="btn btn-ghost btn-sm"
                          disabled={removingKb === kb.id}
                          onClick={() => removeEmptyKb(kb)}
                        >
                          {removingKb === kb.id ? "Deleting…" : "Delete"}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
                {sources.map((s) => (
                  <tr key={s.id}>
                    <td>
                      <div className="bold">{s.title}</div>
                      <div className="kb-source-sub">
                        {s.kbName} · updated {timeAgo(s.updated_at)}
                      </div>
                    </td>
                    <td>{sourceType(s)}</td>
                    <td>
                      <span className={`badge ${STATUS_BADGE[s.status] ?? "gray"}`}>{s.status}</span>
                      {s.status === "failed" && s.error && (
                        <div className="kb-source-sub" title={s.error}>{s.error.slice(0, 60)}</div>
                      )}
                    </td>
                    <td className="mono">
                      {s.status === "ready" ? `${(s.chunk_count ?? 0).toLocaleString()} chunks` : "—"}
                    </td>
                    <td>
                      {s.agents.length === 0
                        ? <span className="kb-source-sub">Not used yet</span>
                        : s.agents.length === 1
                          ? s.agents[0].agent_name
                          : `${s.agents.length} agents`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {tab === "apis" && (
        !tenant ? (
          <div className="empty-state">No account selected.</div>
        ) : (
          <CustomApisPanel tenantId={tenant.id} />
        )
      )}

      <div className="form-hint" style={{ marginTop: 12 }}>
        A <strong>knowledge base</strong> is the container a source is filed in; adding your first
        source creates one, and several sources can share it. Embeddings live in this account&apos;s
        own namespace and are never readable by another account — detaching a source from an agent
        does not delete it, remove it here.
      </div>

      {addSourceOpen && (
        <AddSourceModal
          tenants={allTenants}
          knowledgeBases={kbs}
          canManage={canManage}
          onClose={() => setAddSourceOpen(false)}
          onCreated={refresh}
        />
      )}
    </>
  );
}
