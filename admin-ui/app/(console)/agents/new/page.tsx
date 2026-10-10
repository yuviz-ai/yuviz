"use client";

// Stage-wise agent creation over the existing create/update/assign endpoints.

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, ArrowRight, Check, Sparkles } from "lucide-react";
import {
  Agent,
  AgentUpdate,
  ApiError,
  ProviderConfig,
  Tenant,
  createAgent,
  enableExecuteApi,
  generateSystemPrompt,
  listProviders,
  listTenants,
  updateAgent,
} from "@/lib/api";
import { KnowledgeBase, assignKnowledgeBase, listKnowledgeBases } from "@/lib/knowledgeApi";
import { CustomApi, listCustomApis, setAgentCustomApiEnabled } from "@/lib/toolexecApi";
import { AgentVoiceSettings, isLanguageError, multilingualPayload } from "@/components/AgentVoiceSettings";
import { OTHER } from "@/lib/engineCatalog";
import { buildSystemPrompt } from "@/lib/systemPromptBuilder";
import { templateByKey } from "@/lib/agentTemplates";
import { normalizeDialTarget } from "@/lib/dialTargets";
import { QuickCreateAgent } from "@/components/QuickCreateAgent";
import { AgentDraft, clearAgentDraft, draftSavedLabel, loadAgentDraft, saveAgentDraft } from "@/lib/agentDraft";

type Step = "identity" | "voice" | "limits" | "advanced" | "knowledge" | "review";

const STEPS: { key: Step; label: string }[] = [
  { key: "identity", label: "About" },
  { key: "voice", label: "Language & Voice" },
  { key: "limits", label: "Call length" },
  { key: "advanced", label: "Transfers & rules" },
  { key: "knowledge", label: "Knowledge" },
  { key: "review", label: "Review" },
];

const TONES = ["Friendly and professional", "Warm and empathetic", "Concise and formal", "Upbeat and casual"];

