"use client";

// Language, voice, hearing and thinking for one agent — shared by the creation
// wizard and the agent page so both read the same.

import { Dispatch, SetStateAction, useEffect, useState } from "react";
import Link from "next/link";
import { AlertCircle, Check } from "lucide-react";
import {
  AgentUpdate, ApiError, ElevenLabsVoice, LanguageMode, ProviderConfig, ProviderConfigCreate, createProvider, listProviderVoices,
  listProviders, updateProvider,
} from "@/lib/api";
import {
  BUILTIN_TTS_ENGINE, BUILTIN_TTS_VOICE, DEEPGRAM_MULTI_LANGUAGES, LANGUAGE_MODES, LOCAL_ENGINES, MAX_LANGUAGES,
  PREMIUM_ENGINES, SUPPORTED_LANGUAGES, baseLanguage, deepgramSupportsMulti, keptGreetings, languageLabel, languagesWithoutVoice, ttsLanguages, voiceSpeaks,
} from "@/lib/engineCatalog";
import { ModelRef, useLocalModels } from "@/lib/useLocalModels";

type Role = "stt" | "llm" | "tts";

export type LocalModels = ReturnType<typeof useLocalModels>;

// The catalogue model a provider config points at, or null when the Config Service does not manage it:
// premium engines, and an Ollama with its own base_url (Config never pulls into a URL a tenant supplies).
export function localModelRef(p: ProviderConfig): ModelRef | null {
  if (!LOCAL_ENGINES.has(p.engine) || (p.engine === "ollama" && p.extra?.base_url)) return null;
  const name = p.role === "tts" ? p.voice || (p.engine === BUILTIN_TTS_ENGINE ? BUILTIN_TTS_VOICE : "") : p.model;
  return name ? { engine: p.engine, name } : null;
}

// The free models behind the given provider configs.
export function agentLocalModels(providers: ProviderConfig[], ids: (string | null | undefined)[]): ModelRef[] {
  return ids.flatMap((id) => {
    const p = providers.find((x) => x.id === id);
    const ref = p && localModelRef(p);
    return ref ? [ref] : [];
  });
}

// Models that are in the catalogue but not usable yet (missing, downloading, failed, unavailable).
export function notReadyModels(refs: ModelRef[], { status }: LocalModels) {
  return refs.flatMap((r) => {
    const s = status(r.engine, r.name);
    return s && s !== "ready" ? [{ ...r, status: s }] : [];
  });
}

// PATCH fields for the language settings. A monolingual agent clears the per-language voice map;
// a multilingual one keeps entries for its non-default languages only (the default language's voice
// is `tts_config_id`), dropping blank ones. The server clears the stored greetings on a monolingual write.
export function multilingualPayload(
  mode: LanguageMode,
  languages: string[],
  ttsByLanguage: Record<string, string> | null | undefined,
): Pick<AgentUpdate, "language" | "language_mode" | "supported_languages" | "tts_config_by_language"> {
  if (mode === "monolingual") {
    return { language: languages[0] ?? null, language_mode: mode, supported_languages: null, tts_config_by_language: null };
  }
  const voices: Record<string, string> = {};
  for (const l of languages.slice(1)) if (ttsByLanguage?.[l]?.trim()) voices[l] = ttsByLanguage[l].trim();
  return {
    language: languages[0], language_mode: mode, supported_languages: languages,
    tts_config_by_language: Object.keys(voices).length ? voices : null,
  };
}

// The Config Service's 400s for language/voice coverage all name the language field or say
// it can't be spoken; those belong next to the languages section, not just in the banner.
export function isLanguageError(detail: string): boolean {
  return /language|can't be spoken/i.test(detail);
}

