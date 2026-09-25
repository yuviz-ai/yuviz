"use client";

import { useEffect, useState } from "react";
import {
  Agent,
  ApiError,
  createProvider,
  deleteProvider,
  listAgents,
  listProviders,
  ProviderConfig,
  ProviderConfigCreate,
  ProviderConfigUpdate,
  ProviderEnvironment,
  ProviderRole,
  updateProvider,
} from "@/lib/api";
import { listKnowledgeBases } from "@/lib/knowledgeApi";
import { Modal } from "@/components/Modal";
import { SecretRefInput, secretPayload } from "./SecretRefInput";
import { EMBEDDING_MODELS_BY_ENGINE, ENGINES_BY_ROLE, LOCAL_ENGINES, MODELS_BY_ENGINE, OTHER, VOICES_BY_ENGINE } from "@/lib/engineCatalog";
import { useActiveTenant } from "@/lib/useActiveTenant";

const ALL_ROLES: ProviderRole[] = ["stt", "llm", "tts"];
const ENVIRONMENTS: ProviderEnvironment[] = ["prod", "staging", "dev"];

const emptyForm = (defaultRole: ProviderRole): ProviderConfigCreate => ({
  name: "",
  role: defaultRole,
  engine: ENGINES_BY_ROLE[defaultRole][0].value,
  environment: "prod",
});

