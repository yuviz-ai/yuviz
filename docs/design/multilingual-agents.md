# Design: multilingual voice agents

Status: **approved** (open questions resolved — see the end). Branch: `feature/multilingual-agents` (off `redesign`).

## Goal

One agent understands callers in several languages, including Hindi–English code-switching. It replies
in the caller's language, with a matching voice. The design uses only the providers we already have.
`hi` and `en` are first-class. Adding a language is a registry entry plus an optional string table.

## 0. Verified facts that shape the design

- STT, LLM and TTS instances are **shared across calls**. `AIProviderManager` caches them per
  `provider_configs.id`. So language can't live on the instance; it has to be passed per session or
  per request.
- The test fakes implement `feed_stream`, `finalize_stream` and `synthesize_stream` **without** extra
  kwargs (6 STT fakes, plus the TTS fakes). Adding a parameter to the protocol would break them, so we
  use a capability flag (see §3).
- Provider configs are fetched **by id, with no tenant scope**, on the runtime path
  (`provider:{id}` in Redis, `/providers/{id}` over HTTP). The row does carry `tenant_id`. Today the
  only tenant check is at write time, in `services/config/agents.py:_validate_provider_assignments`.
- Deepgram nova-3 `language=multi` covers en, es, fr, de, **hi**, ru, pt, ja, it and nl. The detected
  language is in `alternatives[0].languages` (a list, dominant first) and `alternatives[0].words[].language`.
  The JSON path is the same for REST (`results.channels[0]…`) and live streaming (`channel…`). **There
  is no language-confidence field.** We derive confidence as the dominant language's share of words.
- The workflow `WorkflowRunner` takes a **static** `base_suffix`. `history[0]` is rebuilt every turn by
  `pipeline._refresh_node_prompt`, so that is the hook for the per-turn language instruction.
- Today's greeting comes from the workflow graph's start node (`runner.greeting()`). It is not read
  straight from `agents.greeting`.

## 1. Data model (forward-only, idempotent, in `database/schema.sql`)

```sql
-- agents: NULL/empty supported_languages = single-language agent (today's behaviour).
ALTER TABLE agents ADD COLUMN IF NOT EXISTS supported_languages     TEXT[];
ALTER TABLE agents ADD COLUMN IF NOT EXISTS tts_config_by_language  JSONB;  -- {"hi": "<provider_configs.id>"}
ALTER TABLE agents ADD COLUMN IF NOT EXISTS greeting_by_language    JSONB;  -- {"hi": "नमस्ते…"}
DO $$ BEGIN ALTER TABLE agents ADD CONSTRAINT agents_tts_config_by_language_object
    CHECK (tts_config_by_language IS NULL OR jsonb_typeof(tts_config_by_language) = 'object');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
-- (same CHECK for greeting_by_language)

-- calls: detected languages in order of first appearance, for analytics.
ALTER TABLE calls ADD COLUMN IF NOT EXISTS detected_languages TEXT[];
```

- `default_language` reuses `agents.language`. The API exposes it under both names.
- No new tables, so no new RLS policies. The existing row policies on `agents` and `calls` cover the
  new columns. Everything is read and written through `tenant_conn` as today.
- **Tenant validation (config service):** `_validate_provider_assignments` is extended to every value
  in `tts_config_by_language`. It applies the same rules as today: same tenant, `role='tts'`, a usable
  voice, `FOR SHARE`. It also checks:
  - every key is a known language code and is in `supported_languages`;
  - the default language is added to `supported_languages` automatically;
  - greeting length is capped.
- **Delete guard:** `soft_delete_provider_config` also refuses when an agent references the provider
  through `tts_config_by_language` (`EXISTS (SELECT 1 FROM jsonb_each_text(...) WHERE value = $2)`).
  Without this, a delete leaves a dangling override.
