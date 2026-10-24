"""Hand-written part of docs/knowledge-env-vars.md: the intro, the grouping, and a one-line meaning per variable.
The names, defaults, types and file:line come from the code (gen_knowledge_env_doc.py); only meanings live here.
A variable with no entry shows as "(undocumented)" under "Other"; add a line here when you add a variable."""

from __future__ import annotations

SEC = "SECURITY"
BOTH = "SET ON BOTH CONFIG AND KNOWLEDGE"

INTRO = """
# Knowledge service environment variables

Every environment variable read by the knowledge path: `services/knowledge`, `libs/knowledge_sdk`, `libs/egress_guard.py`
(when present), `services/conversation` (all of it, including the knowledge tool settings), and the three shared modules
that carry the JWT secret and the secret-encryption key. **This file is generated**: names, defaults, types and
`file:line` come from the code, meanings from `scripts/knowledge_env_meanings.py`. Do not edit the tables by hand.

    python scripts/gen_knowledge_env_doc.py          # rewrite this file after adding, renaming or removing a variable
    python scripts/gen_knowledge_env_doc.py --check  # exit 1 if this file differs from a fresh render; the test only checks names

The Default column is read from the code, so it is the value in force when the variable is unset. A bad value for the
parsers' `env_*` helpers (empty, text, inf, nan, out of range) falls back to the default with one warning; the other
reads are plain `int()`/`float()` and fail at import or first use on a bad value.

Marks: **[SECURITY]** a switch that weakens or enables a security boundary; **[SET ON BOTH CONFIG AND KNOWLEDGE]** the value
must be identical in every service that reads it, or tokens or stored secrets stop working across services.

## Must be identical across services

| Variable | Why |
|---|---|
| `JWT_SECRET` | Config signs the tokens; Knowledge (and the DID and Campaigns services) verify them. A mismatch rejects every request with 401. |
| `SECRET_ENCRYPTION_KEY` | Config encrypts provider API keys at rest; Knowledge (embedding, rerank, enrichment keys) and Conversation decrypt them. A mismatch makes stored keys unreadable. |
| `POSTGRES_DSN` | Same database (and the same role model, see `docs/rls-tenant-isolation.md`). Per-service pool sizes are separate. |
| `REDIS_URL` | Same Redis: the agent-has-knowledge flag cache is shared by Config and Conversation, and the Conversation service reads what Knowledge writes. |
""".strip()

GROUPS = [
    ("Retrieval deadlines and stage timeouts", "The hot-path budget. Each stage has its own timeout under one hard deadline; a stage that misses falls back instead of holding the call."),
    ("Admission limiter and connection pool", "Protects the database pool: past the caps a caller gets a typed `unavailable` quickly."),
    ("Plan cache, query and embedding caches", "In-process caches on the retrieval path (every key carries the tenant)."),
    ("Retrieval confidence and lexical search", "Scoring thresholds and the lexical branch's term selection."),
    ("Rerank (opt-in)", "Off unless `KNOWLEDGE_RERANKER` names a provider and an agent turns `rerank` on."),
    ("Logging and retention", "`retrieval_logs`: written off the response path, dropped (and counted) rather than slowing retrieval."),
    ("Ingestion queue and worker", "The Postgres-backed ingestion queue and the worker process."),
    ("Chunking", "How parsed documents become chunks."),
    ("Parsers and sandbox", "Parsing runs in a contained child process with hard resource limits; `KB_PARSE_*` are those limits."),
    ("Enrichment (opt-in)", "LLM-written one-line context per chunk. Off unless enabled and the tenant is on the list."),
    ("Re-embed", "Moving a KB to a different embedding model."),
    ("Knowledge tool and tool guard (Conversation service)", "How the Conversation service presents and guards retrieved text."),
    ("Service wiring, storage and shared secrets", "Where services find each other, and the secrets they share."),
    ("Conversation service (voice pipeline, not knowledge-specific)", "Read by `services/conversation`; listed so the reference is complete."),
    ("Other", "Variables with no hand-written group yet."),
]

