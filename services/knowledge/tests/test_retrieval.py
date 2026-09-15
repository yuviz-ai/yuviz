"""
_resolve_policy() proves the three-tier override chain (call override >
agent's agent_retrieval_policies row > system default) — the mechanism
that makes RetrievalPolicy actually configurable per agent instead of a
hardcoded constant anywhere in the call path.

test_retrieve_end_to_end_with_real_ollama_embeddings hits real Ollama
(nomic-embed-text, already pulled locally — see database/knowledge_schema.
sql's dimension comment) and real pgvector — matching this project's
"real infra when fast/available" testing convention, not mocked.

retrieve()/_resolve_policy()/PgVectorRepository.search() take a connection
rather than the pool (RLS design, libs/tenancy) — each test opens one via
tenant_conn(pool), the same helper routers/retrieve.py opens in production.
"""

from __future__ import annotations

from libs.tenancy import set_caller_tenant, tenant_conn
from services.config import provider_configs
from services.knowledge import agent_kb as agent_kb_service
from services.knowledge import documents as documents_service
from services.knowledge import knowledge_bases as kb_service
from services.knowledge import retrieval_policies as policy_service
from services.knowledge.embedding_manager import EmbeddingProviderConfig, EmbeddingProviderManager
from services.knowledge.retrieval import _resolve_policy, retrieve
from services.knowledge.secret_resolver import CompositeSecretResolver
from services.knowledge.storage import LocalStorageProvider
from services.knowledge.vector_repository import PgVectorRepository


async def test_resolve_policy_uses_system_default_when_nothing_set(pool, tenant_agent):
    tenant, agent = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    async with tenant_conn(pool) as conn:
        resolved = await _resolve_policy(conn, tenant["slug"], agent["slug"], {})
    assert resolved["top_k"] == 5
    assert resolved["max_tokens"] == 1000
    assert resolved["minimum_score"] == 0.0
    assert resolved["include_citations"] is True


async def test_resolve_policy_agent_row_overrides_system_default(pool, tenant_agent):
    tenant, agent = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    await policy_service.upsert_policy(agent["id"], top_k=15, minimum_score=0.6)

    async with tenant_conn(pool) as conn:
        resolved = await _resolve_policy(conn, tenant["slug"], agent["slug"], {})
    assert resolved["top_k"] == 15
    assert resolved["minimum_score"] == 0.6
    assert resolved["max_tokens"] == 1000  # untouched field still falls to system default


async def test_resolve_policy_call_override_wins_over_agent_row(pool, tenant_agent):
    tenant, agent = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    await policy_service.upsert_policy(agent["id"], top_k=15)

    async with tenant_conn(pool) as conn:
        resolved = await _resolve_policy(conn, tenant["slug"], agent["slug"], {"top_k": 3})
    assert resolved["top_k"] == 3  # explicit call override beats the agent's configured 15


async def test_retrieve_end_to_end_with_real_ollama_embeddings(pool, tenant_agent):
    tenant, agent = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    embedding_cfg = await provider_configs.create_provider_config(
        tenant_id=tenant["id"], name="Embed", role="embedding", engine="ollama",
    )
    kb = await kb_service.create_knowledge_base(
        tenant_id=tenant["id"], slug="policies", name="Policies", embedding_config_id=embedding_cfg["id"],
    )
    await agent_kb_service.assign(agent["id"], kb["id"])

    document = dict(await pool.fetchrow(
        "INSERT INTO kb_documents (kb_id, tenant_id, title, source_ref, content_type) "
        "VALUES ($1, $2, 'Refund Policy', 'inline', 'text/plain') RETURNING *",
        kb["id"], tenant["id"],
    ))

    manager = EmbeddingProviderManager(CompositeSecretResolver())
    provider = await manager.get(EmbeddingProviderConfig(id=str(embedding_cfg["id"]), engine="ollama"))

    for i, text in enumerate(["Refunds are processed within 30 days.", "Shipping takes 3 to 5 business days."]):
        [vector] = await provider.embed([text])
        vector_literal = "[" + ",".join(repr(float(x)) for x in vector) + "]"
        await pool.execute(
            "INSERT INTO kb_chunks (document_id, kb_id, tenant_id, chunk_index, content, embedding) "
            "VALUES ($1, $2, $3, $4, $5, $6::vector)",
            document["id"], kb["id"], tenant["id"], i, text, vector_literal,
        )

    vector_repo = PgVectorRepository()
    async with tenant_conn(pool) as conn:
        result = await retrieve(
            conn, vector_repo, manager,
            tenant_slug=tenant["slug"], agent_slug=agent["slug"], query="How long until I get my money back?",
        )

    assert result is not None
    assert result["chunks"][0]["content"] == "Refunds are processed within 30 days."
    assert result["sources"] == ["Refund Policy"]
    assert result["retrieval_metadata"]["top_k"] == 5  # system default, nothing overrode it


