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

## Retrieval deadlines and stage timeouts

The hot-path budget. Each stage has its own timeout under one hard deadline; a stage that misses falls back instead of holding the call.

| Variable | Default | Type | Meaning | Read at |
|---|---|---|---|---|
| `KNOWLEDGE_BREAKER_COOLDOWN_S` | `20` | float | Seconds an open breaker waits before one half-open probe. | `services/knowledge/resilience.py:86` |
| `KNOWLEDGE_BREAKER_FAILURES` | `5` | float | Consecutive failures before a dependency's circuit breaker opens (per embedding config, and rerank). | `services/knowledge/resilience.py:85` |
| `KNOWLEDGE_DENSE_GRACE_MS` | `None` | str | Once keyword search has matches, wait at most this long for a dense branch still embedding, then return the keyword results (0 = do not wait; unset = off, wait for the embedder as usual). Each cut counts against the embedder's circuit breaker. | `services/knowledge/resilience.py:84` |
| `KNOWLEDGE_DENSE_TIMEOUT_MS` | `120` | float | Vector search timeout. | `services/knowledge/resilience.py:80` |
| `KNOWLEDGE_EMBED_TIMEOUT_MS` | `None` | str | Query-embedding timeout; on a miss retrieval continues lexical-only (`embedding_unavailable`). | `services/knowledge/resilience.py:79` |
| `KNOWLEDGE_LEXICAL_TIMEOUT_MS` | `120` | float | Lexical (full-text) search timeout. | `services/knowledge/resilience.py:81` |
| `KNOWLEDGE_MAX_BODY_BYTES` | `65536` | int | Request-body cap for `/internal/retrieve` and `/internal/prewarm` (413 above it). | `services/knowledge/body_limit.py:13` |
| `KNOWLEDGE_RERANK_TIMEOUT_MS` | `120` | float | Stage budget for reranking inside the retrieval deadline. | `services/knowledge/resilience.py:82` |
| `KNOWLEDGE_RETRIEVAL_DEADLINE_MS` | `400` | float | Hard deadline for one retrieval (the design's p99 target); stages are cancelled at it. | `services/knowledge/resilience.py:83` |
| `KNOWLEDGE_RETRIEVAL_MAX_QUERY_CHARS` | `1000` | float | Queries are cut to this many characters before anything touches them. | `services/knowledge/resilience.py:95` |
| `KNOWLEDGE_RETRIEVE_TIMEOUT_S` | `_DEFAULT_RETRIEVE_TIMEOUT_S` | float | Outer guard on `/internal/retrieve`, above the stage deadlines; on expiry the service answers `unavailable` itself. | `services/knowledge/routers/retrieve.py:37` |

## Admission limiter and connection pool

Protects the database pool: past the caps a caller gets a typed `unavailable` quickly.

| Variable | Default | Type | Meaning | Read at |
|---|---|---|---|---|
| `KNOWLEDGE_DB_POOL_IDLE_S` | `0` | float | Close connections idle this long; 0 keeps them (asyncpg's own idle close is disabled). | `services/knowledge/db.py:24` |
| `KNOWLEDGE_DB_POOL_MAX` | `20` | int | Maximum pooled Postgres connections for the knowledge service. | `services/knowledge/db.py:13`, `services/knowledge/resilience.py:77` |
| `KNOWLEDGE_DB_POOL_MIN` | `10` | int | Warm connections kept open (clamped to the maximum), so a burst does not open connections inside the acquire bound. | `services/knowledge/db.py:18` |
| `KNOWLEDGE_HNSW_MAX_SCAN_TUPLES` | `20000` | int | pgvector `hnsw.max_scan_tuples` for the filtered vector scan (pgvector 0.8+ iterative scan). | `services/knowledge/db.py:30`, `services/knowledge/vector_repository.py:19` |
| `KNOWLEDGE_POOL_ACQUIRE_TIMEOUT_MS` | `60` | float | Waiting longer than this for a pool connection counts as overload, not a slow dependency. | `services/knowledge/resilience.py:94` |
| `KNOWLEDGE_RETRIEVAL_MAX_INFLIGHT` | `derived (see code)` | float | Retrievals admitted at once per process; default derives from the pool size. | `services/knowledge/resilience.py:76` |
| `KNOWLEDGE_RETRIEVAL_QUEUE_WAIT_MS` | `150` | float | How long a refused arrival may queue for a slot before getting `overloaded`. | `services/knowledge/resilience.py:93` |
| `KNOWLEDGE_RETRIEVAL_TENANT_MAX_INFLIGHT` | `max(1, max_inflight // 3)` | float | Admitted retrievals per tenant (default a third of the process cap), so one tenant cannot take every slot. | `services/knowledge/resilience.py:92` |

## Plan cache, query and embedding caches

In-process caches on the retrieval path (every key carries the tenant).

| Variable | Default | Type | Meaning | Read at |
|---|---|---|---|---|
| `KNOWLEDGE_AGENT_KB_FLAG_TTL_S` | `604800` | int | Redis TTL of the per-agent "has an enabled KB" flag the Conversation service checks. | `services/knowledge/cache.py:43` |
| `KNOWLEDGE_PLAN_CACHE_MAX` | `2048` | int | Maximum cached plans per process. | `services/knowledge/retrieval_plan.py:79` |
| `KNOWLEDGE_PLAN_TTL_S` | `30` | float | Retrieval-plan cache TTL (agent, KBs, policy, prompt-mode documents); stale-while-revalidate after half of it. | `services/knowledge/retrieval_plan.py:78` |

## Retrieval confidence and lexical search

Scoring thresholds and the lexical branch's term selection.

| Variable | Default | Type | Meaning | Read at |
|---|---|---|---|---|
| `KNOWLEDGE_LEXICAL_CONFIDENT_SCORE` | `0.3` | float | A top hit supported only by lexical evidence is `low_confidence` unless it scores at least this. | `services/knowledge/resilience.py:90` |
| `KNOWLEDGE_LEXICAL_MAX_TERM_DF` | `2000` | int | Terms present in more chunks than this are treated as common words and not searched alone. | `services/knowledge/vector_repository.py:110` |
| `KNOWLEDGE_LEXICAL_MIN_SCORE` | `0.05` | float | Lexical hits scoring below this are dropped. | `services/knowledge/resilience.py:89` |
| `KNOWLEDGE_LOW_CONFIDENCE_GAP` | `0.02` | float | Gap between the top two scores below which a weak top hit is `low_confidence`. | `services/knowledge/resilience.py:88` |
| `KNOWLEDGE_LOW_CONFIDENCE_TOP` | `0.5` | float | Top score below this (and a small gap) marks the answer `low_confidence`. | `services/knowledge/resilience.py:87` |

## Rerank (opt-in)

Off unless `KNOWLEDGE_RERANKER` names a provider and an agent turns `rerank` on.

| Variable | Default | Type | Meaning | Read at |
|---|---|---|---|---|
| `KNOWLEDGE_RERANKER` | `(see code)` | str | `none` (default) | `cohere` | `voyage`. Unknown values log a warning and mean none. Sends query and candidate text to a third party when on. **[SECURITY]** | `services/knowledge/reranker.py:313` |
| `KNOWLEDGE_RERANK_ALLOWED_HOSTS` | `(see code)` | str | Extra hosts (comma-separated) the rerank URL may point at, beyond the provider's own. **[SECURITY]** | `services/knowledge/reranker.py:321` |
| `KNOWLEDGE_RERANK_API_KEY_REF` | `(see code)` | str | Secret reference (for example `env:NAME`) holding the rerank provider key; never the key itself. **[SECURITY]** | `services/knowledge/reranker.py:323` |
| `KNOWLEDGE_RERANK_DECISIVE_GAP` | `0.1` | float | Gap to the runner-up that makes the top hit decisive. | `services/knowledge/reranker.py:78` |
| `KNOWLEDGE_RERANK_DECISIVE_SCORE` | `0.85` | float | Skip reranking when the top fused score is at least this and the gap below holds. | `services/knowledge/reranker.py:77` |
| `KNOWLEDGE_RERANK_DOC_CHARS` | `1200` | int | Characters of each candidate sent. | `services/knowledge/reranker.py:74` |
| `KNOWLEDGE_RERANK_HTTP_TIMEOUT_MS` | `95` | float | HTTP timeout to the provider. | `services/knowledge/reranker.py:76` |
| `KNOWLEDGE_RERANK_MAX_PAYLOAD_CHARS` | `20000` | int | Cap on the whole request payload. | `services/knowledge/reranker.py:75` |
| `KNOWLEDGE_RERANK_MODEL` | `(see code)` | str | Rerank model name; provider default when unset. | `services/knowledge/reranker.py:322` |
| `KNOWLEDGE_RERANK_PLATFORM_RATE` | `10` | float | Platform-wide rerank calls per second across all replicas (the provider quota). | `services/knowledge/reranker.py:94`, `services/knowledge/reranker.py:81` |
| `KNOWLEDGE_RERANK_PROCESSES` | `4` | int | Knowledge-service processes sharing the platform quota; set to the replica count. | `services/knowledge/reranker.py:95`, `services/knowledge/reranker.py:82` |
| `KNOWLEDGE_RERANK_TENANT_BURST` | `5` | int | Per-tenant rerank burst size. | `services/knowledge/reranker.py:80` |
| `KNOWLEDGE_RERANK_TENANT_RATE` | `derived` | float | Per-tenant rerank calls per second; default derives from the platform rate, share and process count. | `services/knowledge/reranker.py:99`, `services/knowledge/reranker.py:98` |
| `KNOWLEDGE_RERANK_TENANT_SHARE` | `0.25` | float | Fraction of the platform quota one tenant may use. | `services/knowledge/reranker.py:96`, `services/knowledge/reranker.py:83` |
| `KNOWLEDGE_RERANK_TOP_N` | `20` | int | Candidates sent to the reranker. | `services/knowledge/reranker.py:73` |
| `KNOWLEDGE_RERANK_URL` | `(see code)` | str | Override the provider endpoint; must be https, carry no credentials and be an allowed host. **[SECURITY]** | `services/knowledge/reranker.py:322` |
| `KNOWLEDGE_RERANK_VALVE_COOLDOWN_S` | `20` | float | How long the valve stays open before one probe. | `services/knowledge/resilience.py:275` |
| `KNOWLEDGE_RERANK_VALVE_TENANTS` | `3` | int | Distinct tenants failing within the window that opens the platform-wide rerank valve. | `services/knowledge/resilience.py:273` |
| `KNOWLEDGE_RERANK_VALVE_WINDOW_S` | `30` | float | Window for counting failing tenants. | `services/knowledge/resilience.py:274` |

## Logging and retention

`retrieval_logs`: written off the response path, dropped (and counted) rather than slowing retrieval.

| Variable | Default | Type | Meaning | Read at |
|---|---|---|---|---|
| `KNOWLEDGE_RETRIEVAL_LOG_BURST` | `20` | float | Per-tenant bucket size. | `services/knowledge/retrieval_log.py:46` |
| `KNOWLEDGE_RETRIEVAL_LOG_QUEUE` | `500` | int | Writer queue length; a full queue drops rows (counted at `/internal/ops`). | `services/knowledge/retrieval_log.py:50` |
| `KNOWLEDGE_RETRIEVAL_LOG_RATE_PER_S` | `5` | float | Per-tenant `retrieval_logs` rows per second (token bucket); over-limit rows are dropped and counted. | `services/knowledge/retrieval_log.py:45` |
| `KNOWLEDGE_RETRIEVAL_LOG_RETENTION_DAYS` | `30` | int | Rows older than this are purged hourly. | `services/knowledge/retrieval_log.py:47` |

## Ingestion queue and worker

The Postgres-backed ingestion queue and the worker process.

| Variable | Default | Type | Meaning | Read at |
|---|---|---|---|---|
| `KB_INGEST_PER_TENANT_CAP` | `2` | int | Ingestion jobs one tenant may have running at once. | `services/knowledge/ingestion_worker.py:55` |
| `KNOWLEDGE_DEFAULT_MAX_DOCUMENTS` | `10` | int | Live documents per tenant when the tenant has no `knowledge_max_documents` (409 above it). | `services/knowledge/quotas.py:14` |
| `KNOWLEDGE_DEFAULT_MAX_STORAGE_BYTES` | `10485760` | int | Stored bytes per tenant when the tenant has no `knowledge_max_storage_bytes` (413 above it). | `services/knowledge/quotas.py:15` |
| `KNOWLEDGE_MAX_UPLOAD_BYTES` | `2097152` | int | Upload size cap (413 above it). | `services/knowledge/routers/documents.py:35` |
| `KNOWLEDGE_STORAGE_ROOT` | `'./data/knowledge_documents'` | str | Directory for uploaded files (local storage, single node). | `services/knowledge/storage.py:26` |

## Chunking

How parsed documents become chunks.

| Variable | Default | Type | Meaning | Read at |
|---|---|---|---|---|
| `KNOWLEDGE_CHUNKER` | `'auto'` | str | `auto` (default: fixed windows for headed prose, Q&A and tables keep their routes) | `structure` | `recursive` | `fixed`; read per call. | `services/knowledge/chunker.py:52` |
| `KNOWLEDGE_CHUNK_BUDGET_S` | `60` | float | Wall-clock budget for chunking one document; past it the fixed-window fallback runs. | `services/knowledge/chunker.py:507` |
| `KNOWLEDGE_MAX_CHUNKS_PER_DOC` | `5000` | float | More chunks than this fails the document permanently ("document too large to index"). | `services/knowledge/chunker.py:507` |

## Parsers and sandbox

Parsing runs in a contained child process with hard resource limits; `KB_PARSE_*` are those limits.

| Variable | Default | Type | Meaning | Read at |
|---|---|---|---|---|
| `KB_PARSE_MAX_BYTES` | `52428800` | int | Largest file the parser will open. | `services/knowledge/parsers/limits.py:59` |
| `KB_PARSE_MAX_CELLS` | `2000000` | int | Table cell cap (rows x columns, padding included), checked before building. | `services/knowledge/parsers/limits.py:64` |
| `KB_PARSE_MAX_CHARS` | `10000000` | int | Extracted-text cap. | `services/knowledge/parsers/limits.py:62` |
| `KB_PARSE_MAX_COLUMNS` | `2000` | int | Columns per table row (a wide file is rejected before padding rows). | `services/knowledge/parsers/limits.py:65` |
| `KB_PARSE_MAX_DECOMPRESSED` | `268435456` | int | Decompressed-size cap (zip-bomb guard). | `services/knowledge/parsers/limits.py:61` |
| `KB_PARSE_MAX_ELEMENTS` | `500000` | int | HTML element cap. | `services/knowledge/parsers/limits.py:67` |
| `KB_PARSE_MAX_OOXML_ELEMENTS` | `2500000` | int | XML element cap inside Office packages. | `services/knowledge/parsers/limits.py:68` |
| `KB_PARSE_MAX_PAGES` | `500` | int | Page cap. | `services/knowledge/parsers/limits.py:60` |
| `KB_PARSE_MAX_PARAGRAPHS` | `800000` | int | Paragraph elements in DOCX/PPTX, counted by streaming before parsing. | `services/knowledge/parsers/limits.py:66` |
| `KB_PARSE_MAX_TABLE_ROWS` | `200000` | int | Table row cap. | `services/knowledge/parsers/limits.py:63` |
| `KB_PARSE_MEMORY_MB` | `1024` | int | RSS cap for the parse child, enforced by the parent's watchdog on every platform (plus RLIMIT_AS on Linux). | `services/knowledge/parsers/limits.py:72` |
| `KB_PARSE_NONCE` | `''` | str | Internal: per-run nonce the parent passes to the child to tell its diagnostics from document text. Not for operators. | `services/knowledge/parsers/_worker.py:22` |
| `KB_PARSE_NPROC_LIMIT` | `1` | int | Process-count limit applied to the sandbox child (Linux). | `services/knowledge/parsers/sandbox.py:218` |
| `KB_PARSE_PDF_BUDGET_S` | `25` | int | Time budget for the PDF text pass. | `services/knowledge/parsers/limits.py:70` |
| `KB_PARSE_PDF_PAGE_BYTES` | `2097152` | int | Per-page content-stream cap for PDFs. | `services/knowledge/parsers/limits.py:69` |
| `KB_PARSE_TIMEOUT_S` | `60` | int | Wall-clock limit for the whole parse child process; it is killed after this. | `services/knowledge/parsers/limits.py:71` |
| `KNOWLEDGE_BINARY_PARSERS` | `0` | int | Default off; set 1 to enable PDF/Office parsing, see docs/knowledge-parser-sandbox.md. Text, markdown, HTML and CSV always work. **[SECURITY]** | `services/knowledge/parsers/limits.py:21` |
| `KNOWLEDGE_INDEX_SPEAKER_NOTES` | `''` | str | Opt in to indexing PPTX speaker notes (dropped by default: a common place to hide instructions). **[SECURITY]** | `services/knowledge/parsers/limits.py:75` |
| `KNOWLEDGE_KEEP_COLLAPSED_CONTENT` | `''` | str | HTML closed disclosures (details, collapsed panels) are dropped by default; set 1 to keep them. **[SECURITY]** | `services/knowledge/parsers/limits.py:74` |
| `PATH` | `''` | str | Passed through to the parse child (and used to find optional OCR binaries); the child gets no other parent environment. | `services/knowledge/parsers/sandbox.py:68` |

## Enrichment (opt-in)

LLM-written one-line context per chunk. Off unless enabled and the tenant is on the list.

| Variable | Default | Type | Meaning | Read at |
|---|---|---|---|---|
| `KNOWLEDGE_ENRICH_CHUNKS` | `(unset)` | flag (0/1) | Master switch (set 1) for LLM chunk enrichment; also needs the tenant in `KNOWLEDGE_ENRICH_TENANTS`. Sends chunk text to an LLM. **[SECURITY]** | `services/knowledge/enrichment.py:85` |
| `KNOWLEDGE_ENRICH_CONCURRENCY` | `4` | int | Concurrent enrichment calls per document. | `services/knowledge/enrichment.py:87` |
| `KNOWLEDGE_ENRICH_DOC_DEADLINE_S` | `90` | float | Time budget for enriching one document. | `services/knowledge/enrichment.py:90` |
| `KNOWLEDGE_ENRICH_MAX_CALLS_PER_TENANT_DAY` | `2000` | int | Per-tenant enrichment calls per UTC day (in-process). | `services/knowledge/enrichment.py:93` |
| `KNOWLEDGE_ENRICH_MAX_CHUNKS_PER_DOC` | `100` | int | Chunks past this are embedded plain. | `services/knowledge/enrichment.py:88` |
| `KNOWLEDGE_ENRICH_MAX_TOKENS_PER_DOC` | `60000` | int | Estimated token budget across one document. | `services/knowledge/enrichment.py:89` |
| `KNOWLEDGE_ENRICH_TENANTS` | `None` | str | Comma-separated tenant ids or slugs allowed to use enrichment; empty means nobody. **[SECURITY]** | `services/knowledge/enrichment.py:91` |
| `KNOWLEDGE_ENRICH_TIMEOUT_S` | `8` | float | Timeout for one enrichment LLM call. | `services/knowledge/enrichment.py:86` |

## Re-embed

Moving a KB to a different embedding model.

| Variable | Default | Type | Meaning | Read at |
|---|---|---|---|---|
| `KNOWLEDGE_REEMBED_MAX_SWAP_CHUNKS` | `20000` | int | Largest KB the re-embed swap will run in one transaction without `--allow-long-swap` (it locks an HNSW-indexed table). | `services/knowledge/reembed.py:77` |

## Knowledge tool and tool guard (Conversation service)

How the Conversation service presents and guards retrieved text.

| Variable | Default | Type | Meaning | Read at |
|---|---|---|---|---|
| `KNOWLEDGE_TAINT_BLOCKS_WRITES` | `1` | int | Default on: after a document lookup in a turn, state-changing tool calls are refused that turn (a planted passage cannot steer a write). Off removes that guard. **[SECURITY]** | `services/conversation/tools/guard.py:73` |
| `KNOWLEDGE_TOOL_MAX_CHARS` | `6000` | int | Character ceiling as well, since words are no bound for CJK or minified text. | `services/conversation/pipeline.py:378` |
| `KNOWLEDGE_TOOL_MAX_TOKENS` | `1500` | int | Ceiling on retrieved text handed to the LLM (words). The server already caps prompt-mode documents at half of `max_tokens`; this bounds the whole result. | `services/conversation/pipeline.py:376` |

## Service wiring, storage and shared secrets

Where services find each other, and the secrets they share.

| Variable | Default | Type | Meaning | Read at |
|---|---|---|---|---|
| `CONFIG_SERVICE_EMAIL` | `''` | str | Service-account email the Conversation service logs in with. | `services/conversation/__main__.py:292`, `services/conversation/__main__.py:306`, `services/conversation/tools/provider_manager.py:27` |
| `CONFIG_SERVICE_PASSWORD` | `''` | str | Service-account password. **[SECURITY]** | `services/conversation/__main__.py:293`, `services/conversation/__main__.py:307`, `services/conversation/tools/provider_manager.py:28` |
| `CONFIG_SERVICE_URL` | `'http://localhost:8000'` | str | Config service base URL (Conversation's login and config fetch). | `services/conversation/__main__.py:291`, `services/conversation/__main__.py:305`, `services/conversation/tools/provider_manager.py:26` |
| `JWT_SECRET` | `''` | str | HMAC secret for access tokens; required at import. **[SECURITY]** **[SET ON BOTH CONFIG AND KNOWLEDGE]** | `services/config/auth.py:12` |
| `KNOWLEDGE_PROVIDER_ALLOWED_HOSTS` | `None` | str | Allowed hosts for tenant-configured provider URLs (egress guard, when present). **[SECURITY]** | `libs/egress_guard.py:89` |
| `KNOWLEDGE_SERVICE_URL` | `'http://localhost:8100'` | str | Where the Conversation service reaches the knowledge service. | `services/conversation/__main__.py:304` |
| `LISTEN_HOST` | `'127.0.0.1'` | str | Bind address of the Conversation gRPC server. | `services/conversation/__main__.py:487` |
| `OLLAMA_BASE_URL` | `None` | str | Base URL of the Ollama embedding server (when read by the code). | `libs/egress_guard.py:92` |
| `POSTGRES_DSN` | `(unset)` | str | Postgres connection string; production runs the pool as the RLS-enforced `yuviz_app` role. **[SECURITY]** **[SET ON BOTH CONFIG AND KNOWLEDGE]** | `services/knowledge/db.py:39`, `services/conversation/__main__.py:324`, `services/conversation/__main__.py:235` (+1) |
| `REDIS_URL` | `'redis://localhost:6379/0'` | str | Redis for the agent-KB flag cache and shared caches. **[SET ON BOTH CONFIG AND KNOWLEDGE]** | `services/knowledge/cache.py:27`, `services/conversation/__main__.py:280`, `services/conversation/__main__.py:285` (+2) |
| `SECRET_ENCRYPTION_KEY` | `''` | str | Fernet key for provider API keys stored in the database. **[SECURITY]** **[SET ON BOTH CONFIG AND KNOWLEDGE]** | `libs/config_sdk/secrets.py:43`, `libs/config_sdk/secrets.py:103` |
| `TOOLEXEC_SERVICE_URL` | `'http://localhost:8600'` | str | Tool execution service base URL. | `services/conversation/tools/provider_manager.py:25` |

## Conversation service (voice pipeline, not knowledge-specific)

Read by `services/conversation`; listed so the reference is complete.

| Variable | Default | Type | Meaning | Read at |
|---|---|---|---|---|
| `OPENAI_API_KEY` | `(unset)` | str | Fallback key for call-sentiment scoring. **[SECURITY]** | `services/conversation/pipeline_config.py:140` |
| `VOICEAI_LANG_HI_WORD_SHARE` | `0.3` | float | Language detection: share of Hindi words that decides Hindi. | `services/conversation/language.py:37` |
| `VOICEAI_LANG_MIN_CONFIDENCE` | `0.7` | float | Language detection: minimum confidence to switch. | `services/conversation/language.py:32` |
| `VOICEAI_LANG_SHORT_MIN_CONFIDENCE` | `0.8` | float | Language detection: minimum confidence for short utterances. | `services/conversation/language.py:41` |
| `VOICEAI_LANG_SHORT_MIN_CONFIDENCE_HI` | `0` | float | Language detection: short-utterance confidence for Hindi. | `services/conversation/language.py:47` |
| `VOICEAI_LANG_SHORT_MIN_S` | `0.45` | float | Language detection: what counts as a short utterance (s). | `services/conversation/language.py:40` |
| `VOICEAI_LANG_SWITCH_STREAK` | `1` | float | Language detection: consecutive detections needed to switch. | `services/conversation/language.py:35` |
| `VOICEAI_MAX_BACKLOG_BYTES` | `7680000` | int | Total bytes of caller input queued behind a busy turn; oldest utterances dropped beyond it. Floor is MAX_UTTERANCE_BYTES + 64 KiB (raised with a warning). | `services/conversation/servicer.py:56` |
| `VOICEAI_MAX_QUEUED_UTTERANCES` | `3` | int | Complete caller utterances queued behind a busy turn; oldest dropped beyond it. 0 means no limit. | `services/conversation/servicer.py:61` |
| `VOICEAI_LLM_MODEL` | `(unset)` | str | Dev override: LLM model name. | `services/conversation/__main__.py:232`, `services/conversation/pipeline_config.py:104` |
| `VOICEAI_LLM_SYSTEM` | `(unset)` | str | Dev override: LLM system prompt. | `services/conversation/pipeline_config.py:105` |
| `VOICEAI_LLM_TEMPERATURE` | `0.7` | float | Dev override: LLM temperature. | `services/conversation/pipeline_config.py:107` |
| `VOICEAI_LLM_TIMEOUT` | `30` | float | Dev override: LLM timeout (s). | `services/conversation/pipeline_config.py:108` |
| `VOICEAI_LLM_URL` | `(unset)` | str | Dev override: LLM base URL. | `services/conversation/ai_provider_manager.py:162`, `services/conversation/pipeline_config.py:106` |
| `VOICEAI_SENTIMENT_API_KEY` | `(unset)` | str | Key for call-sentiment scoring; unset disables it. **[SECURITY]** | `services/conversation/pipeline_config.py:140` |
| `VOICEAI_SENTIMENT_DISABLED` | `(unset)` | str | Set to disable sentiment scoring. | `services/conversation/pipeline_config.py:145` |
| `VOICEAI_SENTIMENT_MAX_TURNS` | `40` | int | Transcript turns sent for scoring. | `services/conversation/pipeline_config.py:144` |
| `VOICEAI_SENTIMENT_MODEL` | `(unset)` | str | Sentiment model name. | `services/conversation/pipeline_config.py:141` |
| `VOICEAI_SENTIMENT_TIMEOUT` | `30` | float | Sentiment call timeout (s). | `services/conversation/pipeline_config.py:143` |
| `VOICEAI_SENTIMENT_URL` | `(unset)` | str | Sentiment provider base URL. | `services/conversation/pipeline_config.py:142` |
| `VOICEAI_STT_DEVICE` | `(unset)` | str | Dev override: STT device. | `services/conversation/pipeline_config.py:88` |
| `VOICEAI_STT_LANGUAGE` | `(required)` | str | Dev override: STT language. | `services/conversation/pipeline_config.py:91` |
| `VOICEAI_STT_MODEL` | `(unset)` | str | Dev override: STT model. | `services/conversation/pipeline_config.py:87` |
| `VOICEAI_STT_SHORT_MIN_SPEECH_CONFIDENCE` | `0.8` | float | Language detection: STT confidence floor for short utterances. | `services/conversation/language.py:44` |
| `VOICEAI_TTS_ENGINE` | `(unset)` | str | Dev override: TTS engine. | `services/conversation/pipeline_config.py:120` |
| `VOICEAI_TTS_KOKORO_SPEED` | `1` | float | Kokoro speech speed. | `services/conversation/pipeline_config.py:124` |
| `VOICEAI_TTS_LANG_CODE` | `(unset)` | str | Kokoro language code. | `services/conversation/pipeline_config.py:122` |
| `VOICEAI_TTS_MACOS_WPM` | `180` | int | macOS TTS words per minute. | `services/conversation/pipeline_config.py:123` |
| `VOICEAI_TTS_VOICE` | `(unset)` | str | Dev override: TTS voice. | `services/conversation/pipeline_config.py:121` |

## Other

Variables with no hand-written group yet.

| Variable | Default | Type | Meaning | Read at |
|---|---|---|---|---|
| `ADMIN_UI_ORIGINS` | `'http://localhost:3000'` | str | (undocumented) | `services/knowledge/app.py:66` |
| `DEEPGRAM_API_HOST` | `''` | str | (undocumented) | `services/conversation/providers/stt/deepgram.py:36` |
| `KNOWLEDGE_OPENAI_BASE_URL` | `None` | str | (undocumented) | `services/knowledge/embedding_manager.py:262` |
| `VOICEAI_BUSINESS_TIMEZONE` | `'UTC'` | str | (undocumented) | `services/conversation/pipeline.py:124` |
| `VOICEAI_FILLER_DELAY_MS` | `_FILLER_DELAY_MS_DEFAULT` | str | (undocumented) | `services/conversation/pipeline.py:310` |
| `VOICEAI_PHRASE_CACHE_ENTRIES` | `64` | int | (undocumented) | `services/conversation/phrase_audio.py:21` |
| `VOICEAI_PRE_RETRIEVAL_WAIT_MS` | `_WAIT_MS_DEFAULT` | str | (undocumented) | `services/conversation/prefetch.py:31` |

## Test and CI only

`KNOWLEDGE_GATE_MARGIN_MS` (the CI deadline margin in `test_retrieval_gates.py`) and `OLLAMA_URL` (where the Ollama-backed
tests look for a model) are read by tests only and are not part of the service's configuration.