function slugify(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

export default function NewAgentPage() {
  const router = useRouter();
  const searchParams = useSearchParams();

  // ?template= is read once as initial state only, so it never overwrites typed input.
  const template = templateByKey(searchParams.get("template"));

  const [mode, setMode] = useState<"quick" | "advanced">(searchParams.get("mode") === "advanced" ? "advanced" : "quick");
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
  const [supportedLanguages, setSupportedLanguages] = useState<string[]>([]);
  const [ttsByLanguage, setTtsByLanguage] = useState<Record<string, string>>({});
  const [greetingByLanguage, setGreetingByLanguage] = useState<Record<string, string>>({});
  const [languagesError, setLanguagesError] = useState<string | null>(null);
  const [sttId, setSttId] = useState<string | null>(null);
  const [llmId, setLlmId] = useState<string | null>(null);
  const [ttsId, setTtsId] = useState<string | null>(null);

  // Step 3 — Limits
  const [maxCallDuration, setMaxCallDuration] = useState<number | "">("");
  const [goodbyeGraceMs, setGoodbyeGraceMs] = useState<number | "">(3000);
  const [escalationThreshold, setEscalationThreshold] = useState<number | "">("");

  // Step 4 — Advanced. Compliance/fallback have no agent columns; they go into the generated prompt.
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
  const createdAgent = useRef<Agent | null>(null);
  // Set once the post-create PATCH and assignments succeed, so a retry re-sends them to the same agent.
  const settingsApplied = useRef(false);
  const [createError, setCreateError] = useState<string | null>(null);

  // Autosave: restore once on mount (never during SSR), then write on every change.
  const [draftReady, setDraftReady] = useState(false);
  const [restoredAt, setRestoredAt] = useState<number | null>(null);
  const [lastSavedAt, setLastSavedAt] = useState<number | null>(null);
  const skipAutosave = useRef(false);

  const tenant = useMemo(() => tenants.find((t) => t.slug === tenantSlug) ?? null, [tenants, tenantSlug]);
  const language =
    languageChoice === "" ? null : languageChoice === OTHER ? customLanguage.trim() || null : languageChoice;

  useEffect(() => {
    // A template click is an explicit fresh start, so it doesn't resume an older draft.
    const d = template ? null : loadAgentDraft();
    if (d) {
      /* eslint-disable react-hooks/set-state-in-effect */
      setStep(STEPS.some((s) => s.key === d.step) ? (d.step as Step) : "identity");
      setName(d.name);
      if (!searchParams.get("tenant")) setTenantSlug(d.tenantSlug);
      setPurpose(d.purpose);
      setPersona(d.persona);
      setTone(d.tone);
      setLanguageChoice(d.languageChoice);
      setCustomLanguage(d.customLanguage);
      // Absent in drafts saved before multilingual agents.
      setSupportedLanguages(d.supportedLanguages ?? []);
      setTtsByLanguage(d.ttsByLanguage ?? {});
      setGreetingByLanguage(d.greetingByLanguage ?? {});
      setSttId(d.sttId);
      setLlmId(d.llmId);
      setTtsId(d.ttsId);
      setMaxCallDuration(d.maxCallDuration);
      setGoodbyeGraceMs(d.goodbyeGraceMs);
      setEscalationThreshold(d.escalationThreshold);
      setTransferType(d.transferType);
      setTransferDestination(d.transferDestination);
      setTransferCondition(d.transferCondition);
      setTransferAnnouncement(d.transferAnnouncement);
      setComplianceInstructions(d.complianceInstructions);
      setFallbackResponse(d.fallbackResponse);
      setSelectedKbIds(new Set(d.selectedKbIds));
      setSelectedApiIds(new Set(d.selectedApiIds));
      setGreeting(d.greeting);
      setSystemPrompt(d.systemPrompt);
      setPromptEdited(d.promptEdited);
      setRestoredAt(d.savedAt);
      setLastSavedAt(d.savedAt);
    }
    setDraftReady(true);
    /* eslint-enable react-hooks/set-state-in-effect */
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const hasContent = name.trim() !== "" || purpose.trim() !== "" || persona.trim() !== "";

  useEffect(() => {
    if (!draftReady || skipAutosave.current || mode !== "advanced") return;
    if (!hasContent) {
      clearAgentDraft();
      return;
    }
    const timer = setTimeout(() => {
      const draft: AgentDraft = {
        savedAt: Date.now(),
        step, name, tenantSlug, purpose, persona, tone, languageChoice, customLanguage,
        supportedLanguages, ttsByLanguage, greetingByLanguage, sttId, llmId, ttsId,
        maxCallDuration, goodbyeGraceMs, escalationThreshold, transferType, transferDestination,
        transferCondition, transferAnnouncement, complianceInstructions, fallbackResponse,
        selectedKbIds: Array.from(selectedKbIds), selectedApiIds: Array.from(selectedApiIds),
        greeting, systemPrompt, promptEdited,
      };
      saveAgentDraft(draft);
      setLastSavedAt(draft.savedAt);
    }, 400);
    return () => clearTimeout(timer);
  }, [
    draftReady, mode, hasContent, step, name, tenantSlug, purpose, persona, tone, languageChoice, customLanguage,
    supportedLanguages, ttsByLanguage, greetingByLanguage,
    sttId, llmId, ttsId, maxCallDuration, goodbyeGraceMs, escalationThreshold, transferType,
    transferDestination, transferCondition, transferAnnouncement, complianceInstructions, fallbackResponse,
    selectedKbIds, selectedApiIds, greeting, systemPrompt, promptEdited,
  ]);

  const startOver = () => {
    skipAutosave.current = true;
    clearAgentDraft();
    window.location.replace("/agents/new?mode=advanced");
  };

  useEffect(() => {
    listTenants()
      .then((ts) => {
        setTenants(ts);
        // Functional update: a restored draft's account may already be set, and must still exist.
        setTenantSlug((prev) => (ts.some((t) => t.slug === prev) ? prev : ts[0]?.slug ?? ""));
      })
      .catch((e) => setLoadError(e instanceof ApiError ? e.detail : String(e)));
  }, []);

  useEffect(() => {
    if (!tenant) return;
    listProviders(tenant.id)
      .then((provs) => {
        setProviders(provs);
        // Keep a valid existing pick (typed or restored); else the account default, else the only option.
        const pick = (role: "stt" | "llm" | "tts", current: string | null, accountDefault: string | null) => {
          const ofRole = provs.filter((p) => p.role === role);
          if (current && ofRole.some((p) => p.id === current)) return current;
          if (accountDefault && ofRole.some((p) => p.id === accountDefault)) return accountDefault;
          return ofRole.length === 1 ? ofRole[0].id : null;
        };
        setSttId((prev) => pick("stt", prev, tenant.default_stt_config_id));
        setLlmId((prev) => pick("llm", prev, tenant.default_llm_config_id));
        setTtsId((prev) => pick("tts", prev, tenant.default_tts_config_id));
      })
      .catch(() => {});
    const loadKnowledge = () => {
      listKnowledgeBases(tenant.id).then(setKbs).catch(() => {});
      listCustomApis(tenant.id).then(setCustomApis).catch(() => {});
    };
    loadKnowledge();
    // The "add one" links open in a new tab; pick up whatever was created there on return.
    window.addEventListener("focus", loadKnowledge);
    return () => window.removeEventListener("focus", loadKnowledge);
  }, [tenant]);

  // Regenerate the draft prompt until the user edits it by hand.
  useEffect(() => {
    if (promptEdited) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
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
     
  }, [
    name, purpose, persona, tone, language, selectedKbIds.size, transferType, transferCondition,
    complianceInstructions, fallbackResponse, promptEdited,
  ]);

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
    setLanguagesError(null);
    // A restored draft can reference things deleted since it was saved.
    const known = (id: string | null) => (id && providers.some((p) => p.id === id) ? id : null);
    try {
      // A retry after a failed settings or execute_api step must not create a second agent.
      let agent = createdAgent.current;
      const apiIds = customApis.filter((api) => selectedApiIds.has(api.id)).map((api) => api.id);
      if (!agent) {
        agent = await createAgent(tenantSlug, {
          slug,
          name: name.trim(),
          greeting,
          system_prompt: systemPrompt,
          stt_config_id: known(sttId),
          llm_config_id: known(llmId),
          tts_config_id: known(ttsId),
          // Sent here so a language 400 rejects the create instead of leaving a half-set-up agent.
          language,
          ...multilingualPayload(language, supportedLanguages, ttsByLanguage, greetingByLanguage),
          status: "inactive",
        });
        createdAgent.current = agent;
      }
      if (!settingsApplied.current) {
        const fresh = agent;
        // Voice ids are re-sent: the language check (a 400 the user fixes on the voice step) reads them.
        await updateAgent(tenantSlug, fresh.id, {
          language,
          ...multilingualPayload(language, supportedLanguages, ttsByLanguage, greetingByLanguage),
          stt_config_id: known(sttId),
          llm_config_id: known(llmId),
          tts_config_id: known(ttsId),
          max_call_duration_s: maxCallDuration === "" ? null : maxCallDuration,
          goodbye_grace_ms: goodbyeGraceMs === "" ? undefined : goodbyeGraceMs,
          transfer_type: transferType,
          transfer_destination: transferType === "none" ? null : normalizeDialTarget(transferDestination),
          transfer_prompt: transferType === "none" ? null : transferCondition.trim() || null,
          transfer_announcement: transferType === "none" ? null : transferAnnouncement.trim() || null,
          escalation_threshold: escalationThreshold === "" ? null : escalationThreshold,
        });

        await Promise.all([
          ...kbs.filter((kb) => selectedKbIds.has(kb.id)).map((kb) => assignKnowledgeBase(fresh.id, kb.id, true)),
          ...apiIds.map((apiId) => setAgentCustomApiEnabled(fresh.id, apiId, true)),
        ]);
        settingsApplied.current = true;
      }
      if (apiIds.length > 0) await enableExecuteApi(tenant.id, agent.id);

      skipAutosave.current = true;
      clearAgentDraft();
      router.push(`/agents/${tenantSlug}/${agent.slug}?new=1`);
    } catch (e) {
      const detail = e instanceof ApiError ? e.detail : String(e);
      setCreateError(detail);
      if (e instanceof ApiError && e.status === 400 && isLanguageError(detail)) setLanguagesError(detail);
      setCreating(false);
    }
  };

  if (mode === "quick") {
    return <QuickCreateAgent initialTemplate={searchParams.get("template")} onStepByStep={() => setMode("advanced")} />;
  }

  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <button className="btn btn-ghost btn-sm" onClick={() => router.push("/agents")}>
          <ArrowLeft size={13} /> All agents
        </button>
        {hasContent && lastSavedAt && (
          <span className="saved-note" style={{ marginLeft: "auto", color: "var(--text-3)" }}>
            <Check size={13} /> Draft saved automatically. You can leave and come back anytime.
          </span>
        )}
      </div>

      {restoredAt && (
        <div className="draft-banner">
          <span>
            Welcome back. We restored the agent you were working on (saved {draftSavedLabel(restoredAt)}).
          </span>
          <button className="btn btn-ghost btn-sm" onClick={startOver}>Start over</button>
          <button className="btn btn-ghost btn-sm" onClick={() => setRestoredAt(null)}>Dismiss</button>
        </div>
      )}

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
            <div className="card-title">About your agent</div>
          </div>
          <div className="card-body">
            <div className="form-group">
              <label className="form-label">Name <span className="required">*</span></label>
              <input className="form-input" autoFocus value={name} placeholder="Booking Bot" onChange={(e) => setName(e.target.value)} />
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
              <label className="form-label">What does it do? <span className="hint">one line</span></label>
              <input
                className="form-input"
                value={purpose}
                placeholder="Book and reschedule salon appointments"
                onChange={(e) => setPurpose(e.target.value)}
              />
            </div>
            <div className="form-group">
              <label className="form-label">Who is it? <span className="hint">describe the agent as a person</span></label>
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

      {step === "voice" && tenant && (
        <AgentVoiceSettings
          tenantId={tenant.id}
          providers={providers}
          setProviders={setProviders}
          languageChoice={languageChoice}
          onLanguageChoice={setLanguageChoice}
          customLanguage={customLanguage}
          onCustomLanguage={setCustomLanguage}
          supportedLanguages={supportedLanguages}
          onSupportedLanguages={setSupportedLanguages}
          ttsByLanguage={ttsByLanguage}
          onTtsByLanguage={setTtsByLanguage}
          greetingByLanguage={greetingByLanguage}
          onGreetingByLanguage={setGreetingByLanguage}
          languagesError={languagesError}
          sttId={sttId}
          llmId={llmId}
          ttsId={ttsId}
          onAssign={(role, id) => (role === "stt" ? setSttId(id) : role === "llm" ? setLlmId(id) : setTtsId(id))}
          onError={setLoadError}
        />
      )}

      {step === "limits" && (
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Call length</div>
            <div className="card-sub">how long calls can last and how they end</div>
          </div>
          <div className="card-body">
            <div className="form-row">
              <div className="form-group">
                <label className="form-label">Longest call <span className="hint">in seconds. Leave blank for no limit.</span></label>
                <input
                  className="form-input"
                  type="number"
                  min={30}
                  max={7200}
                  placeholder="No limit"
                  value={maxCallDuration}
                  onChange={(e) => setMaxCallDuration(e.target.value === "" ? "" : Number(e.target.value))}
                />
              </div>
              <div className="form-group">
                <label className="form-label">Pause before hanging up <span className="hint">in seconds, after saying goodbye</span></label>
                <input
                  className="form-input"
                  type="number"
                  min={0}
                  step={0.5}
                  value={goodbyeGraceMs === "" ? "" : goodbyeGraceMs / 1000}
                  onChange={(e) => setGoodbyeGraceMs(e.target.value === "" ? "" : Math.round(Number(e.target.value) * 1000))}
                />
              </div>
            </div>
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label">Hand off to a person after <span className="hint">this many problems in a row. Needs a transfer set up in the next step.</span></label>
              <input
                className="form-input"
                style={{ width: 80 }}
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
            <div className="card-title">Transfers &amp; rules</div>
            <div className="card-sub">when to pass callers to a person, and rules the agent must follow</div>
          </div>
          <div className="card-body">
            <div className="form-row">
              <div className="form-group">
                <label className="form-label">Transfer to a person</label>
                <select className="form-select" value={transferType || "none"} onChange={(e) => setTransferType(e.target.value as AgentUpdate["transfer_type"])}>
                  <option value="none">Don&apos;t transfer</option>
                  <option value="cold">Connect directly</option>
                  <option value="warm">Introduce first</option>
                </select>
              </div>
              <div className="form-group">
                <label className="form-label">Transfer to <span className="hint">phone number with country code</span></label>
                <input
                  className="form-input"
                  value={transferDestination}
                  onChange={(e) => setTransferDestination(e.target.value)}
                  placeholder="+1 800 555 0100"
                  disabled={transferType === "none"}
                />
              </div>
            </div>
            <div className="form-group">
              <label className="form-label">When should it transfer?</label>
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
              <label className="form-label">What it says before transferring <span className="hint">leave blank to let the agent choose</span></label>
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
              <label className="form-label">When it doesn&apos;t know the answer <span className="hint">what the agent says</span></label>
              <textarea
                className="form-textarea"
                style={{ minHeight: 48 }}
                value={fallbackResponse}
                onChange={(e) => setFallbackResponse(e.target.value)}
                placeholder="I don't have that information — let me check or connect you with someone who does."
              />
            </div>
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label">Rules it must always follow</label>
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
            <div className="card-title">Knowledge</div>
            <div className="card-sub">documents and connections this agent can use to answer</div>
          </div>
          <div className="card-body">
            <div className="form-group">
              <label className="form-label">Knowledge</label>
              {kbs.length === 0 ? (
                <div className="form-hint">
                  No knowledge bases yet in this account.{" "}
                  <a href="/knowledge-bases" target="_blank" rel="noopener noreferrer" style={{ color: "var(--cyan)" }}>
                    Add a knowledge base ↗
                  </a>{" "}
                  It opens in a new tab and shows up here when you come back.
                </div>
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
              <label className="form-label">Connections to your systems</label>
              {customApis.length === 0 ? (
                <div className="form-hint">
                  No connections yet in this account.{" "}
                  <a href="/knowledge-bases?tab=apis" target="_blank" rel="noopener noreferrer" style={{ color: "var(--cyan)" }}>
                    Add a connection ↗
                  </a>{" "}
                  It opens in a new tab and shows up here when you come back.
                </div>
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
            <div className="card-sub">instructions written from your answers — change anything before creating</div>
          </div>
          <div className="card-body">
            {createError && (
              <div className="error-banner">
                {createError}
                {languagesError && (
                  <>
                    {" "}
                    <a href="#" onClick={(e) => { e.preventDefault(); setStep("voice"); }}>Fix in Language &amp; Voice</a>
                  </>
                )}
              </div>
            )}
            <div className="form-group">
              <label className="form-label">Opening line <span className="hint">first thing the agent says</span></label>
              <input className="form-input" value={greeting} onChange={(e) => setGreeting(e.target.value)} />
            </div>
            <div className="form-group" style={{ marginBottom: 0 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <label className="form-label" style={{ marginBottom: 0 }}>How the agent should behave</label>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  style={{ marginLeft: "auto" }}
                  disabled={!llmId || generatingPrompt}
                  onClick={handleGenerateWithAi}
                >
                  {generatingPrompt ? "Writing…" : <><Sparkles size={13} /> Write with AI</>}
                </button>
              </div>
              {!llmId && (
                <div className="voice-missing" style={{ marginBottom: 6 }}>
                  To write this with AI, choose an AI model first.{" "}
                  <a href="#" onClick={(e) => { e.preventDefault(); setStep("voice"); }}>Choose an AI model</a>
                </div>
              )}
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
                  You edited this, so it no longer updates from earlier steps.{" "}
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => setPromptEdited(false)}>
                    Start over from your answers
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
            <ArrowLeft size={13} /> Back
          </button>
        )}
        {step !== "review" ? (
          <button className="btn btn-primary btn-sm" onClick={goNext} disabled={!canLeaveIdentity}>
            Next <ArrowRight size={13} />
          </button>
        ) : (
          <button className="btn btn-primary btn-sm" onClick={handleCreate} disabled={creating || !canLeaveIdentity}>
            {creating ? "Creating…" : "Create agent"}
          </button>
        )}
      </div>
    </>
  );
}
