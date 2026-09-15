"use client";

// Stage-wise agent creation. Replaces the old two-field modal (name + tenant)
// that dropped straight onto the canvas with a hardcoded greeting/system
// prompt and left voice/model/transfer/knowledge-base for later. Every field
// collected here already exists on the agent row or in a tenant-scoped
// junction table (agent_knowledge_bases / agent_custom_apis) — this page is
// pure frontend orchestration over the existing create/update/assign
// endpoints, no backend changes.

import { useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  AgentUpdate,
  ApiError,
  ProviderConfig,
  Tenant,
  createAgent,
  generateSystemPrompt,
  listProviders,
  listTenants,
  updateAgent,
  updateProvider,
} from "@/lib/api";
import { KnowledgeBase, assignKnowledgeBase, listKnowledgeBases } from "@/lib/knowledgeApi";
import { CustomApi, listCustomApis, setAgentCustomApiEnabled } from "@/lib/toolexecApi";
import { LocalVoicePicker } from "@/components/LocalVoicePicker";
import { ElevenLabsVoicePicker } from "@/components/ElevenLabsVoicePicker";
import { LANGUAGES, OTHER, asBrowsableTtsEngine } from "@/lib/engineCatalog";
import { buildSystemPrompt } from "@/lib/systemPromptBuilder";
import { templateByKey } from "@/lib/agentTemplates";

type Step = "identity" | "voice" | "limits" | "advanced" | "knowledge" | "review";

const STEPS: { key: Step; label: string }[] = [
  { key: "identity", label: "Identity" },
  { key: "voice", label: "Language & Voice" },
  { key: "limits", label: "Limits" },
  { key: "advanced", label: "Advanced" },
  { key: "knowledge", label: "Knowledge & Tools" },
  { key: "review", label: "Review" },
];

const TONES = ["Friendly and professional", "Warm and empathetic", "Concise and formal", "Upbeat and casual"];

