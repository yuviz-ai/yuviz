# How to configure a multilingual agent

A multilingual agent understands callers in several languages, including Hindi–English code-switching
(Hinglish). It replies in the caller's language with a matching voice. It uses only the providers
yuviz already supports: no extra vendor, no extra network call per turn, no translation step.

Hindi (`hi`) and English (`en`) are first-class. Spanish, French, German, Portuguese, Italian,
Japanese and Chinese are in the language registry. They work with English system strings (fillers,
goodbyes) until someone adds a string table for them (see [Adding a language](#adding-a-language)).

## Two settings, two behaviours

| Setting | What it does |
|---|---|
| `language` (agent) | Sets the STT and TTS language. It overrides the provider row's own `language`. On its own it changes nothing else: the agent stays single-language and its prompt is unchanged. |
| `supported_languages` (agent) | Turns on the **multilingual path**: per-utterance language detection, switching, and a per-turn "Reply in <language>" instruction. `language` becomes the *default* language, the one the call starts in. |

Agents with no `supported_languages` behave exactly as before.

> **Behaviour change:** before this release the runtime ignored `agents.language`. Agents that already
> had a value now use it for STT and TTS. `scripts/audit_agent_language.sql` lists the affected agents.

## Step by step

1. **Pick an STT that can detect languages.**
   - **Deepgram** `nova-3` (or `nova-2`). For a multilingual agent the runtime listens with
     `language=multi` automatically, whatever the provider row says. `multi` covers en, es, fr, de, hi,
     ru, pt, ja, it and nl. With a supported language outside that set, it falls back to listening in
     the default language only, and logs a warning.
   - **faster-whisper** with a multilingual model (`small`, `medium`, `large-v3`). Never use a `.en`
     model: those are English-only and can't detect other languages. The default model is now `small`.
2. **Pick a TTS that can speak every language,** or add a per-language voice override (step 4).

   | Engine | Languages |
   |---|---|
   | Cartesia `sonic-2` | en hi es fr de pt it ja zh |
   | ElevenLabs `eleven_turbo_v2_5`, `eleven_multilingual_v2`, `eleven_flash_v2_5` | en hi es fr de pt it ja zh |
   | Kokoro | en hi es fr pt it ja zh. Use a native voice per language, e.g. Hindi `hf_alpha`, `hf_beta`, `hm_omega`, `hm_psi` |
   | Deepgram Aura, macOS `say` | English only |

   Saving an agent is **rejected** when a non-English supported language has no voice that can speak
   it. The error names the language and the provider, for example: *"Hindi (hi) can't be spoken by the
   agent's TTS provider 'Aura Asteria' (deepgram) — choose a multilingual TTS provider or add a Hindi
   voice override"*.
3. **Set the languages** in the agent editor (**Language & Voice** tab), or through the API:

   ```http
   PATCH /tenants/{tenant}/agents/{agent_id}
   {
     "language": "hi",
     "supported_languages": ["hi", "en"]
   }
   ```

   The default language always ends up first in `supported_languages`. To turn the multilingual path
   off again, send `"supported_languages": []`.
4. **Optional: a voice per language.** This must be a TTS provider config of the **same tenant**:
   another tenant's id is rejected as "not found". A provider used as an override can't be deleted
   while an agent references it.

   ```json
   { "tts_config_by_language": { "hi": "<provider_configs.id of a Hindi voice>" } }
   ```

   Languages without an override use the agent's own TTS provider, with the session language passed on
   every request.
5. **Optional: a greeting per language.** The greeting for the default language is spoken when the
   call connects. Workflow variables such as `{{agent_name}}` work as usual. If there is no entry for
   the default language, the agent's normal greeting is used.

   ```json
   { "greeting_by_language": { "hi": "नमस्ते, मैं {{agent_name}} बोल रही हूँ।", "en": "Hi, this is {{agent_name}}." } }
   ```

## How language switching works

The session starts in the default language. After each caller utterance, the STT result we already
have (no extra call) says which language was spoken:

- **By default the reply follows the caller's latest language:** one confident utterance
  (confidence ≥ 0.70) in another supported language switches the session, and the next reply uses
  that language and its voice. Set `VOICEAI_LANG_SWITCH_STREAK=2` (or higher) to require that many
  consecutive utterances instead, so one misheard utterance can't flip the call; the first confident
  utterance of a call always switches immediately.
- **Hinglish:** when Hindi is supported, an utterance counts as Hindi if it contains any Devanagari, or
  if at least 30% of its words are tagged Hindi (Deepgram tags every word). Otherwise it counts as
  English. So mixed sentences keep a Hindi session in Hindi; an English-only sentence switches back.
- **Unsupported or low-confidence utterances change nothing.**
- **Short replies** such as "haan", "ji" or "sí" (0.45–1.0 s of audio) are transcribed. They are kept
  only if they are confidently (≥ 0.80) in the current session language. They can never switch the
  language. Anything under 0.45 s is still dropped (Whisper hallucinates "Thank you." on blips).
  Single-language agents keep the 1.0 s minimum.

When the language switches, these follow it from the next turn:
- the reply language (the "Reply in Hindi …" line is rebuilt every turn);
- the voice (the override for that language, or the base voice with the new `language`);
- fillers, goodbyes and fallback lines;
- the guardrail lexicon.

The agent's own `farewell_message` and `transfer_announcement` are always spoken verbatim.

### Tuning

All thresholds can be overridden with environment variables on the conversation service (restart it,
no code change):

| Variable | Default | Meaning |
|---|---|---|
| `VOICEAI_LANG_MIN_CONFIDENCE` | `0.70` | Minimum detection confidence that counts toward a switch |
| `VOICEAI_LANG_SWITCH_STREAK` | `1` | Consecutive utterances needed to switch after the first. `1`: the reply follows the caller's latest language. `2`+: one misheard utterance can't flip it |
| `VOICEAI_LANG_HI_WORD_SHARE` | `0.30` | Share of Hindi-tagged words that makes an utterance Hindi |
| `VOICEAI_LANG_SHORT_MIN_S` | `0.45` | Utterances shorter than this are always dropped (multilingual agents) |
| `VOICEAI_LANG_SHORT_MIN_CONFIDENCE` | `0.80` | Confidence a 0.45–1.0 s utterance needs to be kept |

## Guardrails and escalation

The frustration/abuse lexicon behind `escalation_threshold` is chosen by session language. A Hindi
session is checked against English, Devanagari Hindi and romanised Hindi together. A language with no
lexicon (anything except en and hi today) is **not checked at all**: it never escalates by mistake, and
the conversation service logs `guardrail and booking-claim checks skipped` once per call. The same
applies to the booking-claim backstop.

## Post-call outputs

Sentiment reasons, call summaries and extracted workflow variables are always written in **English**
(names transliterated), whatever language the call was in, so dashboards stay comparable. The languages
the caller actually spoke are stored on `calls.detected_languages`, in order of first appearance.

## Knowledge bases

- **Chunking** is language-aware. Documents in Chinese, Japanese, Thai, Lao, Khmer or Myanmar are sized
  by characters instead of words. The document's `language` decides; if it isn't set, the script is
  sniffed. Other languages, including Hindi, chunk by words as before.
- **Embeddings.** The default `nomic-embed-text` is English-centric. For knowledge bases with
  non-English content, use OpenAI `text-embedding-3-small` with `dimensions=768`. It is multilingual,
  already supported, and keeps the existing `VECTOR(768)` column, so no schema change is needed.

### Re-indexing a knowledge base after changing its embedding model

Vectors from different models can't be compared. After switching, every document must be re-embedded:

1. Create an embedding provider config: engine `openai`, model `text-embedding-3-small`. The
   provider requests 768 dimensions for `text-embedding-3-*` models automatically.
2. Point the knowledge base at it (`PATCH` the knowledge base's `embedding_config_id`).
3. Queue every document of that knowledge base for re-ingestion. The ingestion worker re-reads each
   document from storage, re-chunks it (language-aware) and re-embeds it, replacing the old chunks:

   ```sql
   -- As the platform role, or under the KB's tenant scope.
   INSERT INTO kb_ingestion_jobs (document_id, kb_id)
   SELECT id, kb_id FROM kb_documents
    WHERE kb_id = '<knowledge base id>' AND deleted_at IS NULL;
   ```

4. Until a document's job finishes, its old chunks are still embedded with the old model, and they
   rank poorly against queries embedded with the new one. Re-index outside peak hours, or build a new
   knowledge base and swap it into the agent when it is ready.

Retrieval scoring itself is unchanged.

## Adding a language

1. Add a row to `LANGUAGES` in `libs/config_sdk/languages.py`: the code, the English and native
   names, the Kokoro `lang_code` if any, whether Deepgram `multi` covers it, and an optional script
   hint for the LLM.
2. Optional: add `services/conversation/i18n/<code>.py` with `STRINGS` and `TOOL_FILLERS` (copy
   `en.py`), and register it in `services/conversation/i18n/__init__.py`. Missing strings fall back to
   English.
3. Optional: add a guardrail lexicon in `services/conversation/guardrails.py`. Without one, guardrails
   are skipped for that language (fail safe).
4. If a TTS engine supports it, add it to that engine's set in `tts_languages()` (same file as
   step 1).

## Troubleshooting

| Symptom | Cause |
|---|---|
| Agent always replies in the default language | `supported_languages` isn't set (single-language agent), or STT detection is off: Deepgram model not nova-2/3, or a Whisper `.en` model (error logged once). |
| Agent replies in Hindi but with an English accent | The base voice is English-native. Add a Hindi voice override (`tts_config_by_language`). |
| Short "haan" replies are ignored | They are only kept in the current session language at ≥ 0.80. Check `VOICEAI_LANG_SHORT_MIN_CONFIDENCE`. |
| Language flips too eagerly or too slowly | Tune `VOICEAI_LANG_SWITCH_STREAK` / `VOICEAI_LANG_MIN_CONFIDENCE`. Every switch is logged as `Session language X -> Y`. |
| "TTS override … rejected" in conversation logs | The override row isn't a TTS config of the agent's tenant; it is ignored and the base voice is used. |
