"use client";

// One-screen agent creation: describe the job, get a working agent, then edit and test it.

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowLeft, ArrowRight, Mic, PenLine, Phone, PhoneIncoming, PhoneOutgoing, Sparkles,
} from "lucide-react";
import { ApiError, ProviderConfig, createAgent, createProvider, generateSystemPrompt, listProviders } from "@/lib/api";
import { BUILTIN_TTS_ENGINE, BUILTIN_TTS_VOICE } from "@/lib/engineCatalog";
import { useActiveTenant } from "@/lib/useActiveTenant";
import { AGENT_TEMPLATES, AgentTemplate, CallDirection, templateByKey } from "@/lib/agentTemplates";
import { buildSystemPrompt } from "@/lib/systemPromptBuilder";

const slugify = (text: string) => text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

const DIRECTIONS: { key: CallDirection; title: string; blurb: string }[] = [
  { key: "inbound", title: "People call my agent", blurb: "Inbound — support, bookings" },
  { key: "outbound", title: "My agent calls people", blurb: "Outbound — reminders, leads" },
];

const ROLE_LINE: Record<CallDirection, string> = {
  inbound: "You answer incoming phone calls for the business.",
  outbound: "You place outbound phone calls to customers on behalf of the business.",
};

const DEFAULT_GREETING: Record<CallDirection, string> = {
  inbound: "Hi, thanks for calling! How can I help you today?",
  outbound: "Hi, do you have a quick minute?",
};

const TASK_PLACEHOLDER: Record<CallDirection, string> = {
  inbound: "e.g. Answer calls for Acme Salon, book and reschedule appointments, and pass angry callers to the front desk.",
  outbound: "e.g. Call customers with an unpaid bill, remind them of the amount and note the date they promise to pay.",
};

