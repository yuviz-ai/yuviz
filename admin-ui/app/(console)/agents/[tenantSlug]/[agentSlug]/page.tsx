"use client";

// One-screen agent editor: sections on the left, test call on the right, every change auto-saved.

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import {
  AlertCircle, ArrowLeft, ArrowRight, BookOpen, Check, Copy, FileText, Hash, Mic, MoreHorizontal,
  Loader2, PhoneForwarded, SlidersHorizontal, Sparkles, Trash2, Undo2,
} from "lucide-react";
import {
  Agent, AgentUpdate, ApiError, Call, PhoneNumber, ProviderConfig, createAgent, deleteAgent, getAgent,
  getCurrentUser, getLiveCalls, listCalls, listCampaigns, listPhoneNumbers, listProviders, rewritePrompt,
  undoPrompt, updateAgent,
} from "@/lib/api";
import { KnowledgeBasePanel } from "@/components/KnowledgeBasePanel";
import { AgentCustomApisPanel } from "@/components/AgentCustomApisPanel";
import { Modal } from "@/components/Modal";
import { SipPanel } from "@/components/SipPanel";
import { AgentVoiceSettings, isLanguageError, multilingualPayload } from "@/components/AgentVoiceSettings";
import { AgentTestPanel } from "@/components/AgentTestPanel";
import { LANGUAGES, OTHER } from "@/lib/engineCatalog";
import { normalizeDialTarget } from "@/lib/dialTargets";

type Section = "instructions" | "voice" | "knowledge" | "transfers" | "phone" | "advanced";

const SECTIONS: { key: Section; label: string; hint: string; icon: typeof FileText }[] = [
  { key: "instructions", label: "Instructions", hint: "What the agent says first and how it handles the call.", icon: FileText },
  { key: "voice", label: "Voice", hint: "The language it speaks and how it sounds.", icon: Mic },
  { key: "knowledge", label: "Knowledge", hint: "Documents and tools the agent can use to answer.", icon: BookOpen },
  { key: "transfers", label: "Transfers & ending", hint: "When to pass the caller to a person, and when to hang up.", icon: PhoneForwarded },
  { key: "phone", label: "Phone number", hint: "The number people call to reach this agent.", icon: Hash },
];

const ADVANCED = { key: "advanced" as Section, label: "Advanced", hint: "AI model, speech recognition and fine-tuning.", icon: SlidersHorizontal };

const TRANSFER_TYPES: { value: NonNullable<AgentUpdate["transfer_type"]>; title: string; blurb: string }[] = [
  { value: "none", title: "Don't transfer", blurb: "The agent handles every call on its own." },
  { value: "cold", title: "Connect directly", blurb: "The caller is put straight through to a person." },
  { value: "warm", title: "Introduce first", blurb: "The person picks up first, then the caller joins." },
];

const GRACE_OPTIONS_MS = [0, 500, 1000, 1500, 2000, 3000, 5000];
const ESCALATION_OPTIONS = [1, 2, 3, 4, 5];
const AUTOSAVE_DELAY_MS = 800;
// Free text that callers hear or that steers a live call; on an active agent it waits for blur or Save.
const LIVE_HELD_FIELDS = [
  "greeting", "greeting_by_language", "system_prompt", "end_call_prompt", "farewell_message", "transfer_prompt",
  "transfer_announcement", "transfer_destination", "platform_did", "custom_caller_id",
] as const;
const DIAL_FIELDS = ["transfer_destination", "platform_did", "custom_caller_id"] as const;

const formatSeconds = (ms: number) => (ms === 0 ? "No pause" : `${ms / 1000} second${ms === 1000 ? "" : "s"}`);

const formatCallTime = (iso: string) =>
  new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

const formatLength = (ms: number | null) => {
  if (ms == null) return "—";
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
};

const toLanguage = (choice: string, custom: string) =>
  choice === "" ? null : choice === OTHER ? custom.trim() || null : choice;

// The language-related editor state for a stored agent, in the shape the form and baseline hold it.
function languageState(a: Agent) {
  const known = !a.language || LANGUAGES.some((l) => l.value === a.language);
  return {
    languageChoice: !a.language ? "" : known ? a.language : OTHER,
    customLanguage: known ? "" : a.language ?? "",
    supported_languages: a.supported_languages ?? [],
    tts_config_by_language: a.tts_config_by_language ?? {},
    greeting_by_language: a.greeting_by_language ?? {},
  };
}