async def test_prompt_mode_document_always_included_regardless_of_query_relevance(pool, tenant_agent):
    tenant, agent = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    kb = await kb_service.create_knowledge_base(tenant_id=tenant["id"], slug="notices", name="Notices")
    await agent_kb_service.assign(agent["id"], kb["id"])

    # Tiny document, uploaded and ingested through the real pipeline so it
    # auto-inlines exactly like ingestion_worker.py's own test proves —
    # no embedding_config_id needed on the KB at all, since a usage_mode=
    # 'prompt' document (whether auto-inlined or manually flipped) is
    # never embedded/vector-searched.
    from services.knowledge.ingestion_worker import process_one_job

    doc = await documents_service.upload_document(
        tenant_id=tenant["id"], kb_id=kb["id"], title="Holiday Notice",
        filename="notice.txt", content_type="text/plain",
        content=b"We are closed on all public holidays.", storage=LocalStorageProvider(),
    )
    job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    manager = EmbeddingProviderManager(CompositeSecretResolver())
    await process_one_job(pool, LocalStorageProvider(), manager, job)

    vector_repo = PgVectorRepository()
    async with tenant_conn(pool) as conn:
        result = await retrieve(
            conn, vector_repo, manager,
            tenant_slug=tenant["slug"], agent_slug=agent["slug"],
            query="What is the weather like on Mars?",  # completely unrelated
        )

    assert result is not None
    assert result["chunks"][0]["content"] == "We are closed on all public holidays."
    assert result["retrieval_metadata"]["always_included"] == 1


async def test_prompt_mode_document_coexists_with_vector_search_results(pool, tenant_agent):
    tenant, agent = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    embedding_cfg = await provider_configs.create_provider_config(
        tenant_id=tenant["id"], name="Embed", role="embedding", engine="ollama",
    )
    kb = await kb_service.create_knowledge_base(
        tenant_id=tenant["id"], slug="mixed", name="Mixed", embedding_config_id=embedding_cfg["id"],
    )
    await agent_kb_service.assign(agent["id"], kb["id"])

    manager = EmbeddingProviderManager(CompositeSecretResolver())
    provider = await manager.get(EmbeddingProviderConfig(id=str(embedding_cfg["id"]), engine="ollama"))

    # A normal, embedded, 'auto' document.
    auto_doc = dict(await pool.fetchrow(
        "INSERT INTO kb_documents (kb_id, tenant_id, title, source_ref, content_type, status) "
        "VALUES ($1, $2, 'Refund Policy', 'inline', 'text/plain', 'ready') RETURNING *",
        kb["id"], tenant["id"],
    ))
    [vector] = await provider.embed(["Refunds are processed within 30 days."])
    vector_literal = "[" + ",".join(repr(float(x)) for x in vector) + "]"
    await pool.execute(
        "INSERT INTO kb_chunks (document_id, kb_id, tenant_id, chunk_index, content, embedding) "
        "VALUES ($1, $2, $3, 0, $4, $5::vector)",
        auto_doc["id"], kb["id"], tenant["id"], "Refunds are processed within 30 days.", vector_literal,
    )

    # A 'prompt' document — should show up regardless of the query.
    prompt_doc = dict(await pool.fetchrow(
        "INSERT INTO kb_documents (kb_id, tenant_id, title, source_ref, content_type, status, usage_mode) "
        "VALUES ($1, $2, 'Escalation Contact', 'inline', 'text/plain', 'ready', 'prompt') RETURNING *",
        kb["id"], tenant["id"],
    ))
    await pool.execute(
        "INSERT INTO kb_chunks (document_id, kb_id, tenant_id, chunk_index, content) VALUES ($1, $2, $3, 0, $4)",
        prompt_doc["id"], kb["id"], tenant["id"], "Escalate unresolved issues to manager@acme.example.",
    )

    vector_repo = PgVectorRepository()
    async with tenant_conn(pool) as conn:
        result = await retrieve(
            conn, vector_repo, manager,
            tenant_slug=tenant["slug"], agent_slug=agent["slug"], query="How long until I get my money back?",
        )

    assert result is not None
    contents = [c["content"] for c in result["chunks"]]
    assert "Escalate unresolved issues to manager@acme.example." in contents  # always-include, unconditional
    assert "Refunds are processed within 30 days." in contents  # relevant vector match
    assert result["retrieval_metadata"]["always_included"] == 1


