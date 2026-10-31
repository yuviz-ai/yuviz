"use client";

import { ReactNode, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { Check, X } from "lucide-react";
import { ApiError } from "@/lib/api";
import {
  getRetrievalPolicy,
  KbDocument,
  listAgentDocuments,
  listDocuments,
  listKnowledgeBases,
  replaceAgentDocuments,
  setRetrievalPolicy,
} from "@/lib/knowledgeApi";
import { FILE_STATUS } from "@/lib/knowledgePresentation";

interface PolicyForm {
  top_k: number;
  minimum_score: number;
  max_tokens: number;
  include_citations: boolean;
}

// The server's system defaults (services/knowledge/retrieval_plan.py), shown when the agent has no row of its own.
const DEFAULT_POLICY: PolicyForm = { top_k: 5, minimum_score: 0.3, max_tokens: 1000, include_citations: true };

const sameSet = (a: string[], b: string[]) => a.length === b.length && a.every((id) => b.includes(id));
const samePolicy = (a: PolicyForm, b: PolicyForm) =>
  a.top_k === b.top_k && a.minimum_score === b.minimum_score && a.max_tokens === b.max_tokens
  && a.include_citations === b.include_citations;

// The agent's knowledge: which of the account's documents it answers from, and how it searches them.
export function KnowledgeBasePanel({ tenantId, agentId, children }: { tenantId: string; agentId: string; children?: ReactNode }) {
  const [documents, setDocuments] = useState<KbDocument[]>([]);
  const [savedIds, setSavedIds] = useState<string[]>([]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [savedPolicy, setSavedPolicy] = useState<PolicyForm>(DEFAULT_POLICY);
  const [policy, setPolicy] = useState<PolicyForm>(DEFAULT_POLICY);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [query, setQuery] = useState("");
  const pickerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const [kbs, linked, current] = await Promise.all([
          listKnowledgeBases(tenantId),
          listAgentDocuments(agentId),
          getRetrievalPolicy(agentId).catch(() => null),
        ]);
        const perKb = await Promise.all(kbs.map((kb) => listDocuments(kb.id)));
        if (cancelled) return;
        const ids = linked.map((d) => d.document_id);
        const loadedPolicy: PolicyForm = {
          top_k: current?.top_k ?? DEFAULT_POLICY.top_k,
          minimum_score: current?.minimum_score ?? DEFAULT_POLICY.minimum_score,
          max_tokens: current?.max_tokens ?? DEFAULT_POLICY.max_tokens,
          include_citations: current?.include_citations ?? DEFAULT_POLICY.include_citations,
        };
        setDocuments(perKb.flat().sort((a, b) => a.title.localeCompare(b.title)));
        setSavedIds(ids);
        setSelectedIds(ids);
        setSavedPolicy(loadedPolicy);
        setPolicy(loadedPolicy);
        setError(null);
      } catch (e) {
        if (!cancelled) setError(e instanceof ApiError ? e.detail : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [tenantId, agentId]);

  useEffect(() => {
    if (!pickerOpen) return;
    const close = (e: MouseEvent) => {
      if (!pickerRef.current?.contains(e.target as Node)) setPickerOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [pickerOpen]);

  const byId = useMemo(() => new Map(documents.map((d) => [d.id, d])), [documents]);
  const selected = selectedIds.map((id) => byId.get(id)).filter((d): d is KbDocument => !!d);
  const choices = documents.filter(
    (d) => !selectedIds.includes(d.id) && d.title.toLowerCase().includes(query.trim().toLowerCase()),
  );
  const dirty = !sameSet(selectedIds, savedIds) || !samePolicy(policy, savedPolicy);

  const add = (id: string) => {
    setSelectedIds((ids) => [...ids, id]);
    setQuery("");
  };
  const remove = (id: string) => setSelectedIds((ids) => ids.filter((x) => x !== id));

  const cancel = () => {
    setSelectedIds(savedIds);
    setPolicy(savedPolicy);
    setError(null);
  };

  // Two writes: the document set, then the search settings. A failure says which one did not save.
  const save = async () => {
    setSaving(true);
    setError(null);
    let wroteDocuments = false;
    try {
      if (!sameSet(selectedIds, savedIds)) {
        const linked = await replaceAgentDocuments(agentId, selectedIds);
        const ids = linked.map((d) => d.document_id);
        setSavedIds(ids);
        setSelectedIds(ids);
        wroteDocuments = true;
      }
      if (!samePolicy(policy, savedPolicy)) {
        await setRetrievalPolicy(agentId, policy);
        setSavedPolicy(policy);
      }
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      const detail = e instanceof ApiError ? e.detail : String(e);
      setError(wroteDocuments
        ? `Documents saved, but the search settings were not: ${detail}`
        : `Not saved: ${detail}`);
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="empty-state">Loading…</div>;

  return (
    <div className="knowledge-agent-panel">
      {error && <div className="error-banner" role="alert">{error}</div>}

      <section className="card">
        <div className="card-hdr">
          <div>
            <div className="card-title">Documents</div>
            <div className="card-sub" style={{ marginLeft: 0 }}>
              Tick the files this agent answers from. Uploads and reindexing happen on the Knowledge page.
            </div>
          </div>
        </div>
        <div className="card-body">
          {documents.length === 0 ? (
            <div className="empty-state">
              No files yet. <Link href="/knowledge-bases">Upload one on the Knowledge page</Link>, then pick it here.
            </div>
          ) : (
            <div className="kb-picker" ref={pickerRef}>
              <div className="kb-chips" onClick={() => setPickerOpen(true)}>
                {selected.map((d) => (
                  <span key={d.id} className="kb-chip">
                    <span className="kb-chip-title">{d.title}</span>
                    {d.status !== "ready" && (
                      <span className={`badge ${FILE_STATUS[d.status].color}`}>{FILE_STATUS[d.status].label}</span>
                    )}
                    <button
                      type="button"
                      className="kb-chip-remove"
                      aria-label={`Remove ${d.title}`}
                      onClick={(e) => { e.stopPropagation(); remove(d.id); }}
                    >
                      <X size={13} />
                    </button>
                  </span>
                ))}
                <input
                  className="kb-chips-input"
                  placeholder={selected.length ? "" : "Add a document…"}
                  role="combobox"
                  aria-label="Add a document"
                  aria-expanded={pickerOpen}
                  aria-controls="kb-picker-menu"
                  value={query}
                  onFocus={() => setPickerOpen(true)}
                  onChange={(e) => { setQuery(e.target.value); setPickerOpen(true); }}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") setPickerOpen(false);
                    if (e.key === "Backspace" && !query && selected.length) remove(selected[selected.length - 1].id);
                  }}
                />
              </div>
              {pickerOpen && (
                <div id="kb-picker-menu" className="kb-picker-menu" role="listbox" aria-label="Documents to add">
                  {choices.length === 0 ? (
                    <div className="kb-picker-empty">{query ? "No files match." : "Every file is selected."}</div>
                  ) : (
                    choices.map((d) => (
                      <button
                        key={d.id}
                        type="button"
                        role="option"
                        aria-selected={false}
                        className="kb-picker-option"
                        onClick={() => add(d.id)}
                      >
                        <span>{d.title}</span>
                        <span className={`badge ${FILE_STATUS[d.status].color}`}>{FILE_STATUS[d.status].label}</span>
                      </button>
                    ))
                  )}
                </div>
              )}
              <div className="kb-picker-hint">Click a document to add it. Uploads and reindexing happen on the Knowledge page.</div>
            </div>
          )}
        </div>
        {documents.length > 0 && (
          <div className="kb-picker-count">
            <strong>{selected.length}</strong> of {documents.length} selected
          </div>
        )}
      </section>

      <section className="card">
        <div className="card-hdr">
          <div>
            <div className="card-title">Search settings</div>
            <div className="card-sub" style={{ marginLeft: 0 }}>
              How the agent looks up answers in the documents above. The defaults work for most agents.
            </div>
          </div>
        </div>
        <div className="card-body knowledge-search-settings">
          <div className="form-group">
            <label className="form-label" htmlFor="kb-top-k">
              Snippets to read <span className="hint">passages checked per question</span>
            </label>
            <input
              id="kb-top-k"
              className="form-input"
              type="number"
              min={1}
              max={50}
              value={policy.top_k}
              onChange={(e) => setPolicy({ ...policy, top_k: Number(e.target.value) })}
            />
          </div>
          <div className="form-group">
            <label className="form-label" htmlFor="kb-max-tokens">
              Max text to read <span className="hint">per question</span>
            </label>
            <input
              id="kb-max-tokens"
              className="form-input"
              type="number"
              min={100}
              step={100}
              value={policy.max_tokens}
              onChange={(e) => setPolicy({ ...policy, max_tokens: Number(e.target.value) })}
            />
          </div>
          <div className="form-group knowledge-search-wide">
            <label className="form-label" htmlFor="kb-min-score">
              How close a match must be <span className="hint">stricter gives fewer, more relevant passages</span>
            </label>
            <input
              id="kb-min-score"
              className="form-range"
              style={{ width: "100%" }}
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={policy.minimum_score}
              onChange={(e) => setPolicy({ ...policy, minimum_score: Number(e.target.value) })}
            />
            <div className="form-range-row" style={{ justifyContent: "space-between" }}>
              <span className="form-range-label">Looser</span>
              <span className="form-range-label">Stricter</span>
            </div>
          </div>
          <div className="form-group knowledge-search-wide" style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <label className="toggle-switch">
              <input
                type="checkbox"
                aria-label="Mention where answers come from"
                checked={policy.include_citations}
                onChange={(e) => setPolicy({ ...policy, include_citations: e.target.checked })}
              />
              <span className="toggle-slider" />
            </label>
            <div>
              <div className="form-label" style={{ margin: 0 }}>Mention where answers come from</div>
              <div className="hint">The agent can name the source document it used.</div>
            </div>
          </div>
        </div>
      </section>

      {children}

      <div className="knowledge-agent-footer">
        {saved && <span className="saved-note">Saved <Check size={13} /></span>}
        <button className="btn btn-ghost btn-sm" disabled={!dirty || saving} onClick={cancel}>Cancel</button>
        <button className="btn btn-primary btn-sm" disabled={!dirty || saving} onClick={save}>
          {saving ? "Saving…" : "Save changes"}
        </button>
      </div>
    </div>
  );
}
