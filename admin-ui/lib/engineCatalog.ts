// Static catalog of known engines/models/voices, matching exactly what
// services/conversation/ai_provider_manager.py's _DEFAULT_REGISTRY actually
// implements — every engine listed here is real and instantiable, not
// aspirational (cloud engines deepgram/openai/elevenlabs were added this
// session; see project memory).
//
// Model/voice lists are "known-good starting points," not exhaustive or
// enforced server-side (provider_configs.model/voice are plain TEXT columns)
// — the UI always offers an "Other (custom)" escape hatch per field, since
// e.g. Ollama's actual available models depend on what's pulled locally, and
// ElevenLabs voice_ids are account-specific (never guessed/hardcoded here).

import { ProviderRole } from "./api";

export interface EngineOption {
  value: string;
  label: string;
}

// The Voice card (agent detail page, new-agent page) only has a browsing
// picker for these three — a provider on another real, backend-supported
// TTS engine (e.g. "deepgram", assignable via the raw Provider Assignments
// dropdown) has no catalog entry in LocalVoicePicker and no picker at all,
// so it must fall back to the neutral engine chooser rather than being
// cast/trusted blindly. Shared between both pages rather than duplicated —
// unlike the per-page state (chosenEngine, form vs. local setters), this
// logic is identical in both places.
export type BrowsableTtsEngine = "macos" | "kokoro" | "elevenlabs";
export const BROWSABLE_TTS_ENGINES: readonly BrowsableTtsEngine[] = ["macos", "kokoro", "elevenlabs"];
export function asBrowsableTtsEngine(engine: string | undefined): BrowsableTtsEngine | null {
  return BROWSABLE_TTS_ENGINES.includes(engine as BrowsableTtsEngine) ? (engine as BrowsableTtsEngine) : null;
}

// Local engines run on the same machine as the Conversation Service with
// no account/credential of their own — showing an API Key field for them
// is not just unnecessary, it's misleading (nothing reads it). Matches
// each role's own local entries in ENGINES_BY_ROLE below exactly.
export const LOCAL_ENGINES: ReadonlySet<string> = new Set([
  "faster_whisper", "ollama", "macos", "kokoro",
]);

export const ENGINES_BY_ROLE: Record<ProviderRole, EngineOption[]> = {
  stt: [
    { value: "faster_whisper", label: "FasterWhisper (local)" },
    { value: "deepgram", label: "Deepgram (cloud)" },
  ],
  llm: [
    { value: "ollama", label: "Ollama (local)" },
    { value: "openai", label: "OpenAI (cloud)" },
    { value: "anthropic", label: "Anthropic Claude (cloud)" },
    { value: "gemini", label: "Gemini (cloud)" },
    { value: "groq", label: "Groq (cloud)" },
    { value: "nvidia", label: "NVIDIA NIM (cloud)" },
    { value: "cohere", label: "Cohere (cloud)" },
  ],
  tts: [
    { value: "macos", label: "macOS say (local)" },
    { value: "kokoro", label: "Kokoro (local)" },
    { value: "elevenlabs", label: "ElevenLabs (cloud)" },
  ],
  embedding: [
    { value: "ollama", label: "Ollama (local)" },
    { value: "openai", label: "OpenAI (cloud)" },
  ],
};