export function QuickCreateAgent({ initialTemplate, onStepByStep }: {
  initialTemplate: string | null;
  onStepByStep: () => void;
}) {
  const router = useRouter();
  const { tenant, allTenants, isAllTenants } = useActiveTenant();
  const preset = templateByKey(initialTemplate);

  const [template, setTemplate] = useState<AgentTemplate | null>(preset);
  const [direction, setDirection] = useState<CallDirection>(preset?.direction ?? "inbound");
  const [task, setTask] = useState(preset?.task ?? "");
  const [name, setName] = useState(preset?.label ?? "");
  const [accountSlug, setAccountSlug] = useState("");
  // null until loaded: creating before then would skip the AI model and add a duplicate built-in voice.
  const [providers, setProviders] = useState<ProviderConfig[] | null>(null);
  const [providersError, setProvidersError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // A superadmin on "All tenants" still has to say which account owns the agent.
  const account = tenant ?? allTenants.find((t) => t.slug === accountSlug) ?? allTenants[0] ?? null;

  useEffect(() => {
    if (!account) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setProviders(null);
    setProvidersError(null);
    listProviders(account.id)
      .then(setProviders)
      .catch((e) => setProvidersError(e instanceof ApiError ? e.detail : String(e)));
  }, [account]);

  const pick = (role: "stt" | "llm" | "tts", accountDefault: string | null) => {
    const ofRole = (providers ?? []).filter((p) => p.role === role);
    if (accountDefault && ofRole.some((p) => p.id === accountDefault)) return accountDefault;
    return ofRole[0]?.id ?? null;
  };

  // The account's chosen voice if it has one, otherwise the built-in voice (set up on first use).
  const defaultVoiceId = async (accountId: string, accountDefault: string | null) => {
    const tts = (providers ?? []).filter((p) => p.role === "tts");
    if (accountDefault && tts.some((p) => p.id === accountDefault)) return accountDefault;
    const builtin = tts.find((p) => p.engine === BUILTIN_TTS_ENGINE && p.voice === BUILTIN_TTS_VOICE)
      ?? tts.find((p) => p.engine === BUILTIN_TTS_ENGINE);
    if (builtin) return builtin.id;
    try {
      return (await createProvider(accountId, {
        name: `${BUILTIN_TTS_ENGINE} — ${BUILTIN_TTS_VOICE}`, role: "tts", engine: BUILTIN_TTS_ENGINE, voice: BUILTIN_TTS_VOICE,
      })).id;
    } catch {
      return tts[0]?.id ?? null;
    }
  };

  const chooseTemplate = (t: AgentTemplate | null) => {
    setTemplate(t);
    // Only replace text the user hasn't typed themselves.
    const untouched = (value: string, field: "task" | "label") =>
      value.trim() === "" || AGENT_TEMPLATES.some((x) => x[field] === value);
    if (untouched(task, "task")) setTask(t?.task ?? "");
    if (untouched(name, "label")) setName(t?.label ?? "");
    if (t) setDirection(t.direction);
  };

  const canCreate = account !== null && providers !== null && slugify(name) !== "" && task.trim() !== "" && busy === null;

  const handleCreate = async () => {
    if (!account || !canCreate) return;
    setError(null);
    const llmId = pick("llm", account.default_llm_config_id);
    const tone = template?.tone ?? "Friendly and professional";
    const persona = `${ROLE_LINE[direction]} ${template?.persona ?? ""}`.trim();
    let systemPrompt: string | null = null;
    if (llmId) {
      setBusy("Writing instructions…");
      try {
        ({ system_prompt: systemPrompt } = await generateSystemPrompt(account.slug, {
          name: name.trim(), purpose: task.trim(), persona, tone,
          transfer_condition: template?.transferCondition ?? null, llm_config_id: llmId,
        }));
      } catch {
        // The AI model is optional here; the plain template below still gives a working agent.
      }
    }
    systemPrompt ??= buildSystemPrompt({
      name: name.trim(), purpose: task.trim(), persona, tone, language: null,
      hasKnowledgeBase: false, transferType: "none", transferCondition: null,
    });

    setBusy("Creating agent…");
    try {
      const agent = await createAgent(account.slug, {
        slug: slugify(name),
        name: name.trim(),
        greeting: template?.greeting ?? DEFAULT_GREETING[direction],
        system_prompt: systemPrompt,
        stt_config_id: pick("stt", account.default_stt_config_id),
        llm_config_id: llmId,
        tts_config_id: await defaultVoiceId(account.id, account.default_tts_config_id),
        status: "inactive",
      });
      router.push(`/agents/${account.slug}/${agent.slug}?new=1`);
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 409
          ? "You already have an agent with this name. Try another name."
          : e instanceof ApiError ? e.detail : String(e),
      );
      setBusy(null);
    }
  };

  return (
    <div className="qc">
      <button className="btn btn-ghost btn-sm" onClick={() => router.push("/agents")}>
        <ArrowLeft size={13} /> All agents
      </button>

      <div className="card qc-card">
        <h2>Create an agent</h2>
        <p className="qc-sub">Tell us what it should do. We write the instructions for you.</p>

        {isAllTenants && allTenants.length > 1 && (
          <div className="form-group">
            <label className="form-label">Account</label>
            <select className="form-select" value={account?.slug ?? ""} onChange={(e) => setAccountSlug(e.target.value)}>
              {allTenants.map((t) => <option key={t.id} value={t.slug}>{t.name}</option>)}
            </select>
          </div>
        )}

        <div className="qc-lbl"><span className="qc-num">1</span>Who makes the call?</div>
        <div className="qc-toggle" role="radiogroup" aria-label="Who makes the call?">
          {DIRECTIONS.map((d) => (
            <button
              key={d.key}
              type="button"
              role="radio"
              aria-checked={direction === d.key}
              className={direction === d.key ? "on" : ""}
              onClick={() => setDirection(d.key)}
            >
              <b>{d.title}</b>
              <span>{d.blurb}</span>
            </button>
          ))}
        </div>

        <div className="qc-lbl">
          <span className="qc-num">2</span>
          {preset ? (
            <>Your starting template <span className="qc-opt">— pre-filled for you, switch anytime</span></>
          ) : (
            <>Start from a template <span className="qc-opt">(optional)</span></>
          )}
        </div>
        <div className="qc-tpls">
          {AGENT_TEMPLATES.map((t) => (
            <button
              key={t.key}
              type="button"
              className={`qc-tpl${template?.key === t.key ? " on" : ""}`}
              aria-pressed={template?.key === t.key}
              onClick={() => chooseTemplate(t)}
            >
              <i className="tpl-ico"><t.icon size={15} /></i>
              <b>{t.label}</b>
              <span>{t.blurb}</span>
            </button>
          ))}
          <button
            type="button"
            className={`qc-tpl blank${template === null ? " on" : ""}`}
            aria-pressed={template === null}
            onClick={() => chooseTemplate(null)}
          >
            <i className="tpl-ico"><PenLine size={15} /></i>
            <b>Blank</b>
            <span>Start from scratch and describe the job yourself.</span>
          </button>
        </div>

        <div className="qc-lbl"><span className="qc-num">3</span>What should it do?</div>
        <textarea
          className="form-textarea"
          style={{ minHeight: 110 }}
          value={task}
          maxLength={600}
          placeholder={TASK_PLACEHOLDER[direction]}
          onChange={(e) => setTask(e.target.value)}
        />

        <div className="qc-lbl">Name</div>
        <input
          className="form-input"
          value={name}
          maxLength={80}
          placeholder="Booking Bot"
          onChange={(e) => setName(e.target.value)}
        />

        {providersError && (
          <div className="error-banner" style={{ marginTop: 12 }}>
            Couldn&apos;t load this account&apos;s voice and AI settings, so the agent can&apos;t be created yet: {providersError}
          </div>
        )}
        {error && <div className="error-banner" style={{ marginTop: 12 }}>{error}</div>}

        <div className="qc-foot">
          <span>
            Voice, language and limits use good defaults.{" "}
            <button type="button" className="qc-link" onClick={onStepByStep}>Set everything up step by step</button>
          </span>
          <button className="btn btn-primary btn-sm" onClick={handleCreate} disabled={!canCreate}>
            {busy ?? <>Create agent <ArrowRight size={13} /></>}
          </button>
        </div>
      </div>

      <aside className="qc-side">
        <div className="card qc-preview">
          <div className="qc-side-title">Preview</div>
          <div className="qc-agent">
            <div className="qc-avatar">{(name.trim()[0] ?? "A").toUpperCase()}</div>
            <div className="qc-agent-text">
              <b>{name.trim() || "Your agent"}</b>
              <span>
                {direction === "inbound" ? <PhoneIncoming size={12} /> : <PhoneOutgoing size={12} />}
                {direction === "inbound" ? "Answers calls" : "Places calls"}
                {template && <> · {template.label}</>}
              </span>
            </div>
          </div>
          <div className="qc-side-lbl">First thing it says</div>
          <div className="qc-bubble">{template?.greeting ?? DEFAULT_GREETING[direction]}</div>
          <div className="qc-side-lbl">Its job</div>
          <p className={`qc-job${task.trim() ? "" : " empty"}`}>
            {task.trim() || "Describe what it should do and it shows up here."}
          </p>
        </div>

        <div className="card qc-next">
          <div className="qc-side-title">What happens next</div>
          <ol>
            <li><Sparkles size={14} /><span><b>We write the instructions</b> from your description.</span></li>
            <li><Mic size={14} /><span><b>Test it in your browser</b> by talking to it right away.</span></li>
            <li><Phone size={14} /><span><b>Connect a phone number</b> when you&apos;re happy with it.</span></li>
          </ol>
        </div>
      </aside>
    </div>
  );
}
