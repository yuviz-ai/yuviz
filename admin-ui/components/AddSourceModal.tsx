"use client";

import { useState } from "react";
import { ApiError, Tenant } from "@/lib/api";
import { createKnowledgeBase, KnowledgeBase, uploadDocument } from "@/lib/knowledgeApi";
import { Modal } from "@/components/Modal";

// Only "Upload files" is wired up (AC5-AC7) — the other three cards exist so
// the picker reads as the eventual full set, matching every other SaaS KB
// import screen, but each of them just explains what's coming rather than
// doing anything yet.
const SOURCE_CARDS = [
  { key: "upload", label: "Upload files", hint: ".txt or .md", enabled: true },
  { key: "website", label: "Sync a website", hint: "coming soon", enabled: false },
  { key: "text", label: "Write text", hint: "coming soon", enabled: false },
  { key: "integration", label: "Connect an integration", hint: "coming soon", enabled: false },
] as const;

export const ACCEPTED_DOC_EXTENSIONS = [".txt", ".md"];
export const ACCEPTED_DOC_ACCEPT = ".txt,.md,text/plain,text/markdown";

// Extension-lowercased check plus, when the browser supplies one, the MIME
// type — matches services/knowledge/ingestion_worker.py's
// _SUPPORTED_CONTENT_TYPES exactly, never widening beyond it. Extension
// alone is already enforced by the <input accept> attribute; this exists
// because drag-and-drop bypasses `accept` (AC7).
const ACCEPTED_MIME_TYPES = ["text/plain", "text/markdown"];

export function rejectionReasonFor(file: File): string | null {
  const name = file.name.toLowerCase();
  const hasAcceptedExtension = ACCEPTED_DOC_EXTENSIONS.some((ext) => name.endsWith(ext));
  if (!hasAcceptedExtension) {
    return `"${file.name}" isn't a .txt or .md file.`;
  }
  if (file.type && !ACCEPTED_MIME_TYPES.includes(file.type)) {
    return `"${file.name}" isn't a .txt or .md file.`;
  }
  return null;
}

function deriveSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

export function AddSourceModal({
  tenants,
  knowledgeBases,
  canManage,
  onClose,
  onCreated,
}: {
  tenants: Tenant[];
  knowledgeBases: KnowledgeBase[];
  canManage: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [card, setCard] = useState<(typeof SOURCE_CARDS)[number]["key"] | null>(null);

  const [tenantId, setTenantId] = useState(tenants.length === 1 ? tenants[0].id : "");
  const [target, setTarget] = useState<"new" | string>("new");
  const [newName, setNewName] = useState("");
  const [newSlug, setNewSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const tenantKbs = knowledgeBases.filter((kb) => kb.tenant_id === tenantId);

  const handleNameChange = (value: string) => {
    setNewName(value);
    if (!slugTouched) setNewSlug(deriveSlug(value));
  };

  const handleFileChange = (f: File | null) => {
    setFile(null);
    setFileError(null);
    if (!f) return;
    const reason = rejectionReasonFor(f);
    if (reason) {
      setFileError(reason);
      return;
    }
    setFile(f);
  };

  const canSubmit =
    canManage && !!tenantId && !!file && !fileError && (target !== "new" || (!!newName && !!newSlug));

  const handleSubmit = async () => {
    if (!file) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const kb =
        target === "new"
          ? await createKnowledgeBase(tenantId, { slug: newSlug, name: newName })
          : { id: target };
      await uploadDocument(kb.id, file, file.name);
      onCreated();
      onClose();
    } catch (e) {
      setSubmitError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  if (card === null) {
    return (
      <Modal open title="Add source" onClose={onClose}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          {SOURCE_CARDS.map((c) => (
            <button
              key={c.key}
              className="btn btn-ghost"
              style={{ flexDirection: "column", height: 90, opacity: c.enabled ? 1 : 0.5 }}
              disabled={!c.enabled}
              onClick={() => setCard(c.key)}
            >
              <div style={{ fontWeight: 500 }}>{c.label}</div>
              <div style={{ fontSize: ".72rem", color: "var(--text-3)" }}>{c.hint}</div>
            </button>
          ))}
        </div>
      </Modal>
    );
  }

  return (
    <Modal
      open
      title="Upload files"
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost btn-sm" onClick={() => setCard(null)}>
            Back
          </button>
          <button className="btn btn-primary btn-sm" onClick={handleSubmit} disabled={!canSubmit || submitting}>
            {submitting ? "Uploading…" : "Upload"}
          </button>
        </>
      }
    >
      {submitError && <div className="error-banner">{submitError}</div>}

      {tenants.length > 1 && (
        <div className="form-group">
          <label className="form-label">Account</label>
          <select
            className="form-select"
            value={tenantId}
            onChange={(e) => {
              setTenantId(e.target.value);
              setTarget("new");
            }}
          >
            <option value="">— select —</option>
            {tenants.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </div>
      )}

      <div className="form-group">
        <label className="form-label">Knowledge base</label>
        <select className="form-select" value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="new">+ Create new</option>
          {tenantKbs.map((kb) => (
            <option key={kb.id} value={kb.id}>
              {kb.name}
            </option>
          ))}
        </select>
      </div>

      {target === "new" && (
        <>
          <div className="form-group">
            <label className="form-label">
              Name <span className="required">*</span>
            </label>
            <input
              className="form-input"
              value={newName}
              onChange={(e) => handleNameChange(e.target.value)}
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
              value={newSlug}
              onChange={(e) => {
                setSlugTouched(true);
                setNewSlug(e.target.value);
              }}
              placeholder="reception-faq"
            />
          </div>
        </>
      )}

      <div className="form-group">
        <label className="form-label">
          File <span className="hint">.txt or .md only, for now</span>
        </label>
        <input
          className="form-input"
          type="file"
          accept={ACCEPTED_DOC_ACCEPT}
          onChange={(e) => handleFileChange(e.target.files?.[0] || null)}
        />
        {fileError && <div style={{ color: "var(--red)", fontSize: ".76rem", marginTop: 4 }}>{fileError}</div>}
      </div>
    </Modal>
  );
}