async def test_prompt_mode_document_excluded_from_ordinary_vector_search(pool, tenant_agent):
    tenant, agent = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    embedding_cfg = await provider_configs.create_provider_config(
        tenant_id=tenant["id"], name="Embed", role="embedding", engine="ollama",
    )
    kb = await kb_service.create_knowledge_base(
        tenant_id=tenant["id"], slug="prompt-only", name="Prompt Only", embedding_config_id=embedding_cfg["id"],
    )
    manager = EmbeddingProviderManager(CompositeSecretResolver())
    provider = await manager.get(EmbeddingProviderConfig(id=str(embedding_cfg["id"]), engine="ollama"))

    doc = dict(await pool.fetchrow(
        "INSERT INTO kb_documents (kb_id, tenant_id, title, source_ref, content_type, status, usage_mode) "
        "VALUES ($1, $2, 'Doc', 'inline', 'text/plain', 'ready', 'prompt') RETURNING *",
        kb["id"], tenant["id"],
    ))
    [vector] = await provider.embed(["Refunds are processed within 30 days."])
    vector_literal = "[" + ",".join(repr(float(x)) for x in vector) + "]"
    await pool.execute(
        "INSERT INTO kb_chunks (document_id, kb_id, tenant_id, chunk_index, content, embedding) "
        "VALUES ($1, $2, $3, 0, $4, $5::vector)",
        doc["id"], kb["id"], tenant["id"], "Refunds are processed within 30 days.", vector_literal,
    )

    [query_vector] = await provider.embed(["How long until I get my money back?"])
    vector_repo = PgVectorRepository()
    async with tenant_conn(pool) as conn:
        matches = await vector_repo.search(conn, [str(kb["id"])], query_vector, top_k=5, minimum_score=0.0)

    assert matches == []  # embedded, but usage_mode='prompt' — never returned by similarity search


async def test_agent_with_prompt_only_kb_and_no_embedding_provider_still_retrieves(pool, tenant_agent):
    tenant, agent = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    # No embedding_config_id at all — a KB holding only always-include
    # documents needs no embedding provider, since none of its content is
    # ever vector-searched.
    kb = await kb_service.create_knowledge_base(tenant_id=tenant["id"], slug="no-embed", name="No Embed")
    await agent_kb_service.assign(agent["id"], kb["id"])

    from services.knowledge.ingestion_worker import process_one_job

    doc = await documents_service.upload_document(
        tenant_id=tenant["id"], kb_id=kb["id"], title="Notice", filename="notice.txt",
        content_type="text/plain", content=b"Office closes early on Fridays.", storage=LocalStorageProvider(),
    )
    job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))
    manager = EmbeddingProviderManager(CompositeSecretResolver())
    await process_one_job(pool, LocalStorageProvider(), manager, job)

    vector_repo = PgVectorRepository()
    async with tenant_conn(pool) as conn:
        result = await retrieve(
            conn, vector_repo, manager,
            tenant_slug=tenant["slug"], agent_slug=agent["slug"], query="anything at all",
        )

    assert result is not None
    assert result["chunks"][0]["content"] == "Office closes early on Fridays."
