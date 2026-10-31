"use client";

// Languages, voice, hearing and thinking for one agent, from the models the platform provides.
// Shared by the creation wizard and the agent page so both read the same.

import { Dispatch, SetStateAction, useEffect, useState } from "react";
import { AlertCircle, Check, RotateCcw } from "lucide-react";
import {
  AgentUpdate, AiCatalog, AiOption, ApiError, ElevenLabsVoice, LanguageMode, ProviderConfig, chooseAiOption, createProvider,
  listAiOptionVoices, previewAiVoice, listProviders, managedOptionId, updateProvider,
} from "@/lib/api";
import {
  LOCAL_ENGINES, SARVAM_V3_VOICES, SUPPORTED_LANGUAGES, VOICES_BY_ENGINE, BUILTIN_TTS_ENGINE, BUILTIN_TTS_VOICE, baseLanguage,
  deepgramSupportsMulti, languageLabel, voiceSpeaks,
} from "@/lib/engineCatalog";
import { ModelRef, useLocalModels } from "@/lib/useLocalModels";
import { useAiCatalog } from "@/lib/useAiCatalog";
import { VoicePickerWithPreview } from "./VoicePickerWithPreview";

type Role = "stt" | "llm" | "tts";

export type LocalModels = ReturnType<typeof useLocalModels>;

// The catalogue model a provider config points at, or null when the Config Service does not manage it:
// cloud engines, and an Ollama with its own base_url (Config never pulls into a URL a tenant supplies).
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
  if (lang) return `${lang} needs a voice that can speak it. Pick one in the Voice card.`;
  if (/can't be spoken|TTS provider|voice override|multilingual TTS/i.test(detail)) {
    return "One of these languages needs a different voice. Pick one in the Voice card.";
  }
  return detail;
}

const OWN = "__own__";
const article = (word: string) => (/^[AEIOU]/i.test(word) ? "an" : "a");
// Saaras v3 output modes; the server accepts these values only.
const SARVAM_STT_MODES = [
  { value: "transcribe", label: "Transcribe", hint: "Words as spoken, in each language's own script." },
  { value: "codemix", label: "Code-mix", hint: "Hindi in Devanagari, English words kept in English." },
  { value: "translit", label: "Transliterate", hint: "Everything in Roman letters, e.g. \"aap kaise hain\"." },
  { value: "translate", label: "Translate to English", hint: "The agent reads every caller turn in English." },
  { value: "verbatim", label: "Verbatim", hint: "Keeps fillers like \"umm\" and repeated words." },
];
// Voice engines that run on this server, as the In-house voice models.
const IN_HOUSE_TTS: Record<string, string> = { kokoro: "Kokoro", macos: "macOS voices" };
const MACOS_ACCENTS: Record<string, string> = { Samantha: "American", Alex: "American", Karen: "Australian", Moira: "Irish", Daniel: "British" };
const sampleOf = (engine: string, voice: string) => VOICES_BY_ENGINE[engine]?.find((v) => v.id === voice)?.sampleUrl;

function shapeVoice(id: string, name: string, labels: Record<string, string> = {}): ElevenLabsVoice {
  return { voice_id: id, name, category: null, labels, preview_url: null, verified_languages: [] };
}
// Kokoro voice ids start with their accent: af_sarah = American female.
const KOKORO_ACCENTS: Record<string, string> = {
  a: "American", b: "British", e: "Spanish", f: "French", h: "Hindi", i: "Italian", j: "Japanese", p: "Portuguese", z: "Chinese",
};
const kokoroSample = (voice: string) => VOICES_BY_ENGINE.kokoro?.find((v) => v.id === voice)?.sampleUrl;
const DEFAULT_TEMPERATURE = 0.7;
const money = (n: number) => `$${n < 0.01 ? n.toFixed(4) : n.toFixed(3)}`;

// Mirrors recommended("stt") in services/config/platform_ai.py.
function recommendedHearing(options: AiOption[], languages: string[]): AiOption | null {
  return options.find((o) => o.role === "stt" && o.available && hears(o, languages)) ?? null;
}

function hears(o: AiOption, languages: string[]): boolean {
  if (!languages.every((l) => o.languages.includes(l))) return false;
  return o.engine !== "deepgram" || languages.length < 2 || deepgramSupportsMulti(o.model ?? "", languages);
}

// The agent languages a model can't handle; language models aren't language-bound here.
function missingLanguages(o: AiOption, languages: string[]): string[] {
  if (o.role === "llm") return [];
  const missing = languages.filter((l) => !o.languages.includes(l));
  if (!missing.length && o.role === "stt" && !hears(o, languages)) return languages.slice(1);
  return missing;
}

const shortLabel = (code: string) => SUPPORTED_LANGUAGES.find((l) => l.value === code)?.label ?? code;
const noLanguages = (missing: string[]) => `No ${missing.map(shortLabel).join(", ")}`;

function unavailableHint(o: AiOption | undefined): string {
  if (!o) return "";
  return o.key_env ? `Add ${o.key_env} to the platform .env to turn this on.` : "Not set up on this platform yet.";
}