- **Runtime defence in depth:** SDK `ProviderConfig` gains an optional `tenant_id`. When
  `get_runtime_config` builds the config, it **drops** any override whose `tenant_id` doesn't match
  `agent.tenant_id`, and logs an error. That matters because provider fetches aren't tenant-scoped.

**Language registry**: `libs/config_sdk/languages.py`, shared by the config service and runtime:

```python
LANGUAGES = {
  "en": Language("English", "English",  kokoro="a", deepgram_multi=True,  script_hint=None),
  "hi": Language("Hindi",   "हिन्दी",  kokoro="h", deepgram_multi=True,
                 script_hint="Write Hindi words in Devanagari and English words in Latin script."),
  "es": …, "fr": …, "de": …, "pt": …, "it": …, "ja": …, "zh": …
}
```

Adding a language means adding a registry row. Its strings fall back to `en` until someone adds a
string table.

## 2. Session language state machine

This is a new pure module, `services/conversation/language.py` (`LanguageTracker`), with one instance
per session in `_SessionState`. It is only active when `supported_languages` is set (the multilingual path).
Otherwise the session language is fixed and nothing changes.

```
inputs per utterance:  detected (normalised "hi-IN" → "hi", "hi-Latn" → "hi"), confidence, word count
state:                 current, candidate, streak, has_evidence

normalise:
  - Hinglish rule (hi ∈ supported): the utterance counts as "hi" if its text contains any
      Devanagari, or ≥ VOICEAI_LANG_HI_WORD_SHARE (0.30) of its words are tagged hi
      (Deepgram per-word tags; Whisper: detected language hi); otherwise it is "en"
  - detected not in supported or confidence < 0.70           → ignore (no change; candidate reset)
transition:
  - detected == current                                       → streak = 0
  - not has_evidence (first confident utterance of the call)  → switch now
  - detected == candidate → streak += 1; streak ≥ 2           → switch
  - else                                                      → candidate = detected, streak = 1
```

- The **first** confident utterance switches immediately. The default language is a prior, so a caller
  who opens in Hindi gets Hindi on turn 1.
- After that, a switch needs `VOICEAI_LANG_SWITCH_STREAK` consecutive qualifying utterances.
  **Default 1** (changed from 2 after local testing: callers expected the very next reply to follow
  them). Set 2+ for hysteresis, so one noisy utterance can't flip the language.
- **Hinglish:** once the session is `hi`, an utterance with both hi and en words stays `hi`; an
  English-only utterance switches back (after `SWITCH_STREAK` of them).
- Every switch is logged, and the language is recorded for `calls.detected_languages`.
- Every threshold is a module constant overridable by env: `VOICEAI_LANG_MIN_CONFIDENCE` (0.70),
  `VOICEAI_LANG_SWITCH_STREAK` (1), `VOICEAI_LANG_HI_WORD_SHARE` (0.30),
  `VOICEAI_LANG_SHORT_MIN_S` (0.45), `VOICEAI_LANG_SHORT_MIN_CONFIDENCE` (0.80).

## 3. Provider resolution

**Effective language, per role** (this is what makes `agent.language` real):

| role | precedence |
|---|---|
| STT | multilingual agent → `multi` (Deepgram) / `None` = auto (Whisper); else `agent.language` › `stt.language` |
| TTS | session language per request; base language `agent.language` › `tts.language` |

I deliberately don't use `MediaInfo.language` for TTS. It falls through to `stt.language`, so an
agent with no language and Deepgram `en` would suddenly force `language_code=en` on an ElevenLabs
voice that auto-detects today. `MediaInfo` itself stays as it is, for display.

**Backward-compatible interface change.** `ISTT` and `ITTS` gain an optional keyword
`language: str | None = None`, plus a class attribute `accepts_language: bool = False`. The pipeline
only passes `language=` to providers that set the flag. Third-party implementations and test fakes
keep working unchanged.

