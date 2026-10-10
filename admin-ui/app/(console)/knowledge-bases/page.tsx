"use client";

// Knowledge: document sources and tenant-level custom APIs (attached per agent in AgentCustomApisPanel).
// Stats show only stored values; storage/retrieval aren't metered.

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { RefreshCw, Upload } from "lucide-react";
import { ApiError, getCurrentUser } from "@/lib/api";
import { useActiveTenant } from "@/lib/useActiveTenant";
import {
  KbAgent,
  KbDocument,
  KnowledgeBase,
  deleteDocument,
  deleteKnowledgeBase,
  listDocuments,
  listKbAgents,
  listKnowledgeBases,
  retryDocument,
  updateDocument,
  uploadDocument,
} from "@/lib/knowledgeApi";
import { CustomApi, listCustomApis } from "@/lib/toolexecApi";
import { AddSourceModal, ACCEPTED_DOC_ACCEPT } from "@/components/AddSourceModal";
import { CustomApisPanel } from "@/components/CustomApisPanel";
import { Modal } from "@/components/Modal";

interface KbRow extends KnowledgeBase {
  tenantName: string;
  tenantSlug: string;
}

interface SourceRow extends KbDocument {
  kbName: string;
  tenantName: string;
  // null when the agent lookup failed: unknown, not "unused".
  agents: KbAgent[] | null;
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
  const { tenant, allTenants, isAllTenants, loading: tenantLoading } = useActiveTenant();
  const [kbs, setKbs] = useState<KbRow[]>([]);
  const [sources, setSources] = useState<SourceRow[]>([]);
  const [emptyKbs, setEmptyKbs] = useState<KbRow[]>([]);
  const [removingKb, setRemovingKb] = useState<string | null>(null);
  const [removingSource, setRemovingSource] = useState<string | null>(null);
  const [apis, setApis] = useState<CustomApi[]>([]);
  const [tenantErrors, setTenantErrors] = useState<string[]>([]);
  const [canManage, setCanManage] = useState(false);
  const [loading, setLoading] = useState(true);
  // Page-level error only for listTenants(); everything downstream depends on it.
  const [pageError, setPageError] = useState<string | null>(null);
  const [addSourceOpen, setAddSourceOpen] = useState(false);
  const searchParams = useSearchParams();
  const router = useRouter();
  const [tab, setTab] = useState<Tab>(searchParams.get("tab") === "apis" ? "apis" : "sources");

  const [deleteTarget, setDeleteTarget] = useState<SourceRow | null>(null);

  const [retryingSource, setRetryingSource] = useState<string | null>(null);
  const [replacingSource, setReplacingSource] = useState<string | null>(null);
  const replaceInputRef = useRef<HTMLInputElement>(null);
  const pendingReplaceId = useRef<string | null>(null);