// Provider buttons for one role, grouped from the catalogue; the workspace's own providers come last.
function ProviderChips({ options, current, own, label, onPick, showOwn = false, languages = [] }: {
  options: AiOption[];
  /** The agent's languages: a provider with no model covering them is greyed out. */
  languages?: string[];
  current: string | null;
  own: ProviderConfig[];
  /** Offer In-house even before the workspace has a local model of its own. */
  showOwn?: boolean;
  label: string;
  onPick: (provider: string) => void;
}) {
  const providers = [...new Set(options.map((o) => o.provider))];
  return (
    <div className="ai-chips" role="radiogroup" aria-label={label}>
      {providers.map((name) => {
        const group = options.filter((o) => o.provider === name);
        const ready = group.some((o) => o.available);
        const fit = group.filter((o) => o.available && missingLanguages(o, languages).length === 0);
        const missing = ready && !fit.length ? missingLanguages(group.find((o) => o.available)!, languages) : [];
        const local = group.every((o) => o.local);
        return (
          <button
            key={name}
            type="button"
            role="radio"
            aria-checked={current === name}
            disabled={!ready || missing.length > 0}
            title={!ready ? unavailableHint(group[0]) : missing.length ? `${name} doesn't support ${missing.map(shortLabel).join(" or ")}` : undefined}
            className={`ai-chip${current === name ? " on" : ""}`}
            onClick={() => onPick(name)}
          >
            {current === name && <Check size={12} />} {name}
            {local && <span className="ai-chip-tag">Local</span>}
            {!ready && <span className="ai-chip-tag">Not set up</span>}
            {missing.length > 0 && <span className="ai-chip-tag">{noLanguages(missing)}</span>}
          </button>
        );
      })}
      {(own.length > 0 || showOwn) && (
        <button
          type="button"
          role="radio"
          aria-checked={current === OWN}
          className={`ai-chip${current === OWN ? " on" : ""}`}
          onClick={() => onPick(OWN)}
          title="A provider set up in this workspace with its own key"
        >
          {current === OWN && <Check size={12} />} In-house
        </button>
      )}
    </div>
  );
}

function LanguageCoverage({ verb, option }: { verb: string; option: AiOption }) {
  if (!option.languages.length) return null;
  return <div className="form-hint">{verb} {option.languages.map(shortLabel).join(", ")}.</div>;
}

function ModelSelect({ options, value, languages, onPick, label }: {
  options: AiOption[];
  value: string | null;
  languages?: string[];
  label: string;
  onPick: (option: AiOption) => void;
}) {
  return (
    <select
      className="form-select"
      aria-label={label}
      value={value ?? ""}
      onChange={(e) => {
        const o = options.find((x) => x.id === e.target.value);
        if (o) onPick(o);
      }}
    >
      {!value && <option value="" disabled>Choose a model…</option>}
      {options.map((o) => {
        const missing = languages ? missingLanguages(o, languages) : [];
        return (
          <option key={o.id} value={o.id} disabled={!o.available || missing.length > 0}>
            {o.label} · {money(o.price_per_min)}/min{!o.available ? " (not set up)" : missing.length ? ` (${noLanguages(missing).toLowerCase()})` : ""}
          </option>
        );
      })}
    </select>
  );
}

