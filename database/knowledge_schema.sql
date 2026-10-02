-- Knowledge platform schema.
-- Run: psql voiceai -f database/knowledge_schema.sql (after database/schema.sql). Idempotent.

CREATE EXTENSION IF NOT EXISTS vector;

-- 'embedding' providers are chosen per knowledge base, not on tenants/agents.
ALTER TABLE provider_configs DROP CONSTRAINT IF EXISTS provider_configs_role_check;
ALTER TABLE provider_configs ADD CONSTRAINT provider_configs_role_check
    CHECK (role IN ('stt', 'llm', 'tts', 'embedding'));

-- ── knowledge_bases ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS knowledge_bases (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenants(id),
    slug                  TEXT NOT NULL,
    name                  TEXT NOT NULL,
    description           TEXT NOT NULL DEFAULT '',
    embedding_config_id   UUID REFERENCES provider_configs(id),  -- which embedding provider this KB's chunks were embedded with
    -- inactive = excluded from retrieval, data kept.
    status                TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    config_version        INT NOT NULL DEFAULT 1,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at            TIMESTAMPTZ,
    UNIQUE (tenant_id, slug)
);

DROP TRIGGER IF EXISTS knowledge_bases_version ON knowledge_bases;
CREATE TRIGGER knowledge_bases_version BEFORE UPDATE ON knowledge_bases
    FOR EACH ROW EXECUTE FUNCTION bump_config_version();

-- ── kb_documents — one row per uploaded source document ─────────────────────
-- source_ref points at StorageProvider-managed content; raw bytes never live in Postgres.
CREATE TABLE IF NOT EXISTS kb_documents (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kb_id         UUID NOT NULL REFERENCES knowledge_bases(id),
    tenant_id     UUID NOT NULL REFERENCES tenants(id),  -- denormalized for cheap tenant-scoped queries/RLS-style checks
    title         TEXT NOT NULL,
    source_ref    TEXT NOT NULL,
    content_type  TEXT NOT NULL,  -- e.g. 'application/pdf', 'text/plain', 'text/markdown'
    language      TEXT,
    tags          JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- ready = chunks+embeddings exist; failed keeps the doc visible for retry.
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'ready', 'failed')),
    error         TEXT,
    version       INT NOT NULL DEFAULT 1,  -- bumped on re-upload/re-ingest; kb_chunks.version tags which pass produced a chunk
    -- auto: vector retrieval only. prompt: injected into every turn (set by admin, or
    -- automatically for docs under AUTO_INLINE_THRESHOLD_BYTES).
    usage_mode    TEXT NOT NULL DEFAULT 'auto' CHECK (usage_mode IN ('auto', 'prompt')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ
);

-- For databases created before usage_mode existed.
ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS usage_mode TEXT NOT NULL DEFAULT 'auto';
DO $$ BEGIN
    ALTER TABLE kb_documents ADD CONSTRAINT kb_documents_usage_mode_check CHECK (usage_mode IN ('auto', 'prompt'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS idx_kb_documents_kb ON kb_documents(kb_id);
CREATE INDEX IF NOT EXISTS idx_kb_documents_tenant ON kb_documents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kb_documents_status ON kb_documents(status);

-- ── kb_chunks — one row per chunk, with its embedding ────────────────────────
-- 768 dims = nomic-embed-text. Other-dimension models (e.g. 1536) aren't supported yet.
CREATE TABLE IF NOT EXISTS kb_chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES kb_documents(id),
    kb_id           UUID NOT NULL REFERENCES knowledge_bases(id),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    chunk_index     INT NOT NULL,
    content         TEXT NOT NULL,
    embedding       VECTOR(768),
    token_count     INT,
    page            INT,
    language        TEXT,
    tags            JSONB NOT NULL DEFAULT '{}'::jsonb,
    version         INT NOT NULL DEFAULT 1,  -- = kb_documents.version at the time this chunk was produced
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index, version)
);

CREATE INDEX IF NOT EXISTS idx_kb_chunks_document ON kb_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_kb ON kb_chunks(kb_id);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_tenant ON kb_chunks(tenant_id);
-- Cosine ops to match the retrieval query's <=> operator.
CREATE INDEX IF NOT EXISTS idx_kb_chunks_embedding_hnsw
    ON kb_chunks USING hnsw (embedding vector_cosine_ops);