  const refresh = async () => {
    setLoading(true);
    setPageError(null);
    setTenantErrors([]);
    // Each tenant's fetch fails independently below (Promise.allSettled).
    const fetchedTenants = isAllTenants ? allTenants : tenant ? [tenant] : [];
    if (fetchedTenants.length === 0) {
      setLoading(false);
      return;
    }
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
          : null;
        nextSources.push(
          ...result.value.map((d) => ({ ...d, kbName: kb.name, tenantName: kb.tenantName, agents })),
        );
      });
      setSources(nextSources);
      // Empty KBs (usually failed uploads) are listed so they can be removed.
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
     
    refresh();
    getCurrentUser()
      .then((me) => setCanManage(me.role === "superadmin" || me.role === "admin"))
      .catch(() => {
        // Leave canManage false — same "fail closed on write controls" as
        // every other page's canManage check.
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenant, allTenants, isAllTenants, tenantLoading]);

  const stats = useMemo(() => {
    const ready = sources.filter((s) => s.status === "ready").length;
    const chunks = sources.reduce((n, s) => n + (s.chunk_count ?? 0), 0);
    // "Chained" = a param comes from another API's response; chain_levels is only the depth budget.
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

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    const id = deleteTarget.id;
    setDeleteTarget(null);
    setRemovingSource(id);
    try {
      await deleteDocument(id);
      await refresh();
    } catch (e) {
      setTenantErrors((errs) => [...errs, e instanceof ApiError ? e.detail : String(e)]);
    } finally {
      setRemovingSource(null);
    }
  };

  const handleRetry = async (source: SourceRow) => {
    setRetryingSource(source.id);
    try {
      await retryDocument(source.id);
      await refresh();
    } catch (e) {
      setTenantErrors((errs) => [...errs, e instanceof ApiError ? e.detail : String(e)]);
    } finally {
      setRetryingSource(null);
    }
  };

  const handleReplaceClick = (source: SourceRow) => {
    pendingReplaceId.current = source.id;
    replaceInputRef.current?.click();
  };

  const handleReplaceFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    const docId = pendingReplaceId.current;
    if (!file || !docId) return;
    e.target.value = "";
    const source = sources.find((s) => s.id === docId);
    if (!source) return;
    setReplacingSource(docId);
    const message = (e: unknown) => (e instanceof ApiError ? e.detail : String(e));
    let failure: string | null = null;
    try {
      const fresh = await uploadDocument(source.kb_id, file, source.title, {
        language: source.language,
        tags: source.tags,
      });
      if (source.usage_mode !== "auto") await updateDocument(fresh.id, { usage_mode: source.usage_mode });
      try {
        await deleteDocument(docId);
      } catch (e) {
        failure = `New file uploaded, but the old copy of "${source.title}" could not be removed: ${message(e)}`;
      }
    } catch (e) {
      failure = message(e);
    }
    await refresh();
    if (failure) setTenantErrors((errs) => [...errs, failure!]);
    setReplacingSource(null);
    pendingReplaceId.current = null;
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
          <div className="kb-stat-sub">
            {isAllTenants ? `across ${allTenants.length} tenants` : `in ${tenant?.name ?? "this account"}`}
          </div>
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
                  <th>Source</th><th>Type</th><th>Size</th><th>Status</th><th>Uploaded</th><th>Indexed</th><th>Used by</th><th>Actions</th>
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
                    <td>—</td>
                    <td><span className="badge gray">empty</span></td>
                    <td>—</td>
                    <td className="kb-source-sub">No documents in it yet</td>
                    <td><span className="kb-source-sub">—</span></td>
                    <td>
                      {canManage && (
                        <button
                          className="btn btn-danger btn-sm"
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
                    <td className="mono">{s.byte_size != null ? `${(s.byte_size / 1024).toFixed(1)} KB` : "—"}</td>
                    <td>
                      <span className={`badge ${STATUS_BADGE[s.status] ?? "gray"}`}>{s.status}</span>
                      {s.status === "failed" && s.error && (
                        <div className="kb-source-sub" style={{ marginTop: 4, maxWidth: 260 }} title={s.error}>
                          {s.error}
                        </div>
                      )}
                    </td>
                    <td className="kb-source-sub">{timeAgo(s.created_at)}</td>
                    <td className="mono">
                      {s.status === "ready" ? `${(s.chunk_count ?? 0).toLocaleString()} chunks` : "—"}
                    </td>
                    <td>
                      {s.agents === null
                        ? <span className="kb-source-sub">Couldn&apos;t check</span>
                        : s.agents.length === 0
                        ? <span className="kb-source-sub">Not used yet</span>
                        : s.agents.length === 1
                          ? s.agents[0].agent_name
                          : `${s.agents.length} agents`}
                    </td>
                    <td>
                      {canManage && (
                        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                          {s.status === "failed" && (
                            <>
                              <button
                                className="btn btn-ghost btn-sm"
                                disabled={retryingSource === s.id || replacingSource === s.id}
                                onClick={() => handleRetry(s)}
                                title="Re-run ingestion without uploading again"
                              >
                                <RefreshCw size={12} />
                                {retryingSource === s.id ? "Retrying…" : "Retry"}
                              </button>
                              <button
                                className="btn btn-ghost btn-sm"
                                disabled={retryingSource === s.id || replacingSource === s.id}
                                onClick={() => handleReplaceClick(s)}
                                title="Upload a different file to replace this one"
                              >
                                <Upload size={12} />
                                {replacingSource === s.id ? "Replacing…" : "Replace"}
                              </button>
                            </>
                          )}
                          <button
                            className="btn btn-danger btn-sm"
                            disabled={removingSource === s.id}
                            onClick={() => setDeleteTarget(s)}
                          >
                            {removingSource === s.id ? "Deleting…" : "Delete"}
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      <input
        ref={replaceInputRef}
        type="file"
        accept={ACCEPTED_DOC_ACCEPT}
        hidden
        onChange={handleReplaceFile}
      />

      {tab === "apis" && (
        !tenant ? (
          <div className="empty-state">
            {isAllTenants
              ? "Pick a single tenant from the header switcher to manage its APIs."
              : "No account selected."}
          </div>
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

      {(addSourceOpen || (canManage && searchParams.get("add") === "1")) && (
        <AddSourceModal
          tenants={allTenants}
          knowledgeBases={kbs}
          canManage={canManage}
          onClose={() => {
            setAddSourceOpen(false);
            if (searchParams.has("add")) router.replace("/knowledge-bases");
          }}
          onCreated={refresh}
        />
      )}

      <Modal
        open={!!deleteTarget}
        title={`Delete "${deleteTarget?.title}"?`}
        onClose={() => setDeleteTarget(null)}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setDeleteTarget(null)}>Cancel</button>
            <button className="btn btn-danger btn-sm" onClick={confirmDelete}>Delete document</button>
          </>
        }
      >
        {deleteTarget && (
          <div style={{ fontSize: ".82rem", color: "var(--text-2)", lineHeight: 1.6 }}>
            {deleteTarget.agents === null ? (
              <p style={{ margin: 0 }}>
                Couldn&apos;t check which agents use this document. Any agent using it will lose
                it permanently.
              </p>
            ) : deleteTarget.agents.length === 0 ? (
              <p style={{ margin: 0 }}>
                This document is not currently used by any agents. Deleting it will remove it
                from the knowledge base permanently.
              </p>
            ) : (
              <>
                <p style={{ margin: "0 0 10px" }}>
                  This document is currently used by{" "}
                  <strong>{deleteTarget.agents.length} agent{deleteTarget.agents.length !== 1 ? "s" : ""}</strong>.
                  Deleting it will remove it from{" "}
                  {deleteTarget.agents.length === 1 ? "that agent" : "all of them"} permanently.
                </p>
                <div
                  style={{
                    borderLeft: "2px solid var(--red-border)",
                    padding: "6px 0 6px 10px",
                    display: "flex",
                    flexDirection: "column",
                    gap: 4,
                  }}
                >
                  <div style={{ fontWeight: 600, marginBottom: 2 }}>Agents affected:</div>
                  {deleteTarget.agents.map((a) => (
                    <div key={a.agent_id}>{a.agent_name}</div>
                  ))}
                </div>
              </>
            )}
          </div>
        )}
      </Modal>
    </>
  );
}
