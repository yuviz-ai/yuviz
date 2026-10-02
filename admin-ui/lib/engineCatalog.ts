// Engines must match ai_provider_manager.py's _DEFAULT_REGISTRY. Model/voice lists are
// suggestions only (not enforced server-side); the UI always offers "Other (custom)".

import { ProviderRole } from "./api";

export interface EngineOption {
  value: string;
  label: string;
}

// TTS engines the Voice card has a browsing picker for; others fall back to the engine chooser.
export type BrowsableTtsEngine = "macos" | "kokoro" | "elevenlabs";
export const BROWSABLE_TTS_ENGINES: readonly BrowsableTtsEngine[] = ["macos", "kokoro", "elevenlabs"];
export function asBrowsableTtsEngine(engine: string | undefined): BrowsableTtsEngine | null {
  return BROWSABLE_TTS_ENGINES.includes(engine as BrowsableTtsEngine) ? (engine as BrowsableTtsEngine) : null;
}

// Local engines take no credential, so the UI hides the API Key field for them.
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
  // .en variants skip language auto-detection (avoids wrong-language hallucinations on noise).
  faster_whisper: ["small.en", "tiny.en", "base.en", "medium.en", "tiny", "base", "small", "medium", "large-v3"],
  deepgram: ["nova-3", "nova-2"],
  // Must be pulled locally first. gemma4:e2b needs extra.think:false or voice latency regresses.
  ollama: ["llama3.2", "llama3", "qwen2.5", "mistral", "phi3", "gemma3:4b", "gemma4:e2b"],
  // Cheapest-capable first. A blank model uses the backend default (for groq, the 70b).
  openai: ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo"],
  anthropic: ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"],
  // Groq and NVIDIA host other vendors' models, hence publisher-namespaced ids.
  groq: ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"],
  nvidia: ["meta/llama-3.1-8b-instruct", "meta/llama-3.3-70b-instruct", "mistralai/mistral-7b-instruct-v0.3"],
  cohere: ["command-r7b-12-2024", "command-r-08-2024", "command-a-03-2025"],
  // "-latest" aliases survive Google deprecating pinned versions for new callers.
  gemini: ["gemini-flash-latest", "gemini-pro-latest", "gemini-2.5-flash", "gemini-2.5-pro"],
};

// Separate from MODELS_BY_ENGINE because engine names overlap with the llm role.
// Unset model falls back to embedding_manager.py's engine default.
export const EMBEDDING_MODELS_BY_ENGINE: Record<string, string[] | null> = {
  ollama: ["nomic-embed-text", "mxbai-embed-large", "all-minilm"],
  openai: ["text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"],
};

// Picker filter metadata only; never sent to a TTS engine.
export type VoiceGender = "female" | "male" | "neutral";

export interface VoiceOption {
  id:       string;
  label:    string;
  gender:   VoiceGender;
  // Same format as LANGUAGES values, so a voice pick can set agent.language.
  language: string;
  // Pre-rendered by scripts/generate_voice_samples.py.
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
  // af_/bf_ = American/British female, am_/bm_ = American/British male.
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

// Suggested values for agents.language; not enforced (free text via "Other").
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

// Languages ElevenLabs multilingual models accept via language_code (any voice can speak any of them).
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