- **Deepgram STT:** `language` is applied when the live socket opens (per session) and on REST calls.
  The constructor default stays `"en"`, and the docstring is fixed: omitting `language` means English,
  not auto-detect. The parser reads `languages[0]` and gets confidence from the per-word share. If the
  model isn't nova-2/3, or a supported language isn't covered by `multi`, it logs a warning and falls
  back to the default language.
- **Whisper:** a per-call `language` (`None` means auto). It returns `info.language` and
  `info.language_probability`. A `.en` model on a multilingual agent logs an error once.
- **Cartesia:** `language` goes into `_body()`. **ElevenLabs:** `language_code` is set per request.
  The row's value is the fallback.
- **Kokoro:** `KPipeline`s are created lazily, one per `lang_code`, and share one `KModel`
  (`KPipeline(lang_code=…, model=self._model)`, so the weights load only once). The ISO→Kokoro map
  comes from the registry. To keep the turn path clear, the handler **pre-builds** the pipelines for
  the agent's supported languages in a background executor at session start.
- **Deepgram Aura / macOS:** English only; they ignore the language. The config service **rejects**
  saving an agent where a non-`en` supported language has neither a multilingual base TTS nor a
  per-language override. The error names the language and the provider, and the UI shows it inline.
- **Per-language voice:** `ProviderConfigs.tts_by_language` resolves through the same
  `AIProviderManager` cache, so it is still keyed by `provider_configs.id`, and that id is
  tenant-owned. `ProviderBundle.tts_for(lang)` returns the override if there is one, otherwise the
  base TTS.
  - Overrides are resolved at session setup, in parallel with the base bundle. They are never resolved
    mid-turn.
  - The existing `provider_config_changed` eviction already covers the override rows.

## 4. Pipeline changes (`services/conversation/pipeline.py`)

- **LLM instruction, per turn:** `_refresh_node_prompt(history, session_id)` appends this line when the
  agent is multilingual:
  > The caller is speaking Hindi now. Reply only in Hindi (हिन्दी), even if earlier turns of the
  > call were in another language; if the caller mixes Hindi and English in their sentences, you
  > may mix the same way. Always write Hindi words in Devanagari script … Keep any [[…]] tokens,
  > numbers and tool arguments exactly as specified.

  It is anchored to the caller's *latest* language. An earlier "mirror the caller's mix" wording
  let llama3.2 keep answering in Hindi after the caller switched back to English (1/6 correct;
  6/6 with this wording, measured against the real agent prompt).

  It isn't baked into `base_suffix`, so it changes when the session language does.
- **Sentence splitter:** add the alternative `(?<=[।॥。！？])\s*`. No whitespace is needed after it,
  because CJK doesn't put a space there. The English abbreviation guards stay as they are.
- **i18n:** a new `services/conversation/i18n/` with `en.py`, `hi.py` and `t(key, lang)`. Missing keys
  and unknown languages fall back to `en`. The strings that move there:
  - `_FALLBACK_GOODBYE`, `_MAX_DURATION_GOODBYE`, `_FALLBACK_LLM_ERROR`
  - `_FIRST_TURN_FILLER`, `_TRANSFER_FAILED_FALLBACK`, `_BOOKING_FABRICATION_TRANSFER_ANNOUNCEMENT`
  - the tool fillers (`FillerSelector.select_tool_filler(..., language=)`)

  The English text stays byte-identical. The agent's own `farewell_message` and
  `transfer_announcement` still take precedence.
- **Greeting:** if `greeting_by_language[default_language]` is set, it is rendered through the
  workflow's variables. Otherwise the existing `runner.greeting()` is used.
- **Short-utterance gate:** this applies only to multilingual agents. Single-language agents keep the
  1.0 s gate exactly as it is.
  - Audio under 0.45 s is always dropped. Whisper hallucinates a confident "Thank you." below that.
  - Audio between 0.45 s and 1.0 s is transcribed. It is kept only if STT reports
    `language_confidence ≥ 0.80` **and** the detected language equals the current session language.
  - A short utterance is never fed to the tracker, so it can never switch the session language.
  - This adds no extra network call. Deepgram already has the transcript from the live stream, and
    Whisper runs locally.