function OwnSelect({ own, value, onPick, label }: {
  own: ProviderConfig[]; value: string | null | undefined; label: string; onPick: (id: string) => void;
}) {
  return (
    <select className="form-select" aria-label={label} value={value ?? ""} onChange={(e) => e.target.value && onPick(e.target.value)}>
      <option value="" disabled>Choose an in-house provider…</option>
      {own.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
    </select>
  );
}

export function AgentCostBar({ catalog, stt, llm, tts }: {
  catalog: AiCatalog | null;
  stt: ProviderConfig | null;
  llm: ProviderConfig | null;
  tts: ProviderConfig | null;
}) {
  if (!catalog) return null;
  const price = (p: ProviderConfig | null) => {
    const o = catalog.options.find((x) => x.id === managedOptionId(p));
    return o ? o.price_per_min : null;
  };
  const parts = [
    { key: "stt", label: "Hearing", value: price(stt), set: !!stt },
    { key: "llm", label: "Thinking", value: price(llm), set: !!llm },
    { key: "tts", label: "Voice", value: price(tts), set: !!tts },
    { key: "fee", label: "Platform", value: catalog.platform_fee_per_min, set: true },
  ];
  const total = parts.reduce((sum, p) => sum + (p.value ?? 0), 0);
  const own = parts.some((p) => p.set && p.value === null);
  return (
    <div className="ai-cost" aria-label="Estimated cost per minute">
      <div className="ai-cost-head">
        <span>Estimated cost per minute</span>
        <strong>{money(total)}{own ? " +" : ""}</strong>
      </div>
      <div className="ai-cost-bar" aria-hidden="true">
        {parts.map((p) => p.value ? (
          <span key={p.key} className={`ai-cost-seg ${p.key}`} style={{ flexGrow: p.value }} />
        ) : null)}
      </div>
      <div className="ai-cost-legend">
        {parts.map((p) => (
          <span key={p.key}>
            <i className={`ai-cost-dot ${p.key}`} />
            {p.label} {!p.set ? "not chosen" : p.value === null ? "In-house" : money(p.value)}
          </span>
        ))}
      </div>
      <div className="ai-cost-note">An estimate for planning; billing isn&apos;t live yet.</div>
    </div>
  );
}

export function AgentVoiceSettings({
  tenantId, providers, setProviders, localModels, defaultTtsId,
  languageMode, onLanguageMode, languages, onLanguages, ttsByLanguage, onTtsByLanguage, languagesError,
  sttId, llmId, ttsId, onAssign, onError, part, catalog: catalogProp, showCost = true,
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
  /** Base codes, primary first: one when monolingual, two when switching languages. */
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
  /** Given by a page that loads the catalogue itself (e.g. to place the cost bar); omitted = loaded here. */
  catalog?: AiCatalog | null;
  showCost?: boolean;
}) {
  const [busy, setBusy] = useState<Role | null>(null);
  const [tempDraft, setTempDraft] = useState<number | null>(null);
  const [speedDraft, setSpeedDraft] = useState<number | null>(null);
  // Chips chosen before a model/voice is: the workspace's own list, or a voice service still being picked.
  const [llmChip, setLlmChip] = useState<string | null>(null);
  const [sttChip, setSttChip] = useState<string | null>(null);
  const [voiceOptionId, setVoiceOptionId] = useState<string | null>(null);
  const [voiceOwn, setVoiceOwn] = useState(false);
  const [ownTtsEngine, setOwnTtsEngine] = useState<string | null>(null);
  const [hearingCustom, setHearingCustom] = useState<boolean | null>(null);
  const [otherVoiceFor, setOtherVoiceFor] = useState<string | null>(null);
  const [vendorVoices, setVendorVoices] = useState<Record<string, ElevenLabsVoice[] | null>>({});
  const { models, supports, status, trigger } = localModels;

  const fetched = useAiCatalog(catalogProp === undefined ? tenantId : null);
  const catalog = catalogProp === undefined ? fetched.catalog : catalogProp;
  const catalogError = catalogProp === undefined ? fetched.error : null;

  // Providers may be added on the AI page mid-setup: re-read on open and when the tab regains focus.
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

  const options = catalog?.options ?? [];
  const ofRole = (role: Role) => options.filter((o) => o.role === role);
  // In-house = models on the workspace's own server; its cloud keys stay usable only where already assigned.
  const assigned = new Set([sttId, llmId, ttsId, ...Object.values(ttsByLanguage)].filter(Boolean));
  const own = (role: Role) => providers.filter((p) => p.role === role && !managedOptionId(p)
    && (LOCAL_ENGINES.has(p.engine) || assigned.has(p.id)));
  const find = (id: string | null | undefined) => providers.find((p) => p.id === id) ?? null;
  const optionOf = (p: ProviderConfig | null) => options.find((o) => o.id === managedOptionId(p)) ?? null;

  const selectedStt = find(sttId);
  const selectedLlm = find(llmId);
  const selectedTts = find(ttsId);
  const multilingual = languageMode === "multilingual";
  const primaryLanguage = languages[0] ?? null;
  const secondaryLanguage = multilingual ? languages[1] ?? null : null;

  const upsert = (row: ProviderConfig) =>
    setProviders((prev) => (prev.some((p) => p.id === row.id) ? prev.map((p) => (p.id === row.id ? row : p)) : [...prev, row]));

  // Choosing a free model starts its download when the host doesn't have it yet.
  const prefetch = (p: ProviderConfig | null | undefined) => {
    const ref = p && localModelRef(p);
    const s = ref && status(ref.engine, ref.name);
    if (ref && (s === "missing" || s === "failed")) {
      trigger(ref.engine, ref.name).catch((e) => onError(e instanceof ApiError ? e.detail : String(e)));
    }
  };

  const choose = async (role: Role, option: AiOption, extra: { voice?: string; voice_name?: string; temperature?: number } = {}) => {
    setBusy(role);
    try {
      const row = await chooseAiOption(tenantId, { option_id: option.id, ...extra });
      upsert(row);
      prefetch(row);
      return row;
    } catch (e) {
      onError(e instanceof ApiError ? e.detail : String(e));
      return null;
    } finally {
      setBusy(null);
    }
  };

  const patchExtra = async (p: ProviderConfig, patch: Record<string, unknown>) => {
    try {
      upsert(await updateProvider(p.id, { extra: { ...(p.extra || {}), ...patch } }));
    } catch (e) {
      onError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  // ── Hearing ────────────────────────────────────────────────────────────────
  const recommended = recommendedHearing(options, languages);
  const sttOption = optionOf(selectedStt);
  const custom = hearingCustom ?? (!!selectedStt && (!recommended || sttOption?.id !== recommended.id));

  const applyRecommendedHearing = async (langs: string[]) => {
    const rec = recommendedHearing(options, langs);
    if (!rec) return;
    const row = await choose("stt", rec);
    if (row) onAssign("stt", row.id);
  };

  // Languages change in one place, so the platform's hearing pick can follow them.
  const applyLanguages = (next: string[]) => {
    const keepVoice = !!next[0] && voiceSpeaks(selectedTts, next[0], supports);
    if (next[0] !== primaryLanguage && !keepVoice) onAssign("tts", (next[0] && ttsByLanguage[next[0]]) || null);
    onLanguages(next);
    const nextRec = recommendedHearing(options, next);
    if (!custom && nextRec && nextRec.id !== sttOption?.id) void applyRecommendedHearing(next);
  };

  const setSwitching = (on: boolean) => {
    if (on === multilingual) return;
    onLanguageMode(on ? "multilingual" : "monolingual");
    if (on) {
      const second = SUPPORTED_LANGUAGES.find((l) => l.value !== primaryLanguage && (primaryLanguage === "en" ? l.value === "hi" : l.value === "en"))
        ?? SUPPORTED_LANGUAGES.find((l) => l.value !== primaryLanguage);
      applyLanguages([primaryLanguage ?? "en", second!.value]);
    } else {
      applyLanguages(languages.slice(0, 1));
    }
  };

  const sttUnheard = selectedStt?.model && !sttOption ? languages.filter((l) => !supports(selectedStt.engine, selectedStt.model!, l)) : [];
  const sttWarning = selectedStt && !sttOption && sttUnheard.length > 0
    ? `${selectedStt.name} can't recognise ${sttUnheard.map(languageLabel).join(", ")}.`
    : sttOption && !hears(sttOption, languages)
      ? `${sttOption.provider} ${sttOption.label} can't hear ${languages.map(languageLabel).join(" and ")}${languages.length > 1 ? " in one call" : ""}.`
      : null;

  // ── Thinking ───────────────────────────────────────────────────────────────
  const llmOption = optionOf(selectedLlm);
  const llmProvider = llmChip ?? llmOption?.provider ?? (selectedLlm ? OWN : null);
  const savedTemperature = Number(selectedLlm?.extra?.temperature ?? DEFAULT_TEMPERATURE);
  const temperature = tempDraft ?? savedTemperature;
  const temperatureArg = (t: number) => (t === DEFAULT_TEMPERATURE ? {} : { temperature: t });

  const pickLlm = async (option: AiOption, t = savedTemperature) => {
    const row = await choose("llm", option, temperatureArg(t));
    if (row) { onAssign("llm", row.id); setLlmChip(null); }
  };

  const commitTemperature = async (t: number) => {
    setTempDraft(null);
    if (!selectedLlm || t === savedTemperature) return;
    if (llmOption) await pickLlm(llmOption, t);
    else await patchExtra(selectedLlm, { temperature: t });
  };

  // Must match _is_thinking_capable() in services/conversation/ai_provider_manager.py.
  const thinkingCapable = selectedLlm?.engine === "ollama" && !!selectedLlm.model?.startsWith("gemma4");

  // ── Voice ──────────────────────────────────────────────────────────────────
  const baseVoice = selectedTts ?? find(defaultTtsId);
  const voiceOption = voiceOwn ? null : options.find((o) => o.id === voiceOptionId) ?? optionOf(baseVoice);
  const voiceProvider = voiceOwn ? OWN : voiceOption?.provider ?? (baseVoice ? OWN : null);
  const voiceIdFor = (lang: string) => (lang === primaryLanguage ? ttsId : ttsByLanguage[lang]) ?? null;
  const voiceFor = (lang: string) => find(voiceIdFor(lang));

  const vendorKey = voiceOption && (voiceOption.engine === "elevenlabs" || voiceOption.engine === "cartesia") ? voiceOption.id : null;
  useEffect(() => {
    if (!vendorKey || vendorKey in vendorVoices) return;
    let cancelled = false;
    listAiOptionVoices(tenantId, vendorKey)
      .then((v) => { if (!cancelled) setVendorVoices((m) => ({ ...m, [vendorKey]: v })); })
      .catch(() => { if (!cancelled) setVendorVoices((m) => ({ ...m, [vendorKey]: null })); });
    return () => { cancelled = true; };
  }, [tenantId, vendorKey, vendorVoices]);

  // Voices of an option that speak `lang`, in the shape the picker takes.
  const voicesOf = (o: AiOption, lang: string): ElevenLabsVoice[] | undefined | null => {
    if (o.engine === "elevenlabs" || o.engine === "cartesia") {
      const list = vendorVoices[o.id];
      if (!list) return list;
      if (!o.languages.includes(lang)) return [];
      return list.filter((v) => {
        const labelled = baseLanguage(v.labels.language);
        const langs = v.verified_languages.length ? v.verified_languages.map((x) => x.language) : labelled ? [labelled] : o.languages;
        return langs.includes(lang);
      });
    }
    if (o.engine === "kokoro") {
      return models.filter((m) => m.role === "tts" && m.engine === "kokoro" && m.status !== "unavailable" && supports(m.engine, m.name, lang))
        .map((m) => shapeVoice(m.name, m.label.replace(/\s*\(.*\)$/, ""), {
          gender: m.name[1] === "f" ? "female" : m.name[1] === "m" ? "male" : "",
          accent: KOKORO_ACCENTS[m.name[0]] ?? "",
        }));
    }
    if (!o.languages.includes(lang)) return [];
    const list = o.engine === "sarvam" && o.model === "bulbul:v3" ? SARVAM_V3_VOICES : VOICES_BY_ENGINE[o.engine] ?? [];
    return list.map((v) => shapeVoice(v.id, v.label, {
      gender: v.gender, accent: o.engine === "sarvam" ? "Indian" : o.engine === "deepgram" ? "American" : "",
    }));
  };

  const pickVoiceOption = async (option: AiOption) => {
    setVoiceOwn(false);
    setVoiceOptionId(option.id);
    setOtherVoiceFor(null);
    onTtsByLanguage({});
    const voice = option.engine === "kokoro"
      ? voicesOf(option, primaryLanguage ?? "en")?.[0]?.voice_id
      : option.default_voice ?? undefined;
    if (!voice) { onAssign("tts", null); return; }
    const row = await choose("tts", option, { voice });
    if (row) onAssign("tts", row.id);
  };

  const pickVoice = async (lang: string, voice: string, name: string) => {
    if (!voiceOption) return;
    const row = await choose("tts", voiceOption, { voice, voice_name: name });
    if (!row) return;
    if (lang === primaryLanguage) onAssign("tts", row.id);
    else onTtsByLanguage({ ...ttsByLanguage, [lang]: row.id });
    setOtherVoiceFor(null);
  };

  const voiceConfigs = languages.flatMap((l) => voiceFor(l) ?? []);
  const savedSpeed = Number(voiceConfigs[0]?.extra?.speed ?? 1.0);
  const speed = speedDraft ?? savedSpeed;
  const commitSpeed = (v: number) => {
    setSpeedDraft(null);
    for (const p of voiceConfigs) if (v !== Number(p.extra?.speed ?? 1.0)) patchExtra(p, { speed: v });
  };

  const inHouseEngines = Object.keys(IN_HOUSE_TTS).filter((e) => models.some((m) => m.role === "tts" && m.engine === e && m.status !== "unavailable"));
  const ownEngine = ownTtsEngine
    ?? (baseVoice && IN_HOUSE_TTS[baseVoice.engine] ? baseVoice.engine : inHouseEngines[0] ?? null);

  // Local voices of an engine that speak every given language.
  const inHouseVoices = (engine: string, langs: string[]): ElevenLabsVoice[] =>
    models.filter((m) => m.role === "tts" && m.engine === engine && m.status !== "unavailable" && langs.every((l) => supports(engine, m.name, l)))
      .map((m) => engine === "kokoro"
        ? shapeVoice(m.name, m.label.replace(/\s*\(.*\)$/, ""), {
          gender: m.name[1] === "f" ? "female" : m.name[1] === "m" ? "male" : "", accent: KOKORO_ACCENTS[m.name[0]] ?? "",
        })
        : shapeVoice(m.name, m.label, {
          gender: VOICES_BY_ENGINE[engine]?.find((v) => v.id === m.name)?.gender ?? "", accent: MACOS_ACCENTS[m.name] ?? "",
        }));

  // The workspace's config for this local voice, created on first use.
  const pickInHouseVoice = async (lang: string, engine: string, voice: string, name: string) => {
    try {
      const existing = providers.find((p) => p.role === "tts" && !managedOptionId(p) && p.engine === engine && p.voice === voice);
      const row = existing ?? await createProvider(tenantId, { name: `${IN_HOUSE_TTS[engine]} — ${name}`, role: "tts", engine, voice });
      upsert(row);
      prefetch(row);
      if (lang === primaryLanguage) onAssign("tts", row.id);
      else onTtsByLanguage({ ...ttsByLanguage, [lang]: row.id });
      setOtherVoiceFor(null);
    } catch (e) {
      onError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  // One voice for every language when the service's voices code-mix (Sarvam, ElevenLabs, Cartesia).
  const oneVoice = multilingual && (voiceOption
    ? languages.every((l) => voiceOption.languages.includes(l))
    : !!baseVoice && languages.every((l) => voiceSpeaks(baseVoice, l, supports)));

  const languageVoice = (lang: string, all = false) => {
    const current = voiceFor(lang);
    const label = all ? languages.map(languageLabel).join(" + ") : languageLabel(lang);
    const isPrimary = lang === primaryLanguage;
    const workspaceVoice = isPrimary && !current ? find(defaultTtsId) : null;
    const shared = !isPrimary && !current && voiceSpeaks(baseVoice, lang, supports) ? baseVoice : null;
    const heading = all ? (
      <label className="form-label">Voice <span className="hint">speaks {label}</span></label>
    ) : (
      <label className="form-label">{label} voice{isPrimary && multilingual && <span className="hint">main</span>}</label>
    );
    if (shared && otherVoiceFor !== lang) {
      return (
        <div className="form-group">
          {heading}
          <div className="ai-shared-voice">
            <Check size={13} /> Uses {shared.name}, which also speaks {label}.
            {voiceOption && (
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => setOtherVoiceFor(lang)}>
                Use a different voice
              </button>
            )}
          </div>
        </div>
      );
    }
    let picker;
    if (voiceProvider === OWN || !voiceOption) {
      const list = ownEngine ? inHouseVoices(ownEngine, all ? languages : [lang]) : [];
      const sameEngine = current?.engine === ownEngine;
      // A voice set up earlier with the workspace's own cloud key: still in use, shown until replaced.
      const legacy = current && !managedOptionId(current) && !IN_HOUSE_TTS[current.engine] ? current : null;
      picker = !ownEngine ? (
        <div className="form-hint" style={{ marginTop: 0 }}>Pick a voice service above.</div>
      ) : list.length === 0 ? (
        <div className="voice-missing"><AlertCircle size={13} /><span>{IN_HOUSE_TTS[ownEngine]} has no {label} voices. Pick another model or voice service.</span></div>
      ) : (
        <>
        {legacy && (
          <div className="ai-shared-voice" style={{ marginBottom: 8 }}>
            <Check size={13} /> Now using {legacy.name}, set up with this workspace&apos;s own key. Pick a voice below to switch.
          </div>
        )}
        <VoicePickerWithPreview
          voices={list}
          currentVoiceId={sameEngine ? current?.voice : null}
          currentVoiceLabel={null}
          placeholder={`Choose ${article(label)} ${label} voice…`}
          canPreview={(id) => !!sampleOf(ownEngine, id)}
          preview={(id) => fetch(sampleOf(ownEngine, id)!).then((r) => r.blob())}
          onPick={(id, name) => void pickInHouseVoice(lang, ownEngine, id, name)}
        />
        </>
      );
    } else {
      const lists = (all ? languages : [lang]).map((l) => voicesOf(voiceOption, l));
      const list = lists.some((l) => l === undefined) ? undefined : lists.some((l) => l === null) ? null
        : lists[0]!.filter((v) => lists.every((l) => l!.some((x) => x.voice_id === v.voice_id)));
      const sameOption = current && managedOptionId(current) === voiceOption.id;
      picker = list === undefined ? (
        <div className="form-hint" style={{ marginTop: 0 }}>Loading voices…</div>
      ) : list === null ? (
        <div className="voice-missing"><AlertCircle size={13} /><span>Couldn&apos;t load {voiceOption.provider}&apos;s voices. Try again shortly.</span></div>
      ) : list.length === 0 ? (
        <div className="voice-missing"><AlertCircle size={13} /><span>{voiceOption.provider} {voiceOption.label} has no {label} voices. Pick another voice service.</span></div>
      ) : (
        <VoicePickerWithPreview
          voices={list}
          currentVoiceId={sameOption ? current?.voice : null}
          currentVoiceLabel={sameOption ? current?.name : null}
          placeholder={workspaceVoice ? `Workspace default voice (${workspaceVoice.name})` : `Choose ${article(label)} ${label} voice…`}
          canPreview={(id) => voiceOption.engine !== "kokoro" || !!kokoroSample(id)}
          preview={(id) => voiceOption.engine === "kokoro"
            ? fetch(kokoroSample(id)!).then((r) => r.blob())
            : previewAiVoice(tenantId, voiceOption.id, id, lang)}
          onPick={(id, name) => void pickVoice(lang, id, name)}
        />
      );
    }
    return (
      <div className="form-group">
        {heading}
        {picker}
        {!current && !workspaceVoice && !shared && (
          <div className="voice-missing">
            <AlertCircle size={13} />
            <span>{isPrimary ? `Voice needed — pick ${article(label)} ${label} voice.` : `Voice needed — the agent can't be saved until ${label} has a voice.`}</span>
          </div>
        )}
      </div>
    );
  };

  const catalogNotice = catalogError ? (
    <div className="error-banner">Couldn&apos;t load the available models: {catalogError}</div>
  ) : null;

  const sttGroup = ofRole("stt").filter((o) => o.provider === (sttChip ?? sttOption?.provider));
  const llmGroup = ofRole("llm").filter((o) => o.provider === llmProvider);
  const voiceGroup = ofRole("tts").filter((o) => o.provider === voiceProvider);

  return (
    <>
      {showCost && <AgentCostBar catalog={catalog} stt={selectedStt} llm={selectedLlm} tts={baseVoice} />}
      {catalogNotice}

      {part !== "engines" && <>
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="card-hdr">
            <div className="card-title">Languages</div>
            <div className="card-sub">Agent understands and speaks with callers.</div>
          </div>
          <div className="card-body">
            <div className="form-group">
              <label className="form-label" htmlFor="agent-primary-language">Primary language</label>
              <select
                id="agent-primary-language"
                className="form-select"
                value={primaryLanguage ?? ""}
                onChange={(e) => applyLanguages([e.target.value, ...languages.slice(1).filter((l) => l !== e.target.value)])}
              >
                {SUPPORTED_LANGUAGES.map((l) => <option key={l.value} value={l.value}>{languageLabel(l.value)}</option>)}
              </select>
              <div className="form-hint">The agent greets callers in this language.</div>
            </div>
            <div className="ai-toggle-row">
              <label className="toggle-switch">
                <input
                  type="checkbox"
                  aria-label="Auto switch language"
                  checked={multilingual}
                  onChange={(e) => setSwitching(e.target.checked)}
                />
                <span className="toggle-slider" />
              </label>
              <div>
                <div className="form-label" style={{ margin: 0 }}>Auto switch language <span className="ed2-optional">(optional)</span></div>
                <div className="form-hint" style={{ marginTop: 2 }}>When the caller speaks another language, the agent follows them.</div>
              </div>
            </div>
            {multilingual && (
              <div className="form-group" style={{ marginTop: 14, marginBottom: 0 }}>
                <label className="form-label" htmlFor="agent-secondary-language">Secondary language <span className="ed2-optional">(optional)</span></label>
                <select
                  id="agent-secondary-language"
                  className="form-select"
                  value={secondaryLanguage ?? ""}
                  onChange={(e) => applyLanguages([primaryLanguage ?? "en", e.target.value])}
                >
                  {SUPPORTED_LANGUAGES.filter((l) => l.value !== primaryLanguage).map((l) => (
                    <option key={l.value} value={l.value}>{languageLabel(l.value)}</option>
                  ))}
                </select>
              </div>
            )}
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
            <div className="card-title">Voice</div>
            <div className="card-sub">How agent will sound on every call.</div>
          </div>
          <div className="card-body">
            <div className="form-group">
              <label className="form-label">Provider</label>
              <ProviderChips
                options={ofRole("tts")}
                languages={languages}
                current={voiceProvider}
                own={own("tts")}
                showOwn={!!catalog?.local_models_enabled && inHouseEngines.length > 0}
                label="Voice provider"
                onPick={(name) => {
                  if (name === OWN) { setVoiceOwn(true); setVoiceOptionId(null); return; }
                  const group = ofRole("tts").filter((o) => o.provider === name && o.available);
                  const fit = group.find((o) => languages.every((l) => o.languages.includes(l))) ?? group[0];
                  if (fit) void pickVoiceOption(fit);
                }}
              />
            </div>
            {voiceProvider === OWN && inHouseEngines.length > 0 && (
              <div className="form-group">
                <label className="form-label">Model</label>
                <select className="form-select" aria-label="In-house voice model" value={ownEngine ?? ""}
                  onChange={(e) => setOwnTtsEngine(e.target.value)}>
                  {inHouseEngines.map((e) => <option key={e} value={e}>{IN_HOUSE_TTS[e]} · runs on your server</option>)}
                </select>
              </div>
            )}
            {voiceOption && voiceProvider !== OWN && voiceGroup.length > 0 && (
              <div className="form-group">
                <label className="form-label">Model</label>
                <ModelSelect options={voiceGroup} value={voiceOption.id} languages={languages} label="Voice model" onPick={(o) => void pickVoiceOption(o)} />
                <LanguageCoverage verb="Speaks" option={voiceOption} />
              </div>
            )}
            {oneVoice && primaryLanguage ? (
              <>
                {languageVoice(primaryLanguage, true)}
                {secondaryLanguage && ttsByLanguage[secondaryLanguage] && languageVoice(secondaryLanguage)}
              </>
            ) : (
              languages.map((lang) => <div key={lang}>{languageVoice(lang)}</div>)
            )}
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label">Speaking speed</label>
              <div className="voice-speed">
                <span>Slower</span>
                <input
                  type="range" min={0.7} max={1.2} step={0.05} value={speed}
                  disabled={voiceConfigs.length === 0 || busy === "tts"}
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
                {voiceConfigs.length > 0 ? "Saved right away for every agent using this voice." : "Pick a voice first."}
              </div>
            </div>
          </div>
        </div>
      </>}

      {part !== "voice" && <>
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="card-hdr">
            <div className="card-title">Thinking</div>
            <div className="card-sub">the model that decides what to say</div>
          </div>
          <div className="card-body">
            <div className="form-group">
              <label className="form-label">Provider</label>
              <ProviderChips
                options={ofRole("llm")}
                current={llmProvider}
                own={own("llm")}
                label="Thinking provider"
                onPick={(name) => {
                  if (name === OWN) { setLlmChip(OWN); return; }
                  const first = ofRole("llm").find((o) => o.provider === name && o.available);
                  if (first) void pickLlm(first);
                }}
              />
            </div>
            <div className="form-group">
              <label className="form-label">Model</label>
              {llmProvider === OWN ? (
                <OwnSelect own={own("llm")} value={llmOption ? null : llmId} label="Thinking model"
                  onPick={(id) => { onAssign("llm", id); setLlmChip(null); }} />
              ) : llmGroup.length ? (
                <ModelSelect options={llmGroup} value={llmOption?.id ?? null} label="Thinking model" onPick={(o) => void pickLlm(o)} />
              ) : (
                <div className="form-hint" style={{ marginTop: 0 }}>Pick a provider above.</div>
              )}
              {llmOption?.blurb && <div className="form-hint">{llmOption.blurb}</div>}
              {!llmId && (
                <div className="voice-missing"><AlertCircle size={13} /><span>Required — the agent can&apos;t take calls without it.</span></div>
              )}
            </div>
            <div className="form-group" style={{ marginBottom: thinkingCapable ? undefined : 0 }}>
              <label className="form-label">Response style</label>
              <div className="voice-speed">
                <span>Focused</span>
                <input
                  type="range" min={0} max={1.2} step={0.1} value={temperature}
                  disabled={!selectedLlm || busy === "llm"}
                  aria-label="Response style, from focused to creative"
                  onChange={(e) => setTempDraft(Number(e.target.value))}
                  onMouseUp={(e) => void commitTemperature(Number(e.currentTarget.value))}
                  onTouchEnd={(e) => void commitTemperature(Number(e.currentTarget.value))}
                  onKeyUp={(e) => void commitTemperature(Number(e.currentTarget.value))}
                />
                <span>Creative</span>
                <span className="voice-speed-value">{temperature.toFixed(1)}</span>
              </div>
              <div className="form-hint">Focused keeps answers consistent; creative varies the wording more.</div>
            </div>
            {thinkingCapable && selectedLlm && (
              <div className="ai-toggle-row">
                <label className="toggle-switch">
                  <input
                    type="checkbox"
                    aria-label="Think before answering"
                    checked={Boolean(selectedLlm.extra?.think ?? false)}
                    onChange={(e) => patchExtra(selectedLlm, { think: e.target.checked })}
                  />
                  <span className="toggle-slider" />
                </label>
                <div>
                  <div className="form-label" style={{ margin: 0 }}>Think before answering</div>
                  <div className="form-hint" style={{ marginTop: 2 }}>Smarter replies, but adds 5–8 seconds per turn.</div>
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="card" style={{ marginBottom: 14 }}>
          <div className="card-hdr">
            <div className="card-title">Hearing</div>
            <div className="card-sub">turns callers&apos; speech into words</div>
          </div>
          <div className="card-body">
            {!custom ? (
              <>
                {recommended ? (
                  <div className="ai-recommended">
                    <div>
                      <div className="ai-recommended-kicker">Platform picks this from your languages</div>
                      <div className="ai-recommended-name">{recommended.provider} {recommended.label}</div>
                      <div className="form-hint" style={{ marginTop: 2 }}>
                        {recommended.blurb} Hears {languages.map(languageLabel).join(" and ")}. {money(recommended.price_per_min)}/min.
                      </div>
                    </div>
                    {sttOption?.id === recommended.id ? (
                      <span className="badge green"><Check size={11} /> In use</span>
                    ) : (
                      <button type="button" className="btn btn-primary btn-sm" disabled={busy === "stt"}
                        onClick={() => void applyRecommendedHearing(languages)}>
                        Use this
                      </button>
                    )}
                  </div>
                ) : (
                  <div className="voice-missing">
                    <AlertCircle size={13} />
                    <span>
                      No speech recognition on this platform hears {languages.map(languageLabel).join(" and ")} yet.
                      {ofRole("stt")[0]?.key_env ? ` ${unavailableHint(ofRole("stt")[0])}` : ""}
                    </span>
                  </div>
                )}
                <button type="button" className="btn btn-ghost btn-sm" style={{ marginTop: 10 }} onClick={() => setHearingCustom(true)}>
                  Choose myself
                </button>
              </>
            ) : (
              <>
                <div className="form-group">
                  <label className="form-label">Provider</label>
                  <ProviderChips
                    options={ofRole("stt")}
                    languages={languages}
                    current={sttChip ?? sttOption?.provider ?? (selectedStt ? OWN : null)}
                    own={own("stt")}
                    label="Hearing provider"
                    onPick={(name) => {
                      setSttChip(name);
                      if (name === OWN) return;
                      const fit = ofRole("stt").find((o) => o.provider === name && o.available && hears(o, languages));
                      if (fit) void choose("stt", fit).then((row) => { if (row) { onAssign("stt", row.id); setSttChip(null); } });
                    }}
                  />
                </div>
                <div className="form-group">
                  <label className="form-label">Model</label>
                  {(sttChip ?? (sttOption ? null : selectedStt ? OWN : null)) === OWN ? (
                    <OwnSelect own={own("stt")} value={sttOption ? null : sttId} label="Hearing model"
                      onPick={(id) => { onAssign("stt", id); setSttChip(null); }} />
                  ) : sttGroup.length ? (
                    <>
                      <ModelSelect options={sttGroup} value={sttOption?.id ?? null} languages={languages} label="Hearing model"
                        onPick={(o) => void choose("stt", o).then((row) => { if (row) onAssign("stt", row.id); })} />
                      {sttOption && <LanguageCoverage verb="Hears" option={sttOption} />}
                    </>
                  ) : (
                    <div className="form-hint" style={{ marginTop: 0 }}>Pick a provider above.</div>
                  )}
                </div>
                {recommended && (
                  <button type="button" className="btn btn-ghost btn-sm" disabled={busy === "stt"}
                    onClick={() => { setHearingCustom(false); setSttChip(null); void applyRecommendedHearing(languages); }}>
                    <RotateCcw size={12} /> Reset to recommended
                  </button>
                )}
              </>
            )}
            {selectedStt?.engine === "sarvam" && selectedStt.model === "saaras:v3" && (
              <div className="form-group" style={{ marginTop: 12, marginBottom: 0 }}>
                <label className="form-label" htmlFor="sarvam-stt-mode">Output</label>
                <select
                  id="sarvam-stt-mode"
                  className="form-select"
                  value={String(selectedStt.extra?.mode ?? "transcribe")}
                  onChange={(e) => patchExtra(selectedStt, { mode: e.target.value })}
                >
                  {SARVAM_STT_MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
                </select>
                <div className="form-hint">{SARVAM_STT_MODES.find((m) => m.value === (selectedStt.extra?.mode ?? "transcribe"))?.hint}</div>
              </div>
            )}
            {sttWarning && (
              <div className="voice-missing"><AlertCircle size={13} /><span>{sttWarning}</span></div>
            )}
            {!sttId && custom && (
              <div className="voice-missing"><AlertCircle size={13} /><span>Required — the agent can&apos;t take calls without it.</span></div>
            )}
          </div>
        </div>
      </>}
    </>
  );
}