// Server 400s name engines and voice ids; show a short plain-language line instead.
function friendlyLanguageError(detail: string): string {
  const lang = detail.match(/^(.+?) \([a-z]{2}(?:-[A-Z]{2})?\) can't be spoken/i)?.[1]
    ?? detail.match(/can't speak (.+?)\./i)?.[1];
  if (lang) {
    return `${lang} needs a voice that can speak it. Pick one below, or add one in AI & Voice.`;
  }
  if (/can't be spoken|TTS provider|voice override|multilingual TTS/i.test(detail)) {
    return "One of these languages needs a different voice. Pick one below, or add one in AI & Voice.";
  }
  return detail;
}

// `ready: false` = no voice-service integration yet, shown so users know it's coming.
const VOICE_SERVICES: { engine: string; name: string; blurb: string; ready: boolean }[] = [
  { engine: "elevenlabs", name: "ElevenLabs", blurb: "The most lifelike voices, many languages.", ready: true },
  { engine: "cartesia", name: "Cartesia", blurb: "Very fast, natural voices.", ready: true },
  { engine: "sarvam", name: "Sarvam", blurb: "Made for Indian languages.", ready: false },
  { engine: "smallest", name: "Smallest AI", blurb: "Low-cost, quick voices.", ready: false },
];

const SERVICE_NAMES: Record<string, string> = {
  elevenlabs: "ElevenLabs", cartesia: "Cartesia", deepgram: "Deepgram", macos: "Basic voice",
};

// Voices of a connected premium account that speak `lang`. Falls back to a pasted voice ID when
// the account's voice list can't be loaded.
function PremiumLanguageVoice({ provider, lang, current, placeholder, onPick }: {
  provider: ProviderConfig;
  lang: string;
  /** The voice config now chosen for `lang`, if any. */
  current: ProviderConfig | undefined;
  /** Shown while nothing is chosen. */
  placeholder: string;
  onPick: (voiceId: string, label: string) => void;
}) {
  // undefined = still loading, null = the list failed to load.
  const [loaded, setLoaded] = useState<{ providerId: string; voices: ElevenLabsVoice[] | null } | null>(null);
  const [manualId, setManualId] = useState("");

  useEffect(() => {
    let cancelled = false;
    listProviderVoices(provider.id)
      .then((voices) => { if (!cancelled) setLoaded({ providerId: provider.id, voices }); })
      .catch(() => { if (!cancelled) setLoaded({ providerId: provider.id, voices: null }); });
    return () => { cancelled = true; };
  }, [provider.id]);

  const voices = loaded?.providerId === provider.id ? loaded.voices : undefined;
  // The model must speak `lang` too: an English-only model can't use a voice verified for Hindi.
  // Cloned or generated voices have no verified languages: go by their label, else by the model.
  const modelLanguages = ttsLanguages(provider);
  const speaks = (v: ElevenLabsVoice) => {
    const labelled = baseLanguage(v.labels.language);
    const langs = v.verified_languages.length ? v.verified_languages.map((x) => x.language) : labelled ? [labelled] : modelLanguages;
    return langs.includes(lang);
  };
  const matching = voices?.filter((v) => modelLanguages.includes(lang) && speaks(v));
  const pasteVoiceId = (
    <>
      <div style={{ display: "flex", gap: 8 }}>
        <input
          className="form-input"
          style={{ flex: 1 }}
          value={manualId}
          onChange={(e) => setManualId(e.target.value)}
          placeholder="Paste the voice ID"
        />
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          disabled={!manualId.trim()}
          onClick={() => { onPick(manualId.trim(), manualId.trim()); setManualId(""); }}
        >
          Use
        </button>
      </div>
    </>
  );

  return (
    <div>
      {voices === undefined ? (
        <div className="form-hint" style={{ marginTop: 0 }}>Loading voices…</div>
      ) : voices === null ? (
        <>
          {pasteVoiceId}
          <div className="form-hint">Couldn&apos;t load the voice list, so paste a voice ID instead.</div>
        </>
      ) : matching!.length === 0 ? (
        <>
          <div className="form-hint" style={{ marginTop: 0, marginBottom: 6 }}>
            No {languageLabel(lang)} voices found on this account. Paste a voice ID instead.
          </div>
          {pasteVoiceId}
        </>
      ) : (
        <select
          className="form-select"
          value={current?.voice ?? ""}
          onChange={(e) => {
            const v = matching!.find((x) => x.voice_id === e.target.value);
            if (v) onPick(v.voice_id, v.name);
          }}
        >
          <option value="" disabled>{placeholder}</option>
          {/* A saved voice that is no longer in the list still shows as selected. */}
          {current?.voice && !matching!.some((v) => v.voice_id === current.voice) && (
            <option value={current.voice}>{current.name}</option>
          )}
          {matching!.map((v) => <option key={v.voice_id} value={v.voice_id}>{v.name}</option>)}
        </select>
      )}
    </div>
  );
}

export function AgentVoiceSettings({
  tenantId, providers, setProviders, localModels, defaultTtsId,
  languageMode, onLanguageMode, languages, onLanguages, ttsByLanguage, onTtsByLanguage, languagesError,
  sttId, llmId, ttsId, onAssign, onError, part,
}: {
  /** Omitted = everything; the agent editor splits voice and engines across two sections. */
  part?: "voice" | "engines";
  tenantId: string;
  providers: ProviderConfig[];
  setProviders: Dispatch<SetStateAction<ProviderConfig[]>>;
  /** Owned by the page, which also needs the statuses to warn on save and block testing. */
  localModels: LocalModels;
  /** The workspace's default voice config, which the default language uses when `ttsId` is unset. */
  defaultTtsId: string | null;
  languageMode: LanguageMode;
  onLanguageMode: (v: LanguageMode) => void;
  /** Base codes, primary first: exactly one when monolingual, up to MAX_LANGUAGES when multilingual. */
  languages: string[];
  onLanguages: (v: string[]) => void;
  /** Voice configs of the non-default languages; the default language's is `ttsId`. */
  ttsByLanguage: Record<string, string>;
  onTtsByLanguage: (v: Record<string, string>) => void;
  // The server's 400 detail for the language fields, shown inline.
  languagesError?: string | null;
  sttId: string | null | undefined;
  llmId: string | null | undefined;
  ttsId: string | null | undefined;
  onAssign: (role: Role, id: string | null) => void;
  onError: (message: string) => void;
}) {
  // null = follow the assigned voice: built-in unless the agent already uses another service.
  const [chosenMode, setChosenMode] = useState<"builtin" | "own" | null>(null);
  const [showConnect, setShowConnect] = useState(false);
  // The connected voice service whose voices are listed; null = the one the agent's voice already uses.
  const [accountId, setAccountId] = useState<string | null>(null);
  const [service, setService] = useState("elevenlabs");
  const [apiKey, setApiKey] = useState("");
  const [voiceId, setVoiceId] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);
  const [speedDraft, setSpeedDraft] = useState<number | null>(null);
  // The second language dropped by the last Multi→Mono switch, until that choice is undone.
  const [droppedLanguage, setDroppedLanguage] = useState<string | null>(null);
  const { models, supports, status, trigger } = localModels;

  // Voices are often created on the AI & Voice page mid-setup: re-read the list when this
  // panel opens and whenever the browser tab regains focus, so new ones show up here.
  useEffect(() => {
    let cancelled = false;
    const refresh = () => {
      listProviders(tenantId)
        .then((provs) => { if (!cancelled) setProviders(provs); })
        .catch(() => {});  // keep the list we have; the page's own load reports real failures
    };
    refresh();
    window.addEventListener("focus", refresh);
    return () => { cancelled = true; window.removeEventListener("focus", refresh); };
  }, [tenantId, setProviders]);

  const byRole = (role: Role) => providers.filter((p) => p.role === role);
  const selectedTts = providers.find((p) => p.id === ttsId) ?? null;
  const selectedLlm = providers.find((p) => p.id === llmId) ?? null;
  const selectedStt = providers.find((p) => p.id === sttId) ?? null;

  const multilingual = languageMode === "multilingual";
  const primaryLanguage = languages[0] ?? null;

  const voiceIdFor = (lang: string) => (lang === primaryLanguage ? ttsId : ttsByLanguage[lang]) ?? null;
  // With no voice chosen for the agent, calls use the workspace's default voice.
  const baseVoice = selectedTts ?? providers.find((p) => p.id === defaultTtsId) ?? null;
  const voiceFor = (lang: string) => providers.find((p) => p.id === voiceIdFor(lang));

  const changeMode = (mode: LanguageMode) => {
    if (mode === languageMode) return;
    onLanguageMode(mode);
    if (mode === "monolingual") {
      // Keep the primary language; the other one's voice goes on save.
      setDroppedLanguage(languages[1] ?? null);
      onLanguages(languages.slice(0, 1));
    } else {
      setDroppedLanguage(null);
    }
  };

  // The default language's voice lives in `ttsId`: when another language becomes the default,
  // it takes its own voice over, or none.
  const changeLanguages = (next: string[]) => {
    // A monolingual agent keeps its voice when it can speak the new language.
    const keepVoice = !multilingual && !!next[0] && voiceSpeaks(selectedTts, next[0], supports);
    if (next[0] !== primaryLanguage && !keepVoice) onAssign("tts", (next[0] && ttsByLanguage[next[0]]) || null);
    onLanguages(next);
  };

  const toggleLanguage = (code: string) => {
    changeLanguages(languages.includes(code) ? languages.filter((c) => c !== code) : [...languages, code]);
  };

  // A voice from the other source can't carry over, so switching source starts the choices afresh.
  const changeVoiceSource = (source: "builtin" | "own") => {
    if (source === voiceMode) return;
    setChosenMode(source);
    onAssign("tts", null);
    onTtsByLanguage({});
  };

  const setLanguageEntry = (
    map: Record<string, string>, onChange: (v: Record<string, string>) => void, lang: string, value: string,
  ) => {
    const next = { ...map };
    if (value) next[lang] = value;
    else delete next[lang];
    onChange(next);
  };

  // Free models are judged by the catalogue (an .en Whisper can't hear Hindi); Deepgram only code-switches on nova-2/3.
  const sttUnheard = selectedStt?.model ? languages.filter((l) => !supports(selectedStt.engine, selectedStt.model!, l)) : [];
  const sttLanguageWarning = !selectedStt
    ? null
    : sttUnheard.length > 0
      ? `${selectedStt.model} can't recognise ${sttUnheard.map(languageLabel).join(", ")}. Pick a speech model that supports ${languages.map(languageLabel).join(" and ")}.`
      : !multilingual || languages.length < 2
        ? null
      : selectedStt.engine === "deepgram" && selectedStt.model && !deepgramSupportsMulti(selectedStt.model, languages)
        ? (/^nova-[23]/i.test(selectedStt.model)
          // Already the right model: the gap is a language Deepgram's multi mode doesn't cover.
          ? `${selectedStt.model} can't recognise ${languages.filter((l) => !DEEPGRAM_MULTI_LANGUAGES.includes(l)).map(languageLabel).join(", ")} while switching languages. Remove it, or use a Whisper speech recognition provider.`
          : `${selectedStt.model} can't switch between ${languages.map(languageLabel).join(", ")} mid-call; it will listen in the default language only. Use nova-2 or nova-3, which cover en, hi, es, fr, de, pt, it and ja.`)
        : null;
  const connectedVoices = byRole("tts").filter((p) => PREMIUM_ENGINES.includes(p.engine));
  const voiceMode = chosenMode ?? (selectedTts && selectedTts.engine !== BUILTIN_TTS_ENGINE ? "own" : "builtin");
  const account = providers.find((p) => p.id === accountId) ?? (selectedTts && PREMIUM_ENGINES.includes(selectedTts.engine) ? selectedTts : null);
  const connectOpen = showConnect || connectedVoices.length === 0;

  const replaceProvider = (updated: ProviderConfig) =>
    setProviders((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));

  const patchExtra = async (p: ProviderConfig, patch: Record<string, unknown>) => {
    try {
      replaceProvider(await updateProvider(p.id, { extra: { ...(p.extra || {}), ...patch } }));
    } catch (e) {
      onError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  // Choosing a free model starts its download when the host doesn't have it yet.
  const prefetch = (p: ProviderConfig | undefined) => {
    const ref = p && localModelRef(p);
    const s = ref && status(ref.engine, ref.name);
    if (ref && (s === "missing" || s === "failed")) {
      trigger(ref.engine, ref.name).catch((e) => onError(e instanceof ApiError ? e.detail : String(e)));
    }
  };

  const assign = (role: Role, id: string | null) => {
    onAssign(role, id);
    prefetch(providers.find((p) => p.id === id));
  };

  const setVoiceFor = (lang: string, id: string) => {
    if (lang === primaryLanguage) onAssign("tts", id);
    else setLanguageEntry(ttsByLanguage, onTtsByLanguage, lang, id);
  };

  // Points one language's voice at a voice config, creating it when none matches yet.
  const assignLanguageVoice = async (lang: string, spec: ProviderConfigCreate) => {
    try {
      const existing = providers.find(
        (p) => p.role === "tts" && p.engine === spec.engine && p.voice === spec.voice && p.api_key_ref === (spec.api_key_ref ?? null),
      );
      const voice = existing ?? await createProvider(tenantId, spec);
      if (!existing) setProviders((prev) => [...prev, voice]);
      prefetch(voice);
      setVoiceFor(lang, voice.id);
    } catch (e) {
      onError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleConnect = async () => {
    const svc = VOICE_SERVICES.find((s) => s.engine === service)!;
    setConnecting(true);
    setConnectError(null);
    try {
      const created = await createProvider(tenantId, {
        name: svc.name, role: "tts", engine: svc.engine, api_key: apiKey.trim(),
        ...(svc.engine === "cartesia" ? { voice: voiceId.trim() } : {}),
      });
      setProviders((prev) => [...prev, created]);
      setAccountId(created.id);
      setApiKey("");
      setVoiceId("");
      setShowConnect(false);
    } catch (e) {
      setConnectError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setConnecting(false);
    }
  };

  // One speed for every voice the agent uses.
  const voiceConfigs = languages.flatMap((l) => voiceFor(l) ?? []);
  const savedSpeed = Number(voiceConfigs[0]?.extra?.speed ?? 1.0);
  const speed = speedDraft ?? savedSpeed;
  const commitSpeed = (v: number) => {
    setSpeedDraft(null);
    for (const p of voiceConfigs) if (v !== Number(p.extra?.speed ?? 1.0)) patchExtra(p, { speed: v });
  };

  // Must match _is_thinking_capable() in services/conversation/ai_provider_manager.py.
  const thinkingCapable = selectedLlm?.engine === "ollama" && !!selectedLlm.model?.startsWith("gemma4");

  const providerSelect = (role: "stt" | "llm", value: string | null | undefined, label: string, hint: string) => {
    // Speech models that can't hear every chosen language are left out (the current pick stays visible).
    const options = byRole(role).filter((p) =>
      role !== "stt" || !p.model || p.id === value || languages.every((l) => supports(p.engine, p.model!, l)));
    const noFreeStt = role === "stt" && models.length > 0
      && !models.some((m) => m.role === "stt" && languages.every((l) => supports(m.engine, m.name, l)));
    const pick = providers.find((p) => p.id === value);
    const ref = pick && localModelRef(pick);
    const pickStatus = ref && status(ref.engine, ref.name);
    return (
      <div className="form-group">
        <label className="form-label">{label}</label>
        <div className="form-hint" style={{ marginTop: 0, marginBottom: 6 }}>{hint}</div>
        {options.length === 0 ? (
          <div className="voice-missing">
            <AlertCircle size={13} />
            <span>None set up yet. <Link href="/ai-voice">Add one in AI &amp; Voice</Link>.</span>
          </div>
        ) : (
          <>
            <select className="form-select" value={value || ""} onChange={(e) => assign(role, e.target.value || null)}>
              <option value="">Choose…</option>
              {options.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}{p.environment !== "prod" ? ` (${p.environment})` : ""}
                </option>
              ))}
            </select>
            {!value && (
              <div className="voice-missing">
                <AlertCircle size={13} />
                <span>Required — the agent can&apos;t take calls without it.</span>
              </div>
            )}
            {pickStatus === "downloading" && <div className="form-hint">Downloading the model…</div>}
            {pickStatus === "failed" && (
              <div className="voice-missing">
                <AlertCircle size={13} />
                <span>The model download failed. Choose another model, or try again later.</span>
              </div>
            )}
          </>
        )}
        {noFreeStt && (
          <div className="voice-missing">
            <AlertCircle size={13} />
            <span>
              No free model supports {languages.map(languageLabel).join(" and ")}.{" "}
              <Link href="/ai-voice">Use a premium speech service in AI &amp; Voice</Link>.
            </span>
          </div>
        )}
      </div>
    );
  };

  // The voice dropdown for one selected language: only voices of the chosen source that speak it.
  const languageVoice = (lang: string) => {
    const current = voiceFor(lang);
    const label = languageLabel(lang);
    const workspaceVoice = lang === primaryLanguage && !current ? providers.find((p) => p.id === defaultTtsId) : undefined;
    // A secondary language the default voice can already speak needs no voice of its own.
    const sharedVoice = lang !== primaryLanguage && !current && voiceSpeaks(baseVoice, lang, supports) ? baseVoice : null;
    const placeholder = workspaceVoice ? `Workspace default voice (${workspaceVoice.name})`
      : sharedVoice ? `Uses ${sharedVoice.name} (speaks ${label})`
      : `Choose a ${label} voice…`;
    let picker;
    if (voiceMode === "own") {
      picker = account ? (
        <PremiumLanguageVoice
          provider={account}
          lang={lang}
          current={current}
          placeholder={placeholder}
          onPick={(voice, voiceName) => assignLanguageVoice(lang, {
            name: `${SERVICE_NAMES[account.engine]} — ${voiceName}`, role: "tts", engine: account.engine,
            environment: account.environment, model: account.model ?? undefined, voice,
            api_key_ref: account.api_key_ref ?? undefined, extra: account.extra ?? undefined,
          })}
        />
      ) : (
        <div className="form-hint" style={{ marginTop: 0 }}>Connect or pick a voice service above to list its voices.</div>
      );
    } else {
      const freeVoices = models.filter((m) =>
        m.role === "tts" && m.engine === BUILTIN_TTS_ENGINE && m.status !== "unavailable" && supports(m.engine, m.name, lang));
      const configFor = (name: string) => providers.find((p) => p.role === "tts" && p.engine === BUILTIN_TTS_ENGINE && p.voice === name);
      picker = (
        <select
          className="form-select"
          value={current?.id ?? ""}
          onChange={(e) => {
            const picked = e.target.value;
            if (!picked.startsWith("free:")) {
              prefetch(providers.find((p) => p.id === picked));
              return setVoiceFor(lang, picked);
            }
            const name = picked.slice(5);
            void assignLanguageVoice(lang, { name: `${BUILTIN_TTS_ENGINE} — ${name}`, role: "tts", engine: BUILTIN_TTS_ENGINE, voice: name });
          }}
        >
          <option value="" disabled>{placeholder}</option>
          {/* A saved voice outside the list still shows as selected. */}
          {current && !freeVoices.some((m) => configFor(m.name)?.id === current.id) && (
            <option value={current.id}>{current.name}</option>
          )}
          {freeVoices.map((m) => (
            <option key={m.name} value={configFor(m.name)?.id ?? `free:${m.name}`}>{m.label}</option>
          ))}
        </select>
      );
    }
    return (
      <div className="form-group">
        <label className="form-label">
          {label} voice{lang === primaryLanguage && multilingual && <span className="hint">main</span>}
        </label>
        {picker}
        {!current && !workspaceVoice && !sharedVoice && (
          <div className="voice-missing">
            <AlertCircle size={13} />
            <span>
              {lang === primaryLanguage
                ? `Voice needed — ${label} has no voice and the workspace has no default voice.`
                : `Voice needed — the agent can't be saved until ${label} has a voice.`}
            </span>
          </div>
        )}
      </div>
    );
  };

  return (
    <>
      {part !== "engines" && <>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-hdr">
          <div className="card-title">Language</div>
        </div>
        <div className="card-body">
          <div className="voice-engine-row" role="radiogroup" aria-label="Language mode" style={{ marginBottom: 14 }}>
            {LANGUAGE_MODES.map((m) => (
              <button
                key={m.value}
                type="button"
                role="radio"
                aria-checked={languageMode === m.value}
                className={`voice-engine-card${languageMode === m.value ? " on" : ""}`}
                onClick={() => changeMode(m.value)}
              >
                <div className="agent-template-title">{m.label}</div>
                <div className="agent-template-blurb">{m.blurb}</div>
              </button>
            ))}
          </div>

          {multilingual ? (
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label">Languages <span className="hint">pick {MAX_LANGUAGES}</span></label>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }} role="group" aria-label="Languages">
                {SUPPORTED_LANGUAGES.map((l) => {
                  const on = languages.includes(l.value);
                  const full = !on && languages.length >= MAX_LANGUAGES;
                  return (
                    <button
                      key={l.value}
                      type="button"
                      aria-pressed={on}
                      disabled={full}
                      className={`btn btn-sm ${on ? "btn-primary" : "btn-ghost"}`}
                      onClick={() => toggleLanguage(l.value)}
                      title={full ? `maximum ${MAX_LANGUAGES} languages` : undefined}
                    >
                      {languageLabel(l.value)}{on && l.value === primaryLanguage ? " · main" : ""}
                    </button>
                  );
                })}
              </div>
              <div className="form-hint">
                The agent greets in the main language, then follows whichever of the two the caller uses.
                {languages.length < MAX_LANGUAGES && ` Pick ${MAX_LANGUAGES - languages.length} more.`}
                {languages.length >= MAX_LANGUAGES && ` Maximum ${MAX_LANGUAGES} languages: remove one to pick another.`}
              </div>
            </div>
          ) : (
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label">What language will callers speak?</label>
              <select className="form-select" value={primaryLanguage ?? ""} onChange={(e) => changeLanguages([e.target.value])}>
                {SUPPORTED_LANGUAGES.map((l) => (
                  <option key={l.value} value={l.value}>{languageLabel(l.value)}</option>
                ))}
              </select>
              <div className="form-hint">Callers are heard and answered in this language.</div>
              {droppedLanguage && (
                <div className="voice-missing">
                  <AlertCircle size={13} />
                  <span>
                    {languageLabel(droppedLanguage)} will be removed when you save, along with its voice.
                  </span>
                </div>
              )}
            </div>
          )}

          <div style={{ marginTop: 14 }}>
            {languages.map((lang) => <div key={lang}>{languageVoice(lang)}</div>)}
          </div>

          {languagesError && (
            <div className="voice-missing">
              <AlertCircle size={13} />
              <span>{friendlyLanguageError(languagesError)}</span>
            </div>
          )}
        </div>
      </div>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-hdr">
          <div className="card-title">How it sounds</div>
          <div className="card-sub">the voice callers hear on the phone</div>
        </div>
        <div className="card-body">
          <div className="voice-engine-row" role="radiogroup" aria-label="Where the voice comes from">
            <button
              type="button"
              role="radio"
              aria-checked={voiceMode === "builtin"}
              className={`voice-engine-card${voiceMode === "builtin" ? " on" : ""}`}
              onClick={() => changeVoiceSource("builtin")}
            >
              <div className="agent-template-title">Built-in voice <span className="voice-default-tag">Default</span></div>
              <div className="agent-template-blurb">Included with your plan. Works right away, no setup needed.</div>
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={voiceMode === "own"}
              className={`voice-engine-card${voiceMode === "own" ? " on" : ""}`}
              onClick={() => changeVoiceSource("own")}
            >
              <div className="agent-template-title">Your own voice service</div>
              <div className="agent-template-blurb">Use your ElevenLabs, Cartesia or other account.</div>
            </button>
          </div>

          {voiceMode === "own" && (
            <>
              {connectedVoices.length > 0 && (
                <div className="form-group">
                  <label className="form-label">Which service should the voices come from?</label>
                  <div className="voice-list">
                    {connectedVoices.map((p) => (
                      <button
                        key={p.id}
                        type="button"
                        className={`voice-list-row${p.id === account?.id ? " on" : ""}`}
                        onClick={() => setAccountId(p.id)}
                      >
                        <span className="voice-list-check">{p.id === account?.id && <Check size={13} />}</span>
                        <span className="voice-list-name">{p.name}</span>
                        <span className="voice-list-meta">{SERVICE_NAMES[p.engine] ?? p.engine}</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {!connectOpen ? (
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowConnect(true)}>
                  + Connect another service
                </button>
              ) : (
              <>
              <div className="form-group">
                <label className="form-label">Which service do you use?</label>
                <div className="voice-service-row" role="radiogroup" aria-label="Voice service">
                  {VOICE_SERVICES.map((s) => (
                    <button
                      key={s.engine}
                      type="button"
                      role="radio"
                      aria-checked={service === s.engine}
                      disabled={!s.ready}
                      className={`voice-service${service === s.engine ? " on" : ""}`}
                      onClick={() => setService(s.engine)}
                    >
                      <b>{s.name}</b>
                      <span>{s.ready ? s.blurb : "Coming soon"}</span>
                    </button>
                  ))}
                </div>
              </div>
              {connectError && <div className="error-banner">{connectError}</div>}
              <div className="form-group">
                <label className="form-label">Your {VOICE_SERVICES.find((s) => s.engine === service)?.name} API key</label>
                <input
                  className="form-input"
                  type="password"
                  autoComplete="off"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="Paste your API key"
                />
                <div className="form-hint">
                  Find it in your {service === "cartesia" ? "Cartesia" : "ElevenLabs"} account settings. We store it encrypted.
                </div>
              </div>
              {service === "cartesia" && (
                <div className="form-group">
                  <label className="form-label">Voice ID</label>
                  <input
                    className="form-input"
                    value={voiceId}
                    onChange={(e) => setVoiceId(e.target.value)}
                    placeholder="Paste the voice ID"
                  />
                  <div className="form-hint">Open the voice you like in Cartesia and copy its ID.</div>
                </div>
              )}
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={handleConnect}
                disabled={connecting || !apiKey.trim() || (service === "cartesia" && !voiceId.trim())}
              >
                {connecting ? "Connecting…" : "Connect and use this service"}
              </button>
              {connectedVoices.length > 0 && (
                <button type="button" className="btn btn-ghost btn-sm" style={{ marginLeft: 6 }} onClick={() => setShowConnect(false)}>
                  Cancel
                </button>
              )}
              </>
              )}
            </>
          )}

          <div className="form-group" style={{ marginTop: 16, marginBottom: 0 }}>
            <label className="form-label">Speaking speed</label>
            <div className="voice-speed">
              <span>Slower</span>
              <input
                type="range"
                min={0.7}
                max={1.2}
                step={0.05}
                value={speed}
                disabled={voiceConfigs.length === 0}
                aria-label="Speaking speed"
                onChange={(e) => setSpeedDraft(Number(e.target.value))}
                onMouseUp={(e) => commitSpeed(Number(e.currentTarget.value))}
                onTouchEnd={(e) => commitSpeed(Number(e.currentTarget.value))}
                onKeyUp={(e) => commitSpeed(Number(e.currentTarget.value))}
              />
              <span>Faster</span>
              <span className="voice-speed-value">{speed === 1 ? "Normal" : `${speed.toFixed(2)}×`}</span>
            </div>
            <div className="form-hint">
              {voiceConfigs.length > 0
                ? "Saved right away. Every agent using these voices shares the same speed."
                : "Pick a voice first."}
            </div>
          </div>
        </div>
      </div>

      </>}

      {part !== "voice" && <div className="card">
        <div className="card-hdr">
          <div className="card-title">Hearing &amp; thinking</div>
          <div className="card-sub">both are needed to take calls</div>
        </div>
        <div className="card-body">
          <div className="form-row" style={{ flexWrap: "wrap" }}>
            {providerSelect("stt", sttId, "How it hears callers", "Turns speech into words the agent can understand.")}
            {providerSelect("llm", llmId, "How it thinks", "Chooses what to say next.")}
          </div>
          {sttLanguageWarning && (
            <div className="voice-missing" style={{ marginTop: 0, marginBottom: 14 }}>
              <AlertCircle size={13} />
              <span>{sttLanguageWarning}</span>
            </div>
          )}
          {thinkingCapable && selectedLlm && (
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                Think before answering
                <span className="hint">smarter replies, but adds 5–8 seconds per turn</span>
              </label>
              <label className="toggle-switch">
                <input
                  type="checkbox"
                  checked={Boolean(selectedLlm.extra?.think ?? false)}
                  onChange={(e) => patchExtra(selectedLlm, { think: e.target.checked })}
                />
                <span className="toggle-slider" />
              </label>
            </div>
          )}
        </div>
      </div>}
    </>
  );
}
