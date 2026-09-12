"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { Agent, ApiError, listAgents, listTenants, Tenant } from "@/lib/api";
import {
  assignKnowledgeBase,
  deleteDocument,
  detachKnowledgeBase,
  KbAgent,
  KbDocument,
  KnowledgeBase,
  listDocuments,
  listKbAgents,
  listKnowledgeBases,
  updateDocument,
  uploadDocument,
} from "@/lib/knowledgeApi";
import { ACCEPTED_DOC_ACCEPT, rejectionReasonFor } from "@/components/AddSourceModal";

export default function KnowledgeBaseDetailPage() {
  const params = useParams<{ tenantSlug: string; kbId: string }>();
  const { tenantSlug, kbId } = params;

  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [kb, setKb] = useState<KnowledgeBase | null>(null);
  const [docs, setDocs] = useState<KbDocument[]>([]);
  const [agents, setAgents] = useState<KbAgent[]>([]);
  const [tenantAgents, setTenantAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [docsError, setDocsError] = useState<string | null>(null);
  const [agentsError, setAgentsError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadFileError, setUploadFileError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [sourceTab, setSourceTab] = useState<"documents" | "apis">("documents");

  const refreshAgents = () => {
    if (!kbId) return;
    listKbAgents(kbId)
      .then((rows) => {
        setAgents(rows);
        setAgentsError(null);
      })
      .catch((e) => setAgentsError(e instanceof ApiError ? e.detail : String(e)));
  };

  const refreshDocs = () => {
    if (!kbId) return;
    listDocuments(kbId)
      .then((rows) => {
        setDocs(rows);
        setDocsError(null);
      })
      .catch((e) => setDocsError(e instanceof ApiError ? e.detail : String(e)));
  };

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setNotFound(false);
    listTenants()
      .then(async (tenants) => {
        const t = tenants.find((x) => x.slug === tenantSlug);
        if (!t) {
          if (!cancelled) setNotFound(true);
          return;
        }
        if (!cancelled) setTenant(t);
        const kbs = await listKnowledgeBases(t.id);
        const found = kbs.find((x) => x.id === kbId);
        if (cancelled) return;
        if (!found) {
          setNotFound(true);
          return;
        }
        setKb(found);
        refreshDocs();
        refreshAgents();
        listAgents(t.slug)
          .then(setTenantAgents)
          .catch((e) => setAgentsError(e instanceof ApiError ? e.detail : String(e)));
      })
      .catch(() => setNotFound(true))
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantSlug, kbId]);

  const handleUsageModeToggle = async (doc: KbDocument) => {
    try {
      await updateDocument(doc.id, { usage_mode: doc.usage_mode === "auto" ? "prompt" : "auto" });
      refreshDocs();
    } catch (e) {
      setActionError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleDeleteDoc = async (doc: KbDocument) => {
    if (!confirm(`Delete document "${doc.title}"?`)) return;
    try {
      await deleteDocument(doc.id);
      refreshDocs();
    } catch (e) {
      setActionError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleUploadFileChange = (f: File | null) => {
    setUploadFile(null);
    setUploadFileError(null);
    if (!f) return;
    const reason = rejectionReasonFor(f);
    if (reason) {
      setUploadFileError(reason);
      return;
    }
    setUploadFile(f);
  };

  const handleUpload = async () => {
    if (!uploadFile || !kbId) return;
    setUploading(true);
    setActionError(null);
    try {
      await uploadDocument(kbId, uploadFile, uploadFile.name);
      setUploadFile(null);
      refreshDocs();
    } catch (e) {
      setActionError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setUploading(false);
    }
  };

  const handleAttach = async (agentId: string) => {
    if (!kbId) return;
    try {
      await assignKnowledgeBase(agentId, kbId);
      refreshAgents();
    } catch (e) {
      setActionError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleDetach = async (agent: KbAgent) => {
    if (!kbId) return;
    if (!confirm(`Detach agent "${agent.agent_name}"? Documents themselves are not deleted.`)) return;
    try {
      await detachKnowledgeBase(agent.agent_id, kbId);
      refreshAgents();
    } catch (e) {
      setActionError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const statusBadge = (status: KbDocument["status"]) => {
    const cls = status === "ready" ? "green" : status === "failed" ? "red" : status === "processing" ? "amber" : "gray";
    return <span className={`badge ${cls}`}>{status}</span>;
  };

  const docTypeLabel = (contentType: string) => {
    if (contentType === "text/markdown") return "Markdown";
    if (contentType === "text/plain") return "Text";
    return contentType || "Unknown";
  };

  if (loading) return <div className="empty-state">Loading…</div>;
  if (notFound || !kb || !tenant) return <div className="empty-state">Knowledge base not found.</div>;

  const attachedAgentIds = new Set(agents.map((a) => a.agent_id));
  const unattachedAgents = tenantAgents.filter((a) => !attachedAgentIds.has(a.id));

  return (
    <>
      {actionError && <div className="error-banner">{actionError}</div>}

      <div className="card">
        <div className="card-hdr">
          <div className="card-title">Document Library — {kb.name}</div>
        </div>
        <div className="card-body" style={{ fontSize: ".82rem", color: "var(--text-3)" }}>
          {tenant.name} · {kb.description || "No description"} · Upload and ingest documents once — used by{" "}
          {agents.length} agent{agents.length === 1 ? "" : "s"} · {docs.length} document{docs.length === 1 ? "" : "s"}
        </div>
      </div>

      <div className="card">
        <div className="card-hdr" style={{ gap: 16 }}>
          <div style={{ display: "flex", gap: 4 }}>
            <button
              className={`btn btn-sm ${sourceTab === "documents" ? "btn-primary" : "btn-ghost"}`}
              onClick={() => setSourceTab("documents")}
            >
              Documents ({docs.length})
            </button>
            <button
              className={`btn btn-sm ${sourceTab === "apis" ? "btn-primary" : "btn-ghost"}`}
              onClick={() => setSourceTab("apis")}
            >
              APIs (0)
            </button>
          </div>
          {sourceTab === "documents" && (
            <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
              <input
                className="form-input"
                type="file"
                accept={ACCEPTED_DOC_ACCEPT}
                onChange={(e) => handleUploadFileChange(e.target.files?.[0] || null)}
              />
              <button className="btn btn-primary btn-sm" onClick={handleUpload} disabled={!uploadFile || uploading}>
                {uploading ? "Uploading…" : "Upload"}
              </button>
            </div>
          )}
        </div>
        {sourceTab === "apis" ? (
          <div className="empty-state">
            No API sources connected yet. Live API knowledge sources aren&apos;t available in this release — API/tool
            connections are still managed from each agent&apos;s Tools tab.
          </div>
        ) : (
          <>
            {uploadFileError && (
              <div style={{ color: "var(--red)", fontSize: ".76rem", padding: "0 12px" }}>{uploadFileError}</div>
            )}
            {docsError && <div className="error-banner">{docsError}</div>}
            {docs.length === 0 ? (
              <div className="empty-state">No documents yet.</div>
            ) : (
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Type</th>
                    <th>Chunks</th>
                    <th>Status</th>
                    <th>Last updated</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {docs.map((doc) => (
                    <tr key={doc.id}>
                      <td className="bold">
                        {doc.title}
                        {doc.error && <div style={{ color: "var(--red)", fontSize: ".68rem" }}>{doc.error}</div>}
                      </td>
                      <td>{docTypeLabel(doc.content_type)}</td>
                      <td className="mono">{doc.chunk_count}</td>
                      <td>{statusBadge(doc.status)}</td>
                      <td>{new Date(doc.updated_at).toLocaleDateString()}</td>
                      <td>
                        <div style={{ display: "flex", alignItems: "center", gap: 8, justifyContent: "flex-end" }}>
                          <label
                            style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--text-3)" }}
                            title="Always inject this document's full content into the LLM prompt every turn, regardless of query relevance"
                          >
                            <input
                              type="checkbox"
                              checked={doc.usage_mode === "prompt"}
                              onChange={() => handleUsageModeToggle(doc)}
                              disabled={doc.status !== "ready"}
                            />
                            Always include in prompt
                          </label>
                          <button className="btn btn-ghost btn-sm" onClick={() => handleDeleteDoc(doc)}>
                            Delete
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>

      <div className="card">
        <div className="card-hdr">
          <div className="card-title">Attached agents</div>
          {unattachedAgents.length > 0 && (
            <select
              className="form-select"
              style={{ marginLeft: "auto", width: 200 }}
              defaultValue=""
              onChange={(e) => {
                if (e.target.value) handleAttach(e.target.value);
                e.target.value = "";
              }}
            >
              <option value="">+ Attach agent…</option>
              {unattachedAgents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          )}
        </div>
        {agentsError && <div className="error-banner">{agentsError}</div>}
        {agents.length === 0 ? (
          <div className="empty-state">Not used by any agent.</div>
        ) : (
          agents.map((a) => (
            <div key={a.agent_id} className="kb-row">
              <div style={{ flex: 1 }}>{a.agent_name}</div>
              <button className="btn btn-danger btn-sm" onClick={() => handleDetach(a)}>
                Detach
              </button>
            </div>
          ))
        )}
      </div>
    </>
  );
}