function slugify(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

export default function NewAgentPage() {
  const router = useRouter();
  const searchParams = useSearchParams();

  // Prefill from ?template= (see lib/agentTemplates.ts). Read once as the
  // initial state of the fields it touches, never as an effect that writes
  // over what's already typed — a template is a starting point, and every
  // field stays freely editable afterwards.
  const template = templateByKey(searchParams.get("template"));

  const [step, setStep] = useState<Step>("identity");
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [providers, setProviders] = useState<ProviderConfig[]>([]);
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [customApis, setCustomApis] = useState<CustomApi[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Step 1 — Identity
  const [name, setName] = useState(template?.label ?? "");
  const [tenantSlug, setTenantSlug] = useState(searchParams.get("tenant") || "");
  const [purpose, setPurpose] = useState(template?.purpose ?? "");
  const [persona, setPersona] = useState(template?.persona ?? "");
  const [tone, setTone] = useState(template?.tone ?? TONES[0]);

  // Step 2 — Language & Voice
  const [languageChoice, setLanguageChoice] = useState("");
  const [customLanguage, setCustomLanguage] = useState("");
  const [sttId, setSttId] = useState<string | null>(null);
  const [llmId, setLlmId] = useState<string | null>(null);
  const [ttsId, setTtsId] = useState<string | null>(null);
  const [chosenEngine, setChosenEngine] = useState<"macos" | "kokoro" | "elevenlabs" | null>(null);

  // Step 3 — Limits
  const [maxCallDuration, setMaxCallDuration] = useState<number | "">("");
  const [goodbyeGraceMs, setGoodbyeGraceMs] = useState<number | "">(3000);
  const [escalationThreshold, setEscalationThreshold] = useState<number | "">("");

  // Step 4 — Advanced (transfer rules + compliance/fallback — the latter
  // two have no dedicated agent columns, so they're folded straight into
  // the generated system prompt rather than invented as new DB fields).
  // A template's transfer condition is only meaningful with a transfer type
  // set — otherwise the Advanced step renders it disabled and it never
  // reaches the prompt.
  const [transferType, setTransferType] = useState<AgentUpdate["transfer_type"]>(
    template ? "cold" : "none",
  );
  const [transferDestination, setTransferDestination] = useState("");
  const [transferCondition, setTransferCondition] = useState(template?.transferCondition ?? "");
  const [transferAnnouncement, setTransferAnnouncement] = useState("");
  const [complianceInstructions, setComplianceInstructions] = useState("");
  const [fallbackResponse, setFallbackResponse] = useState("");

  // Step 4 — Knowledge & tools
  const [selectedKbIds, setSelectedKbIds] = useState<Set<string>>(new Set());
  const [selectedApiIds, setSelectedApiIds] = useState<Set<string>>(new Set());

  // Step 5 — Review
  const [greeting, setGreeting] = useState(template?.greeting ?? "Hello! How can I help you today?");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [promptEdited, setPromptEdited] = useState(false);
  const [generatingPrompt, setGeneratingPrompt] = useState(false);
  const [generateError, setGenerateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const tenant = useMemo(() => tenants.find((t) => t.slug === tenantSlug) ?? null, [tenants, tenantSlug]);
  const language =
    languageChoice === "" ? null : languageChoice === OTHER ? customLanguage.trim() || null : languageChoice;

  useEffect(() => {
    listTenants()
      .then((ts) => {
        setTenants(ts);
        if (!tenantSlug && ts.length > 0) setTenantSlug(ts[0].slug);
      })
      .catch((e) => setLoadError(e instanceof ApiError ? e.detail : String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!tenant) return;
    listProviders(tenant.id).then(setProviders).catch(() => {});
    listKnowledgeBases(tenant.id).then(setKbs).catch(() => {});
    listCustomApis(tenant.id).then(setCustomApis).catch(() => {});
  }, [tenant]);

  // Regenerate the draft prompt from structured inputs until the reviewer
  // edits it by hand — once edited, their own wording wins and stops being
  // silently overwritten by a later step change.
  useEffect(() => {
    if (promptEdited) return;
    setSystemPrompt(
      buildSystemPrompt({
        name,
        purpose,
        persona,
        tone,
        language,
        hasKnowledgeBase: selectedKbIds.size > 0,
        transferType: transferType ?? "none",
        transferCondition,
        complianceInstructions,
        fallbackResponse,
      }),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    name, purpose, persona, tone, language, selectedKbIds.size, transferType, transferCondition,
    complianceInstructions, fallbackResponse, promptEdited,
  ]);

  const byRole = (role: string) => providers.filter((p) => p.role === role);
  const selectedTts = providers.find((p) => p.id === ttsId) ?? null;

  const applyDetectedLanguage = (l: string) => {
    if (LANGUAGES.some((x) => x.value === l)) {
      setLanguageChoice(l);
    } else {
      setLanguageChoice(OTHER);
      setCustomLanguage(l);
    }
  };

  const toggleKb = (id: string) =>
    setSelectedKbIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const toggleApi = (id: string) =>
    setSelectedApiIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const handleGenerateWithAi = async () => {
    if (!llmId) return;
    setGeneratingPrompt(true);
    setGenerateError(null);
    try {
      const { system_prompt } = await generateSystemPrompt(tenantSlug, {
        name,
        purpose,
        persona,
        tone,
        language,
        has_knowledge_base: selectedKbIds.size > 0,
        transfer_condition: transferType === "none" ? null : transferCondition,
        compliance_instructions: complianceInstructions,
        fallback_response: fallbackResponse,
        llm_config_id: llmId,
      });
      setSystemPrompt(system_prompt);
      setPromptEdited(true);
    } catch (e) {
      setGenerateError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setGeneratingPrompt(false);
    }
  };

  const stepIndex = STEPS.findIndex((s) => s.key === step);
  const canLeaveIdentity = slugify(name) !== "" && tenantSlug !== "";
  const goNext = () => setStep(STEPS[Math.min(stepIndex + 1, STEPS.length - 1)].key);
  const goBack = () => setStep(STEPS[Math.max(stepIndex - 1, 0)].key);

  const handleCreate = async () => {
    if (!tenant) return;
    const slug = slugify(name);
    if (!slug) return;
    setCreating(true);
    setCreateError(null);
    try {
      const agent = await createAgent(tenantSlug, {
        slug,
        name: name.trim(),
        greeting,
        system_prompt: systemPrompt,
        stt_config_id: sttId,
        llm_config_id: llmId,
        tts_config_id: ttsId,
      });

      await updateAgent(tenantSlug, agent.id, {
        language,
        max_call_duration_s: maxCallDuration === "" ? null : maxCallDuration,
        goodbye_grace_ms: goodbyeGraceMs === "" ? undefined : goodbyeGraceMs,
        transfer_type: transferType,
        transfer_destination: transferType === "none" ? null : transferDestination.trim() || null,
        transfer_prompt: transferType === "none" ? null : transferCondition.trim() || null,
        transfer_announcement: transferType === "none" ? null : transferAnnouncement.trim() || null,
        escalation_threshold: escalationThreshold === "" ? null : escalationThreshold,
      });

      await Promise.all([
        ...Array.from(selectedKbIds).map((kbId) => assignKnowledgeBase(agent.id, kbId, true)),
        ...Array.from(selectedApiIds).map((apiId) => setAgentCustomApiEnabled(agent.id, apiId, true)),
      ]);

      // Land on a live test call, not the canvas — the first thing to
      // verify is that the agent actually talks the way steps 1-4 said it
      // should, before touching the flow at all.
      router.push(`/agents/${tenantSlug}/${agent.slug}?test=1`);
    } catch (e) {
      setCreateError(e instanceof ApiError ? e.detail : String(e));
      setCreating(false);
    }
  };

  return (
    <>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 14 }}>
        <button className="btn btn-ghost btn-sm" onClick={() => router.push("/agents")}>
          ← Cancel
        </button>
      </div>

      <div className="tabs">
        {STEPS.map((s, i) => (
          <button
            key={s.key}
            className={`tab${step === s.key ? " active" : ""}`}
            disabled={i > 0 && !canLeaveIdentity}
            onClick={() => setStep(s.key)}
          >
            {i + 1}. {s.label}
          </button>
        ))}
      </div>

      {loadError && <div className="error-banner">{loadError}</div>}

      {step === "identity" && (
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Identity</div>
          </div>
          <div className="card-body">
            <div className="form-group">
              <label className="form-label">Name <span className="required">*</span></label>
              <input className="form-input" autoFocus value={name} placeholder="Booking Bot" onChange={(e) => setName(e.target.value)} />
              {name.trim() !== "" && <div className="form-hint">Address: <span className="mono">{slugify(name) || "—"}</span></div>}
            </div>
            <div className="form-group">
              <label className="form-label">Account <span className="required">*</span></label>
              <select className="form-select" value={tenantSlug} onChange={(e) => setTenantSlug(e.target.value)}>
                {tenants.map((t) => (
                  <option key={t.id} value={t.slug}>{t.name}</option>
                ))}
              </select>
            </div>
            <div className="form-group">
              <label className="form-label">Purpose <span className="hint">one line — what is this agent for?</span></label>
              <input
                className="form-input"
                value={purpose}
                placeholder="Book and reschedule salon appointments"
                onChange={(e) => setPurpose(e.target.value)}
              />
            </div>
            <div className="form-group">
              <label className="form-label">Identity <span className="hint">who the agent is — read out loud to the reviewer, so write it as a persona</span></label>
              <textarea
                className="form-textarea"
                style={{ minHeight: 70 }}
                value={persona}
                placeholder="You are Aria, a friendly scheduling assistant for Acme Salon."
                onChange={(e) => setPersona(e.target.value)}
              />
            </div>
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label">Tone</label>
              <select className="form-select" value={tone} onChange={(e) => setTone(e.target.value)}>
                {TONES.map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            </div>
          </div>
        </div>
      )}

      {step === "voice" && (
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Language & Voice</div>
          </div>
          <div className="card-body">
            <div className="form-group">
              <label className="form-label">Language <span className="hint">overrides the STT/TTS provider's own language when set</span></label>
              <select className="form-select" value={languageChoice} onChange={(e) => setLanguageChoice(e.target.value)}>
                <option value="">— derive from provider —</option>
                {LANGUAGES.map((l) => (
                  <option key={l.value} value={l.value}>{l.label}</option>
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

            <div className="form-group">
              <label className="form-label">Voice</label>
              {tenant &&
                (() => {
                  const engine = chosenEngine ?? asBrowsableTtsEngine(selectedTts?.engine);
                  if (!engine) {
                    return (
                      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                        {(["macos", "kokoro", "elevenlabs"] as const).map((e) => (
                          <button key={e} type="button" className="btn btn-ghost btn-sm" onClick={() => setChosenEngine(e)}>
                            {e === "macos" ? "macOS say" : e === "kokoro" ? "Kokoro" : "ElevenLabs"}
                          </button>
                        ))}
                      </div>
                    );
                  }
                  if (engine === "elevenlabs") {
                    const elevenLabsProvider =
                      (selectedTts?.engine === "elevenlabs" ? selectedTts : undefined) ??
                      providers.find((p) => p.role === "tts" && p.engine === "elevenlabs") ??
                      null;
                    return (
                      <ElevenLabsVoicePicker
                        tenantId={tenant.id}
                        provider={elevenLabsProvider}
                        isCurrentAssignment={selectedTts?.engine === "elevenlabs"}
                        onProviderCreated={(p) => {
                          setProviders((prev) => [...prev, p]);
                          setTtsId(p.id);
                        }}
                        onVoicePicked={(updated) => {
                          setProviders((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
                          setTtsId(updated.id);
                        }}
                        onLanguageDetected={applyDetectedLanguage}
                      />
                    );
                  }
                  return (
                    <LocalVoicePicker
                      engine={engine}
                      tenantId={tenant.id}
                      providers={providers}
                      value={ttsId}
                      onChange={setTtsId}
                      onProviderCreated={(p) => setProviders((prev) => [...prev, p])}
                      onLanguageDetected={applyDetectedLanguage}
                    />
                  );
                })()}
              {selectedTts && (
                <div style={{ marginTop: 10 }}>
                  <label className="form-label">Speaking Speed <span className="hint">0.7 (slower) – 1.2 (faster)</span></label>
                  <select
                    className="form-select"
                    style={{ width: 140 }}
                    value={String(Number((selectedTts.extra as Record<string, unknown> | null)?.speed ?? 1.0))}
                    onChange={async (e) => {
                      const v = Number(e.target.value);
                      const updated = await updateProvider(selectedTts.id, { extra: { ...((selectedTts.extra as Record<string, unknown>) || {}), speed: v } });
                      setProviders((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
                    }}
                  >
                    {[0.7, 0.8, 0.9, 1.0, 1.1, 1.2].map((v) => (
                      <option key={v} value={String(v)}>{v.toFixed(1)}{v === 1.0 ? " (default)" : ""}</option>
                    ))}
                  </select>
                </div>
              )}
            </div>

            <div className="form-row">
              <div className="form-group">
                <label className="form-label">STT</label>
                <select className="form-select" value={sttId || ""} onChange={(e) => setSttId(e.target.value || null)}>
                  <option value="">— none —</option>
                  {byRole("stt").map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              </div>
              <div className="form-group">
                <label className="form-label">LLM</label>
                <select className="form-select" value={llmId || ""} onChange={(e) => setLlmId(e.target.value || null)}>
                  <option value="">— none —</option>
                  {byRole("llm").map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              </div>
            </div>
          </div>
        </div>
      )}

      {step === "limits" && (
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Limits</div>
            <div className="card-sub">hard caps this agent runs under on every call</div>
          </div>
          <div className="card-body">
            <div className="form-row">
              <div className="form-group">
                <label className="form-label">Max Call Duration <span className="hint">seconds — hard cutoff. Blank = unlimited.</span></label>
                <input
                  className="form-input"
                  style={{ fontFamily: "var(--mono)" }}
                  type="number"
                  min={30}
                  max={7200}
                  placeholder="unlimited"
                  value={maxCallDuration}
                  onChange={(e) => setMaxCallDuration(e.target.value === "" ? "" : Number(e.target.value))}
                />
              </div>
              <div className="form-group">
                <label className="form-label">Goodbye Grace <span className="hint">ms — pause before hanging up after the farewell</span></label>
                <input
                  className="form-input"
                  style={{ fontFamily: "var(--mono)" }}
                  type="number"
                  value={goodbyeGraceMs}
                  onChange={(e) => setGoodbyeGraceMs(e.target.value === "" ? "" : Number(e.target.value))}
                />
              </div>
            </div>
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label">Escalate after <span className="hint">consecutive guardrail triggers before transferring — requires a transfer rule set in Advanced</span></label>
              <input
                className="form-input"
                style={{ fontFamily: "var(--mono)", width: 80 }}
                type="number"
                min={1}
                value={escalationThreshold}
                onChange={(e) => setEscalationThreshold(e.target.value === "" ? "" : Number(e.target.value))}
              />
            </div>
          </div>
        </div>
      )}

      {step === "advanced" && (
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Advanced</div>
            <div className="card-sub">transfer rules, compliance and fallback wording — all folded into the generated system prompt</div>
          </div>
          <div className="card-body">
            <div className="form-row">
              <div className="form-group">
                <label className="form-label">Transfer Type</label>
                <select className="form-select" value={transferType || "none"} onChange={(e) => setTransferType(e.target.value as AgentUpdate["transfer_type"])}>
                  <option value="none">Never Escalate</option>
                  <option value="cold">Cold Transfer</option>
                  <option value="warm">Warm Transfer</option>
                </select>
              </div>
              <div className="form-group">
                <label className="form-label">Transfer Destination <span className="hint">phone number or SIP URI</span></label>
                <input
                  className="form-input"
                  style={{ fontFamily: "var(--mono)", fontSize: ".75rem" }}
                  value={transferDestination}
                  onChange={(e) => setTransferDestination(e.target.value)}
                  placeholder="+18005550100 or sip:agent@example.com"
                  disabled={transferType === "none"}
                />
              </div>
            </div>
            <div className="form-group">
              <label className="form-label">Transfer Condition <span className="hint">an &quot;If the caller…&quot; clause — also drives the generated system prompt</span></label>
              <textarea
                className="form-textarea"
                style={{ minHeight: 48 }}
                value={transferCondition}
                onChange={(e) => setTransferCondition(e.target.value)}
                placeholder="If the caller explicitly asks to speak to a human agent"
                disabled={transferType === "none"}
              />
            </div>
            <div className="form-group">
              <label className="form-label">Transfer Announcement <span className="hint">exact words spoken before transferring — blank = AI chooses the wording</span></label>
              <textarea
                className="form-textarea"
                style={{ minHeight: 48 }}
                value={transferAnnouncement}
                onChange={(e) => setTransferAnnouncement(e.target.value)}
                placeholder="Please hold while I transfer your call."
                disabled={transferType === "none"}
              />
            </div>
            <div className="form-group">
              <label className="form-label">Fallback Response <span className="hint">said when the agent genuinely doesn't know the answer</span></label>
              <textarea
                className="form-textarea"
                style={{ minHeight: 48 }}
                value={fallbackResponse}
                onChange={(e) => setFallbackResponse(e.target.value)}
                placeholder="I don't have that information — let me check or connect you with someone who does."
              />
            </div>
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label">Compliance Instructions <span className="hint">brand/regulatory rules the agent must always follow</span></label>
              <textarea
                className="form-textarea"
                style={{ minHeight: 48 }}
                value={complianceInstructions}
                onChange={(e) => setComplianceInstructions(e.target.value)}
                placeholder="Never mention a competitor by name. Do not collect card or OTP details on this call."
              />
            </div>
          </div>
        </div>
      )}

      {step === "knowledge" && (
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Knowledge & Tools</div>
            <div className="card-sub">shared across every agent in this account — upload documents or add APIs from the Knowledge Base tab</div>
          </div>
          <div className="card-body">
            <div className="form-group">
              <label className="form-label">Knowledge Bases</label>
              {kbs.length === 0 ? (
                <div className="form-hint">No knowledge bases yet in this account — add one from the Knowledge Base tab, then come back here.</div>
              ) : (
                kbs.map((kb) => (
                  <label key={kb.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0" }}>
                    <input type="checkbox" checked={selectedKbIds.has(kb.id)} onChange={() => toggleKb(kb.id)} />
                    {kb.name}
                  </label>
                ))
              )}
            </div>
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label">Custom APIs</label>
              {customApis.length === 0 ? (
                <div className="form-hint">No custom APIs yet in this account.</div>
              ) : (
                customApis.map((api) => (
                  <label key={api.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0" }}>
                    <input type="checkbox" checked={selectedApiIds.has(api.id)} onChange={() => toggleApi(api.id)} />
                    {api.name}
                  </label>
                ))
              )}
            </div>
          </div>
        </div>
      )}

      {step === "review" && (
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Review</div>
            <div className="card-sub">system prompt generated from your answers — edit freely before creating</div>
          </div>
          <div className="card-body">
            {createError && <div className="error-banner">{createError}</div>}
            <div className="form-group">
              <label className="form-label">Greeting <span className="hint">first thing the agent says</span></label>
              <input className="form-input" value={greeting} onChange={(e) => setGreeting(e.target.value)} />
            </div>
            <div className="form-group" style={{ marginBottom: 0 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <label className="form-label" style={{ marginBottom: 0 }}>System Prompt</label>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  style={{ marginLeft: "auto" }}
                  disabled={!llmId || generatingPrompt}
                  title={!llmId ? "Pick an LLM provider in step 2 first" : undefined}
                  onClick={handleGenerateWithAi}
                >
                  {generatingPrompt ? "Generating…" : "✨ Generate with AI"}
                </button>
              </div>
              {generateError && <div className="error-banner">{generateError}</div>}
              <textarea
                className="form-textarea"
                style={{ minHeight: 160 }}
                value={systemPrompt}
                onChange={(e) => {
                  setSystemPrompt(e.target.value);
                  setPromptEdited(true);
                }}
              />
              {promptEdited && (
                <div className="form-hint">
                  Edited — no longer auto-updates from earlier steps.{" "}
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => setPromptEdited(false)}>
                    Reset to the plain template
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 14 }}>
        {stepIndex > 0 && (
          <button className="btn btn-ghost btn-sm" onClick={goBack} disabled={creating}>
            ← Back
          </button>
        )}
        {step !== "review" ? (
          <button className="btn btn-primary btn-sm" onClick={goNext} disabled={!canLeaveIdentity}>
            Next →
          </button>
        ) : (
          <button className="btn btn-primary btn-sm" onClick={handleCreate} disabled={creating || !canLeaveIdentity}>
            {creating ? "Creating…" : "Create Agent"}
          </button>
        )}
      </div>
    </>
  );
}