export function ProvidersPanel({ allowedRoles = ALL_ROLES, title }: { allowedRoles?: ProviderRole[]; title?: string }) {
  const ROLES = allowedRoles;
  const { tenant: activeTenant, isAllTenants, loading: tenantLoading } = useActiveTenant();
  const tenantId = activeTenant?.id ?? "";
  const [providers, setProviders] = useState<ProviderConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<ProviderConfig | null>(null);

  const [deleteTarget, setDeleteTarget] = useState<ProviderConfig | null>(null);
  // null = still checking; stt/llm/tts are used by agents, embedding by knowledge bases.
  const [deleteResourceType, setDeleteResourceType] = useState<"agent" | "knowledge_base" | null>(null);
  const [deleteResourceNames, setDeleteResourceNames] = useState<string[] | null>(null);
  const [deleteChecking, setDeleteChecking] = useState(false);
  const [deleteSubmitting, setDeleteSubmitting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const [form, setForm] = useState<ProviderConfigCreate>(emptyForm(allowedRoles[0]));
  const [modelChoice, setModelChoice] = useState<string>("");
  const [customModel, setCustomModel] = useState("");
  const [voiceChoice, setVoiceChoice] = useState<string>("");
  const [customVoice, setCustomVoice] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const refresh = () => {
    if (!tenantId) return;
    setLoading(true);
    listProviders(tenantId)
      .then((all) => setProviders(all.filter((p) => allowedRoles.includes(p.role))))
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps, react-hooks/set-state-in-effect
  useEffect(refresh, [tenantId]);

  const resetForm = () => {
    setForm(emptyForm(allowedRoles[0]));
    setModelChoice("");
    setCustomModel("");
    setVoiceChoice("");
    setCustomVoice("");
    setEditing(null);
  };

  const handleRoleChange = (role: ProviderRole) => {
    setForm({ ...form, role, engine: ENGINES_BY_ROLE[role][0].value });
    setModelChoice("");
    setCustomModel("");
    setVoiceChoice("");
    setCustomVoice("");
  };

  const handleEngineChange = (engine: string) => {
    // Local engines have no credential of their own.
    setForm({ ...form, engine, api_key_ref: LOCAL_ENGINES.has(engine) ? undefined : form.api_key_ref });
    setModelChoice("");
    setCustomModel("");
    setVoiceChoice("");
    setCustomVoice("");
  };

  const openCreate = () => {
    resetForm();
    setModalOpen(true);
  };

  const openEdit = (p: ProviderConfig) => {
    setEditing(p);
    setForm({ name: p.name, role: p.role, engine: p.engine, environment: p.environment, api_key_ref: p.api_key_ref || undefined });
    const models = MODELS_BY_ENGINE[p.engine];
    if (p.model) setModelChoice(models && models.includes(p.model) ? p.model : OTHER);
    if (p.model && !(models && models.includes(p.model))) setCustomModel(p.model);
    const voices = VOICES_BY_ENGINE[p.engine];
    const knownVoice = voices?.some((v) => v.id === p.voice) ?? false;
    if (p.voice) setVoiceChoice(knownVoice ? p.voice : OTHER);
    if (p.voice && !knownVoice) setCustomVoice(p.voice);
    setModalOpen(true);
  };

  const handleSubmit = async () => {
    setSubmitting(true);
    setFormError(null);
    try {
      const model = modelChoice === OTHER ? customModel : modelChoice || undefined;
      const voice = voiceChoice === OTHER ? customVoice : voiceChoice || undefined;
      if (editing) {
        const body: ProviderConfigUpdate = {
          name: form.name,
          engine: form.engine,
          environment: form.environment,
          model,
          voice,
          ...secretPayload(form.api_key_ref || "", editing.api_key_ref || ""),
        };
        await updateProvider(editing.id, body);
      } else {
        const { api_key_ref: typed, ...rest } = form;
        await createProvider(tenantId, { ...rest, model, voice, ...secretPayload(typed || "") });
      }
      setModalOpen(false);
      resetForm();
      refresh();
    } catch (e) {
      setFormError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const openDelete = async (p: ProviderConfig) => {
    setDeleteTarget(p);
    setDeleteResourceType(null);
    setDeleteResourceNames(null);
    setDeleteError(null);
    setDeleteChecking(true);
    try {
      if (p.role === "embedding") {
        const kbs = await listKnowledgeBases(p.tenant_id);
        setDeleteResourceType("knowledge_base");
        setDeleteResourceNames(
          kbs.filter((k) => k.status === "active" && k.embedding_config_id === p.id).map((k) => k.name),
        );
      } else if (p.role === "stt" || p.role === "llm" || p.role === "tts") {
        const tenantSlug = activeTenant?.id === p.tenant_id ? activeTenant.slug : undefined;
        const agents: Agent[] = tenantSlug ? await listAgents(tenantSlug) : [];
        const column = `${p.role}_config_id` as "stt_config_id" | "llm_config_id" | "tts_config_id";
        setDeleteResourceType("agent");
        setDeleteResourceNames(
          agents.filter((a) => a.status === "active" && a[column] === p.id).map((a) => a.name),
        );
      } else {
        setDeleteResourceNames([]);
      }
    } catch (e) {
      setDeleteError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setDeleteChecking(false);
    }
  };

  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return;
    setDeleteSubmitting(true);
    setDeleteError(null);
    try {
      await deleteProvider(deleteTarget.id);
      setDeleteTarget(null);
      refresh();
    } catch (e) {
      // 409 carries resource names — switch to the blocked variant.
      if (e instanceof ApiError && e.status === 409 && e.body?.resource_names) {
        setDeleteResourceType(e.body.resource_type as "agent" | "knowledge_base");
        setDeleteResourceNames(e.body.resource_names as string[]);
      } else {
        setDeleteError(e instanceof ApiError ? e.detail : String(e));
      }
    } finally {
      setDeleteSubmitting(false);
    }
  };

  const handleForceDelete = async () => {
    if (!deleteTarget) return;
    setDeleteSubmitting(true);
    setDeleteError(null);
    try {
      await deleteProvider(deleteTarget.id, true);
      setDeleteTarget(null);
      setDeleteResourceNames(null);
      refresh();
    } catch (e) {
      setDeleteError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setDeleteSubmitting(false);
    }
  };

  const modelOptions = form.role === "embedding" ? EMBEDDING_MODELS_BY_ENGINE[form.engine] : MODELS_BY_ENGINE[form.engine];
  const voiceOptions = VOICES_BY_ENGINE[form.engine];

  return (
    <>
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 14, gap: 10 }}>
        <button className="btn btn-primary btn-sm" onClick={openCreate} disabled={!tenantId}>
          + New Provider
        </button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        {title && (
          <div className="card-hdr">
            <div className="card-title">{title}</div>
          </div>
        )}
        {tenantLoading || loading ? (
          <div className="empty-state">Loading…</div>
        ) : isAllTenants ? (
          <div className="empty-state">Select a specific account above to view and manage its providers.</div>
        ) : providers.length === 0 ? (
          <div className="empty-state">No providers configured for this tenant yet.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Name</th>
                {ROLES.length > 1 && <th>Role</th>}
                <th>Engine</th>
                <th>Model / Voice</th>
                <th>Environment</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {providers.map((p) => (
                <tr key={p.id}>
                  <td className="bold">{p.name}</td>
                  {ROLES.length > 1 && (
                    <td>
                      <span className="badge indigo">{p.role.toUpperCase()}</span>
                    </td>
                  )}
                  <td className="mono">{p.engine}</td>
                  <td className="mono">{p.model || p.voice || "—"}</td>
                  <td>
                    <span className={`env-chip ${p.environment}`}>{p.environment}</span>
                  </td>
                  <td>
                    <button className="btn btn-ghost btn-sm" onClick={() => openEdit(p)}>
                      Edit
                    </button>{" "}
                    <button className="btn btn-danger btn-sm" onClick={() => openDelete(p)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Modal
        open={modalOpen}
        title={editing ? `Edit Provider — ${editing.name}` : "New Provider Config"}
        onClose={() => {
          setModalOpen(false);
          resetForm();
        }}
        footer={
          <>
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => {
                setModalOpen(false);
                resetForm();
              }}
            >
              Cancel
            </button>
            <button
              className="btn btn-primary btn-sm"
              onClick={handleSubmit}
              disabled={submitting || !form.name || !form.engine}
            >
              {submitting ? "Saving…" : editing ? "Save Changes" : "Create Provider"}
            </button>
          </>
        }
      >
        {formError && <div className="error-banner">{formError}</div>}
        <div className="form-group">
          <label className="form-label">
            Name <span className="required">*</span>
          </label>
          <input className="form-input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Deepgram Nova-3" />
        </div>
        <div className="form-row">
          {ROLES.length > 1 && (
            <div className="form-group">
              <label className="form-label">
                Role {editing && <span className="hint">(fixed after creation)</span>}
              </label>
              <select
                className="form-select"
                value={form.role}
                onChange={(e) => handleRoleChange(e.target.value as ProviderRole)}
                disabled={!!editing}
              >
                {ROLES.map((r) => (
                  <option key={r} value={r}>
                    {r.toUpperCase()}
                  </option>
                ))}
              </select>
            </div>
          )}
          <div className="form-group">
            <label className="form-label">Environment</label>
            <select
              className="form-select"
              value={form.environment}
              onChange={(e) => setForm({ ...form, environment: e.target.value as ProviderEnvironment })}
            >
              {ENVIRONMENTS.map((e) => (
                <option key={e} value={e}>
                  {e}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="form-group">
          <label className="form-label">
            Engine <span className="required">*</span>
          </label>
          <select className="form-select" value={form.engine} onChange={(e) => handleEngineChange(e.target.value)}>
            {ENGINES_BY_ROLE[form.role].map((eng) => (
              <option key={eng.value} value={eng.value}>
                {eng.label}
              </option>
            ))}
          </select>
        </div>

        {(form.role === "stt" || form.role === "llm" || form.role === "embedding") && (
          <div className="form-group">
            <label className="form-label">
              Model {form.role === "embedding" && <span className="hint">leave unset to use the engine&apos;s default</span>}
              {form.role === "stt" && form.engine === "faster_whisper" && (
                <span className="hint">
                  prefer a .en model (e.g. base.en) for English-only agents — the plain multilingual
                  models (base, small, …) auto-detect language per utterance and can mis-hear unclear
                  audio as a different language entirely, causing the agent to reply in the wrong
                  language mid-call
                </span>
              )}
            </label>
            {modelOptions ? (
              <>
                <select className="form-select" value={modelChoice} onChange={(e) => setModelChoice(e.target.value)}>
                  <option value="">— select a model —</option>
                  {modelOptions.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                  <option value={OTHER}>Other (custom)…</option>
                </select>
                {modelChoice === OTHER && (
                  <input
                    className="form-input"
                    style={{ marginTop: 6, fontFamily: "var(--mono)" }}
                    value={customModel}
                    onChange={(e) => setCustomModel(e.target.value)}
                    placeholder="custom model name"
                  />
                )}
              </>
            ) : (
              <input className="form-input" style={{ fontFamily: "var(--mono)" }} value={customModel} onChange={(e) => setCustomModel(e.target.value)} placeholder="model name" />
            )}
          </div>
        )}

        {form.role === "tts" && (
          <div className="form-group">
            <label className="form-label">
              Voice {form.engine === "elevenlabs" && <span className="hint">account-specific voice_id — enter your own</span>}
            </label>
            {voiceOptions ? (
              <>
                <select className="form-select" value={voiceChoice} onChange={(e) => setVoiceChoice(e.target.value)}>
                  <option value="">— select a voice —</option>
                  {voiceOptions.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.label} ({v.gender})
                    </option>
                  ))}
                  <option value={OTHER}>Other (custom)…</option>
                </select>
                {voiceChoice === OTHER && (
                  <input
                    className="form-input"
                    style={{ marginTop: 6, fontFamily: "var(--mono)" }}
                    value={customVoice}
                    onChange={(e) => setCustomVoice(e.target.value)}
                    placeholder="custom voice name"
                  />
                )}
              </>
            ) : (
              <input
                className="form-input"
                style={{ fontFamily: "var(--mono)" }}
                value={customVoice}
                onChange={(e) => setCustomVoice(e.target.value)}
                placeholder="e.g. 21m00Tcm4TlvDq8ikWAM"
              />
            )}
          </div>
        )}

        {!LOCAL_ENGINES.has(form.engine) && (
          <div className="form-group">
            <label className="form-label">
              API Key Ref{" "}
              <span className="hint">paste it — we encrypt it</span>
            </label>
            <SecretRefInput
              value={form.api_key_ref || ""}
              onChange={(v) => setForm({ ...form, api_key_ref: v })}
              canEncrypt
            />
          </div>
        )}
      </Modal>

      {(() => {
        const isBlocked = deleteResourceNames !== null && deleteResourceNames.length > 0;
        const resourceLabel = deleteResourceType === "knowledge_base" ? "Knowledge bases" : "Agents";
        return (
          <Modal
            open={deleteTarget !== null}
            title={
              deleteChecking
                ? `Checking "${deleteTarget?.name}"…`
                : isBlocked
                  ? `Can't delete "${deleteTarget?.name}"`
                  : `Delete "${deleteTarget?.name}"?`
            }
            onClose={() => setDeleteTarget(null)}
            footer={
              deleteChecking ? (
                <button className="btn btn-ghost btn-sm" onClick={() => setDeleteTarget(null)}>Cancel</button>
              ) : isBlocked ? (
                <>
                  <button className="btn btn-ghost btn-sm" onClick={() => setDeleteTarget(null)}>Cancel</button>
                  <button className="btn btn-danger btn-sm" onClick={handleForceDelete} disabled={deleteSubmitting}>
                    {deleteSubmitting ? "Deleting…" : "Force delete anyway"}
                  </button>
                </>
              ) : (
                <>
                  <button className="btn btn-ghost btn-sm" onClick={() => setDeleteTarget(null)}>Cancel</button>
                  <button className="btn btn-danger btn-sm" onClick={handleDeleteConfirm} disabled={deleteSubmitting}>
                    {deleteSubmitting ? "Deleting…" : "Delete provider"}
                  </button>
                </>
              )
            }
          >
            {deleteError && <div className="error-banner">{deleteError}</div>}
            {deleteChecking ? (
              <p style={{ fontSize: ".78rem", color: "var(--text-3)" }}>
                Checking for agents and knowledge bases still using this provider…
              </p>
            ) : isBlocked && deleteResourceNames ? (
              <>
                <div style={{ padding: "10px 0", borderBottom: "1px solid var(--border-2)", marginBottom: 12 }}>
                  <div style={{ fontSize: ".64rem", color: "var(--text-3)", textTransform: "uppercase", letterSpacing: ".05em", marginBottom: 3 }}>
                    {resourceLabel} using this provider
                  </div>
                  <div style={{ fontSize: "1rem", fontFamily: "var(--mono)", color: "var(--text)" }}>{deleteResourceNames.length}</div>
                </div>
                <p style={{
                  fontSize: ".78rem", color: "var(--text-2)", lineHeight: 1.5,
                  borderLeft: "2px solid var(--red-border)", padding: "6px 0 6px 10px", margin: 0,
                }}>
                  {(() => {
                    const many = deleteResourceNames.length > 1;
                    const consequence = deleteResourceType === "knowledge_base"
                      ? `the next document it ingests, or query it answers, would fail to resolve its embedding provider the moment this provider is gone. Reassign ${many ? "them" : "it"} to a different embedding provider first`
                      : `the next call ${many ? "either handles" : "it handles"} would fail to resolve it mid-setup the moment this provider is gone. Reassign ${many ? "them" : "it"} to a different provider first`;
                    return (
                      <>
                        <b>{deleteResourceNames.join(", ")}</b>{" "}
                        {`${many ? "use" : "uses"} this provider right now — ${consequence}, or force the delete if you're certain.`}
                      </>
                    );
                  })()}
                </p>
              </>
            ) : (
              <p style={{
                fontSize: ".78rem", color: "var(--text-2)", lineHeight: 1.5,
                borderLeft: "2px solid var(--green-border)", padding: "6px 0 6px 10px", margin: 0,
              }}>
                {deleteResourceType === "knowledge_base"
                  ? "No active knowledge bases use this provider."
                  : "No active agents use this provider."} This cannot be undone.
              </p>
            )}
          </Modal>
        );
      })()}
    </>
  );
}