-- ── agent_knowledge_bases — which KBs an agent retrieves from ───────────────
-- enabled=false pauses a KB for one agent without detaching it.
CREATE TABLE IF NOT EXISTS agent_knowledge_bases (
    agent_id    UUID NOT NULL REFERENCES agents(id),
    kb_id       UUID NOT NULL REFERENCES knowledge_bases(id),
    enabled     BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, kb_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_kb_agent ON agent_knowledge_bases(agent_id) WHERE enabled;

-- ── agent_retrieval_policies — per-agent RetrievalPolicy, never hardcoded ──
-- Precedence: per-call override > this row > system default. No row = system default.
CREATE TABLE IF NOT EXISTS agent_retrieval_policies (
    agent_id            UUID PRIMARY KEY REFERENCES agents(id),
    top_k               INT,
    max_tokens          INT,
    minimum_score       FLOAT,
    rerank              BOOLEAN,       -- Phase 6B extension point — not implemented yet
    hybrid_search       BOOLEAN,       -- Phase 6B extension point — not implemented yet
    include_citations   BOOLEAN,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── kb_ingestion_jobs — Postgres-backed job queue, polled by the worker ─────
CREATE TABLE IF NOT EXISTS kb_ingestion_jobs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id   UUID NOT NULL REFERENCES kb_documents(id),
    kb_id         UUID NOT NULL REFERENCES knowledge_bases(id),
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    error         TEXT,
    attempts      INT NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_kb_ingestion_jobs_status ON kb_ingestion_jobs(status) WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_kb_ingestion_jobs_document ON kb_ingestion_jobs(document_id);

-- knowledge_base_ids is default-deny: give the start node the agent's enabled KBs
-- when no node in the graph lists any.
DO $kb_workflow_backfill$
DECLARE
    patched INT := 0;
BEGIN
    WITH agent_kbs AS (
        SELECT
            akb.agent_id,
            jsonb_agg(akb.kb_id::text ORDER BY akb.kb_id) AS kb_ids
        FROM agent_knowledge_bases akb
        WHERE akb.enabled
        GROUP BY akb.agent_id
    ),
    targets AS (
        SELECT a.id, a.workflow, k.kb_ids,
            (
                SELECT (ord - 1)
                FROM jsonb_array_elements(a.workflow->'nodes') WITH ORDINALITY AS t(n, ord)
                WHERE n->>'type' = 'start'
                LIMIT 1
            ) AS start_idx
        FROM agents a
        JOIN agent_kbs k ON k.agent_id = a.id
        WHERE a.workflow IS NOT NULL
          AND a.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM jsonb_array_elements(a.workflow->'nodes') n
              WHERE jsonb_typeof(COALESCE(n->'data'->'knowledge_base_ids', '[]'::jsonb)) = 'array'
                AND jsonb_array_length(COALESCE(n->'data'->'knowledge_base_ids', '[]'::jsonb)) > 0
          )
    ),
    updated AS (
        UPDATE agents a
        SET
            workflow = jsonb_set(
                a.workflow,
                ARRAY['nodes', t.start_idx::text, 'data', 'knowledge_base_ids'],
                t.kb_ids,
                true
            ),
            workflow_draft = CASE
                WHEN a.workflow_draft IS NULL THEN NULL
                WHEN NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(a.workflow_draft->'nodes') n
                    WHERE jsonb_typeof(COALESCE(n->'data'->'knowledge_base_ids', '[]'::jsonb)) = 'array'
                      AND jsonb_array_length(COALESCE(n->'data'->'knowledge_base_ids', '[]'::jsonb)) > 0
                ) AND EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(a.workflow_draft->'nodes') n
                    WHERE n->>'type' = 'start'
                ) THEN jsonb_set(
                    a.workflow_draft,
                    ARRAY[
                        'nodes',
                        (
                            SELECT (ord - 1)::text
                            FROM jsonb_array_elements(a.workflow_draft->'nodes')
                                WITH ORDINALITY AS d(n, ord)
                            WHERE n->>'type' = 'start'
                            LIMIT 1
                        ),
                        'data',
                        'knowledge_base_ids'
                    ],
                    t.kb_ids,
                    true
                )
                ELSE a.workflow_draft
            END
        FROM targets t
        WHERE a.id = t.id AND t.start_idx IS NOT NULL
        RETURNING a.id
    )
    SELECT COUNT(*) INTO patched FROM updated;

    IF patched > 0 THEN
        RAISE NOTICE 'kb workflow backfill patched % agent(s)', patched;
    END IF;
END
$kb_workflow_backfill$;


-- A KB's embedding provider must belong to the KB's tenant.
DO $$
DECLARE violations int;
BEGIN
  SELECT count(*) INTO violations
    FROM knowledge_bases k JOIN provider_configs p ON p.id = k.embedding_config_id
   WHERE p.tenant_id IS DISTINCT FROM k.tenant_id;
  IF violations > 0 THEN
    RAISE EXCEPTION
      'knowledge_bases has % embedding_config_id(s) pointing at another tenant''s provider; '
      'fix them before applying knowledge_schema.sql', violations;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'knowledge_bases_embedding_config_tenant_fkey') THEN
    EXECUTE $sql$ALTER TABLE knowledge_bases ADD CONSTRAINT knowledge_bases_embedding_config_tenant_fkey
      FOREIGN KEY (embedding_config_id, tenant_id) REFERENCES provider_configs(id, tenant_id)$sql$;
  END IF;
END $$;