_R, _A, _C, _L, _RR, _LG, _Q, _CH, _P, _E, _RE, _T, _S, _V = (g for g, _ in GROUPS[:14])

# name -> (group, meaning, flags)
M: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # retrieval
    "KNOWLEDGE_RETRIEVAL_DEADLINE_MS": (_R, "Hard deadline for one retrieval (the design's p99 target); stages are cancelled at it.", ()),
    "KNOWLEDGE_DENSE_GRACE_MS": (_R, "Once keyword search has matches, wait at most this long for a dense branch still embedding, then return the keyword results (0 = do not wait; unset = off, wait for the embedder as usual). Each cut counts against the embedder's circuit breaker.", ()),
    "KNOWLEDGE_EMBED_TIMEOUT_MS": (_R, "Query-embedding timeout; on a miss retrieval continues lexical-only (`embedding_unavailable`).", ()),
    "KNOWLEDGE_DENSE_TIMEOUT_MS": (_R, "Vector search timeout.", ()),
    "KNOWLEDGE_LEXICAL_TIMEOUT_MS": (_R, "Lexical (full-text) search timeout.", ()),
    "KNOWLEDGE_RERANK_TIMEOUT_MS": (_R, "Stage budget for reranking inside the retrieval deadline.", ()),
    "KNOWLEDGE_RETRIEVE_TIMEOUT_S": (_R, "Outer guard on `/internal/retrieve`, above the stage deadlines; on expiry the service answers `unavailable` itself.", ()),
    "KNOWLEDGE_RETRIEVAL_MAX_QUERY_CHARS": (_R, "Queries are cut to this many characters before anything touches them.", ()),
    "KNOWLEDGE_BREAKER_FAILURES": (_R, "Consecutive failures before a dependency's circuit breaker opens (per embedding config, and rerank).", ()),
    "KNOWLEDGE_BREAKER_COOLDOWN_S": (_R, "Seconds an open breaker waits before one half-open probe.", ()),
    "KNOWLEDGE_MAX_BODY_BYTES": (_R, "Request-body cap for `/internal/retrieve` and `/internal/prewarm` (413 above it).", ()),
    # limiter / pool
    "KNOWLEDGE_RETRIEVAL_MAX_INFLIGHT": (_A, "Retrievals admitted at once per process; default derives from the pool size.", ()),
    "KNOWLEDGE_RETRIEVAL_TENANT_MAX_INFLIGHT": (_A, "Admitted retrievals per tenant (default a third of the process cap), so one tenant cannot take every slot.", ()),
    "KNOWLEDGE_RETRIEVAL_QUEUE_WAIT_MS": (_A, "How long a refused arrival may queue for a slot before getting `overloaded`.", ()),
    "KNOWLEDGE_POOL_ACQUIRE_TIMEOUT_MS": (_A, "Waiting longer than this for a pool connection counts as overload, not a slow dependency.", ()),
    "KNOWLEDGE_DB_POOL_MAX": (_A, "Maximum pooled Postgres connections for the knowledge service.", ()),
    "KNOWLEDGE_DB_POOL_MIN": (_A, "Warm connections kept open (clamped to the maximum), so a burst does not open connections inside the acquire bound.", ()),
    "KNOWLEDGE_DB_POOL_IDLE_S": (_A, "Close connections idle this long; 0 keeps them (asyncpg's own idle close is disabled).", ()),
    "KNOWLEDGE_HNSW_MAX_SCAN_TUPLES": (_A, "pgvector `hnsw.max_scan_tuples` for the filtered vector scan (pgvector 0.8+ iterative scan).", ()),
    # caches
    "KNOWLEDGE_PLAN_TTL_S": (_C, "Retrieval-plan cache TTL (agent, KBs, policy, prompt-mode documents); stale-while-revalidate after half of it.", ()),
    "KNOWLEDGE_PLAN_CACHE_MAX": (_C, "Maximum cached plans per process.", ()),
    "KNOWLEDGE_AGENT_KB_FLAG_TTL_S": (_C, "Redis TTL of the per-agent \"has an enabled KB\" flag the Conversation service checks.", ()),
    "KNOWLEDGE_LEXICAL_MAX_TERM_DF": (_L, "Terms present in more chunks than this are treated as common words and not searched alone.", ()),
    # confidence / lexical
    "KNOWLEDGE_LOW_CONFIDENCE_TOP": (_L, "Top score below this (and a small gap) marks the answer `low_confidence`.", ()),
    "KNOWLEDGE_LOW_CONFIDENCE_GAP": (_L, "Gap between the top two scores below which a weak top hit is `low_confidence`.", ()),
    "KNOWLEDGE_LEXICAL_MIN_SCORE": (_L, "Lexical hits scoring below this are dropped.", ()),
    "KNOWLEDGE_LEXICAL_CONFIDENT_SCORE": (_L, "A top hit supported only by lexical evidence is `low_confidence` unless it scores at least this.", ()),
    # rerank
    "KNOWLEDGE_RERANKER": (_RR, "`none` (default) | `cohere` | `voyage`. Unknown values log a warning and mean none. Sends query and candidate text to a third party when on.", (SEC,)),
    "KNOWLEDGE_RERANK_URL": (_RR, "Override the provider endpoint; must be https, carry no credentials and be an allowed host.", (SEC,)),
    "KNOWLEDGE_RERANK_ALLOWED_HOSTS": (_RR, "Extra hosts (comma-separated) the rerank URL may point at, beyond the provider's own.", (SEC,)),
    "KNOWLEDGE_RERANK_API_KEY_REF": (_RR, "Secret reference (for example `env:NAME`) holding the rerank provider key; never the key itself.", (SEC,)),
    "KNOWLEDGE_RERANK_MODEL": (_RR, "Rerank model name; provider default when unset.", ()),
    "KNOWLEDGE_RERANK_TOP_N": (_RR, "Candidates sent to the reranker.", ()),
    "KNOWLEDGE_RERANK_DOC_CHARS": (_RR, "Characters of each candidate sent.", ()),
    "KNOWLEDGE_RERANK_MAX_PAYLOAD_CHARS": (_RR, "Cap on the whole request payload.", ()),
    "KNOWLEDGE_RERANK_HTTP_TIMEOUT_MS": (_RR, "HTTP timeout to the provider.", ()),
    "KNOWLEDGE_RERANK_DECISIVE_SCORE": (_RR, "Skip reranking when the top fused score is at least this and the gap below holds.", ()),
    "KNOWLEDGE_RERANK_DECISIVE_GAP": (_RR, "Gap to the runner-up that makes the top hit decisive.", ()),
    "KNOWLEDGE_RERANK_TENANT_RATE": (_RR, "Per-tenant rerank calls per second; default derives from the platform rate, share and process count.", ()),
    "KNOWLEDGE_RERANK_TENANT_BURST": (_RR, "Per-tenant rerank burst size.", ()),
    "KNOWLEDGE_RERANK_PLATFORM_RATE": (_RR, "Platform-wide rerank calls per second across all replicas (the provider quota).", ()),
    "KNOWLEDGE_RERANK_PROCESSES": (_RR, "Knowledge-service processes sharing the platform quota; set to the replica count.", ()),
    "KNOWLEDGE_RERANK_TENANT_SHARE": (_RR, "Fraction of the platform quota one tenant may use.", ()),
    "KNOWLEDGE_RERANK_VALVE_TENANTS": (_RR, "Distinct tenants failing within the window that opens the platform-wide rerank valve.", ()),
    "KNOWLEDGE_RERANK_VALVE_WINDOW_S": (_RR, "Window for counting failing tenants.", ()),
    "KNOWLEDGE_RERANK_VALVE_COOLDOWN_S": (_RR, "How long the valve stays open before one probe.", ()),
    # logging
    "KNOWLEDGE_RETRIEVAL_LOG_RATE_PER_S": (_LG, "Per-tenant `retrieval_logs` rows per second (token bucket); over-limit rows are dropped and counted.", ()),
    "KNOWLEDGE_RETRIEVAL_LOG_BURST": (_LG, "Per-tenant bucket size.", ()),
    "KNOWLEDGE_RETRIEVAL_LOG_QUEUE": (_LG, "Writer queue length; a full queue drops rows (counted at `/internal/ops`).", ()),
    "KNOWLEDGE_RETRIEVAL_LOG_RETENTION_DAYS": (_LG, "Rows older than this are purged hourly.", ()),
    # ingestion
    "KB_INGEST_PER_TENANT_CAP": (_Q, "Ingestion jobs one tenant may have running at once.", ()),
    "KNOWLEDGE_MAX_UPLOAD_BYTES": (_Q, "Upload size cap (413 above it).", ()),
    "KNOWLEDGE_STORAGE_ROOT": (_Q, "Directory for uploaded files (local storage, single node).", ()),
    # chunking
    "KNOWLEDGE_CHUNKER": (_CH, "`auto` (default: fixed windows for headed prose, Q&A and tables keep their routes) | `structure` | `recursive` | `fixed`; read per call.", ()),
    "KNOWLEDGE_CHUNK_BUDGET_S": (_CH, "Wall-clock budget for chunking one document; past it the fixed-window fallback runs.", ()),
    "KNOWLEDGE_MAX_CHUNKS_PER_DOC": (_CH, "More chunks than this fails the document permanently (\"document too large to index\").", ()),
    # parsers
    "KNOWLEDGE_BINARY_PARSERS": (_P, "Default off; set 1 to enable PDF/Office parsing, see docs/knowledge-parser-sandbox.md. Text, markdown, HTML and CSV always work.", (SEC,)),
    "KNOWLEDGE_KEEP_COLLAPSED_CONTENT": (_P, "HTML closed disclosures (details, collapsed panels) are dropped by default; set 1 to keep them.", (SEC,)),
    "KNOWLEDGE_INDEX_SPEAKER_NOTES": (_P, "Opt in to indexing PPTX speaker notes (dropped by default: a common place to hide instructions).", (SEC,)),
    "KB_PARSE_MAX_BYTES": (_P, "Largest file the parser will open.", ()),
    "KB_PARSE_MAX_DECOMPRESSED": (_P, "Decompressed-size cap (zip-bomb guard).", ()),
    "KB_PARSE_MAX_PAGES": (_P, "Page cap.", ()),
    "KB_PARSE_MAX_CHARS": (_P, "Extracted-text cap.", ()),
    "KB_PARSE_MAX_TABLE_ROWS": (_P, "Table row cap.", ()),
    "KB_PARSE_MAX_CELLS": (_P, "Table cell cap (rows x columns, padding included), checked before building.", ()),
    "KB_PARSE_MAX_COLUMNS": (_P, "Columns per table row (a wide file is rejected before padding rows).", ()),
    "KB_PARSE_MAX_PARAGRAPHS": (_P, "Paragraph elements in DOCX/PPTX, counted by streaming before parsing.", ()),
    "KB_PARSE_MAX_ELEMENTS": (_P, "HTML element cap.", ()),
    "KB_PARSE_MAX_OOXML_ELEMENTS": (_P, "XML element cap inside Office packages.", ()),
    "KB_PARSE_PDF_PAGE_BYTES": (_P, "Per-page content-stream cap for PDFs.", ()),
    "KB_PARSE_PDF_BUDGET_S": (_P, "Time budget for the PDF text pass.", ()),
    "KB_PARSE_TIMEOUT_S": (_P, "Wall-clock limit for the whole parse child process; it is killed after this.", ()),
    "KB_PARSE_MEMORY_MB": (_P, "RSS cap for the parse child, enforced by the parent's watchdog on every platform (plus RLIMIT_AS on Linux).", ()),
    "KB_PARSE_NPROC_LIMIT": (_P, "Process-count limit applied to the sandbox child (Linux).", ()),
    "KB_PARSE_NONCE": (_P, "Internal: per-run nonce the parent passes to the child to tell its diagnostics from document text. Not for operators.", ()),
    "PATH": (_P, "Passed through to the parse child (and used to find optional OCR binaries); the child gets no other parent environment.", ()),
    # enrichment
    "KNOWLEDGE_ENRICH_CHUNKS": (_E, "Master switch (set 1) for LLM chunk enrichment; also needs the tenant in `KNOWLEDGE_ENRICH_TENANTS`. Sends chunk text to an LLM.", (SEC,)),
    "KNOWLEDGE_ENRICH_TENANTS": (_E, "Comma-separated tenant ids or slugs allowed to use enrichment; empty means nobody.", (SEC,)),
    "KNOWLEDGE_ENRICH_TIMEOUT_S": (_E, "Timeout for one enrichment LLM call.", ()),
    "KNOWLEDGE_ENRICH_CONCURRENCY": (_E, "Concurrent enrichment calls per document.", ()),
    "KNOWLEDGE_ENRICH_MAX_CHUNKS_PER_DOC": (_E, "Chunks past this are embedded plain.", ()),
    "KNOWLEDGE_ENRICH_MAX_TOKENS_PER_DOC": (_E, "Estimated token budget across one document.", ()),
    "KNOWLEDGE_ENRICH_DOC_DEADLINE_S": (_E, "Time budget for enriching one document.", ()),
    "KNOWLEDGE_ENRICH_MAX_CALLS_PER_TENANT_DAY": (_E, "Per-tenant enrichment calls per UTC day (in-process).", ()),
    # reembed
    "KNOWLEDGE_REEMBED_MAX_SWAP_CHUNKS": (_RE, "Largest KB the re-embed swap will run in one transaction without `--allow-long-swap` (it locks an HNSW-indexed table).", ()),
    # conversation knowledge
    "KNOWLEDGE_TOOL_MAX_TOKENS": (_T, "Ceiling on retrieved text handed to the LLM (words). The server already caps prompt-mode documents at half of `max_tokens`; this bounds the whole result.", ()),
    "KNOWLEDGE_TOOL_MAX_CHARS": (_T, "Character ceiling as well, since words are no bound for CJK or minified text.", ()),
    "KNOWLEDGE_TAINT_BLOCKS_WRITES": (_T, "Default on: after a document lookup in a turn, state-changing tool calls are refused that turn (a planted passage cannot steer a write). Off removes that guard.", (SEC,)),
    # wiring
    "KNOWLEDGE_SERVICE_URL": (_S, "Where the Conversation service reaches the knowledge service.", ()),
    "CONFIG_SERVICE_URL": (_S, "Config service base URL (Conversation's login and config fetch).", ()),
    "CONFIG_SERVICE_EMAIL": (_S, "Service-account email the Conversation service logs in with.", ()),
    "CONFIG_SERVICE_PASSWORD": (_S, "Service-account password.", (SEC,)),
    "TOOLEXEC_SERVICE_URL": (_S, "Tool execution service base URL.", ()),
    "POSTGRES_DSN": (_S, "Postgres connection string; production runs the pool as the RLS-enforced `yuviz_app` role.", (SEC, BOTH)),
    "REDIS_URL": (_S, "Redis for the agent-KB flag cache and shared caches.", (BOTH,)),
    "JWT_SECRET": (_S, "HMAC secret for access tokens; required at import.", (SEC, BOTH)),
    "SECRET_ENCRYPTION_KEY": (_S, "Fernet key for provider API keys stored in the database.", (SEC, BOTH)),
    "LISTEN_HOST": (_S, "Bind address of the Conversation gRPC server.", ()),
    "OLLAMA_BASE_URL": (_S, "Base URL of the Ollama embedding server (when read by the code).", ()),
    "KNOWLEDGE_PROVIDER_ALLOWED_HOSTS": (_S, "Allowed hosts for tenant-configured provider URLs (egress guard, when present).", (SEC,)),
    # conversation
    "OPENAI_API_KEY": (_V, "Fallback key for call-sentiment scoring.", (SEC,)),
    "VOICEAI_SENTIMENT_API_KEY": (_V, "Key for call-sentiment scoring; unset disables it.", (SEC,)),
    "VOICEAI_SENTIMENT_DISABLED": (_V, "Set to disable sentiment scoring.", ()),
    "VOICEAI_SENTIMENT_MAX_TURNS": (_V, "Transcript turns sent for scoring.", ()),
    "VOICEAI_SENTIMENT_MODEL": (_V, "Sentiment model name.", ()),
    "VOICEAI_SENTIMENT_TIMEOUT": (_V, "Sentiment call timeout (s).", ()),
    "VOICEAI_SENTIMENT_URL": (_V, "Sentiment provider base URL.", ()),
    "VOICEAI_LLM_MODEL": (_V, "Dev override: LLM model name.", ()),
    "VOICEAI_LLM_SYSTEM": (_V, "Dev override: LLM system prompt.", ()),
    "VOICEAI_LLM_TEMPERATURE": (_V, "Dev override: LLM temperature.", ()),
    "VOICEAI_LLM_TIMEOUT": (_V, "Dev override: LLM timeout (s).", ()),
    "VOICEAI_LLM_URL": (_V, "Dev override: LLM base URL.", ()),
    "VOICEAI_STT_DEVICE": (_V, "Dev override: STT device.", ()),
    "VOICEAI_STT_LANGUAGE": (_V, "Dev override: STT language.", ()),
    "VOICEAI_STT_MODEL": (_V, "Dev override: STT model.", ()),
    "VOICEAI_TTS_ENGINE": (_V, "Dev override: TTS engine.", ()),
    "VOICEAI_TTS_KOKORO_SPEED": (_V, "Kokoro speech speed.", ()),
    "VOICEAI_TTS_LANG_CODE": (_V, "Kokoro language code.", ()),
    "VOICEAI_TTS_MACOS_WPM": (_V, "macOS TTS words per minute.", ()),
    "VOICEAI_TTS_VOICE": (_V, "Dev override: TTS voice.", ()),
    "VOICEAI_LANG_HI_WORD_SHARE": (_V, "Language detection: share of Hindi words that decides Hindi.", ()),
    "VOICEAI_LANG_MIN_CONFIDENCE": (_V, "Language detection: minimum confidence to switch.", ()),
    "VOICEAI_LANG_SHORT_MIN_CONFIDENCE": (_V, "Language detection: minimum confidence for short utterances.", ()),
    "VOICEAI_LANG_SHORT_MIN_CONFIDENCE_HI": (_V, "Language detection: short-utterance confidence for Hindi.", ()),
    "VOICEAI_LANG_SHORT_MIN_S": (_V, "Language detection: what counts as a short utterance (s).", ()),
    "VOICEAI_LANG_SWITCH_STREAK": (_V, "Language detection: consecutive detections needed to switch.", ()),
    "VOICEAI_STT_SHORT_MIN_SPEECH_CONFIDENCE": (_V, "Language detection: STT confidence floor for short utterances.", ()),
}

OUTRO = """
## Test and CI only

`KNOWLEDGE_GATE_MARGIN_MS` (the CI deadline margin in `test_retrieval_gates.py`) and `OLLAMA_URL` (where the Ollama-backed
tests look for a model) are read by tests only and are not part of the service's configuration.
""".strip()


def group_of(name: str) -> str:
    if name in M:
        return M[name][0]
    return GROUPS[-1][0]


def meaning_of(name: str) -> tuple[str, tuple[str, ...]]:
    if name in M:
        return M[name][1], M[name][2]
    return "(undocumented)", ()
