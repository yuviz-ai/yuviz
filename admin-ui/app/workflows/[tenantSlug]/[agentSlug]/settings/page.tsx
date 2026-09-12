"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { Agent, AgentStatus, AgentUpdate, ApiError, deleteAgent, getAgent, listProviders, ProviderConfig, updateAgent, updateProvider } from "@/lib/api";
import { KnowledgeBaseTabs } from "@/components/KnowledgeBaseTabs";
import { ToolsPanel } from "@/components/ToolsPanel";
import { SipPanel } from "@/components/SipPanel";
import { TestAgentPanel } from "@/components/TestAgentPanel";
import { LocalVoicePicker } from "@/components/LocalVoicePicker";
import { ElevenLabsVoicePicker } from "@/components/ElevenLabsVoicePicker";
import { LANGUAGES, OTHER, asBrowsableTtsEngine } from "@/lib/engineCatalog";

type Tab = "overview" | "behaviour" | "escalation" | "sip" | "tools" | "knowledge-base";

export default function AgentDetailPage() {
  const params = useParams<{ tenantSlug: string; agentSlug: string }>();
  const router = useRouter();
  const { tenantSlug, agentSlug } = params;

  const [agent, setAgent] = useState<Agent | null>(null);
  const [providers, setProviders] = useState<ProviderConfig[]>([]);
  const [tab, setTab] = useState<Tab>("overview");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [testingAgent, setTestingAgent] = useState(false);

  const [form, setForm] = useState<AgentUpdate>({});
  const [languageChoice, setLanguageChoice] = useState<string>("");
  const [customLanguage, setCustomLanguage] = useState("");
  // Explicit override once the user picks an engine from the chooser (or
  // clicks "Change engine") — null defers to whatever engine the agent's
  // current tts_config_id actually points at, so the Voice card only ever
  // shows the picker matching the configured provider, never an unrelated
  // engine's voices (picking one there would silently swap tts_config_id
  // to a different provider without it being obvious that happened).
  const [chosenEngine, setChosenEngine] = useState<"macos" | "kokoro" | "elevenlabs" | null>(null);
  const [showEngineChooser, setShowEngineChooser] = useState(false);

  // Greeting / system prompt live on the canvas; end-call and transfer
  // speech still use agent columns (pipeline _prompt_suffix / scripted lines).

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    getAgent(tenantSlug, agentSlug)
      .then(async (a) => {
        setAgent(a);
        setForm({
          name: a.name,
          goodbye_grace_ms: a.goodbye_grace_ms,
          language: a.language,
          stt_config_id: a.stt_config_id,
          llm_config_id: a.llm_config_id,
          tts_config_id: a.tts_config_id,
          transfer_type: a.transfer_type,
          transfer_destination: a.transfer_destination,
          queue_id: a.queue_id,
          escalation_threshold: a.escalation_threshold,
          caller_id_policy: a.caller_id_policy,
          platform_did: a.platform_did,
          custom_caller_id: a.custom_caller_id,
          transfer_waiting_experience: a.transfer_waiting_experience,
          max_call_duration_s: a.max_call_duration_s,
          status: a.status,
          end_call_prompt: a.end_call_prompt,
          transfer_prompt: a.transfer_prompt,
          farewell_message: a.farewell_message,
          transfer_announcement: a.transfer_announcement,
        });
        if (a.language && LANGUAGES.some((l) => l.value === a.language)) {
          setLanguageChoice(a.language);
        } else if (a.language) {
          setLanguageChoice(OTHER);
          setCustomLanguage(a.language);
        } else {
          setLanguageChoice(""); // derive from STT/TTS provider — the pre-this-field behavior
        }
        const provs = await listProviders(a.tenant_id);
        setProviders(provs);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  }, [tenantSlug, agentSlug]);

  // Picking a voice sets the agent's language to match it, rather than the
  // other way around — a voice is a concrete, single-language artifact,
  // while agent.language is a looser override (see its own "derive from
  // provider" default), so syncing from voice -> language is the direction
  // that can't produce a contradiction.
  const applyDetectedLanguage = (language: string) => {
    if (LANGUAGES.some((l) => l.value === language)) {
      setLanguageChoice(language);
    } else {
      setLanguageChoice(OTHER);
      setCustomLanguage(language);
    }
  };

  const handleSave = async () => {
    if (!agent) return;
    setSaving(true);
    setSaveError(null);
    setSaved(false);
    try {
      const language =
        languageChoice === "" ? null : languageChoice === OTHER ? customLanguage.trim() || null : languageChoice;
      const updated = await updateAgent(tenantSlug, agent.id, { ...form, language });
      setAgent(updated);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      setSaveError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!agent) return;
    if (!window.confirm(`Delete agent "${agent.name}"? Any DIDs still pointing at it will fall back to their fallback agent, or default.`)) return;
    setDeleting(true);
    try {
      await deleteAgent(tenantSlug, agent.id);
      router.push("/workflows");
    } catch (e) {
      setSaveError(e instanceof ApiError ? e.detail : String(e));
      setDeleting(false);
    }
  };

  if (loading) return <div className="empty-state">Loading…</div>;
  if (error) return <div className="error-banner">{error}</div>;
  if (!agent) return null;

  const byRole = (role: string) => providers.filter((p) => p.role === role);

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
        {/* A sub-route of the agent's own flow (2026-08-30), so back goes
            up one level to the canvas, not out to a list. */}
        <button
          className="btn btn-ghost btn-sm"
          onClick={() => router.push(`/workflows/${tenantSlug}/${agentSlug}`)}
        >
          ← Back to workflow
        </button>
        <button className="btn btn-primary btn-sm" onClick={() => setTestingAgent(true)}>
          🎙️ Test Agent
        </button>
      </div>

      <TestAgentPanel
        open={testingAgent}
        onClose={() => setTestingAgent(false)}
        tenantSlug={tenantSlug}
        agentSlug={agentSlug}
      />

      <div className="tabs">
        <button className={`tab${tab === "overview" ? " active" : ""}`} onClick={() => setTab("overview")}>
          Overview
        </button>
        <button className={`tab${tab === "behaviour" ? " active" : ""}`} onClick={() => setTab("behaviour")}>
          Behaviour
        </button>
        <button className={`tab${tab === "escalation" ? " active" : ""}`} onClick={() => setTab("escalation")}>
          Escalation
        </button>
        <button className={`tab${tab === "sip" ? " active" : ""}`} onClick={() => setTab("sip")}>
          SIP
        </button>
        <button className={`tab${tab === "tools" ? " active" : ""}`} onClick={() => setTab("tools")}>
          Tools
        </button>
        <button className={`tab${tab === "knowledge-base" ? " active" : ""}`} onClick={() => setTab("knowledge-base")}>
          Knowledge Base
        </button>
      </div>

      {saveError && <div className="error-banner">{saveError}</div>}

      {tab === "overview" && (
        <div className="cols">
          <div className="col-main">
            <div className="card">
              <div className="card-hdr">
                <div className="card-title">Identity</div>
                <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
                  <span className="ver-badge">v{agent.config_version}</span>
                  <span className={`badge ${agent.status === "active" ? "green" : "gray"}`}>{agent.status}</span>
                </div>
              </div>
              <div className="card-body">
                <div className="form-row">
                  <div className="form-group">
                    <label className="form-label">
                      Display Name <span className="required">*</span>
                    </label>
                    <input className="form-input" value={form.name || ""} onChange={(e) => setForm({ ...form, name: e.target.value })} />
                  </div>
                  <div className="form-group">
                    <label className="form-label">
                      Goodbye Grace <span className="hint">ms</span>
                    </label>
                    <input
                      className="form-input"
                      style={{ fontFamily: "var(--mono)" }}
                      value={form.goodbye_grace_ms ?? ""}
                      onChange={(e) => setForm({ ...form, goodbye_grace_ms: Number(e.target.value) })}
                    />
                  </div>
                </div>
                <div className="form-group">
                  <label className="form-label">
                    Max Call Duration <span className="hint">seconds — hard cutoff, caller hears a wrap-up line then the call ends. Leave blank for unlimited.</span>
                  </label>
                  <input
                    className="form-input"
                    style={{ fontFamily: "var(--mono)", maxWidth: 160 }}
                    type="number"
                    min={30}
                    max={7200}
                    placeholder="unlimited"
                    value={form.max_call_duration_s ?? ""}
                    onChange={(e) =>
                      setForm({ ...form, max_call_duration_s: e.target.value === "" ? null : Number(e.target.value) })
                    }
                  />
                </div>
                <div className="form-group">
                  <label className="form-label">
                    Status <span className="hint">inactive agents keep their config but stop resolving on calls</span>
                  </label>
                  <select
                    className={`form-select status-select ${form.status || "active"}`}
                    value={form.status || "active"}
                    onChange={(e) => setForm({ ...form, status: e.target.value as AgentStatus })}
                  >
                    <option value="active">active</option>
                    <option value="inactive">inactive</option>
                  </select>
                </div>
                <div className="form-group">
                  <label className="form-label">
                    Language <span className="hint">overrides the STT/TTS provider&apos;s own language when set</span>
                  </label>
                  <select
                    className="form-select"
                    value={languageChoice}
                    onChange={(e) => setLanguageChoice(e.target.value)}
                  >
                    <option value="">— derive from provider —</option>
                    {LANGUAGES.map((l) => (
                      <option key={l.value} value={l.value}>
                        {l.label}
                      </option>
                    ))}
                    <option value={OTHER}>Other (custom)…</option>
                  </select>
                  {languageChoice === OTHER && (
                    <input
                      className="form-input"
                      style={{ marginTop: 6, fontFamily: "var(--mono)" }}
                      value={customLanguage}
                      onChange={(e) => setCustomLanguage(e.target.value)}
                      placeholder="e.g. nl-BE"
                    />
                  )}
                </div>
              </div>
            </div>
          </div>
          <div className="col-side">
            <div className="card">
              <div className="card-body" style={{ fontSize: ".75rem", color: "var(--text-3)" }}>
                <div>
                  Account: <b style={{ color: "var(--text)" }}>{tenantSlug}</b>
                </div>
                <div style={{ marginTop: 6 }}>
                  Slug: <span className="mono">{agent.slug}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {tab === "behaviour" && (
        <div className="cols">
          <div className="col-main">
            <div className="info-banner">
              <strong>Stage prompts live in the{" "}
              <Link href={`/workflows/${tenantSlug}/${agentSlug}`}>flow</Link>.</strong>{" "}
              Greeting is on the start step; always-applies holds global instructions
              (one freeform textarea — the old Personality / Environment / Tone splitter
              is gone). End-call and transfer wording below still apply on every call.
            </div>

            <div className="card" style={{ marginBottom: 14 }}>
              <div className="card-hdr">
                <div className="card-title">Call ending</div>
                <div className="card-sub">condition + verbatim farewell</div>
              </div>
              <div className="card-body">
                <div className="form-group">
                  <label className="form-label">
                    End Call Condition <span className="hint">WHEN to end — a &quot;When the caller…&quot; clause, not what to say. Blank = default.</span>
                  </label>
                  <textarea
                    className="form-textarea"
                    style={{ minHeight: 48 }}
                    value={form.end_call_prompt || ""}
                    onChange={(e) => setForm({ ...form, end_call_prompt: e.target.value || null })}
                    placeholder="When the conversation is genuinely finished (the caller says goodbye, has no more questions, or the issue is resolved)"
                  />
                </div>
                <div className="form-group" style={{ marginBottom: 0 }}>
                  <label className="form-label">
                    Farewell Message <span className="hint">exact words spoken when ending the call — verbatim, never paraphrased. Blank = AI chooses the wording.</span>
                  </label>
                  <textarea
                    className="form-textarea"
                    style={{ minHeight: 48 }}
                    value={form.farewell_message || ""}
                    onChange={(e) => setForm({ ...form, farewell_message: e.target.value || null })}
                    placeholder="Thank you for calling. Have a wonderful day. Goodbye!"
                  />
                </div>
              </div>
            </div>

            <div className="card" style={{ marginBottom: 14 }}>
              <div className="card-hdr">
                <div className="card-title">Voice</div>
                <div className="card-sub">sets the same TTS assignment as below</div>
              </div>
              <div className="card-body">
                {(() => {
                  const selectedTts = providers.find((p) => p.id === form.tts_config_id);
                  const engine = chosenEngine ?? asBrowsableTtsEngine(selectedTts?.engine);

                  if (showEngineChooser || !engine) {
                    return (
                      <div>
                        <div className="form-hint" style={{ marginBottom: 8 }}>
                          Choose a TTS engine to browse its voices.
                        </div>
                        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                          {(["macos", "kokoro", "elevenlabs"] as const).map((e) => (
                            <button
                              key={e}
                              type="button"
                              className="btn btn-ghost btn-sm"
                              onClick={() => {
                                setChosenEngine(e);
                                setShowEngineChooser(false);
                              }}
                            >
                              {e === "macos" ? "macOS say" : e === "kokoro" ? "Kokoro" : "ElevenLabs"}
                            </button>
                          ))}
                        </div>
                      </div>
                    );
                  }

                  const changeEngineButton = (
                    <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 8 }}>
                      <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowEngineChooser(true)}>
                        Change engine
                      </button>
                    </div>
                  );

                  if (engine === "elevenlabs") {
                    // Prefer the provider actually assigned to this agent —
                    // falling back to "any ElevenLabs provider on the
                    // tenant" only when the agent isn't currently on one —
                    // otherwise a tenant with multiple connected accounts
                    // could show a different agent's selected voice here.
                    const elevenLabsProvider =
                      (selectedTts?.engine === "elevenlabs" ? selectedTts : undefined) ??
                      providers.find((p) => p.role === "tts" && p.engine === "elevenlabs") ??
                      null;
                    return (
                      <>
                        <ElevenLabsVoicePicker
                          tenantId={agent.tenant_id}
                          provider={elevenLabsProvider}
                          isCurrentAssignment={selectedTts?.engine === "elevenlabs"}
                          onProviderCreated={(p) => {
                            setProviders((prev) => [...prev, p]);
                            setForm((prev) => ({ ...prev, tts_config_id: p.id }));
                            setChosenEngine(null);
                          }}
                          onVoicePicked={(updated) => {
                            setProviders((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
                            setForm((prev) => ({ ...prev, tts_config_id: updated.id }));
                            setChosenEngine(null);
                          }}
                          onLanguageDetected={applyDetectedLanguage}
                        />
                        {changeEngineButton}
                      </>
                    );
                  }

                  return (
                    <>
                      <LocalVoicePicker
                        engine={engine}
                        tenantId={agent.tenant_id}
                        providers={providers}
                        value={form.tts_config_id}
                        onChange={(id) => {
                          setForm((prev) => ({ ...prev, tts_config_id: id }));
                          setChosenEngine(null);
                        }}
                        onProviderCreated={(p) => setProviders((prev) => [...prev, p])}
                        onLanguageDetected={applyDetectedLanguage}
                      />
                      {changeEngineButton}
                    </>
                  );
                })()}
                {(() => {
                  const selectedTts = providers.find((p) => p.id === form.tts_config_id);
                  const speed = Number((selectedTts?.extra as Record<string, unknown> | null)?.speed ?? 1.0);
                  return (
                    <div className="form-group" style={{ marginTop: 12, marginBottom: 0 }}>
                      <label className="form-label">
                        Speaking Speed <span className="hint">0.7 (slower) – 1.2 (faster), default 1.0 — saved on the selected voice, applies immediately to the next call</span>
                      </label>
                      <select
                        className="form-select"
                        style={{ width: 140 }}
                        value={String(speed)}
                        disabled={!selectedTts}
                        onChange={async (e) => {
                          if (!selectedTts) return;
                          const v = Number(e.target.value);
                          try {
                            const updated = await updateProvider(selectedTts.id, {
                              extra: { ...((selectedTts.extra as Record<string, unknown>) || {}), speed: v },
                            });
                            setProviders(providers.map((p) => (p.id === updated.id ? updated : p)));
                          } catch (err) {
                            setError(err instanceof ApiError ? err.detail : String(err));
                          }
                        }}
                      >
                        {[0.7, 0.8, 0.9, 1.0, 1.1, 1.2].map((v) => (
                          <option key={v} value={String(v)}>
                            {v.toFixed(1)}{v === 1.0 ? " (default)" : ""}
                          </option>
                        ))}
                      </select>
                    </div>
                  );
                })()}
              </div>
            </div>

            <div className="card">
              <div className="card-hdr">
                <div className="card-title">Provider Assignments</div>
                <div className="card-sub">prod-first, dev warns</div>
              </div>
              <div className="card-body">
                <div className="form-row">
                  {(["stt", "llm", "tts"] as const).map((role) => {
                    const key = `${role}_config_id` as const;
                    return (
                      <div className="form-group" key={role}>
                        <label className="form-label">{role.toUpperCase()}</label>
                        <select
                          className="form-select"
                          value={form[key] || ""}
                          onChange={(e) => setForm({ ...form, [key]: e.target.value || null })}
                        >
                          <option value="">— none —</option>
                          {byRole(role).map((p) => (
                            <option key={p.id} value={p.id}>
                              {p.name} {p.environment !== "prod" ? "⚠ " + p.environment : ""}
                            </option>
                          ))}
                        </select>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {tab === "escalation" && (
        <div className="cols">
          <div className="col-main">
            <div className="card">
              <div className="card-hdr">
                <div className="card-title">Human Escalation</div>
                <div className="card-sub">AI-to-human transfer</div>
              </div>
              <div className="card-body">
                <div className="form-row">
                  <div className="form-group">
                    <label className="form-label">Transfer Type</label>
                    <select
                      className="form-select"
                      value={form.transfer_type || "none"}
                      onChange={(e) => setForm({ ...form, transfer_type: e.target.value as AgentUpdate["transfer_type"] })}
                    >
                      <option value="none">Never Escalate</option>
                      <option value="cold">Cold Transfer</option>
                      {/* Warm transfer is fully implemented and live-verified
                          (bridge-based attended transfer via the gateway's
                          WarmTransferCoordinator) — see project's transfer
                          architecture phases. */}
                      <option value="warm">Warm Transfer</option>
                    </select>
                  </div>
                  <div className="form-group">
                    <label className="form-label">
                      Transfer Destination <span className="hint">phone number or SIP URI</span>
                    </label>
                    <input
                      className="form-input"
                      style={{ fontFamily: "var(--mono)", fontSize: ".75rem" }}
                      value={form.transfer_destination || ""}
                      onChange={(e) => setForm({ ...form, transfer_destination: e.target.value || null })}
                      placeholder="+18005550100 or sip:agent@example.com"
                      disabled={(form.transfer_type || "none") === "none"}
                    />
                  </div>
                  <div className="form-group">
                    <label className="form-label">
                      Transfer Condition <span className="hint">WHEN to transfer — an &quot;If the caller…&quot; clause, not what to say. Blank = default.</span>
                    </label>
                    <textarea
                      className="form-textarea"
                      style={{ minHeight: 48 }}
                      value={form.transfer_prompt || ""}
                      onChange={(e) => setForm({ ...form, transfer_prompt: e.target.value || null })}
                      placeholder="If the caller explicitly asks to speak to a human agent or representative"
                      disabled={(form.transfer_type || "none") === "none"}
                    />
                  </div>
                  <div className="form-group">
                    <label className="form-label">
                      Transfer Announcement <span className="hint">exact words spoken before transferring — verbatim, never paraphrased. Blank = AI chooses the wording.</span>
                    </label>
                    <textarea
                      className="form-textarea"
                      style={{ minHeight: 48 }}
                      value={form.transfer_announcement || ""}
                      onChange={(e) => setForm({ ...form, transfer_announcement: e.target.value || null })}
                      placeholder="Please hold while I transfer your call."
                      disabled={(form.transfer_type || "none") === "none"}
                    />
                  </div>
                  <div className="form-group">
                    <label className="form-label">
                      Queue ID <span className="hint">reserved — not yet used by any routing logic</span>
                    </label>
                    <input
                      className="form-input"
                      style={{ fontFamily: "var(--mono)", fontSize: ".75rem" }}
                      value={form.queue_id || ""}
                      onChange={(e) => setForm({ ...form, queue_id: e.target.value || null })}
                    />
                  </div>
                </div>
                <div className="form-group" style={{ marginBottom: 0 }}>
                  <label className="form-label">
                    Escalate after <span className="hint">consecutive guardrail triggers — requires a guardrail detector (not yet built) to actually fire</span>
                  </label>
                  <input
                    className="form-input"
                    style={{ fontFamily: "var(--mono)", width: 80 }}
                    type="number"
                    min={1}
                    step={1}
                    value={form.escalation_threshold ?? ""}
                    onChange={(e) => {
                      if (e.target.value === "") {
                        setForm({ ...form, escalation_threshold: null });
                        return;
                      }
                      const v = Number(e.target.value);
                      if (Number.isInteger(v) && v >= 1) setForm({ ...form, escalation_threshold: v });
                    }}
                  />
                </div>
              </div>
            </div>

            {form.transfer_type === "warm" && (
              <div className="card" style={{ marginTop: 16 }}>
                <div className="card-hdr">
                  <div className="card-title">Warm Transfer Options</div>
                  <div className="card-sub">No equivalent for cold transfer</div>
                </div>
                <div className="card-body">
                  <div className="form-row">
                    <div className="form-group">
                      <label className="form-label">
                        Caller ID <span className="hint">what the human agent sees on their phone</span>
                      </label>
                      <select
                        className="form-select"
                        value={form.caller_id_policy || "original"}
                        onChange={(e) =>
                          setForm({ ...form, caller_id_policy: e.target.value as AgentUpdate["caller_id_policy"] })
                        }
                      >
                        <option value="original">Original Caller</option>
                        <option value="platform">Platform DID</option>
                        <option value="custom">Custom</option>
                      </select>
                    </div>
                    {form.caller_id_policy === "platform" && (
                      <div className="form-group">
                        <label className="form-label">Platform DID</label>
                        <input
                          className="form-input"
                          style={{ fontFamily: "var(--mono)", fontSize: ".75rem" }}
                          value={form.platform_did || ""}
                          onChange={(e) => setForm({ ...form, platform_did: e.target.value || null })}
                          placeholder="+18005550100"
                        />
                      </div>
                    )}
                    {form.caller_id_policy === "custom" && (
                      <div className="form-group">
                        <label className="form-label">Custom Caller ID</label>
                        <input
                          className="form-input"
                          style={{ fontFamily: "var(--mono)", fontSize: ".75rem" }}
                          value={form.custom_caller_id || ""}
                          onChange={(e) => setForm({ ...form, custom_caller_id: e.target.value || null })}
                          placeholder="+18005550100"
                        />
                      </div>
                    )}
                    <div className="form-group">
                      <label className="form-label">
                        Waiting Experience <span className="hint">what the caller hears while the agent&apos;s phone is ringing</span>
                      </label>
                      <select
                        className="form-select"
                        value={form.transfer_waiting_experience || "announcement_moh"}
                        onChange={(e) =>
                          setForm({
                            ...form,
                            transfer_waiting_experience: e.target.value as AgentUpdate["transfer_waiting_experience"],
                          })
                        }
                      >
                        <option value="announcement_moh">Announcement + Hold Music</option>
                        <option value="announcement_silence">Announcement + Silence</option>
                      </select>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {tab === "sip" && <SipPanel tenantId={agent.tenant_id} agentId={agent.id} />}

      {tab === "tools" && <ToolsPanel tenantId={agent.tenant_id} agentId={agent.id} />}

      {tab === "knowledge-base" && <KnowledgeBaseTabs tenantId={agent.tenant_id} agentId={agent.id} />}

      {tab !== "knowledge-base" && tab !== "tools" && (
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 14 }}>
          {saved && <span style={{ alignSelf: "center", fontSize: ".76rem", color: "var(--green)" }}>Saved ✓</span>}
          <button className="btn btn-danger btn-sm" onClick={handleDelete} disabled={deleting}>
            {deleting ? "Deleting…" : "Delete Agent"}
          </button>
          <button className="btn btn-primary btn-sm" onClick={handleSave} disabled={saving}>
            {saving ? "Saving…" : "Save Changes"}
          </button>
        </div>
      )}
    </>
  );
}