// null = no fixed list; render a free-text input instead of a dropdown.
export const MODELS_BY_ENGINE: Record<string, string[] | null> = {
  // .en variants skip language auto-detection entirely — the recommended
  // default for English-only agents (see pipeline_config.py's SttConfig
  // docstring: eliminates the class of bug where a short noise blip gets
  // transcribed as a wrong-language hallucination instead of ignored).
  faster_whisper: ["small.en", "tiny.en", "base.en", "medium.en", "tiny", "base", "small", "medium", "large-v3"],
  deepgram: ["nova-3", "nova-2"],
  // Local models, so the list is what dev.sh can pull rather than what an
  // account is entitled to; `ollama pull <tag>` first, or dev.sh --llm-model.
  // gemma4:e2b is the thinking-capable/tool-calling option — set
  // extra.think:false on its provider config or voice latency regresses.
  ollama: ["llama3.2", "llama3", "qwen2.5", "mistral", "phi3", "gemma3:4b", "gemma4:e2b"],
  // Cheapest-capable first below, so the cheap option is the one a reader
  // reaches for. It is NOT what the form submits by default — the model
  // select starts blank ("— select a model —"), and a blank model means the
  // backend engine default in ai_provider_manager.py applies instead. Those
  // two agree everywhere except groq, whose backend fallback is the 70b.
  openai: ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo"],
  anthropic: ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"],
  // Groq and NVIDIA host other vendors' models, hence publisher-namespaced ids.
  groq: ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"],
  nvidia: ["meta/llama-3.1-8b-instruct", "meta/llama-3.3-70b-instruct", "mistralai/mistral-7b-instruct-v0.3"],
  cohere: ["command-r7b-12-2024", "command-r-08-2024", "command-a-03-2025"],
  // "-latest" aliases track Google's current stable model; pinned versions
  // get deprecated for new callers (gemini-2.5-flash returned 404 "no longer
  // available to new users"), so the alias is the resilient default.
  gemini: ["gemini-flash-latest", "gemini-pro-latest", "gemini-2.5-flash", "gemini-2.5-pro"],
};

// Kept separate from MODELS_BY_ENGINE rather than merged into it: "ollama"
// and "openai" are shared engine names across the llm and embedding roles,
// but their model lists are completely different catalogs (llama3.2 is not
// an embedding model) — a single engine-keyed map can't represent that.
// Engine-level defaults in embedding_manager.py (nomic-embed-text for
// ollama, text-embedding-3-small for openai) apply whenever model is left
// unset — leaving this unset is a perfectly normal choice, not a gap.
export const EMBEDDING_MODELS_BY_ENGINE: Record<string, string[] | null> = {
  ollama: ["nomic-embed-text", "mxbai-embed-large", "all-minilm"],
  openai: ["text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"],
};

// Gender is metadata for filtering/labeling the picker UI only — it is
// never sent to any TTS engine. No engine takes "gender" as a synthesis
// parameter; it's a fixed property of each concrete voice id (Kokoro
// encodes it in the name prefix, macOS voices have fixed built-in
// genders). "neutral" exists in the type for future engines/voices; no
// currently-shipped local voice qualifies, so none is labeled that way —
// see VoicePicker's gender filter, which only shows chips that have at
// least one matching voice.
export type VoiceGender = "female" | "male" | "neutral";

export interface VoiceOption {
  id:       string;
  label:    string;
  gender:   VoiceGender;
  // Matches LANGUAGES' value format below — lets a voice pick drive
  // agent.language automatically. Real macOS system-voice locales (not
  // guessed): Samantha/Alex are en-US, Karen is en-AU, Moira is en-IE,
  // Daniel is en-GB.
  language: string;
  // Pre-rendered preview clip (see scripts/generate_voice_samples.py) —
  // the macOS/Kokoro equivalent of ElevenLabs' preview_url. Static rather
  // than synthesized on demand: unlike ElevenLabs' account-specific,
  // unbounded voice list, this catalog is small and fixed, so there's no
  // need for a live synthesis endpoint just to preview it.
  sampleUrl: string;
}

