"use client";

import { useEffect, useState } from "react";
import { ApiError, listProviders, ProviderConfig } from "@/lib/api";
import {
  AgentKnowledgeBase,
  assignKnowledgeBase,
  createKnowledgeBase,
  deleteDocument,
  detachKnowledgeBase,
  getRetrievalPolicy,
  KbDocument,
  KnowledgeBase,
  listAgentKnowledgeBases,
  listDocuments,
  listKnowledgeBases,
  setKnowledgeBaseEnabled,
  setRetrievalPolicy,
  updateDocument,
  uploadDocument,
} from "@/lib/knowledgeApi";
import { Modal } from "@/components/Modal";

interface PolicyForm {
  top_k: number;
  minimum_score: number;
  max_tokens: number;
  include_citations: boolean;
}

const DEFAULT_POLICY: PolicyForm = { top_k: 5, minimum_score: 0, max_tokens: 1000, include_citations: true };

// agentId absent (Knowledge Base page) = authoring: list every tenant KB
// and its documents, create/upload/usage-mode/delete-document, no attach
// affordance and no retrieval-policy card. agentId present (agent's own
// Knowledge Base tab) = attach-only: the same rows read-only plus
// attach/enable/detach and the retrieval-policy card — no create, upload
// or delete-document control (lesson 17: this is the only file for both
// surfaces, per the design's authoring/attachment split).
export function KnowledgeBasePanel({ tenantId, agentId }: { tenantId: string; agentId?: string }) {
  const [allKbs, setAllKbs] = useState<KnowledgeBase[]>([]);
  const [kbsError, setKbsError] = useState<string | null>(null);
  const [assignments, setAssignments] = useState<AgentKnowledgeBase[]>([]);
  const [assignmentsError, setAssignmentsError] = useState<string | null>(null);
  const [providersError, setProvidersError] = useState<string | null>(null);
  const [docsByKb, setDocsByKb] = useState<Record<string, KbDocument[]>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [embeddingProviders, setEmbeddingProviders] = useState<ProviderConfig[]>([]);
  const [loading, setLoading] = useState(true);

  const [policyForm, setPolicyForm] = useState<PolicyForm>(DEFAULT_POLICY);
  const [policySaving, setPolicySaving] = useState(false);
  const [policySaved, setPolicySaved] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState({ slug: "", name: "", description: "", embedding_config_id: "" });
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const [uploadKbId, setUploadKbId] = useState<string | null>(null);
  const [uploadTitle, setUploadTitle] = useState("");
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  // Each source fetched and caught independently (lesson 21): a viewer's
  // page must still show the KB/document lists even if a write-scoped
  // sibling fetch failed, and one 403 must never blank data the API
  // already returned for the others.
  const refresh = async () => {
    setLoading(true);
    let kbs: KnowledgeBase[] = [];
    let assigns: AgentKnowledgeBase[] = [];
    await Promise.allSettled([
      listKnowledgeBases(tenantId)
        .then((ks) => {
          kbs = ks;
          setAllKbs(ks);
        })
        .then(() => setKbsError(null))
        .catch((e) => setKbsError(e instanceof ApiError ? e.detail : String(e))),
      agentId
        ? listAgentKnowledgeBases(agentId)
            .then((a) => {
              assigns = a;
              setAssignments(a);
            })
            .then(() => setAssignmentsError(null))
            .catch((e) => setAssignmentsError(e instanceof ApiError ? e.detail : String(e)))
        : Promise.resolve(),
      listProviders(tenantId, { role: "embedding" })
        .then(setEmbeddingProviders)
        .then(() => setProvidersError(null))
        .catch((e) => setProvidersError(e instanceof ApiError ? e.detail : String(e))),
    ]);

    // Authoring lists documents per tenant KB; attach lists documents per
    // the agent's assigned KBs only.
    const kbIdsForDocs = agentId ? assigns.map((a) => a.kb_id) : kbs.map((kb) => kb.id);
    const docEntries = await Promise.all(kbIdsForDocs.map(async (id) => [id, await listDocuments(id)] as const));
    setDocsByKb(Object.fromEntries(docEntries));
    setLoading(false);
  };

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    if (agentId) {
      getRetrievalPolicy(agentId)
        .then((p) =>
          setPolicyForm({
            top_k: p.top_k ?? DEFAULT_POLICY.top_k,
            minimum_score: p.minimum_score ?? DEFAULT_POLICY.minimum_score,
            max_tokens: p.max_tokens ?? DEFAULT_POLICY.max_tokens,
            include_citations: p.include_citations ?? DEFAULT_POLICY.include_citations,
          }),
        )
        .catch((e) => setAssignmentsError(e instanceof ApiError ? e.detail : String(e)));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, agentId]);

  const assignedKbIds = new Set(assignments.map((a) => a.kb_id));
  const unassignedKbs = allKbs.filter((kb) => !assignedKbIds.has(kb.id));
  // Mirrors ElevenLabs' "Enable RAG" toggle: there's no separate on/off
  // switch in our model — retrieval is effectively on exactly when at
  // least one attached KB is enabled — but the settings detail fields
  // should only render while that's true, same as theirs.
  const ragEnabled = assignments.some((a) => a.enabled);

  const withErrorHandling = async (fn: () => Promise<unknown>, onError: (msg: string) => void) => {
    try {
      await fn();
      await refresh();
    } catch (e) {
      onError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleAttach = (kbId: string) => {
    if (!agentId) return;
    return withErrorHandling(() => assignKnowledgeBase(agentId, kbId), setAssignmentsError);
  };
  const handleToggleEnabled = (kbId: string, enabled: boolean) => {
    if (!agentId) return;
    return withErrorHandling(() => setKnowledgeBaseEnabled(agentId, kbId, enabled), setAssignmentsError);
  };
  const handleDetach = (kbId: string) => {
    if (!agentId) return;
    if (!confirm("Detach this knowledge base from the agent? Documents themselves are not deleted.")) return;
    withErrorHandling(() => detachKnowledgeBase(agentId, kbId), setAssignmentsError);
  };
  const handleUsageModeToggle = (doc: KbDocument) =>
    withErrorHandling(
      () => updateDocument(doc.id, { usage_mode: doc.usage_mode === "auto" ? "prompt" : "auto" }),
      setKbsError,
    );
  const handleDeleteDoc = (doc: KbDocument) => {
    if (!confirm(`Delete document "${doc.title}"?`)) return;
    withErrorHandling(() => deleteDocument(doc.id), setKbsError);
  };

  const handleCreateKb = async () => {
    setCreating(true);
    setCreateError(null);
    try {
      await createKnowledgeBase(tenantId, {
        slug: createForm.slug,
        name: createForm.name,
        description: createForm.description,
        embedding_config_id: createForm.embedding_config_id || undefined,
      });
      setCreateOpen(false);
      setCreateForm({ slug: "", name: "", description: "", embedding_config_id: "" });
      await refresh();
    } catch (e) {
      setCreateError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setCreating(false);
    }
  };

  const handleUpload = async () => {
    if (!uploadKbId || !uploadFile) return;
    setUploading(true);
    setUploadError(null);
    try {
      await uploadDocument(uploadKbId, uploadFile, uploadTitle || uploadFile.name);
      setUploadKbId(null);
      setUploadTitle("");
      setUploadFile(null);
      await refresh();
    } catch (e) {
      setUploadError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setUploading(false);
    }
  };

  const handleSavePolicy = async () => {
    if (!agentId) return;
    setPolicySaving(true);
    setPolicySaved(false);
    try {
      await setRetrievalPolicy(agentId, policyForm);
      setPolicySaved(true);
      setTimeout(() => setPolicySaved(false), 2000);
    } catch (e) {
      setAssignmentsError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setPolicySaving(false);
    }
  };

  const statusBadge = (status: KbDocument["status"]) => {
    const cls = status === "ready" ? "green" : status === "failed" ? "red" : status === "processing" ? "amber" : "gray";
    return <span className={`badge ${cls}`}>{status}</span>;
  };

  if (loading) return <div className="empty-state">Loading…</div>;

  // Authoring: every tenant KB. Attach: only the agent's assigned KBs.
  const rows = agentId
    ? assignments.map((a) => ({ id: a.kb_id, name: a.kb_name, enabled: a.enabled as boolean | null }))
    : allKbs.map((kb) => ({ id: kb.id, name: kb.name, enabled: null as boolean | null }));

  return (
    <div className="cols">
      <div className="col-main">
        {kbsError && <div className="error-banner">{kbsError}</div>}
        {agentId && assignmentsError && <div className="error-banner">{assignmentsError}</div>}
        {providersError && <div className="error-banner">{providersError}</div>}

        <div className="card">
          <div className="card-hdr">
            <div className="card-title">{agentId ? "Attached Knowledge Bases" : "Knowledge Bases"}</div>
            <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
              {agentId && unassignedKbs.length > 0 && (
                <select
                  className="form-select"
                  style={{ width: 200 }}
                  defaultValue=""
                  onChange={(e) => {
                    if (e.target.value) handleAttach(e.target.value);
                    e.target.value = "";
                  }}
                >
                  <option value="">+ Attach existing…</option>
                  {unassignedKbs.map((kb) => (
                    <option key={kb.id} value={kb.id}>
                      {kb.name}
                    </option>
                  ))}
                </select>
              )}
              {!agentId && (
                <button className="btn btn-primary btn-sm" onClick={() => setCreateOpen(true)}>
                  + New Knowledge Base
                </button>
              )}
            </div>
          </div>

          {rows.length === 0 ? (
            <div className="empty-state">
              {agentId
                ? "No knowledge bases attached — this agent behaves exactly as it did before RAG existed, with zero added latency."
                : "No knowledge bases yet."}
            </div>
          ) : (
            rows.map((row) => {
              const docs = docsByKb[row.id] || [];
              const isExpanded = !!expanded[row.id];
              return (
                <div key={row.id}>
                  <div className="kb-row">
                    <button
                      className="btn btn-ghost btn-sm"
                      style={{ padding: "2px 6px" }}
                      onClick={() => setExpanded({ ...expanded, [row.id]: !isExpanded })}
                    >
                      {isExpanded ? "▾" : "▸"}
                    </button>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontWeight: 500 }}>{row.name}</div>
                      <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
                        {docs.length} document{docs.length === 1 ? "" : "s"}
                      </div>
                    </div>
                    {agentId && (
                      <label
                        className="toggle-switch"
                        title={row.enabled ? "Enabled — retrieved when relevant" : "Disabled — never retrieved"}
                      >
                        <input
                          type="checkbox"
                          checked={!!row.enabled}
                          onChange={(e) => handleToggleEnabled(row.id, e.target.checked)}
                        />
                        <span className="toggle-slider" />
                      </label>
                    )}
                    {!agentId && (
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => {
                          setUploadKbId(row.id);
                          setUploadTitle("");
                          setUploadFile(null);
                          setUploadError(null);
                        }}
                      >
                        + Document
                      </button>
                    )}
                    {agentId && (
                      <button className="btn btn-danger btn-sm" onClick={() => handleDetach(row.id)}>
                        Detach
                      </button>
                    )}
                  </div>
                  {isExpanded &&
                    (docs.length === 0 ? (
                      <div className="kb-doc-row" style={{ color: "var(--text-3)" }}>
                        No documents yet.
                      </div>
                    ) : (
                      docs.map((doc) => (
                        <div key={doc.id} className="kb-doc-row">
                          <div style={{ flex: 1 }}>
                            <span className="mono">{doc.title}</span>
                            {doc.error && <div style={{ color: "var(--red)", fontSize: ".68rem" }}>{doc.error}</div>}
                          </div>
                          {statusBadge(doc.status)}
                          {agentId ? (
                            doc.usage_mode === "prompt" && (
                              <span className="badge gray" title="Always injected into the LLM prompt every turn">
                                always in prompt
                              </span>
                            )
                          ) : (
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
                          )}
                          {!agentId && (
                            <button className="btn btn-ghost btn-sm" onClick={() => handleDeleteDoc(doc)}>
                              Delete
                            </button>
                          )}
                        </div>
                      ))
                    ))}
                </div>
              );
            })
          )}
        </div>
      </div>

      {agentId && (
        <div className="col-side">
          <div className="card">
            <div className="card-hdr">
              <div className="card-title">Retrieval Settings</div>
              {ragEnabled && <div className="card-sub">unset falls back to the platform default</div>}
            </div>
            <div className="card-body">
              {!ragEnabled ? (
                <div style={{ fontSize: ".76rem", color: "var(--text-3)", lineHeight: 1.5 }}>
                  Attach and enable at least one knowledge base to configure retrieval — with none enabled, this agent
                  behaves exactly as it did before RAG existed, with zero added latency.
                </div>
              ) : (
                <>
                  <div className="form-group">
                    <label className="form-label">
                      Chunk limit <span className="hint">chunks retrieved per query</span>
                    </label>
                    <input
                      className="form-input"
                      type="number"
                      min={1}
                      max={50}
                      value={policyForm.top_k}
                      onChange={(e) => setPolicyForm({ ...policyForm, top_k: Number(e.target.value) })}
                    />
                  </div>
                  <div className="form-group">
                    <label className="form-label">
                      Character limit <span className="hint">total retrieved per query</span>
                    </label>
                    <input
                      className="form-input"
                      type="number"
                      min={100}
                      step={100}
                      value={policyForm.max_tokens}
                      onChange={(e) => setPolicyForm({ ...policyForm, max_tokens: Number(e.target.value) })}
                    />
                  </div>
                  <div className="form-group">
                    <label className="form-label">
                      Vector distance limit <span className="hint">chunks below this similarity are never retrieved</span>
                    </label>
                    <input
                      className="form-range"
                      style={{ width: "100%" }}
                      type="range"
                      min={0}
                      max={1}
                      step={0.05}
                      value={policyForm.minimum_score}
                      onChange={(e) => setPolicyForm({ ...policyForm, minimum_score: Number(e.target.value) })}
                    />
                    <div className="form-range-row" style={{ justifyContent: "space-between" }}>
                      <span className="form-range-label">More similar</span>
                      <span className="form-range-label">Less similar</span>
                    </div>
                  </div>
                  <div className="form-group" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <label className="toggle-switch">
                      <input
                        type="checkbox"
                        checked={policyForm.include_citations}
                        onChange={(e) => setPolicyForm({ ...policyForm, include_citations: e.target.checked })}
                      />
                      <span className="toggle-slider" />
                    </label>
                    <span className="form-label" style={{ margin: 0 }}>
                      Include source citations
                    </span>
                  </div>
                  <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 4 }}>
                    {policySaved && <span style={{ alignSelf: "center", fontSize: ".76rem", color: "var(--green)" }}>Saved ✓</span>}
                    <button className="btn btn-primary btn-sm" onClick={handleSavePolicy} disabled={policySaving}>
                      {policySaving ? "Saving…" : "Save"}
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {!agentId && (
        <Modal
          open={createOpen}
          title="New Knowledge Base"
          onClose={() => setCreateOpen(false)}
          footer={
            <>
              <button className="btn btn-ghost btn-sm" onClick={() => setCreateOpen(false)}>
                Cancel
              </button>
              <button
                className="btn btn-primary btn-sm"
                onClick={handleCreateKb}
                disabled={creating || !createForm.slug || !createForm.name}
              >
                {creating ? "Creating…" : "Create"}
              </button>
            </>
          }
        >
          {createError && <div className="error-banner">{createError}</div>}
          <div className="form-group">
            <label className="form-label">
              Name <span className="required">*</span>
            </label>
            <input
              className="form-input"
              value={createForm.name}
              onChange={(e) => setCreateForm({ ...createForm, name: e.target.value })}
              placeholder="Reception FAQ"
            />
          </div>
          <div className="form-group">
            <label className="form-label">
              Slug <span className="required">*</span>
            </label>
            <input
              className="form-input"
              style={{ fontFamily: "var(--mono)" }}
              value={createForm.slug}
              onChange={(e) => setCreateForm({ ...createForm, slug: e.target.value })}
              placeholder="reception-faq"
            />
          </div>
          <div className="form-group">
            <label className="form-label">Description</label>
            <textarea
              className="form-textarea"
              value={createForm.description}
              onChange={(e) => setCreateForm({ ...createForm, description: e.target.value })}
            />
          </div>
          <div className="form-group">
            <label className="form-label">
              Embedding Provider <span className="hint">only needed for documents over ~500 bytes</span>
            </label>
            <select
              className="form-select"
              value={createForm.embedding_config_id}
              onChange={(e) => setCreateForm({ ...createForm, embedding_config_id: e.target.value })}
            >
              <option value="">— none (prompt-only KB) —</option>
              {embeddingProviders.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
        </Modal>
      )}

      {!agentId && (
        <Modal
          open={!!uploadKbId}
          title="Upload Document"
          onClose={() => setUploadKbId(null)}
          footer={
            <>
              <button className="btn btn-ghost btn-sm" onClick={() => setUploadKbId(null)}>
                Cancel
              </button>
              <button className="btn btn-primary btn-sm" onClick={handleUpload} disabled={uploading || !uploadFile}>
                {uploading ? "Uploading…" : "Upload"}
              </button>
            </>
          }
        >
          {uploadError && <div className="error-banner">{uploadError}</div>}
          <div className="form-group">
            <label className="form-label">Title</label>
            <input
              className="form-input"
              value={uploadTitle}
              onChange={(e) => setUploadTitle(e.target.value)}
              placeholder="defaults to filename"
            />
          </div>
          <div className="form-group">
            <label className="form-label">
              File <span className="hint">.txt or .md only, for now</span>
            </label>
            <input
              className="form-input"
              type="file"
              accept=".txt,.md,text/plain,text/markdown"
              onChange={(e) => setUploadFile(e.target.files?.[0] || null)}
            />
          </div>
        </Modal>
      )}
    </div>
  );
}