- **Guardrails / booking claim:**
  - `GuardrailDetector.check(text, language)` selects a lexicon set: `en` → English; `hi` → English +
    Devanagari Hindi + romanised Hindi, since callers code-switch.
  - Any other language returns `None` and logs `guardrail check skipped language=xx` once per session,
    so it never causes a false escalation.
  - The booking-claim regexes follow the same pattern.
- **Detected languages:** `TranscriptBuilder.record_language()` accumulates them, and `end_call` writes
  `calls.detected_languages`.

## 5. Post-call

The sentiment, summary and extractor prompts gain one line: *"Write every output value in English,
whatever language the call was in; transliterate names to Latin script."* Nothing else changes.

## 6. Knowledge

- **Chunking:** `chunk_text(..., language=None)` uses the document's `language` when set, otherwise it
  sniffs the script. When more than 30% of characters are Han, Kana, Hangul, Thai, Lao, Khmer or
  Myanmar, it sizes by **characters**: `chunk_size × 1.5` characters, overlap scaled the same way.
  Otherwise the word-based path is unchanged.
- **Embeddings:** no schema change (`VECTOR(768)` stays). The docs and the KB UI hint recommend OpenAI
  `text-embedding-3-small` with `dimensions=768` for multilingual KBs. The docs give a re-index step:
  switch the KB's embedding config, then re-ingest. Retrieval scoring is untouched.

## 7. Config & UI

- STT default `small.en` → `small` in `.env.example`, `docker-compose.yml` (×2), `dev.sh`,
  `pipeline_config.py`, `docs/docker-startup.md` and the seed script.
- Agent editor, "Language & Voice" tab:
  - a default-language select and a supported-languages multi-select;
  - a voice override and a greeting for each language;
  - corrected hint text.
- `ProvidersPanel`:
  - a language field on STT/TTS forms (`multi` for Deepgram STT);
  - stop recommending `.en` Whisper models when an agent has more than one language.
- `engineCatalog.ts`: Cartesia and Deepgram added, plus the Kokoro Hindi voices (`hf_alpha`,
  `hf_beta`, `hm_omega`, `hm_psi`).

## 8. Latency & cost

Language detection uses the STT result we already get. The tracker is pure CPU work in microseconds.
There are no translation calls. Overrides are resolved and Kokoro pipelines pre-built at session
setup, never on the turn path. Whisper auto-detect reuses the encoder pass. No new vendors.

## 9. Commits (small, reviewable)

1. schema + SDK models + language registry
2. config service validation, delete guard, and tests (cross-tenant override rejected)
3. `SttResult` language + Deepgram/Whisper
4. TTS per-request language (Cartesia, ElevenLabs, Kokoro) + bundle overrides
5. `LanguageTracker` + pipeline wiring (instruction, splitter, gate)
6. i18n strings + fillers + greeting
7. guardrails / booking-claim lexicons
8. post-call English prompts + `calls.detected_languages`
9. knowledge chunking
10. env/default model changes + admin UI
11. docs: "How to configure a multilingual agent"

## Decisions (resolved open questions)

1. Fixing `agents.language` changes behaviour for agents with a stale value. No data migration by
   default; the PR includes an audit query (agents whose `language` differs from their STT/TTS row's)
   and its output. Rows that look accidental are nulled in the migration.
2. Thresholds approved as above, each env-overridable (`VOICEAI_LANG_*`). Short gate floor 0.45 s,
   same-language-only, never switches the session.
3. English-only TTS on a multilingual agent is rejected by the config service (API and easy-agent
   flow bypass UI warnings).
4. Single-language agents get no prompt injection. `language` alone controls STT/TTS;
   `supported_languages` turns on prompt injection and language switching.