export const VOICES_BY_ENGINE: Record<string, VoiceOption[] | null> = {
  macos: [
    { id: "Samantha", label: "Samantha", gender: "female", language: "en-US", sampleUrl: "/voice-samples/macos/Samantha.wav" },
    { id: "Karen",    label: "Karen",    gender: "female", language: "en-AU", sampleUrl: "/voice-samples/macos/Karen.wav" },
    { id: "Moira",    label: "Moira",    gender: "female", language: "en-IE", sampleUrl: "/voice-samples/macos/Moira.wav" },
    { id: "Alex",     label: "Alex",     gender: "male",   language: "en-US", sampleUrl: "/voice-samples/macos/Alex.wav" },
    { id: "Daniel",   label: "Daniel",   gender: "male",   language: "en-GB", sampleUrl: "/voice-samples/macos/Daniel.wav" },
  ],
  // af_/bf_ = American/British female, am_/bm_ = American/British male
  // (Kokoro's own naming convention) — full set of voices shipped with
  // the installed model, not just the original 5.
  kokoro: [
    { id: "af_sarah",  label: "Sarah (US)",   gender: "female", language: "en-US", sampleUrl: "/voice-samples/kokoro/af_sarah.wav" },
    { id: "af_bella",  label: "Bella (US)",   gender: "female", language: "en-US", sampleUrl: "/voice-samples/kokoro/af_bella.wav" },
    { id: "af_nicole", label: "Nicole (US)",  gender: "female", language: "en-US", sampleUrl: "/voice-samples/kokoro/af_nicole.wav" },
    { id: "bf_emma",   label: "Emma (UK)",    gender: "female", language: "en-GB", sampleUrl: "/voice-samples/kokoro/bf_emma.wav" },
    { id: "bf_isabella", label: "Isabella (UK)", gender: "female", language: "en-GB", sampleUrl: "/voice-samples/kokoro/bf_isabella.wav" },
    { id: "am_adam",   label: "Adam (US)",    gender: "male",   language: "en-US", sampleUrl: "/voice-samples/kokoro/am_adam.wav" },
    { id: "am_michael", label: "Michael (US)", gender: "male",  language: "en-US", sampleUrl: "/voice-samples/kokoro/am_michael.wav" },
    { id: "bm_george", label: "George (UK)",  gender: "male",   language: "en-GB", sampleUrl: "/voice-samples/kokoro/bm_george.wav" },
    { id: "bm_lewis",  label: "Lewis (UK)",   gender: "male",   language: "en-GB", sampleUrl: "/voice-samples/kokoro/bm_lewis.wav" },
  ],
  elevenlabs: null, // account-specific voice_id — never guessed, always free text
};

// agents.language — an explicit per-agent override (see lib/api.ts's Agent
// interface); this is a curated starting list for the dropdown, not an
// enforced set (the column is plain TEXT, same posture as engine/model/
// voice above) — "Other (custom)" always escapes to free text for a
// language/locale not listed here.
export const LANGUAGES = [
  { value: "en", label: "English" },
  { value: "en-US", label: "English (US)" },
  { value: "en-GB", label: "English (UK)" },
  { value: "en-AU", label: "English (Australia)" },
  { value: "en-IE", label: "English (Ireland)" },
  { value: "es", label: "Spanish" },
  { value: "fr", label: "French" },
  { value: "de", label: "German" },
  { value: "it", label: "Italian" },
  { value: "pt", label: "Portuguese" },
  { value: "hi", label: "Hindi" },
  { value: "ja", label: "Japanese" },
  { value: "zh", label: "Chinese" },
  { value: "ar", label: "Arabic" },
];

export const OTHER = "__other__";

// ElevenLabs' own documented set of languages supported by its multilingual
// models (eleven_multilingual_v2/eleven_turbo_v2_5/eleven_flash_v2_5) via
// the language_code request field — independent of which languages this
// account's current voices happen to be tagged with (any of these voices
// can speak any of these languages when a multilingual model is used; see
// services/conversation/providers/tts/elevenlabs.py's language_code
// docstring). Not the same list as LANGUAGES above: this is specifically
// what ElevenLabs can synthesize, not a general agent-language catalog.
export const ELEVENLABS_LANGUAGES = [
  { value: "en", label: "English" },
  { value: "ja", label: "Japanese" },
  { value: "zh", label: "Chinese" },
  { value: "de", label: "German" },
  { value: "hi", label: "Hindi" },
  { value: "fr", label: "French" },
  { value: "ko", label: "Korean" },
  { value: "pt", label: "Portuguese" },
  { value: "it", label: "Italian" },
  { value: "es", label: "Spanish" },
  { value: "id", label: "Indonesian" },
  { value: "nl", label: "Dutch" },
  { value: "tr", label: "Turkish" },
  { value: "fil", label: "Filipino" },
  { value: "pl", label: "Polish" },
  { value: "sv", label: "Swedish" },
  { value: "bg", label: "Bulgarian" },
  { value: "ro", label: "Romanian" },
  { value: "ar", label: "Arabic" },
  { value: "cs", label: "Czech" },
  { value: "el", label: "Greek" },
  { value: "fi", label: "Finnish" },
  { value: "hr", label: "Croatian" },
  { value: "ms", label: "Malay" },
  { value: "sk", label: "Slovak" },
  { value: "da", label: "Danish" },
  { value: "ta", label: "Tamil" },
  { value: "uk", label: "Ukrainian" },
  { value: "ru", label: "Russian" },
];
