"""Retrieval hot path: plan cache, query-embedding cache, hybrid search, fallbacks, deadline, breakers, logs.
Real Postgres + pgvector, fake embedder (rag_fakes). Tenant A/B isolation is asserted on every cache and branch."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
import time
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest
import pytest_asyncio
from rag_fakes import FakeEmbedder, add_document, embed_text, make_kb, make_manager, vec_literal

from libs.tenancy import TenantScopeConflict, set_caller_tenant, tenant_conn
from services.knowledge import cache, db, resilience, retrieval, retrieval_log
from services.knowledge import retrieval_plan as plans
from services.knowledge import retrieval_policies as policy_service
from services.knowledge.resilience import CircuitBreaker, RetrievalConfig, RetrievalUnavailable
from services.knowledge.retrieval import dedupe_sections, retrieve, rrf_merge
from services.knowledge.vector_repository import PgVectorRepository, VectorMatch, build_tsquery

REFUND = "Refunds are processed within 30 days of the return request."
SHIPPING = "Shipping takes 3 to 5 business days for standard delivery."


def _match(chunk_id: str, score: float = 0.5, *, document_id: str = "d1", parent_id: str | None = None,
           content: str | None = None, found_by: tuple[str, ...] = ("dense",)) -> VectorMatch:
    return VectorMatch(
        chunk_id=chunk_id, document_id=document_id, kb_id="kb", content=content or f"text {chunk_id}", score=score,
        token_count=3, page=None, language=None, tags={}, version=1, document_title="T", kb_slug="kb",
        parent_id=parent_id, found_by=found_by,
    )


async def _run(tenant, agent, provider, query, *, repo=None, **kw):
    set_caller_tenant(str(tenant["id"]))
    return await retrieve(
        None, repo or PgVectorRepository(), make_manager(provider),
        tenant_slug=tenant["slug"], agent_slug=agent["slug"], query=query, **kw,
    )


async def _set_app_password(dsn: str) -> None:
    # Other suites reset yuviz_app's password to their own; set ours so test order doesn't matter.
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("ALTER ROLE yuviz_app PASSWORD 'testpw'")
    finally:
        await conn.close()


@pytest_asyncio.fixture(loop_scope="session")
async def app_pool():
    """A pool as yuviz_app (NOBYPASSRLS). The suite's default DSN is a superuser, which RLS never applies to, so
    every isolation assertion in this file runs through this pool instead."""
    dsn = os.environ["POSTGRES_DSN"]
    dbname = dsn.rsplit("/", 1)[1]
    try:
        await _set_app_password(dsn)
        p = await asyncpg.create_pool(f"postgresql://yuviz_app:testpw@localhost:5432/{dbname}", min_size=1, max_size=4)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"yuviz_app role not available: {exc}")
    yield p
    await p.close()


@pytest_asyncio.fixture(loop_scope="session")
async def world(pool, tenant_agent):
    """One tenant, one agent, one embedded KB with a refund and a shipping document."""
    tenant, agent = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    kb = await make_kb(pool, tenant, agent, "policies")
    refund = await add_document(pool, tenant, kb, "Refund Policy", [REFUND])
    shipping = await add_document(pool, tenant, kb, "Shipping Policy", [SHIPPING])
    return {"tenant": tenant, "agent": agent, "kb": kb, "refund": refund, "shipping": shipping,
            "embedder": FakeEmbedder()}


# --- fake-embedder equivalents of the three Ollama baseline tests --------------------------------------------

async def test_retrieve_end_to_end_with_fake_embeddings(world):
    result = await _run(world["tenant"], world["agent"], world["embedder"], "how long do refunds take")
    assert result["status"] == "ok"
    assert result["chunks"][0]["content"] == REFUND
    assert result["sources"][0] == "Refund Policy"
    assert result["retrieval_metadata"]["top_k"] == 5
    assert result["degraded"] == [] and result["retrieval_metadata"]["plan_cache"] == "miss"  # not a fallback
    assert result["trace_id"]
    assert result["low_confidence"] is True  # lexical-only evidence this weak is flagged (see test_retrieval_hardening)


async def test_prompt_mode_document_coexists_with_search_results(pool, world):
    await add_document(
        pool, world["tenant"], world["kb"], "Escalation", ["Escalate unresolved issues to manager@acme.example."],
        usage_mode="prompt", embed=False,
    )
    result = await _run(world["tenant"], world["agent"], world["embedder"], "how long do refunds take")
    contents = [c["content"] for c in result["chunks"]]
    assert contents[0] == "Escalate unresolved issues to manager@acme.example."  # prompt docs always first
    assert REFUND in contents
    assert result["retrieval_metadata"]["always_included"] == 1


async def test_prompt_mode_document_excluded_from_dense_and_lexical_search(pool, world):
    await add_document(pool, world["tenant"], world["kb"], "Prompt Doc", ["Zanzibar protocol applies."],
                       usage_mode="prompt")
    repo = PgVectorRepository()
    async with tenant_conn(pool) as conn:
        dense = await repo.search(conn, [str(world["kb"]["id"])], embed_text("Zanzibar protocol"), 5, 0.0)
        lexical = await repo.lexical_search(conn, [str(world["kb"]["id"])], "Zanzibar protocol", 5, 0.0)
    assert all("Zanzibar" not in m.content for m in dense + lexical)


# --- lexical / hybrid / dense-only ---------------------------------------------------------------------------

async def test_lexical_finds_what_dense_cannot(pool, world):
    # No embedding at all: only the tsvector branch can reach this chunk.
    await add_document(pool, world["tenant"], world["kb"], "Catalogue", ["Part number ZX9000X ships in blue."],
                       embed=False)
    result = await _run(world["tenant"], world["agent"], world["embedder"], "ZX9000X")
    assert result["status"] == "ok"
    assert result["chunks"][0]["content"] == "Part number ZX9000X ships in blue."
    assert result["retrieval_metadata"]["lexical_candidates"] >= 1


async def test_hybrid_search_false_means_dense_only(pool, world):
    await add_document(pool, world["tenant"], world["kb"], "Catalogue", ["Part number ZX9000X ships in blue."],
                       embed=False)
    result = await _run(world["tenant"], world["agent"], world["embedder"], "ZX9000X", hybrid_search=False)
    assert all("ZX9000X" not in c["content"] for c in result["chunks"])
    assert result["retrieval_metadata"]["lexical_candidates"] == 0


async def test_hybrid_is_the_default_and_an_agent_row_can_turn_it_off(pool, world):
    await add_document(pool, world["tenant"], world["kb"], "Catalogue", ["Part number ZX9000X ships in blue."],
                       embed=False)
    assert (await _run(world["tenant"], world["agent"], world["embedder"], "ZX9000X"))["status"] == "ok"
    await policy_service.upsert_policy(world["agent"]["id"], hybrid_search=False)  # invalidates the plan
    off = await _run(world["tenant"], world["agent"], world["embedder"], "ZX9000X")
    assert off["retrieval_metadata"]["hybrid_search"] is False
    assert off["status"] == "no_match"


def test_build_tsquery_handles_empty_and_hostile_queries():
    assert build_tsquery("") is None
    assert build_tsquery("???  !!") is None
    assert build_tsquery("the of and") is None
    assert build_tsquery("How long do refunds take?") == "'long' | 'refunds' | 'take'"
    assert build_tsquery("hello' | world:* & !x ) (") == "'hello' | 'world'"  # operators are not smuggled through


async def test_query_that_tokenises_to_nothing_skips_the_lexical_round_trip(pool, world, monkeypatch):
    calls = []
    original = asyncpg.Connection.fetch

    async def spy(self, query, *args, **kw):
        calls.append(query)
        return await original(self, query, *args, **kw)

    set_caller_tenant(str(world["tenant"]["id"]))
    async with tenant_conn(pool) as conn:
        monkeypatch.setattr(asyncpg.Connection, "fetch", spy)
        assert await PgVectorRepository().lexical_search(conn, [str(world["kb"]["id"])], "?!?", 5, 0.0) == []
    assert calls == []
    result = await _run(world["tenant"], world["agent"], world["embedder"], "?!?")
    assert result["status"] == "no_match"


async def test_nothing_above_threshold_is_no_match_not_an_error(world):
    result = await _run(world["tenant"], world["agent"], world["embedder"], "quantum chromodynamics lattice")
    assert result["status"] == "no_match"
    assert result["chunks"] == [] and result["sources"] == [] and result["token_count"] == 0


async def test_unknown_agent_is_a_typed_no_match(world):
    result = await _run(world["tenant"], {"slug": "nope"}, world["embedder"], "refund")
    assert result["status"] == "no_match"
    assert result["retrieval_metadata"]["reason"] == "agent_not_found"


# --- dense search: threshold in SQL, statuses, parents, filtered HNSW ----------------------------------------

async def test_threshold_is_applied_inside_the_query(pool, world):
    repo = PgVectorRepository()
    async with tenant_conn(pool) as conn:
        loose = await repo.search(conn, [str(world["kb"]["id"])], embed_text("refunds processed 30 days"), 10, -1.0)
        tight = await repo.search(conn, [str(world["kb"]["id"])], embed_text("refunds processed 30 days"), 10, 0.5)
    assert len(loose) == 2  # both chunks come back with no floor
    assert [m.content for m in tight] == [REFUND]


async def test_only_ready_and_ready_degraded_live_documents_are_searchable(pool, world):
    t, kb = world["tenant"], world["kb"]
    await add_document(pool, t, kb, "Degraded", ["Gizmo warranty covers two years."], status="ready_degraded")
    await add_document(pool, t, kb, "Pending", ["Gizmo warranty pending draft."], status="pending")
    await add_document(pool, t, kb, "Failed", ["Gizmo warranty failed doc."], status="failed")
    await add_document(pool, t, kb, "Deleted", ["Gizmo warranty deleted doc."], deleted=True)
    repo = PgVectorRepository()
    async with tenant_conn(pool) as conn:
        dense = await repo.search(conn, [str(kb["id"])], embed_text("gizmo warranty"), 10, 0.0)
        lexical = await repo.lexical_search(conn, [str(kb["id"])], "gizmo warranty", 10, 0.0)
    for found in (dense, lexical):
        assert {m.document_title for m in found} == {"Degraded"}


async def test_parent_rows_are_never_hits_but_their_text_is_returned(pool, world):
    parent_text = "Warranty section. Gizmo warranty covers two years. Claims need a receipt. Void if opened."
    await add_document(pool, world["tenant"], world["kb"], "Warranty", ["Gizmo warranty covers two years."],
                       parent=parent_text)
    repo = PgVectorRepository()
    async with tenant_conn(pool) as conn:
        dense = await repo.search(conn, [str(world["kb"]["id"])], embed_text(parent_text), 10, -1.0)
        lexical = await repo.lexical_search(conn, [str(world["kb"]["id"])], "claims receipt void", 10, 0.0,
                                            approx_chunks=100)
    assert [m.content for m in dense if m.document_title == "Warranty"] == [parent_text]
    # The parent text matches "claims receipt void", but only children are hits: the parent is context, not a result.
    assert [m for m in lexical if m.document_title == "Warranty"] == []
    assert len([m for m in dense if m.document_title == "Warranty"]) == 1  # the parent row itself is not a second hit


async def _selective_world(pool, tenant, agent, big_n: int):
    big = await make_kb(pool, tenant, agent, "big")
    small = await make_kb(pool, tenant, agent, "small")
    doc = dict(await pool.fetchrow(
        "INSERT INTO kb_documents (kb_id, tenant_id, title, source_ref, content_type, status) "
        "VALUES ($1, $2, 'Big', 'x', 'text/plain', 'ready') RETURNING *", big["id"], tenant["id"]))
    rows = [(doc["id"], big["id"], tenant["id"], i, f"alpha beta gamma filler {i}", 5,
             vec_literal(embed_text(f"alpha beta gamma filler {i}"))) for i in range(big_n)]
    await pool.executemany(
        "INSERT INTO kb_chunks (document_id, kb_id, tenant_id, chunk_index, content, token_count, embedding) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7::vector)", rows)
    await add_document(pool, tenant, small, "Small", [f"zeta omega {i} unrelated" for i in range(8)])
    return big, small


@asynccontextmanager
async def _hnsw_forced(pool):
    """A rolled-back transaction in which the other kb_chunks indexes, bitmap/seq scans and sorts are unavailable, so the planner has
    only the HNSW index to order by (it otherwise picks the btree for a selective filter: the very case this
    guards, which production hits on large multi-KB tables)."""
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            for index in ("idx_kb_chunks_kb", "idx_kb_chunks_tenant", "idx_kb_chunks_document", "idx_kb_chunks_tsv",
                          "idx_kb_chunks_parent"):
                await conn.execute(f"DROP INDEX {index}")
            await conn.execute("SELECT set_config('enable_bitmapscan', 'off', true), "
                               "set_config('enable_seqscan', 'off', true), set_config('enable_sort', 'off', true)")
            yield conn
        finally:
            await tx.rollback()


@pytest.mark.parametrize("mode", ["session", "local", "fallback"])
async def test_selective_kb_filter_still_returns_top_k_through_hnsw(pool, tenant_agent, mode):
    from services.knowledge import vector_repository as vr
    tenant, agent = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    big, small = await _selective_world(pool, tenant, agent, 1500 if mode != "fallback" else 300)
    args = (vec_literal(embed_text("alpha beta gamma")), [str(small["id"])], 5, vr.SEARCHABLE_STATUSES, -1.0, None)

    async with _hnsw_forced(pool) as conn:
        # Premise 1: the repository's own SQL is driven by the HNSW index here.
        plan = [r[0].strip() for r in await conn.fetch("EXPLAIN " + vr._DENSE_SQL, *args)]
        assert any("idx_kb_chunks_embedding_hnsw" in line for line in plan), [ln[:90] for ln in plan]
        # Premise 2: the starvation is real. Plain ef_search = 40 returns fewer than top_k rows on this data.
        await conn.execute("SELECT set_config('hnsw.iterative_scan', 'off', true), set_config('hnsw.ef_search', '40', true)")
        starved = await conn.fetch(vr._DENSE_SQL, *args)
        assert len(starved) < 5, len(starved)

    # The repository, on the same HNSW-driven plan, must still return top_k.
    repo = PgVectorRepository()
    test_pool = pool
    if mode == "local":  # a pool without the startup parameters: the repository must compensate per query
        test_pool = await asyncpg.create_pool(os.environ["POSTGRES_DSN"], min_size=1, max_size=2)
    try:
        async with _hnsw_forced(test_pool) as conn:
            if mode == "fallback":
                repo._hnsw_mode = "fallback"
            found = await repo.search(conn, [str(small["id"])], embed_text("alpha beta gamma"), 5, -1.0)
            if mode != "fallback":
                assert repo._hnsw_mode == mode
    finally:
        if test_pool is not pool:
            await test_pool.close()
    assert len(found) == 5 and all(m.kb_id == str(small["id"]) for m in found)


# --- fusion and dedupe (pure) --------------------------------------------------------------------------------

def test_rrf_prefers_chunks_both_branches_found():
    dense = [_match("a"), _match("b"), _match("c")]
    lexical = [_match("c", found_by=("lexical",)), _match("d", found_by=("lexical",)), _match("a", found_by=("lexical",))]
    fused = rrf_merge([dense, lexical])
    assert [m.chunk_id for m in fused] == ["a", "c", "b", "d"]  # a: 1/61+1/63, c: 1/63+1/61 tie -> higher score order
    assert set(next(m for m in fused if m.chunk_id == "a").found_by) == {"dense", "lexical"}


def test_rrf_uses_k_60_so_agreement_beats_a_single_first_place():
    # y: 1/62 + 1/62 = .0323 (both lists, rank 2) beats x: 1/61 = .0164 (rank 1, one list). A steeper
    # decay (k small) would put x first, so this pins the constant.
    fused = rrf_merge([[_match("x"), _match("y")], [_match("z", found_by=("lexical",)), _match("y", found_by=("lexical",))]])
    assert fused[0].chunk_id == "y"


def test_rrf_single_list_keeps_order_and_scores_use_k_60():
    assert [m.chunk_id for m in rrf_merge([[_match("x"), _match("y")]])] == ["x", "y"]
    # rank 1 in one list (1/61) beats rank 3 in two lists only if 2/(60+3) < 1/61 is false; check the arithmetic.
    one = [_match("solo")]
    two = [_match("p"), _match("q"), _match("both")]
    fused = rrf_merge([one, two, [_match("both")] ])
    # both: 1/63 + 1/61 = .03226; solo: 1/61 = .01639; p: 1/61.
    assert fused[0].chunk_id == "both"


def test_dedupe_collapses_children_of_one_parent_and_overlaps():
    children = [
        _match("c1", parent_id="P", content="whole section text with c1 and c2"),
        _match("c2", parent_id="P", content="a different rendering of the same section"),
        _match("c3", parent_id=None, content="alpha beta gamma delta"),
        _match("c4", parent_id=None, content="beta gamma"),  # inside c3's text, same document
        _match("c5", parent_id=None, content="beta gamma", document_id="other-doc"),
    ]
    assert [m.chunk_id for m in dedupe_sections(children)] == ["c1", "c3", "c5"]


async def test_parent_expansion_returns_the_section_once(pool, world):
    section = ("Warranty. Gizmo warranty covers two years of parts. Claims need a receipt. "
               "The warranty is void if the gizmo is opened.")
    await add_document(pool, world["tenant"], world["kb"], "Warranty",
                       ["Gizmo warranty covers two years of parts.", "Claims need a receipt for the gizmo warranty."],
                       parent=section)
    result = await _run(world["tenant"], world["agent"], world["embedder"], "gizmo warranty claims receipt")
    hits = [c for c in result["chunks"] if c["source"]["document_title"] == "Warranty"]
    assert [c["content"] for c in hits] == [section]  # two children matched, one parent returned


async def test_parent_too_big_for_the_budget_falls_back_to_its_child(pool, world):
    section = "Gizmo warranty " + " ".join(f"filler{i}" for i in range(200))
    await add_document(pool, world["tenant"], world["kb"], "Warranty", ["Gizmo warranty covers two years."],
                       parent=section)
    await add_document(pool, world["tenant"], world["kb"], "Prompt", ["Always greet politely."],
                       usage_mode="prompt", embed=False)
    result = await _run(world["tenant"], world["agent"], world["embedder"], "gizmo warranty", max_tokens=30)
    contents = [c["content"] for c in result["chunks"]]
    assert "Gizmo warranty covers two years." in contents and section not in contents


# --- plan cache ----------------------------------------------------------------------------------------------

_PLAN_TABLES = ("FROM agents", "agent_retrieval_policies", "agent_knowledge_bases", "provider_configs", "kb_documents d")


def _spy_queries(monkeypatch) -> list[str]:
    seen: list[str] = []
    for name in ("fetch", "fetchrow", "fetchval", "execute"):
        original = getattr(asyncpg.Connection, name)

        def make(orig):
            async def spy(self, query, *args, **kw):
                seen.append(query)
                return await orig(self, query, *args, **kw)
            return spy
        monkeypatch.setattr(asyncpg.Connection, name, make(original))
    return seen


async def test_warm_hot_path_makes_no_plan_queries_and_only_the_search_queries(world, monkeypatch):
    repo = PgVectorRepository()  # one process-wide instance in production: its capability probe runs once
    await _run(world["tenant"], world["agent"], world["embedder"], "refunds processed", repo=repo)  # cold
    await retrieval_log.flush()
    seen = _spy_queries(monkeypatch)
    result = await _run(world["tenant"], world["agent"], world["embedder"], "refunds processed days", repo=repo)
    seen = list(seen)  # snapshot before the background log writer runs: its own queries are off the hot path
    await retrieval_log.flush()
    assert result["retrieval_metadata"]["plan_cache"] == "hit" and result["status"] == "ok"
    plan_queries = [q for q in seen if any(t in q for t in _PLAN_TABLES) and "kb_chunks" not in q]
    assert plan_queries == []
    non_log = [q for q in seen if "retrieval_logs" not in q and q.strip().rstrip(";") not in ("BEGIN", "COMMIT")
               and "RESET ALL" not in q]  # asyncpg's pool-release reset is housekeeping, not a query of ours
    # per branch: one tenant-scope statement + one search statement; nothing else
    assert len(non_log) == 4, non_log


async def test_cold_plan_costs_queries_and_prewarm_removes_them(world, monkeypatch):
    seen = _spy_queries(monkeypatch)
    set_caller_tenant(str(world["tenant"]["id"]))
    assert await plans.prewarm(world["tenant"]["slug"], world["agent"]["slug"],
                               embedding_manager=make_manager(world["embedder"])) is True
    assert len([q for q in seen if "FROM agents" in q or "agent_knowledge_bases" in q]) >= 2
    result = await _run(world["tenant"], world["agent"], world["embedder"], "refund")
    assert result["retrieval_metadata"]["plan_cache"] == "hit"


async def test_plan_invalidates_when_a_kb_is_assigned_or_disabled(pool, world):
    from services.knowledge import agent_kb as agent_kb_service
    t, a = world["tenant"], world["agent"]
    first = await _run(t, a, world["embedder"], "orbital docking procedure")
    assert first["status"] == "no_match"
    extra = await make_kb(pool, t, a, "extra", assign=False)
    await add_document(pool, t, extra, "Docking", ["Orbital docking procedure requires two operators."])
    assert (await _run(t, a, world["embedder"], "orbital docking procedure"))["status"] == "no_match"  # cached plan
    await agent_kb_service.assign(a["id"], extra["id"])  # explicit invalidate
    assert (await _run(t, a, world["embedder"], "orbital docking procedure"))["status"] == "ok"
    await agent_kb_service.set_enabled(a["id"], extra["id"], enabled=False)
    assert (await _run(t, a, world["embedder"], "orbital docking procedure"))["status"] == "no_match"


async def test_plan_picks_up_policy_changes(world):
    t, a = world["tenant"], world["agent"]
    assert (await _run(t, a, world["embedder"], "refund 30 days"))["retrieval_metadata"]["top_k"] == 5
    await policy_service.upsert_policy(a["id"], top_k=1)
    assert (await _run(t, a, world["embedder"], "refund 30 days"))["retrieval_metadata"]["top_k"] == 1


def test_ttl_cache_expires_and_bounds_its_size():
    now = [0.0]
    c = cache.TTLCache(ttl_s=10, max_entries=2, clock=lambda: now[0])
    c.set("a", 1); c.set("b", 2); c.set("c", 3)
    assert c.get("a") is None and c.get("b") == 2  # LRU bound
    now[0] = 11
    assert c.get("b") is None  # TTL


async def test_a_cached_plan_is_never_served_to_another_tenants_caller(world, other_tenant_admin):
    t, a = world["tenant"], world["agent"]
    await _run(t, a, world["embedder"], "refund")
    set_caller_tenant(str(other_tenant_admin["tenant"]["id"]))
    with pytest.raises(TenantScopeConflict):
        await retrieve(None, PgVectorRepository(), make_manager(world["embedder"]),
                       tenant_slug=t["slug"], agent_slug=a["slug"], query="refund")


async def test_a_probe_of_another_tenants_slug_plants_nothing_in_the_cache(world, other_tenant_admin, app_pool):
    t, a = world["tenant"], world["agent"]
    foreign = other_tenant_admin["tenant"]
    set_caller_tenant(str(foreign["id"]))
    result = await retrieve(None, PgVectorRepository(), make_manager(world["embedder"]), pool=app_pool,
                            tenant_slug=t["slug"], agent_slug=a["slug"], query="refunds processed days")
    assert result["status"] == "no_match" and result["chunks"] == []  # RLS hides the agent from the probe
    assert (await _run(t, a, world["embedder"], "refunds processed days", pool=app_pool))["status"] == "ok"


# --- query-embedding cache -----------------------------------------------------------------------------------

def _load_tripwire():
    path = pathlib.Path(__file__).resolve().parents[3] / "tests" / "test_rls_coverage.py"
    spec = importlib.util.spec_from_file_location("rls_coverage_for_cache_keys", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._is_tenant_safe_cache_key


def test_cache_keys_pass_the_tenant_safe_tripwire():
    safe = _load_tripwire()
    tid, cid = str(uuid.uuid4()), str(uuid.uuid4())
    assert safe(cache.embedding_key(tid, cid, "How long?"))
    assert safe(cache.plan_key("acme", "sales"), tenant_prefix="acme")
    assert not safe("qemb:" + "deadbeef")  # the classifier still rejects a tenant-less key


def test_embedding_key_separates_tenants_configs_and_normalises_text():
    cid = str(uuid.uuid4())
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert cache.embedding_key(a, cid, "Refund  POLICY ") == cache.embedding_key(a, cid, "refund policy")
    assert cache.embedding_key(a, cid, "refund") != cache.embedding_key(b, cid, "refund")  # tenant in the key
    assert cache.embedding_key(a, cid, "refund") != cache.embedding_key(a, str(uuid.uuid4()), "refund")


async def test_embedding_cache_hits_for_same_tenant_and_never_crosses_tenants(pool, world, other_tenant_admin):
    t, a, embedder = world["tenant"], world["agent"], world["embedder"]
    await _run(t, a, embedder, "How long do REFUNDS take")
    await _run(t, a, embedder, "  how long do refunds   take ")
    assert embedder.calls == 1

    other = other_tenant_admin["tenant"]
    other_agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'b-agent', 'B') RETURNING *", other["id"]))
    other_embedder = FakeEmbedder()
    try:
        kb_b = await make_kb(pool, other, other_agent, "b-kb")
        await add_document(pool, other, kb_b, "B Refunds", ["B tenant refunds take 10 days."])
        result = await _run(other, other_agent, other_embedder, "how long do refunds take")
        assert other_embedder.calls == 1  # tenant A's cached vector was not served to tenant B
        assert [c["content"] for c in result["chunks"]] == ["B tenant refunds take 10 days."]
        assert all("30 days" not in c["content"] for c in result["chunks"])
    finally:
        await retrieval_log.flush()
        await pool.execute("DELETE FROM retrieval_logs WHERE tenant_id = $1", other["id"])
        await pool.execute("DELETE FROM agent_knowledge_bases WHERE agent_id = $1", other_agent["id"])
        await pool.execute("DELETE FROM kb_chunks WHERE tenant_id = $1", other["id"])
        await pool.execute("DELETE FROM kb_documents WHERE tenant_id = $1", other["id"])
        await pool.execute("DELETE FROM knowledge_bases WHERE tenant_id = $1", other["id"])
        await pool.execute("DELETE FROM agents WHERE id = $1", other_agent["id"])
        await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", other["id"])


# --- tenant isolation on both branches -----------------------------------------------------------------------

async def test_rls_holds_on_dense_and_lexical_branches(app_pool, world, other_tenant_admin):
    other = other_tenant_admin["tenant"]
    set_caller_tenant(str(other["id"]))
    repo = PgVectorRepository()
    async with tenant_conn(app_pool) as conn:  # tenant B's connection, tenant A's KB id
        dense = await repo.search(conn, [str(world["kb"]["id"])], embed_text(REFUND), 5, -1.0)
        lexical = await repo.lexical_search(conn, [str(world["kb"]["id"])], "refunds processed days", 5, 0.0,
                                            approx_chunks=100)
    assert dense == [] and lexical == []
    # And the same ids under the owner's scope do return rows (the test is not vacuous).
    set_caller_tenant(str(world["tenant"]["id"]))
    async with tenant_conn(app_pool) as conn:
        assert await repo.search(conn, [str(world["kb"]["id"])], embed_text(REFUND), 5, -1.0)
        assert await repo.lexical_search(conn, [str(world["kb"]["id"])], "refunds processed days", 5, 0.0,
                                         approx_chunks=100)


# --- fallback ladder -----------------------------------------------------------------------------------------

class FaultRepo(PgVectorRepository):
    """Wraps the real repository; slows the DB with pg_sleep or raises, per branch."""

    def __init__(self, dense_sleep=0.0, lexical_sleep=0.0, dense_fail=False, lexical_fail=False):
        super().__init__()
        self.dense_sleep, self.lexical_sleep = dense_sleep, lexical_sleep
        self.dense_fail, self.lexical_fail = dense_fail, lexical_fail

    async def search(self, conn, *a, **kw):
        if self.dense_sleep:
            await conn.execute("SELECT pg_sleep($1)", self.dense_sleep)
        if self.dense_fail:
            raise asyncpg.PostgresError("dense exploded")
        return await super().search(conn, *a, **kw)

    async def lexical_search(self, conn, *a, **kw):
        if self.lexical_sleep:
            await conn.execute("SELECT pg_sleep($1)", self.lexical_sleep)
        if self.lexical_fail:
            raise asyncpg.PostgresError("lexical exploded")
        return await super().lexical_search(conn, *a, **kw)


async def test_embedder_down_answers_from_lexical_only(world):
    result = await _run(world["tenant"], world["agent"], FakeEmbedder(fail=True), "refunds processed days")
    assert result["status"] == "partial"
    assert {"embedding_unavailable", "lexical_only"} <= set(result["degraded"])
    assert result["chunks"][0]["content"] == REFUND


async def test_lexical_error_answers_from_dense_only(world):
    result = await _run(world["tenant"], world["agent"], world["embedder"], "refunds processed days",
                        repo=FaultRepo(lexical_fail=True))
    assert result["status"] == "partial"
    assert {"lexical_failed", "dense_only"} <= set(result["degraded"])
    assert result["chunks"][0]["content"] == REFUND


async def test_dense_search_error_answers_from_lexical_only(world):
    result = await _run(world["tenant"], world["agent"], world["embedder"], "refunds processed days",
                        repo=FaultRepo(dense_fail=True))
    assert {"dense_failed", "lexical_only"} <= set(result["degraded"]) and result["status"] == "partial"
    assert result["chunks"][0]["content"] == REFUND


async def test_both_branches_failing_raises_a_typed_error_and_logs_it(pool, world):
    with pytest.raises(RetrievalUnavailable) as exc:
        await _run(world["tenant"], world["agent"], FakeEmbedder(fail=True), "refunds",
                   repo=FaultRepo(lexical_fail=True))
    assert exc.value.trace_id and "lexical_failed" in exc.value.degraded
    await retrieval_log.flush()
    row = await pool.fetchrow("SELECT status, trace_id FROM retrieval_logs WHERE trace_id = $1", exc.value.trace_id)
    assert row["status"] == "unavailable"


async def test_both_failing_but_prompt_docs_exist_still_answers_with_them(pool, world):
    await add_document(pool, world["tenant"], world["kb"], "Rules", ["Always greet politely."],
                       usage_mode="prompt", embed=False)
    result = await _run(world["tenant"], world["agent"], FakeEmbedder(fail=True), "refunds",
                        repo=FaultRepo(lexical_fail=True))
    assert result["status"] == "partial"
    assert [c["content"] for c in result["chunks"]] == ["Always greet politely."]


async def test_partial_with_no_chunks_is_not_reported_as_no_match(world):
    result = await _run(world["tenant"], world["agent"], FakeEmbedder(fail=True), "quantum lattice")
    assert result["status"] == "partial" and result["chunks"] == []


# --- deadline and timeouts -----------------------------------------------------------------------------------

async def test_hung_embedder_cannot_hold_the_call_past_the_deadline(world):
    t0 = time.monotonic()
    result = await _run(world["tenant"], world["agent"], FakeEmbedder(latency_s=5.0), "refunds processed days")
    elapsed = time.monotonic() - t0
    assert elapsed < 0.4, elapsed
    assert result["status"] == "partial" and "lexical_only" in result["degraded"]
    assert result["chunks"][0]["content"] == REFUND


async def test_slow_embedder_costs_only_the_grace_once_lexical_has_matches(world):
    cfg = RetrievalConfig(dense_grace_s=0.05, deadline_s=0.8, embed_timeout_override_s=0.6)
    t0 = time.monotonic()
    result = await _run(world["tenant"], world["agent"], FakeEmbedder(latency_s=0.5), "refunds processed days",
                        config=cfg)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.3, elapsed                      # not the embedder's 0.5 s
    assert result["chunks"][0]["content"] == REFUND
    assert "lexical_only" in result["degraded"] and "deadline" not in result["degraded"]
    assert result["retrieval_metadata"]["timings_ms"]["dense_grace_cut"] == 1.0


async def test_every_grace_cut_counts_against_the_embedder_until_its_breaker_opens(world):
    cfg = RetrievalConfig(dense_grace_s=0.04, deadline_s=0.8, embed_timeout_override_s=0.6, breaker_failures=3)
    slow = FakeEmbedder(latency_s=0.5)
    for _ in range(3):
        result = await _run(world["tenant"], world["agent"], slow, "refunds processed days", config=cfg)
        assert result["chunks"][0]["content"] == REFUND
    assert {b.state for k, b in resilience.breakers._breakers.items() if k.startswith("embed:")} == {"open"}
    calls_before = slow.calls
    t0 = time.monotonic()
    result = await _run(world["tenant"], world["agent"], slow, "refunds processed days", config=cfg)
    assert time.monotonic() - t0 < 0.15 and slow.calls == calls_before        # open: no wait, no provider call
    assert result["chunks"][0]["content"] == REFUND


async def test_the_grace_is_off_unless_configured(world):
    assert RetrievalConfig().dense_grace_s is None
    result = await _run(world["tenant"], world["agent"], FakeEmbedder(latency_s=0.2), "how long do refunds take",
                        config=RetrievalConfig(deadline_s=0.8, embed_timeout_override_s=0.6))
    assert result["degraded"] == [] and "dense_grace_cut" not in result["retrieval_metadata"]["timings_ms"]


def test_grace_env_parsing_is_defensive(monkeypatch):
    for raw, expected in ((None, None), ("", None), ("  ", None), ("abc", None), ("nan", None), ("100", 0.1),
                          ("0", 0.0), ("-5", 0.0), ("99999", 2.0)):
        if raw is None:
            monkeypatch.delenv("KNOWLEDGE_DENSE_GRACE_MS", raising=False)
        else:
            monkeypatch.setenv("KNOWLEDGE_DENSE_GRACE_MS", raw)
        assert RetrievalConfig.from_env().dense_grace_s == expected, raw


async def test_fast_embedder_still_joins_the_fusion(world):
    cfg = RetrievalConfig(dense_grace_s=0.2, deadline_s=0.8, embed_timeout_override_s=0.6)
    result = await _run(world["tenant"], world["agent"], FakeEmbedder(latency_s=0.03), "how long do refunds take",
                        config=cfg)
    assert result["chunks"][0]["content"] == REFUND
    assert result["degraded"] == [] and "dense_grace_cut" not in result["retrieval_metadata"]["timings_ms"]


async def test_lexical_without_matches_still_waits_for_the_dense_branch(world):
    # Words that appear in no document: lexical finds nothing, so the slower dense branch is the only evidence.
    cfg = RetrievalConfig(dense_grace_s=0.0, deadline_s=0.8, embed_timeout_override_s=0.6)
    result = await _run(world["tenant"], world["agent"], FakeEmbedder(latency_s=0.15), "zzqx unrelated words",
                        config=cfg)
    assert "dense_grace_cut" not in result["retrieval_metadata"]["timings_ms"]
    assert "deadline" not in result["degraded"]


async def test_wait_lexical_first_distinguishes_a_cut_from_a_missed_deadline():
    from types import SimpleNamespace

    async def branch(delay, matches):
        await asyncio.sleep(delay)
        return SimpleNamespace(matches=matches)

    t0 = time.monotonic()
    tasks = {"lexical": asyncio.create_task(branch(0.0, [object()])), "dense": asyncio.create_task(branch(1.0, [object()]))}
    done, pending, cut = await retrieval._wait_lexical_first(tasks, lambda: 5.0, 0.05)
    assert cut and pending == {tasks["dense"]} and time.monotonic() - t0 < 0.3
    tasks["dense"].cancel()

    tasks = {"lexical": asyncio.create_task(branch(0.0, [])), "dense": asyncio.create_task(branch(0.1, [object()]))}
    done, pending, cut = await retrieval._wait_lexical_first(tasks, lambda: 5.0, 0.0)
    assert not cut and not pending and len(done) == 2                # empty lexical: dense is awaited in full

    tasks = {"dense": asyncio.create_task(branch(1.0, [object()]))}
    done, pending, cut = await retrieval._wait_lexical_first(tasks, lambda: 0.05, 0.1)
    assert not cut and pending                                       # a real deadline miss stays a deadline miss
    tasks["dense"].cancel()


async def test_slow_database_returns_unavailable_inside_the_deadline(world):
    await _run(world["tenant"], world["agent"], world["embedder"], "warm the plan")
    t0 = time.monotonic()
    with pytest.raises(RetrievalUnavailable):
        await _run(world["tenant"], world["agent"], world["embedder"], "refunds",
                   repo=FaultRepo(dense_sleep=3.0, lexical_sleep=3.0))
    assert time.monotonic() - t0 < 0.45


async def test_overall_deadline_returns_what_has_finished_even_when_stage_timeouts_are_loose(world):
    cfg = RetrievalConfig(embed_timeout_s=5, dense_timeout_s=5, lexical_timeout_s=5, deadline_s=0.25)
    await _run(world["tenant"], world["agent"], world["embedder"], "warm the plan")
    t0 = time.monotonic()
    result = await _run(world["tenant"], world["agent"], world["embedder"], "refunds processed days",
                        repo=FaultRepo(dense_sleep=3.0), config=cfg)
    assert time.monotonic() - t0 < 0.35
    assert "deadline" in result["degraded"] and "lexical_only" in result["degraded"]
    assert result["chunks"][0]["content"] == REFUND


# --- circuit breakers ----------------------------------------------------------------------------------------

def test_breaker_opens_then_probes_once_after_cooldown():
    now = [0.0]
    b = CircuitBreaker(failures=3, cooldown_s=10, clock=lambda: now[0])
    for _ in range(2):
        b.record_failure()
    assert b.allow() and b.state == "closed"
    b.record_failure()
    assert b.state == "open" and not b.allow()
    now[0] = 10
    assert b.state == "half_open" and b.allow() and not b.allow()  # exactly one probe
    b.record_failure()
    assert b.state == "open"
    now[0] = 21
    assert b.allow()
    b.record_success()
    assert b.state == "closed" and b.allow()


async def test_embedder_breaker_stops_calling_a_dead_embedder_and_recovers(world):
    cfg = RetrievalConfig(breaker_failures=2, breaker_cooldown_s=0.3)
    dead = FakeEmbedder(fail=True)
    for q in ("refunds one", "refunds two"):
        await _run(world["tenant"], world["agent"], dead, q, config=cfg)
    assert dead.calls == 2
    result = await _run(world["tenant"], world["agent"], dead, "refunds three", config=cfg)
    assert dead.calls == 2  # breaker open: the embedder was not even called
    assert "embedding_unavailable" in result["degraded"]
    await asyncio.sleep(0.35)
    dead.fail = False
    ok = await _run(world["tenant"], world["agent"], dead, "refunds processed days", config=cfg)
    assert dead.calls == 3 and ok["status"] == "ok"  # half-open probe succeeded, breaker closed


# --- rerank hook ---------------------------------------------------------------------------------------------

class _Reverse:
    configured = True

    async def rerank(self, query, matches):
        return list(reversed(matches))


class _Reverse_Counting:
    configured = True
    calls = 0

    async def rerank(self, query, matches):
        type(self).calls += 1
        return list(reversed(matches))


class _Slow:
    configured = True

    async def rerank(self, query, matches):
        await asyncio.sleep(2)
        return matches


class _Broken:
    configured = True
    calls = 0

    async def rerank(self, query, matches):
        type(self).calls += 1
        raise RuntimeError("reranker down")


async def _agent_rerank(world, on):
    set_caller_tenant(str(world["tenant"]["id"]))
    await policy_service.upsert_policy(world["agent"]["id"], rerank=on)
    retrieval.clear_caches()


async def test_a_request_cannot_turn_rerank_on_when_the_agent_policy_is_off(world):
    for policy in (None, False):
        if policy is not None:
            await _agent_rerank(world, False)
        _Broken.calls = 0
        result = await _run(world["tenant"], world["agent"], world["embedder"], "refund shipping days",
                            minimum_score=0.0, rerank=True, reranker=_Reverse_Counting())
        assert _Reverse_Counting.calls == 0 and "rerank_skipped" not in result["degraded"]
        assert result["retrieval_metadata"]["rerank"] is False


async def test_a_request_may_turn_rerank_off_for_one_call(world):
    await _agent_rerank(world, True)
    _Reverse_Counting.calls = 0
    off = await _run(world["tenant"], world["agent"], world["embedder"], "refund shipping days", minimum_score=0.0,
                     rerank=False, reranker=_Reverse_Counting())
    assert _Reverse_Counting.calls == 0 and off["retrieval_metadata"]["rerank"] is False
    on = await _run(world["tenant"], world["agent"], world["embedder"], "refund shipping days", minimum_score=0.0,
                    reranker=_Reverse_Counting())  # no request value: the agent's policy applies
    assert _Reverse_Counting.calls == 1 and on["retrieval_metadata"]["rerank"] is True


async def test_rerank_flag_with_no_reranker_is_reported_as_skipped(world):
    await _agent_rerank(world, True)
    result = await _run(world["tenant"], world["agent"], world["embedder"], "refund shipping", rerank=True)
    assert "rerank_skipped" in result["degraded"] and result["status"] == "ok"


async def test_rerank_off_never_calls_the_reranker(world):
    result = await _run(world["tenant"], world["agent"], world["embedder"], "refund shipping", rerank=False,
                        reranker=_Broken())
    assert "rerank_skipped" not in result["degraded"] and _Broken.calls == 0


async def test_configured_reranker_reorders(world):
    await _agent_rerank(world, True)
    plain = await _run(world["tenant"], world["agent"], world["embedder"], "refund shipping days", minimum_score=0.0)
    flipped = await _run(world["tenant"], world["agent"], world["embedder"], "refund shipping days",
                         minimum_score=0.0, rerank=True, reranker=_Reverse())
    assert [c["content"] for c in flipped["chunks"]] == [c["content"] for c in reversed(plain["chunks"])]


async def test_slow_reranker_is_dropped_and_fused_order_kept_within_budget(world):
    await _agent_rerank(world, True)
    t0 = time.monotonic()
    result = await _run(world["tenant"], world["agent"], world["embedder"], "refund shipping days",
                        minimum_score=0.0, rerank=True, reranker=_Slow())
    assert time.monotonic() - t0 < 0.45
    assert "rerank_skipped" in result["degraded"] and result["status"] == "ok"


async def test_reranker_breaker_opens_after_repeated_failures(world):
    await _agent_rerank(world, True)
    cfg = RetrievalConfig(breaker_failures=2, breaker_cooldown_s=60)
    _Broken.calls = 0
    for i in range(4):
        r = await _run(world["tenant"], world["agent"], world["embedder"], f"refunds processed days {i}", rerank=True,
                       reranker=_Broken(), config=cfg)
        assert "rerank_skipped" in r["degraded"]
    assert _Broken.calls == 2


# --- low confidence ------------------------------------------------------------------------------------------

async def test_close_low_scores_are_ok_with_low_confidence(pool, world):
    t, kb = world["tenant"], world["kb"]
    await add_document(pool, t, kb, "Twin A", ["quartz cabinet lamp sofa"])
    await add_document(pool, t, kb, "Twin B", ["quartz cabinet lamp sofa"])
    result = await _run(t, world["agent"], world["embedder"], "quartz tulip nebula orbit", minimum_score=0.05,
                        hybrid_search=False)
    assert result["status"] == "ok" and result["low_confidence"] is True


async def test_a_clear_winner_is_not_low_confidence(world):
    result = await _run(world["tenant"], world["agent"], world["embedder"], REFUND)
    assert result["low_confidence"] is False


# --- retrieval_logs ------------------------------------------------------------------------------------------

async def test_every_retrieval_writes_a_tenant_scoped_log_row(pool, app_pool, world, other_tenant_admin):
    result = await _run(world["tenant"], world["agent"], world["embedder"], "refunds processed days " + "x" * 2000)
    await retrieval_log.flush()
    set_caller_tenant(str(world["tenant"]["id"]))
    async with tenant_conn(app_pool) as conn:
        row = await conn.fetchrow("SELECT * FROM retrieval_logs WHERE trace_id = $1", result["trace_id"])
    assert row["status"] == result["status"] and len(row["query"]) == 500
    assert row["degraded"] == [] and str(row["agent_id"]) == str(world["agent"]["id"])
    assert {"plan", "total"} <= set(json.loads(row["timings_ms"]))
    assert [str(c) for c in row["chunk_ids"]] == [c["source"]["chunk_id"] for c in result["chunks"]]
    set_caller_tenant(str(other_tenant_admin["tenant"]["id"]))
    async with tenant_conn(app_pool) as conn:  # RLS: the other tenant sees nothing
        assert await conn.fetchval("SELECT count(*) FROM retrieval_logs WHERE trace_id = $1", result["trace_id"]) == 0


async def test_a_failing_log_insert_never_touches_the_response(world, monkeypatch):
    async def boom(row):
        raise RuntimeError("db down")
    monkeypatch.setattr(retrieval_log, "_insert", boom)
    result = await _run(world["tenant"], world["agent"], world["embedder"], "refunds processed days")
    assert result["status"] == "ok"
    await retrieval_log.flush()


async def test_log_queue_is_bounded_and_record_never_blocks(monkeypatch):
    gate = asyncio.Event()

    async def stuck(row):
        await gate.wait()
    monkeypatch.setattr(retrieval_log, "_insert", stuck)
    monkeypatch.setattr(retrieval_log, "_QUEUE_MAX", 3)
    retrieval_log._queue = None  # fresh queue with the small bound
    before = retrieval_log.dropped
    row = retrieval_log.RetrievalLogRow("t", None, "x", "q", "ok", [], [], None, 0, {})
    t0 = time.monotonic()
    for _ in range(20):
        retrieval_log.record(row)
    assert time.monotonic() - t0 < 0.05
    assert retrieval_log.dropped - before >= 15  # 3 queued + 1 in flight, the rest dropped
    gate.set()
    await retrieval_log.flush()
    retrieval_log._queue = None


async def test_policy_defaults_keep_the_documented_precedence(pool, world):
    set_caller_tenant(str(world["tenant"]["id"]))
    async with tenant_conn(pool) as conn:
        resolved = await retrieval._resolve_policy(conn, world["tenant"]["slug"], world["agent"]["slug"], {})
    assert resolved["hybrid_search"] is True and resolved["top_k"] == 5
    assert resilience.RetrievalConfig().deadline_s == 0.4 and db is not None