export default function AgentDetailPage() {
  const { tenantSlug, agentSlug } = useParams<{ tenantSlug: string; agentSlug: string }>();
  const router = useRouter();
  const searchParams = useSearchParams();

  const [agent, setAgent] = useState<Agent | null>(null);
  const [providers, setProviders] = useState<ProviderConfig[]>([]);
  const [numbers, setNumbers] = useState<PhoneNumber[] | null>(null);
  const [inCampaign, setInCampaign] = useState(false);
  const [recentCalls, setRecentCalls] = useState<Call[]>([]);
  const [section, setSection] = useState<Section>("instructions");
  const [justCreated] = useState(() => searchParams.get("new") === "1");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isSuperadmin, setIsSuperadmin] = useState(false);

  const [form, setForm] = useState<AgentUpdate>({});
  const [languageChoice, setLanguageChoice] = useState("");
  const [customLanguage, setCustomLanguage] = useState("");
  const [baseline, setBaseline] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  // The snapshot the server last rejected; autosave waits for the next edit instead of retrying it.
  const [rejected, setRejected] = useState<string | null>(null);
  // The server's 400 for the language fields, shown next to them as well as in the banner.
  const [languagesError, setLanguagesError] = useState<string | null>(null);

  const [askText, setAskText] = useState("");
  const [rewriting, setRewriting] = useState(false);
  const [rewriteError, setRewriteError] = useState<string | null>(null);
  // Prompt before the last "Ask AI" rewrite, for a one-step local undo.
  const [beforeRewrite, setBeforeRewrite] = useState<string | null>(null);

  const [menuOpen, setMenuOpen] = useState(false);
  const [duplicating, setDuplicating] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [liveCallCount, setLiveCallCount] = useState<number | null>(null);
  const [deleteChecking, setDeleteChecking] = useState(false);

  const snapshot = JSON.stringify({ form, languageChoice, customLanguage });
  // The latest edits, read when a save's reply lands (the save closure only has the sent ones).
  const latestSnapshot = useRef(snapshot);
  useEffect(() => {
    latestSnapshot.current = snapshot;
  }, [snapshot]);
  const dirty = baseline !== "" && snapshot !== baseline;
  const language = toLanguage(languageChoice, customLanguage);

  useEffect(() => {
    if (justCreated) router.replace(`/agents/${tenantSlug}/${agentSlug}`);
    getCurrentUser().then((me) => setIsSuperadmin(me.role === "superadmin")).catch(() => setIsSuperadmin(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    getAgent(tenantSlug, agentSlug)
      .then(async (a) => {
        setAgent(a);
        const lang = languageState(a);
        const initialForm: AgentUpdate = {
          name: a.name,
          greeting: a.greeting,
          system_prompt: a.system_prompt,
          goodbye_grace_ms: a.goodbye_grace_ms,
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
          supported_languages: lang.supported_languages,
          tts_config_by_language: lang.tts_config_by_language,
          greeting_by_language: lang.greeting_by_language,
        };
        setForm(initialForm);
        setLanguageChoice(lang.languageChoice);
        setCustomLanguage(lang.customLanguage);
        setBaseline(JSON.stringify({
          form: initialForm, languageChoice: lang.languageChoice, customLanguage: lang.customLanguage,
        }));
        listPhoneNumbers(a.tenant_id).then(setNumbers).catch(() => setNumbers(null));
        // Campaigns run in a separate service; if it's down the agent just counts as inbound.
        listCampaigns(a.tenant_id).then((cs) => setInCampaign(cs.some((c) => c.agent_id === a.id))).catch(() => {});
        listCalls(tenantSlug, { agentId: a.id, limit: 5 })
          .then((r) => setRecentCalls(r.items))
          .catch(() => {});
        setProviders(await listProviders(a.tenant_id));
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  }, [tenantSlug, agentSlug]);

  // On a live agent these change the next real call, so half-typed edits wait for blur or Save.
  const savedForm: AgentUpdate = baseline ? JSON.parse(baseline).form : {};
  const held = agent?.status === "active"
    && LIVE_HELD_FIELDS.some((k) => JSON.stringify(form[k] ?? null) !== JSON.stringify(savedForm[k] ?? null));
  const canSave = !!agent && dirty && !saving && snapshot !== rejected && !!form.name?.trim() && !!form.system_prompt?.trim();

  // Only changed fields are sent, so untouched values are never rewritten.
  const save = async () => {
    if (!agent || !canSave) return;
    const sent = snapshot;
    const base = JSON.parse(baseline);
    const changes: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(form)) {
      if (JSON.stringify(v ?? null) !== JSON.stringify(base.form[k] ?? null)) changes[k] = v;
    }
    for (const k of DIAL_FIELDS) if (k in changes) changes[k] = normalizeDialTarget(form[k]);
    const baseLanguage = toLanguage(base.languageChoice, base.customLanguage);
    if (language !== baseLanguage) changes.language = language;
    // The multilingual fields are one unit on the server (default language first, entries for
    // unsupported languages dropped), so send all three, normalised, whenever any differs.
    delete changes.supported_languages;
    delete changes.tts_config_by_language;
    delete changes.greeting_by_language;
    const multilingual = multilingualPayload(
      language, form.supported_languages, form.tts_config_by_language, form.greeting_by_language,
    );
    const savedMultilingual = multilingualPayload(
      baseLanguage, base.form.supported_languages, base.form.tts_config_by_language, base.form.greeting_by_language,
    );
    if (JSON.stringify(multilingual) !== JSON.stringify(savedMultilingual)) Object.assign(changes, multilingual);
    if (Object.keys(changes).length === 0) {
      setBaseline(sent);
      return;
    }
    setSaving(true);
    setSaveError(null);
    setLanguagesError(null);
    try {
      const updated = await updateAgent(tenantSlug, agent.id, changes as AgentUpdate);
      setAgent(updated);
      const sentState = JSON.parse(sent);
      if ("language" in changes || "supported_languages" in changes) {
        // The server normalises the language fields (e.g. picks a default when none was set):
        // the baseline becomes what it stored, and the page shows it for every field the user
        // hasn't edited since sending. An edit made during the save stays, dirty, and autosaves.
        const { languageChoice: choice, customLanguage: custom, ...stored } = languageState(updated);
        const latest = JSON.parse(latestSnapshot.current);
        const same = (a: unknown, b: unknown) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
        if (latest.languageChoice === sentState.languageChoice && latest.customLanguage === sentState.customLanguage) {
          setLanguageChoice(choice);
          setCustomLanguage(custom);
        }
        const keep = (Object.keys(stored) as (keyof typeof stored)[]).filter(
          (k) => same(latest.form[k], sentState.form[k]),
        );
        setForm((prev) => ({ ...prev, ...Object.fromEntries(keep.map((k) => [k, stored[k]])) }));
        setBaseline(JSON.stringify({
          form: { ...sentState.form, ...stored }, languageChoice: choice, customLanguage: custom,
        }));
      } else {
        setBaseline(sent);
      }
      setRejected(null);
    } catch (e) {
      const detail = e instanceof ApiError ? e.detail : String(e);
      setSaveError(detail);
      if (e instanceof ApiError && e.status === 400 && isLanguageError(detail)) setLanguagesError(detail);
      setRejected(sent);
    } finally {
      setSaving(false);
    }
  };
  const saveHeld = () => {
    if (held) save();
  };

  // Autosave. Re-runs when a save lands, so edits made during it are saved next.
  useEffect(() => {
    if (!canSave || held) return;
    const timer = setTimeout(save, AUTOSAVE_DELAY_MS);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshot, baseline, saving, held]);

  const handleAskAi = async () => {
    if (!agent || !askText.trim()) return;
    setRewriting(true);
    setRewriteError(null);
    try {
      const current = form.system_prompt ?? "";
      const { after } = await rewritePrompt(tenantSlug, agent.id, { prompt: current, instruction: askText.trim() });
      setBeforeRewrite(current);
      setForm((f) => ({ ...f, system_prompt: after }));
      setAskText("");
    } catch (e) {
      const detail = e instanceof ApiError ? e.detail : "";
      setRewriteError(
        detail === "no_ai_model" ? "Choose an AI model in Advanced first."
          : detail === "unusable_output" ? "The AI's answer couldn't be used. Try wording it differently."
          : e instanceof ApiError && e.status === 429 ? "Too many requests. Try again in a few minutes."
          : "The AI couldn't rewrite this right now. Try again.",
      );
    } finally {
      setRewriting(false);
    }
  };

  const handleUndo = async () => {
    if (beforeRewrite !== null) {
      setForm((f) => ({ ...f, system_prompt: beforeRewrite }));
      setBeforeRewrite(null);
      return;
    }
    if (!agent) return;
    try {
      const restored = await undoPrompt(tenantSlug, agent.id);
      setAgent(restored);
      setForm((f) => ({ ...f, system_prompt: restored.system_prompt }));
      const base = JSON.parse(baseline);
      base.form.system_prompt = restored.system_prompt;
      setBaseline(JSON.stringify(base));
    } catch (e) {
      setSaveError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleDuplicate = async () => {
    if (!agent) return;
    setMenuOpen(false);
    setDuplicating(true);
    setSaveError(null);
    // Created paused, so a failed second step never leaves a half-copy taking calls.
    const body = {
      name: `${form.name?.trim() || agent.name} (copy)`,
      greeting: form.greeting,
      system_prompt: form.system_prompt,
      stt_config_id: form.stt_config_id,
      llm_config_id: form.llm_config_id,
      tts_config_id: form.tts_config_id,
      status: "inactive" as const,
    };
    const dialTargets = Object.fromEntries(DIAL_FIELDS.map((k) => [k, normalizeDialTarget(form[k])]));
    try {
      let copy: Agent | null = null;
      for (const slug of [`${agent.slug}-copy`, `${agent.slug}-copy-${Math.random().toString(36).slice(2, 6)}`]) {
        try {
          copy = await createAgent(tenantSlug, { ...body, slug });
          break;
        } catch (e) {
          if (!(e instanceof ApiError && e.status === 409)) throw e;
        }
      }
      if (!copy) throw new Error("Couldn't find a free name for the copy.");
      await updateAgent(tenantSlug, copy.id, {
        ...form, ...dialTargets, name: copy.name, language, status: "inactive",
        ...multilingualPayload(language, form.supported_languages, form.tts_config_by_language, form.greeting_by_language),
      });
      router.push(`/agents/${tenantSlug}/${copy.slug}`);
    } catch (e) {
      setSaveError(e instanceof ApiError ? e.detail : String(e));
      setDuplicating(false);
    }
  };

  const openDeleteConfirm = async () => {
    if (!agent) return;
    setMenuOpen(false);
    setDeleteConfirmOpen(true);
    setSaveError(null);
    // Live Calls is superadmin-only; other roles rely on the DELETE's 409.
    if (!isSuperadmin) {
      setLiveCallCount(0);
      return;
    }
    setLiveCallCount(null);
    setDeleteChecking(true);
    try {
      // LiveCall has no agent_id, so match by name; only a pre-check — the DELETE is authoritative.
      const live = await getLiveCalls(tenantSlug);
      setLiveCallCount(live.items.filter((c) => c.agent_name === agent.name).length);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) setLiveCallCount(0);
      else setSaveError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setDeleteChecking(false);
    }
  };

  const handleDelete = async () => {
    if (!agent) return;
    setDeleting(true);
    try {
      await deleteAgent(tenantSlug, agent.id);
      router.push("/agents");
    } catch (e) {
      // 409 = a call started since the pre-check; switch to the blocked variant.
      if (e instanceof ApiError && e.status === 409 && e.body?.live_call_count !== undefined) {
        setLiveCallCount(Number(e.body.live_call_count));
        setDeleting(false);
        return;
      }
      setSaveError(e instanceof ApiError ? e.detail : String(e));
      setDeleting(false);
    }
  };

  if (loading) return <div className="empty-state">Loading…</div>;
  if (error) return <div className="error-banner">{error}</div>;
  if (!agent) return null;

  const isActive = (form.status || "active") === "active";
  const isDraft = !isActive && !agent.activated_at;
  const transferType = form.transfer_type || "none";
  const graceMs = form.goodbye_grace_ms ?? 0;
  const graceOptions = GRACE_OPTIONS_MS.includes(graceMs) ? GRACE_OPTIONS_MS : [...GRACE_OPTIONS_MS, graceMs].sort((a, b) => a - b);
  const escalation = form.escalation_threshold ?? null;
  const escalationOptions =
    escalation === null || ESCALATION_OPTIONS.includes(escalation) ? ESCALATION_OPTIONS : [...ESCALATION_OPTIONS, escalation].sort((a, b) => a - b);
  const hasNumber = numbers?.some((n) => n.agent_id === agent.id) ?? true;
  const needsNumber = !hasNumber && !inCampaign;

  // First thing still stopping the agent from taking real calls.
  const blocker: { text: string; action?: string; section: Section } | null =
    !form.llm_config_id ? { text: "Choose an AI model so the agent can think", action: "Choose", section: "advanced" }
      : !form.stt_config_id ? { text: "Choose speech recognition so the agent can hear callers", action: "Choose", section: "advanced" }
      : !form.tts_config_id ? { text: "Pick a voice so the agent can speak", action: "Pick voice", section: "voice" }
      : !form.system_prompt?.trim() ? { text: "Write instructions so the agent knows its job", section: "instructions" }
      : needsNumber ? { text: "Almost ready — add a phone number to take real calls", action: "Add number", section: "phone" }
      : null;

  const saveStatus = saveError ? (
    <span className="ed-save err" title={saveError}><AlertCircle size={13} /> Not saved</span>
  ) : dirty && !form.name?.trim() ? (
    <span className="ed-save err"><AlertCircle size={13} /> Name needed</span>
  ) : dirty && !form.system_prompt?.trim() ? (
    <span className="ed-save err"><AlertCircle size={13} /> Instructions needed</span>
  ) : held && !saving ? (
    <button className="btn btn-primary btn-sm" onClick={save} title="This agent is taking calls, so these changes wait for you">
      Save changes
    </button>
  ) : saving || dirty ? (
    <span className="ed-save"><Loader2 size={13} className="spin" /> Saving…</span>
  ) : (
    <span className="ed-save ok"><Check size={13} /> Saved</span>
  );
  const sectionInfo = SECTIONS.find((s) => s.key === section) ?? ADVANCED;

  return (
    <div className="ed2">
      <div className="ed2-top">
        <button className="btn btn-ghost btn-sm" onClick={() => router.push("/agents")}>
          <ArrowLeft size={13} /> Agents
        </button>
        <input
          className="ed2-name"
          value={form.name ?? ""}
          aria-label="Agent name"
          placeholder="Agent name"
          onChange={(e) => setForm({ ...form, name: e.target.value })}
        />
        {isDraft ? (
          <>
            <span
              className="ed2-live"
              style={{ cursor: "default", opacity: 0.75 }}
              title="This agent hasn't gone live yet. Configure it, then go live."
            >
              <i /> Draft
            </span>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              title="Make this agent available to take calls"
              onClick={() => setForm({ ...form, status: "active" })}
            >
              Go live
            </button>
          </>
        ) : (
          <button
            type="button"
            role="switch"
            aria-checked={isActive}
            className={`ed2-live${isActive ? " on" : ""}`}
            title={isActive ? "Click to pause this agent" : "Click to let this agent take calls"}
            onClick={() => setForm({ ...form, status: isActive ? "inactive" : "active" })}
          >
            <i /> {isActive ? "Taking calls" : "Paused"}
          </button>
        )}
        <span className="ed2-dir">{inCampaign ? "My agent calls people" : "People call my agent"}</span>
        <div className="ed2-top-right">
          <div className="ed2-save" aria-live="polite">{saveStatus}</div>
          <button
            className="btn btn-primary btn-sm ed2-test-jump"
            onClick={() => document.getElementById("agent-test")?.scrollIntoView({ behavior: "smooth" })}
          >
            Test
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => router.push(`/workflows/${tenantSlug}/${agentSlug}`)}>
            Conversation steps <ArrowRight size={13} />
          </button>
          <div className="ed2-menu">
            <button className="btn btn-ghost btn-sm" aria-label="More actions" aria-expanded={menuOpen} onClick={() => setMenuOpen((o) => !o)}>
              <MoreHorizontal size={15} />
            </button>
            {menuOpen && (
              <>
                <div className="ed2-menu-backdrop" onClick={() => setMenuOpen(false)} />
                <div className="ed2-menu-pop" role="menu">
                  <button
                    role="menuitem"
                    onClick={handleDuplicate}
                    disabled={duplicating}
                    title="Copies the settings. Documents, connections and conversation steps are not copied, and the copy starts paused."
                  >
                    <Copy size={13} /> Duplicate settings
                  </button>
                  <button role="menuitem" className="danger" onClick={openDeleteConfirm}>
                    <Trash2 size={13} /> Delete agent
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      <div className="ed2-body">
        <nav className="ed2-nav" aria-label="Agent settings">
          {SECTIONS.map((s) => (
            <button key={s.key} className={section === s.key ? "on" : ""} onClick={() => setSection(s.key)}>
              <s.icon size={15} /> {s.label}
              {s.key === "phone" && needsNumber && <span className="ed2-warn" aria-label="No phone number" />}
            </button>
          ))}
          <button className={`ed2-nav-adv${section === "advanced" ? " on" : ""}`} onClick={() => setSection("advanced")}>
            <SlidersHorizontal size={15} /> Advanced
          </button>
        </nav>

        <main className="ed2-main">
          <div className="ed2-sec-hdr">
            <h2>{sectionInfo.label}</h2>
            <p>{sectionInfo.hint}</p>
          </div>
          {saveError && (
            <div className="ed2-ready err">
              <AlertCircle size={14} /> Your last change wasn&apos;t saved: {saveError}
            </div>
          )}
          {justCreated && (
            <div className="ed2-ready ok">
              <Check size={14} />{" "}
              {isDraft ? (
                <>Agent created as a draft. Review the settings, then press <strong>Go live</strong> when ready.</>
              ) : (
                "Your agent is ready. Try it with a test call, then fine-tune anything here."
              )}
            </div>
          )}
          {blocker && (
            <div className="ed2-ready">
              <AlertCircle size={14} /> {blocker.text}
              {blocker.action && section !== blocker.section && (
                <button className="ed2-ready-act" onClick={() => setSection(blocker.section)}>{blocker.action}</button>
              )}
            </div>
          )}

          {section === "instructions" && (
            <div className="card">
              <div className="card-body">
                <div className="ed2-ask">
                  <Sparkles size={14} />
                  <input
                    className="form-input"
                    value={askText}
                    maxLength={500}
                    placeholder={'Ask AI: "Make it more polite, also speak Hindi"'}
                    onChange={(e) => setAskText(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && handleAskAi()}
                    disabled={rewriting}
                  />
                  <button className="btn btn-primary btn-sm" onClick={handleAskAi} disabled={rewriting || !askText.trim()}>
                    {rewriting ? "Rewriting…" : "Rewrite"}
                  </button>
                </div>
                {rewriteError && <div className="error-banner">{rewriteError}</div>}

                <div className="form-group">
                  <label className="form-label">Opening line</label>
                  <input
                    className="form-input"
                    value={form.greeting ?? ""}
                    placeholder="Hi, thanks for calling Acme Dental. How can I help you today?"
                    onChange={(e) => setForm({ ...form, greeting: e.target.value })}
                    onBlur={saveHeld}
                  />
                  <div className="form-hint">The first thing callers hear when the agent picks up.</div>
                </div>
                <div className="form-group" style={{ marginBottom: 0 }}>
                  <div className="ed2-lbl-row">
                    <label className="form-label">How the agent should behave</label>
                    {(beforeRewrite !== null || agent.can_undo) && (
                      <button type="button" className="ed2-undo" onClick={handleUndo}>
                        <Undo2 size={12} /> Undo {beforeRewrite !== null ? "AI rewrite" : "last fix"}
                      </button>
                    )}
                  </div>
                  <textarea
                    className="form-textarea"
                    style={{ minHeight: 320 }}
                    value={form.system_prompt ?? ""}
                    placeholder={"Who you are: You are the friendly receptionist for Acme Dental.\nWhat you do: Book, move or cancel appointments.\nWhen you don't know: Offer to take a message.\nNever: Give medical advice."}
                    onChange={(e) => setForm({ ...form, system_prompt: e.target.value })}
                    onBlur={saveHeld}
                  />
                  <div className="form-hint">
                    Say who the agent is, what it helps with, and what it must never do. Changes also update its conversation steps.
                  </div>
                </div>
              </div>
            </div>
          )}

          {(section === "voice" || section === "advanced") && (
            <AgentVoiceSettings
              part={section === "voice" ? "voice" : "engines"}
              tenantId={agent.tenant_id}
              providers={providers}
              setProviders={setProviders}
              languageChoice={languageChoice}
              onLanguageChoice={setLanguageChoice}
              customLanguage={customLanguage}
              onCustomLanguage={setCustomLanguage}
              supportedLanguages={form.supported_languages ?? []}
              onSupportedLanguages={(v) => setForm((prev) => ({ ...prev, supported_languages: v }))}
              ttsByLanguage={form.tts_config_by_language ?? {}}
              onTtsByLanguage={(v) => setForm((prev) => ({ ...prev, tts_config_by_language: v }))}
              greetingByLanguage={form.greeting_by_language ?? {}}
              onGreetingByLanguage={(v) => setForm((prev) => ({ ...prev, greeting_by_language: v }))}
              onGreetingBlur={saveHeld}
              languagesError={languagesError}
              sttId={form.stt_config_id}
              llmId={form.llm_config_id}
              ttsId={form.tts_config_id}
              onAssign={(role, id) => setForm((prev) => ({ ...prev, [`${role}_config_id`]: id }))}
              onError={setSaveError}
            />
          )}

          {section === "knowledge" && (
            <>
              <KnowledgeBasePanel tenantId={agent.tenant_id} agentId={agent.id} />
              <div style={{ marginTop: 16 }}>
                <AgentCustomApisPanel tenantId={agent.tenant_id} agentId={agent.id} />
              </div>
            </>
          )}

          {section === "transfers" && (
            <>
              <div className="card" style={{ marginBottom: 14 }}>
                <div className="card-hdr">
                  <div className="card-title">Transfer to a person</div>
                  <div className="card-sub">hand the caller to someone on your team</div>
                </div>
                <div className="card-body">
                  <div className="voice-engine-row" role="radiogroup" aria-label="Transfer type">
                    {TRANSFER_TYPES.map((t) => (
                      <button
                        key={t.value}
                        type="button"
                        role="radio"
                        aria-checked={transferType === t.value}
                        className={`voice-engine-card${transferType === t.value ? " on" : ""}`}
                        onClick={() => setForm({ ...form, transfer_type: t.value })}
                      >
                        <div className="agent-template-title">{t.title}</div>
                        <div className="agent-template-blurb">{t.blurb}</div>
                      </button>
                    ))}
                  </div>

                  {transferType !== "none" && (
                    <>
                      <div className="form-group">
                        <label className="form-label">Transfer to <span className="required">*</span></label>
                        <input
                          className="form-input"
                          value={form.transfer_destination || ""}
                          onChange={(e) => setForm({ ...form, transfer_destination: e.target.value || null })}
                          onBlur={saveHeld}
                          placeholder="+1 800 555 0100"
                        />
                        <div className="form-hint">Include the country code, e.g. +1 800 555 0100.</div>
                      </div>
                      <div className="form-group">
                        <label className="form-label">When should it transfer?</label>
                        <textarea
                          className="form-textarea"
                          style={{ minHeight: 56 }}
                          value={form.transfer_prompt || ""}
                          onChange={(e) => setForm({ ...form, transfer_prompt: e.target.value || null })}
                          onBlur={saveHeld}
                          placeholder="If the caller asks to speak to a person."
                        />
                        <div className="form-hint">Describe the moment, not the words. Leave blank to use the default.</div>
                      </div>
                      <div className="form-group" style={{ marginBottom: transferType === "warm" ? 14 : 0 }}>
                        <label className="form-label">What it says before transferring</label>
                        <textarea
                          className="form-textarea"
                          style={{ minHeight: 56 }}
                          value={form.transfer_announcement || ""}
                          onChange={(e) => setForm({ ...form, transfer_announcement: e.target.value || null })}
                          onBlur={saveHeld}
                          placeholder="Please hold while I connect you."
                        />
                        <div className="form-hint">Spoken exactly as written. Leave blank to let the agent choose its own words.</div>
                      </div>
                    </>
                  )}

                  {transferType === "warm" && (
                    <div className="form-row" style={{ flexWrap: "wrap" }}>
                      <div className="form-group">
                        <label className="form-label">Number your teammate sees</label>
                        <select
                          className="form-select"
                          value={form.caller_id_policy || "original"}
                          onChange={(e) => setForm({ ...form, caller_id_policy: e.target.value as AgentUpdate["caller_id_policy"] })}
                        >
                          <option value="original">The caller&apos;s number</option>
                          <option value="platform">One of your business numbers</option>
                          <option value="custom">Another number</option>
                        </select>
                      </div>
                      {form.caller_id_policy === "platform" && (
                        <div className="form-group">
                          <label className="form-label">Business number</label>
                          <input
                            className="form-input"
                            value={form.platform_did || ""}
                            onChange={(e) => setForm({ ...form, platform_did: e.target.value || null })}
                            onBlur={saveHeld}
                            placeholder="+1 800 555 0100"
                          />
                        </div>
                      )}
                      {form.caller_id_policy === "custom" && (
                        <div className="form-group">
                          <label className="form-label">Number to show</label>
                          <input
                            className="form-input"
                            value={form.custom_caller_id || ""}
                            onChange={(e) => setForm({ ...form, custom_caller_id: e.target.value || null })}
                            onBlur={saveHeld}
                            placeholder="+1 800 555 0100"
                          />
                        </div>
                      )}
                      <div className="form-group">
                        <label className="form-label">While the caller waits</label>
                        <select
                          className="form-select"
                          value={form.transfer_waiting_experience || "announcement_moh"}
                          onChange={(e) =>
                            setForm({ ...form, transfer_waiting_experience: e.target.value as AgentUpdate["transfer_waiting_experience"] })
                          }
                        >
                          <option value="announcement_moh">Short message, then hold music</option>
                          <option value="announcement_silence">Short message, then silence</option>
                        </select>
                      </div>
                    </div>
                  )}
                </div>
              </div>

              <div className="card">
                <div className="card-hdr">
                  <div className="card-title">Ending the call</div>
                  <div className="card-sub">when the agent hangs up, and how long a call can last</div>
                </div>
                <div className="card-body">
                  <div className="form-group">
                    <label className="form-label">When should the agent end the call?</label>
                    <textarea
                      className="form-textarea"
                      style={{ minHeight: 56 }}
                      value={form.end_call_prompt || ""}
                      onChange={(e) => setForm({ ...form, end_call_prompt: e.target.value || null })}
                      onBlur={saveHeld}
                      placeholder="When the caller says goodbye, has no more questions, or their issue is sorted."
                    />
                    <div className="form-hint">Describe the moment, not the words. Leave blank to use the default.</div>
                  </div>
                  <div className="form-group">
                    <label className="form-label">Goodbye message</label>
                    <textarea
                      className="form-textarea"
                      style={{ minHeight: 56 }}
                      value={form.farewell_message || ""}
                      onChange={(e) => setForm({ ...form, farewell_message: e.target.value || null })}
                      onBlur={saveHeld}
                      placeholder="Thanks for calling. Have a great day. Goodbye!"
                    />
                    <div className="form-hint">Spoken exactly as written. Leave blank to let the agent choose its own words.</div>
                  </div>
                  <div className="form-group" style={{ marginBottom: 0 }}>
                    <label className="form-label">Max call length</label>
                    <div className="agent-unit-input">
                      <input
                        className="form-input"
                        type="number"
                        min={1}
                        max={120}
                        placeholder="No limit"
                        value={form.max_call_duration_s == null ? "" : Math.round(form.max_call_duration_s / 60)}
                        onChange={(e) => {
                          const minutes = Number(e.target.value);
                          setForm({
                            ...form,
                            max_call_duration_s:
                              e.target.value === "" || !(minutes > 0) ? null : Math.min(7200, Math.max(30, Math.round(minutes * 60))),
                          });
                        }}
                      />
                      <span>minutes</span>
                    </div>
                    <div className="form-hint">The agent wraps up politely, then ends the call. Leave blank for no limit.</div>
                  </div>
                </div>
              </div>
            </>
          )}

          {section === "phone" && <SipPanel tenantId={agent.tenant_id} agentId={agent.id} />}

          {section === "advanced" && (
            <div className="card" style={{ marginTop: 14 }}>
              <div className="card-hdr">
                <div className="card-title">Call behaviour</div>
              </div>
              <div className="card-body">
                <div className="form-group">
                  <label className="form-label">Pause before hanging up</label>
                  <select
                    className="form-select"
                    style={{ maxWidth: 260 }}
                    value={graceMs}
                    onChange={(e) => setForm({ ...form, goodbye_grace_ms: Number(e.target.value) })}
                  >
                    {graceOptions.map((ms) => <option key={ms} value={ms}>{formatSeconds(ms)}</option>)}
                  </select>
                  <div className="form-hint">Gives the caller a moment to say goodbye back.</div>
                </div>
                <div className="form-group" style={{ marginBottom: 0 }}>
                  <label className="form-label">Hand off to a person after repeated problems</label>
                  <select
                    className="form-select"
                    style={{ maxWidth: 260 }}
                    value={escalation ?? ""}
                    onChange={(e) => setForm({ ...form, escalation_threshold: e.target.value === "" ? null : Number(e.target.value) })}
                  >
                    <option value="">Never</option>
                    {escalationOptions.map((n) => <option key={n} value={n}>After {n} in a row</option>)}
                  </select>
                  <div className="form-hint">
                    If the caller keeps asking for things the agent isn&apos;t allowed to help with, pass them to someone on your team.
                  </div>
                  {escalation !== null && transferType === "none" && (
                    <div className="voice-missing">
                      This needs a transfer set up first.{" "}
                      <a href="#" onClick={(e) => { e.preventDefault(); setSection("transfers"); }}>Set up a transfer</a>
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
        </main>

        <aside className="ed2-side" id="agent-test">
          <AgentTestPanel
            tenantSlug={tenantSlug}
            agentSlug={agentSlug}
            savePending={saving || (canSave && !held)}
            blockedReason={
              saving || !dirty ? null
                : !form.name?.trim() ? "Add a name to test"
                : !form.system_prompt?.trim() ? "Add instructions to test"
                : snapshot === rejected ? "Fix the error above to test"
                : held ? "Save your changes to test them"
                : null
            }
          />
          {!isDraft && (
            <div className="card ed2-calls">
              <div className="ed2-calls-hdr">
                <b>This agent&apos;s recent calls</b>
                <Link href="/calls">All calls</Link>
              </div>
              {recentCalls.length === 0 ? (
                <div className="ed-test-empty">No calls yet.</div>
              ) : (
                recentCalls.map((c) => (
                  <Link key={c.session_id} href={`/calls/${c.session_id}`} className="ed2-call">
                    <span>{formatCallTime(c.started_at)}</span>
                    <span>{c.ended_at ? formatLength(c.duration_ms) : "Live"}</span>
                  </Link>
                ))
              )}
            </div>
          )}
        </aside>
      </div>

      <Modal
        open={deleteConfirmOpen}
        title={
          deleteChecking
            ? `Checking "${agent.name}"…`
            : liveCallCount
              ? `Can't delete "${agent.name}" right now`
              : `Delete "${agent.name}"?`
        }
        onClose={() => setDeleteConfirmOpen(false)}
        footer={
          deleteChecking ? (
            <button className="btn btn-ghost btn-sm" onClick={() => setDeleteConfirmOpen(false)}>Cancel</button>
          ) : liveCallCount ? (
            // Deliberately no "force delete": this would cut off a live call.
            <>
              <button className="btn btn-ghost btn-sm" onClick={() => setDeleteConfirmOpen(false)}>Cancel</button>
              {isSuperadmin && <Link href="/live-calls" className="btn btn-ghost btn-sm">View live calls</Link>}
            </>
          ) : (
            <>
              <button className="btn btn-ghost btn-sm" onClick={() => setDeleteConfirmOpen(false)}>Cancel</button>
              <button className="btn btn-danger btn-sm" onClick={handleDelete} disabled={deleting}>
                {deleting ? "Deleting…" : "Delete agent"}
              </button>
            </>
          )
        }
      >
        {deleteChecking ? (
          <p style={{ fontSize: ".78rem", color: "var(--text-3)" }}>Checking whether anyone is on a call with this agent…</p>
        ) : liveCallCount ? (
          <p style={{
            fontSize: ".78rem", color: "var(--text-2)", lineHeight: 1.5,
            borderLeft: "2px solid var(--red-border)", padding: "6px 0 6px 10px", margin: 0,
          }}>
            <b>{`${liveCallCount} call${liveCallCount === 1 ? " is" : "s are"} happening`}</b>{" "}
            {`on this agent right now. Deleting it would cut ${liveCallCount === 1 ? "that caller" : "those callers"} off. Wait for the call to end, then try again.`}
          </p>
        ) : (
          <p style={{
            fontSize: ".78rem", color: "var(--text-2)", lineHeight: 1.5,
            borderLeft: "2px solid var(--green-border)", padding: "6px 0 6px 10px", margin: 0,
          }}>
            {isSuperadmin
              ? "No one is on a call with this agent."
              : "If a call is in progress on this agent, deletion will be refused until it ends."}{" "}
            Phone numbers that use it will switch to their backup agent, or to your account&apos;s default agent.
          </p>
        )}
      </Modal>
    </div>
  );
}
