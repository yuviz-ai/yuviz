"""retrieve() backs POST /internal/retrieve. Policy precedence: call override > agent row > system default.

Hot path: cached plan (no DB) -> dense (embed + pgvector) and lexical (tsvector) in parallel, each on its own
tenant-scoped connection -> Reciprocal Rank Fusion -> dedupe by parent section -> optional rerank -> token budget.
Every stage has a timeout and the call has a hard deadline; a stage that fails or is late is dropped and the
rest answers, with the fallback recorded in `degraded`. usage_mode='prompt' docs are always included first.

Always returns a dict. status: ok | no_match | partial | unavailable.
  ok        answered by every branch that was meant to run (a skipped reranker alone still counts as ok)
  no_match  every branch ran, nothing cleared the threshold (chunks: [])
  partial   a branch failed or missed the deadline; chunks are what the survivors found (possibly none, which is
            NOT "no match": the missing branch might have had one)
  unavailable  raised as RetrievalUnavailable when nothing could answer; the router turns it into this status.
Integration requirement: the router should call retrieve(None, ...) and release its own connection first. With
conn=None the two search branches run in parallel on their own pooled connections (2 per retrieval). If a
caller does still pass a connection it keeps it: retrieve() then runs the branches one after the other on that
connection (inside a savepoint) and takes nothing more from the pool, so a router that holds a connection around
the call cannot starve the pool. Either way an in-process limiter caps concurrent retrievals (overall and per
tenant) and answers past the cap with RetrievalUnavailable(degraded=['overloaded']); the router must map
RetrievalUnavailable to status 'unavailable'.

Score scales: minimum_score applies to dense similarity (cosine). Lexical hits are floored separately
(KNOWLEDGE_LEXICAL_MIN_SCORE) and carry ts_rank_cd scores, a different scale. A result whose best hit has no dense
support is low_confidence unless its lexical score reaches KNOWLEDGE_LEXICAL_CONFIDENT_SCORE.

Token budget: prompt-mode docs may use at most half of max_tokens (any beyond that are trimmed and reported), and
the best-ranked match is always returned, truncated to fit if it must be."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from array import array
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import asyncpg

from libs.tenancy import TenantScopeConflict, tenant_conn

from . import cache, db, ops_metrics, retrieval_log
from . import retrieval_plan as plans
from .embedding_manager import EmbeddingProviderManager, effective_model
from .resilience import (
    BoundedPool, PoolOverloaded, RetrievalConfig, RetrievalUnavailable, admission, breakers, resolve_embed_timeout_s, rerank_valve,
)
from .vector_repository import IVectorRepository, VectorMatch

log = logging.getLogger(__name__)

RRF_K = 60

_config = RetrievalConfig.from_env()

_embeddings = cache.TTLCache(ttl_s=600.0, max_entries=4096)


def clear_caches() -> None:
    """Drop the plan and query-embedding caches and breaker state (tests, admin reset)."""
    plans.clear_cache()
    plans.startup()  # re-open background refreshes after a lifespan shutdown
    _embeddings.clear()
    breakers.clear()
    rerank_valve.clear()


class IReranker(Protocol):
    """Phase 7 plugs a hosted reranker in here. rerank() returns the same matches, best first; it may rescore."""

    configured: bool

    async def rerank(self, query: str, matches: list[VectorMatch]) -> list[VectorMatch]: ...


class NoopReranker:
    configured = False

    async def rerank(self, query: str, matches: list[VectorMatch]) -> list[VectorMatch]:
        return matches


_NOOP_RERANKER = NoopReranker()


async def _agent_id_for(conn: asyncpg.Connection, tenant_slug: str, agent_slug: str) -> str | None:
    row = await conn.fetchrow(
        "SELECT a.id FROM agents a JOIN tenants t ON t.id = a.tenant_id "
        "WHERE t.slug = $1 AND a.slug = $2 AND a.deleted_at IS NULL AND t.deleted_at IS NULL",
        tenant_slug, agent_slug,
    )
    return str(row["id"]) if row is not None else None


async def _resolve_policy(
    conn: asyncpg.Connection, tenant_slug: str, agent_slug: str, overrides: dict[str, Any],
) -> dict[str, Any]:
    """Uncached policy resolution (the plan caches the agent-row half); kept for callers that hold a conn."""
    agent_id = await _agent_id_for(conn, tenant_slug, agent_slug)
    agent_policy_row = None
    if agent_id is not None:
        agent_policy_row = await conn.fetchrow(
            "SELECT * FROM agent_retrieval_policies WHERE agent_id = $1", agent_id,
        )
    return plans.merge_policy(agent_policy_row, overrides)


def _approx_token_count(text: str) -> int:
    return len(text.split())


# --- fusion ----------------------------------------------------------------------------------------------------

def rrf_merge(ranked_lists: list[list[VectorMatch]], k: int = RRF_K) -> list[VectorMatch]:
    """Reciprocal Rank Fusion: score(d) = sum over lists of 1/(k + rank). A chunk both branches found outranks one
    only a single branch found. The returned match keeps its dense cosine when it has one, else its lexical score."""
    fused_scores: dict[str, float] = {}
    best: dict[str, VectorMatch] = {}
    found_by: dict[str, list[str]] = {}
    for ranked in ranked_lists:
        for rank, match in enumerate(ranked, start=1):
            fused_scores[match.chunk_id] = fused_scores.get(match.chunk_id, 0.0) + 1.0 / (k + rank)
            found_by.setdefault(match.chunk_id, []).extend(match.found_by)
            current = best.get(match.chunk_id)
            if current is None or ("dense" in match.found_by and "dense" not in current.found_by):
                best[match.chunk_id] = match
    order = sorted(fused_scores, key=lambda cid: (-fused_scores[cid], -best[cid].score))
    return [dataclasses.replace(best[cid], found_by=tuple(dict.fromkeys(found_by[cid]))) for cid in order]


def dedupe_sections(matches: list[VectorMatch]) -> list[VectorMatch]:
    """Keep the best-ranked match per parent section, and drop a match whose text is already inside one kept
    from the same document (overlapping chunks). Order is preserved."""
    kept: list[VectorMatch] = []
    seen_sections: set[str] = set()
    texts: dict[str, list[str]] = {}
    for m in matches:
        section = m.parent_id or m.chunk_id
        if section in seen_sections:
            continue
        doc_texts = texts.setdefault(m.document_id, [])
        if any(m.content in t or t in m.content for t in doc_texts):
            continue
        seen_sections.add(section)
        doc_texts.append(m.content)
        kept.append(m)
    return kept


# --- branches --------------------------------------------------------------------------------------------------

@dataclass
class _Branch:
    name: str
    matches: list[VectorMatch] | None = None  # None = failed or unfinished
    error: str | None = None
    notes: list[str] | None = None
    embed_failed: bool = False
    overloaded: bool = False


_last_warned: dict[str, float] = {}


def _warn_rate_limited(key: str, message: str, *args: Any, every_s: float = 30.0) -> None:
    now = time.monotonic()
    if now - _last_warned.get(key, -every_s) >= every_s:
        _last_warned[key] = now
        log.warning(message, *args)


@asynccontextmanager
async def _branch_conn(held: asyncpg.Connection | None, bpool: BoundedPool, tenant_id: str):
    """The caller's connection (inside a savepoint, so a cancelled statement cannot poison the caller's
    transaction) or a fresh one scoped to the plan's tenant."""
    if held is not None:
        async with held.transaction():
            yield held
    else:
        async with tenant_conn(bpool, explicit_tenant=tenant_id, reason="retrieval-search") as conn:
            yield conn


async def _embed(
    plan: plans.RetrievalPlan, embedding_manager: EmbeddingProviderManager, config_id: str, query: str,
    cfg: RetrievalConfig, time_left: float,
) -> list[float]:
    key = cache.embedding_key(plan.tenant_id, config_id, query)
    hit = _embeddings.get(key)
    if hit is not None:
        return hit.tolist()
    breaker = breakers.get(f"embed:{config_id}", cfg)
    if not breaker.allow():
        raise RuntimeError("embedder circuit open")
    try:
        provider_cfg = plan.embedding_configs[config_id]
        budget = resolve_embed_timeout_s(provider_cfg.engine, provider_cfg.extra, cfg)
        async with asyncio.timeout(max(min(budget, time_left), 0.001)):
            provider = await embedding_manager.get(plan.embedding_configs[config_id])
            [vector] = await provider.embed([query])
    except asyncio.CancelledError:
        breaker.release_probe()  # the overall deadline cancelled us; that says nothing about the embedder
        raise
    except Exception as exc:
        breaker.record_failure()
        _warn_rate_limited(f"embed:{config_id}", "embedding failed embedding_config=%s error=%s",
                           config_id, type(exc).__name__)
        raise
    breaker.record_success()
    _embeddings.set(key, array("f", vector))
    return vector


async def _dense_branch(
    plan: plans.RetrievalPlan, vector_repo: IVectorRepository, embedding_manager: EmbeddingProviderManager,
    held: asyncpg.Connection | None, bpool: BoundedPool, query: str, limit: int, minimum_score: float,
    cfg: RetrievalConfig, timings: dict[str, float], remaining,
) -> _Branch:
    branch = _Branch("dense")
    t0 = time.monotonic()
    try:
        config_ids = list(plan.groups)
        vectors = await asyncio.gather(
            *[_embed(plan, embedding_manager, cid, query, cfg, remaining()) for cid in config_ids],
            return_exceptions=True,
        )
        timings["embed"] = (time.monotonic() - t0) * 1000
        for v in vectors:
            if isinstance(v, asyncio.CancelledError):
                raise v
            if isinstance(v, TenantScopeConflict):
                raise v
        usable = [(cid, v) for cid, v in zip(config_ids, vectors) if not isinstance(v, BaseException)]
        if not usable:
            branch.embed_failed = True
            branch.error = f"embedding failed: {type(vectors[0]).__name__}"
            return branch
        if len(usable) < len(config_ids):
            branch.notes = ["embed_partial"]
        t1 = time.monotonic()
        matches: list[VectorMatch] = []
        async with asyncio.timeout(max(min(cfg.dense_timeout_s, remaining()), 0.001)):
            async with _branch_conn(held, bpool, plan.tenant_id) as conn:
                dense_notes: list[str] = []
                for cid, vector in usable:
                    matches.extend(await vector_repo.search(
                        conn, plan.groups[cid], vector, limit, minimum_score,
                        model=effective_model(plan.embedding_configs[cid]), notes=dense_notes))
                if dense_notes:
                    branch.notes = list(dict.fromkeys([*(branch.notes or []), *dense_notes]))
        matches.sort(key=lambda m: m.score, reverse=True)
        branch.matches = matches[:limit]
        timings["dense"] = (time.monotonic() - t1) * 1000
    except (asyncio.CancelledError, TenantScopeConflict):
        raise
    except PoolOverloaded:
        branch.overloaded = True
        branch.error = "pool overloaded"
    except Exception as exc:  # noqa: BLE001 - a dead branch is dropped, never raised
        branch.error = branch.error or f"{type(exc).__name__}: {exc}"
        log.warning("dense branch failed: %s", branch.error)
    return branch


async def _lexical_branch(
    plan: plans.RetrievalPlan, vector_repo: IVectorRepository, held: asyncpg.Connection | None, bpool: BoundedPool,
    query: str, limit: int, cfg: RetrievalConfig, timings: dict[str, float], remaining,
) -> _Branch:
    branch = _Branch("lexical")
    t0 = time.monotonic()
    try:
        # Every stage, fallbacks included, gets min(its own timeout, what is left of the deadline).
        async with asyncio.timeout(max(min(cfg.lexical_timeout_s, remaining()), 0.001)):
            async with _branch_conn(held, bpool, plan.tenant_id) as conn:
                notes: list[str] = []
                branch.matches = await vector_repo.lexical_search(
                    conn, plan.kb_ids, query, limit, cfg.lexical_min_score, approx_chunks=plan.chunk_count,
                    notes=notes)
                branch.notes = notes or None
        timings["lexical"] = (time.monotonic() - t0) * 1000
    except (asyncio.CancelledError, TenantScopeConflict):
        raise
    except PoolOverloaded:
        branch.overloaded = True
        branch.error = "pool overloaded"
    except Exception as exc:  # noqa: BLE001
        branch.error = f"{type(exc).__name__}: {exc}"
        log.warning("lexical branch failed: %s", branch.error)
    return branch


def _swallow(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()  # mark retrieved so asyncio does not log "never retrieved"


# --- retrieve --------------------------------------------------------------------------------------------------

def _chunk_payload(m: VectorMatch) -> dict[str, Any]:
    return {
        "content": m.content,
        "score": m.score,
        "source": {
            "kb_id": m.kb_id, "kb_slug": m.kb_slug, "document_id": m.document_id,
            "document_title": m.document_title, "chunk_id": m.chunk_id, "page": m.page,
            "language": m.language, "tags": m.tags, "version": m.version, "section_path": m.section_path,
        },
    }


def _result(
    status: str, *, selected: list[VectorMatch], policy: dict[str, Any], degraded: list[str], trace_id: str,
    low_confidence: bool, metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": status,
        "chunks": [_chunk_payload(m) for m in selected],
        "sources": list(dict.fromkeys(m.document_title for m in selected)),
        "token_count": sum(m.token_count or _approx_token_count(m.content) for m in selected),
        "include_citations": policy["include_citations"],
        "retrieval_metadata": metadata,
        "degraded": degraded,
        "trace_id": trace_id,
        "low_confidence": low_confidence,
    }


def _truncate(m: VectorMatch, tokens: int) -> VectorMatch:
    words = m.content.split()
    return dataclasses.replace(m, content=" ".join(words[:tokens]), token_count=min(tokens, len(words)))


def _fit_prompt_docs(prompt: list[VectorMatch], max_tokens: int) -> tuple[list[VectorMatch], int]:
    """Prompt-mode docs may use at most half the budget so a ranked answer always has room. Returns the docs
    kept (in order; the first one is truncated rather than dropped) and how many were trimmed away."""
    cap = max(max_tokens // 2, 1)
    kept: list[VectorMatch] = []
    used = 0
    for doc in prompt:
        tokens = doc.token_count or _approx_token_count(doc.content)
        if used + tokens <= cap:
            kept.append(doc)
            used += tokens
        elif not kept:
            kept.append(_truncate(doc, cap))
            used = cap
            break
        else:
            break
    return kept, len(prompt) - len(kept)


def _is_low_confidence(fused: list[VectorMatch], cfg: RetrievalConfig) -> bool:
    if not fused:
        return False
    top = fused[0]
    if "dense" not in top.found_by:  # only lexical evidence: a different, weaker scale
        return top.score < cfg.lexical_confident_score
    second = fused[1] if len(fused) > 1 and "dense" in fused[1].found_by else None
    return second is not None and top.score < cfg.low_confidence_top and (top.score - second.score) < cfg.low_confidence_gap


async def retrieve(
    conn: asyncpg.Connection | None,
    vector_repo: IVectorRepository,
    embedding_manager: EmbeddingProviderManager,
    *,
    tenant_slug: str,
    agent_slug: str,
    query: str,
    top_k: int | None = None,
    max_tokens: int | None = None,
    minimum_score: float | None = None,
    rerank: bool | None = None,
    hybrid_search: bool | None = None,
    include_citations: bool | None = None,
    trace_id: str | None = None,
    pool: asyncpg.Pool | None = None,
    reranker: IReranker | None = None,
    config: RetrievalConfig | None = None,
) -> dict[str, Any]:
    cfg = config or _config
    started = time.monotonic()
    trace_id = trace_id or uuid.uuid4().hex
    # Cap before anything touches the text (NFKC, regexes, hashing, embedding): a megabyte query must cost
    # microseconds, not an event-loop stall every other tenant would feel.
    query = query[: cfg.max_query_chars]
    wait = min(cfg.queue_wait_s, max(cfg.deadline_s - (time.monotonic() - started), 0.0))
    if not await admission.acquire(tenant_slug, cfg, wait):
        ops_metrics.inc("retrieval_overloaded")
        raise RetrievalUnavailable("retrieval overloaded", degraded=["overloaded"], trace_id=trace_id)
    try:
        return await _retrieve(
            conn, vector_repo, embedding_manager, cfg, started, trace_id, tenant_slug, agent_slug, query,
            {"top_k": top_k, "max_tokens": max_tokens, "minimum_score": minimum_score, "rerank": rerank,
             "hybrid_search": hybrid_search, "include_citations": include_citations},
            pool, reranker,
        )
    finally:
        admission.release(tenant_slug)


def _lexical_has_matches(task: "asyncio.Task | None") -> bool:
    if task is None or not task.done() or task.cancelled() or task.exception() is not None:
        return False
    return bool(task.result().matches)


async def _wait_lexical_first(
    tasks: dict[str, "asyncio.Task"], remaining: Callable[[], float], grace_s: float | None,
) -> tuple[set, set, bool]:
    """Wait for the search branches like asyncio.wait, except that, when `grace_s` is set, a dense branch still
    embedding is not waited for beyond it once the lexical branch has answered with matches. A slow embedding
    provider (a distant region, a bad network) then costs one grace period instead of its whole budget, while a
    fast one still joins the fusion. Returns (done, pending, cut_early); cut_early means pending is only the dense
    branch we chose to drop because the grace ran out first, not a missed deadline."""
    all_tasks = set(tasks.values())
    if grace_s is None:
        done, pending = await asyncio.wait(all_tasks, timeout=max(remaining(), 0.0))
        return done, pending, False
    done, pending = await asyncio.wait(all_tasks, timeout=max(remaining(), 0.0), return_when=asyncio.FIRST_COMPLETED)
    dense = tasks.get("dense")
    if pending and _lexical_has_matches(tasks.get("lexical")) and dense in pending:
        budget = max(remaining(), 0.0)
        late, pending = await asyncio.wait(pending, timeout=min(grace_s, budget))
        return done | late, pending, bool(pending) and grace_s < budget
    if pending:
        late, pending = await asyncio.wait(pending, timeout=max(remaining(), 0.0))
        done |= late
    return done, pending, False


async def _retrieve(
    held: asyncpg.Connection | None, vector_repo: IVectorRepository, embedding_manager: EmbeddingProviderManager,
    cfg: RetrievalConfig, started: float, trace_id: str, tenant_slug: str, agent_slug: str, query: str,
    overrides: dict[str, Any], pool: asyncpg.Pool | None, reranker: IReranker | None,
) -> dict[str, Any]:
    deadline_at = started + cfg.deadline_s
    timings: dict[str, float] = {}
    degraded: list[str] = []

    def remaining() -> float:
        return deadline_at - time.monotonic()

    def finish_timings() -> dict[str, float]:
        timings["total"] = (time.monotonic() - started) * 1000
        return {k: round(v, 2) for k, v in timings.items()}

    # 1. plan: free when cached; a few queries on a cold miss, still inside the deadline.
    bpool = BoundedPool(pool or await db.get_pool(), cfg.acquire_timeout_s)
    try:
        async with asyncio.timeout(max(remaining(), 0.001)):
            plan, plan_cache = await plans.resolve_plan(tenant_slug, agent_slug, conn=held, pool=bpool)
    except TenantScopeConflict:
        raise
    except PoolOverloaded as exc:
        ops_metrics.inc("retrieval_overloaded")
        raise RetrievalUnavailable("retrieval overloaded", degraded=["overloaded"], trace_id=trace_id) from exc
    except Exception as exc:  # noqa: BLE001
        raise RetrievalUnavailable(
            f"retrieval plan unavailable: {type(exc).__name__}", degraded=["plan_unavailable"], trace_id=trace_id,
        ) from exc
    timings["plan"] = (time.monotonic() - started) * 1000
    # A cold plan is reported in retrieval_metadata, not in `degraded`: it is not a fallback, and `degraded` reaches
    # the model's payload and feeds the degraded-ratio alert.
    ops_metrics.inc("plan_cache_miss" if plan_cache == "miss" else "plan_cache_hit")

    policy = plans.merge_policy(None, {}) if plan is None else {
        key: (overrides[key] if overrides[key] is not None else plan.agent_policy[key]) for key in overrides
    }
    # Rerank sends the query and chunk text to a third party under the PLATFORM key, so the agent's policy is the
    # only thing that can switch it on: a request may turn it off for one call, never on.
    if plan is not None:
        policy["rerank"] = bool(plan.agent_policy["rerank"]) and overrides["rerank"] is not False
    if plan is None:
        return _result(
            "no_match", selected=[], policy=policy, degraded=degraded, trace_id=trace_id, low_confidence=False,
            metadata={"plan_cache": plan_cache, "top_k": policy["top_k"], "candidates": 0, "groups": 0, "rerank": policy["rerank"],
                      "hybrid_search": policy["hybrid_search"], "always_included": 0, "reason": "agent_not_found",
                      "timings_ms": finish_timings()},
        )
    degraded.extend(plan.degraded)

    limit = min(max(policy["top_k"] * 3, 10), 50)
    run_dense = bool(plan.groups)
    run_lexical = bool(plan.kb_ids) and bool(policy["hybrid_search"])

    # 2. dense and lexical. Own connections: in parallel. The caller's connection: one after the other on it.
    results: dict[str, _Branch] = {}

    def dense_coro():
        return _dense_branch(plan, vector_repo, embedding_manager, held, bpool, query, limit,
                             policy["minimum_score"], cfg, timings, remaining)

    def lexical_coro():
        return _lexical_branch(plan, vector_repo, held, bpool, query, limit, cfg, timings, remaining)

    async def sequential() -> None:
        if run_dense:
            results["dense"] = await dense_coro()
        if run_lexical:
            results["lexical"] = await lexical_coro()

    tasks: dict[str, asyncio.Task] = {}
    if held is not None:
        tasks["sequential"] = asyncio.create_task(sequential())
    else:
        if run_dense:
            tasks["dense"] = asyncio.create_task(dense_coro())
        if run_lexical:
            tasks["lexical"] = asyncio.create_task(lexical_coro())
    cut_early = False
    if tasks:
        done, pending, cut_early = await _wait_lexical_first(tasks, remaining, cfg.dense_grace_s)
        try:
            for name, task in tasks.items():
                if task in done:
                    outcome = task.result()  # re-raises TenantScopeConflict: that is an error, not a lost branch
                    if name != "sequential":
                        results[name] = outcome
        finally:
            for task in pending:
                task.cancel()
                task.add_done_callback(_swallow)
        if cut_early:
            ops_metrics.inc("dense_grace_cut")
            timings["dense_grace_cut"] = 1.0
            # A miss for each embedder that did not make the grace, so one that never does ends up skipped.
            for config_id in plan.groups:
                breakers.get(f"embed:{config_id}", cfg).record_failure()
        elif pending:
            degraded.append("deadline")

    # Policy says dense-only, but dense produced nothing (or cannot run): lexical beats silence.
    dense_res = results.get("dense")
    dense_dead = (run_dense and (dense_res is None or dense_res.matches is None)) or (
        not run_dense and "embedding_config_missing" in plan.degraded)
    if not run_lexical and plan.kb_ids and dense_dead and remaining() > 0.02:
        results["lexical"] = await _lexical_branch(
            plan, vector_repo, held, bpool, query, limit, cfg, timings, remaining)
        run_lexical = True

    ranked_lists: list[list[VectorMatch]] = []
    lost: list[str] = []
    intended = [n for n, on in (("dense", run_dense), ("lexical", run_lexical)) if on]
    for name in intended:
        res = results.get(name)
        if res is not None and res.matches is not None:
            ranked_lists.append(res.matches)
            degraded.extend(res.notes or [])
        else:
            lost.append(name)
            if res is not None and res.overloaded:
                degraded.append("overloaded")
            if name == "dense":
                degraded.append("embedding_unavailable" if res is not None and res.embed_failed else "dense_failed")
            else:
                degraded.append("lexical_failed")
    survivors = [n for n in intended if n not in lost]
    if intended and lost:
        if survivors == ["lexical"]:
            degraded.append("lexical_only")
        elif survivors == ["dense"]:
            degraded.append("dense_only")
    degraded[:] = list(dict.fromkeys(degraded))

    prompt_matches, trimmed = _fit_prompt_docs(list(plan.prompt_matches), policy["max_tokens"])
    if trimmed:
        degraded.append("prompt_docs_trimmed")
    if intended and not survivors and not prompt_matches:
        timings_out = finish_timings()
        _log(plan, trace_id, query, "unavailable", degraded, [], None, 0, timings_out)
        raise RetrievalUnavailable("every retrieval branch failed", degraded=degraded, trace_id=trace_id)

    # 3. fuse, dedupe by parent section, rerank.
    t_fuse = time.monotonic()
    fused = dedupe_sections(rrf_merge(ranked_lists)) if ranked_lists else []
    timings["fuse"] = (time.monotonic() - t_fuse) * 1000

    if policy["rerank"] and fused:
        rr = reranker or _NOOP_RERANKER
        if not getattr(rr, "configured", True):
            degraded.append("rerank_skipped")
        else:
            fused = await _rerank(rr, query, fused, cfg, remaining(), degraded, timings, plan.tenant_id)

    # 4. token budget. Prompt-mode docs come first (capped at half the budget); the best-ranked match is always
    # returned, truncated if it has to be.
    selected: list[VectorMatch] = list(prompt_matches)
    budget = policy["max_tokens"] - sum(m.token_count or _approx_token_count(m.content) for m in prompt_matches)
    for rank, match in enumerate(fused[: policy["top_k"]]):
        tokens = match.token_count or _approx_token_count(match.content)
        if (selected or rank == 0) and budget - tokens < 0:
            # The whole parent section does not fit; the matched child alone might.
            child_tokens = match.child_token_count or (
                _approx_token_count(match.child_content) if match.child_content else None)
            if match.child_content and match.child_content != match.content and child_tokens is not None \
                    and budget - child_tokens >= 0:
                match = dataclasses.replace(match, content=match.child_content, token_count=child_tokens)
                tokens = child_tokens
            elif rank == 0:
                match = _truncate(match, max(budget, 1))
                tokens = match.token_count or 1
            else:
                break
        selected.append(match)
        budget -= tokens

    low_confidence = _is_low_confidence(fused, cfg)
    status = "partial" if lost else ("ok" if selected else "no_match")
    timings_out = finish_timings()
    chunk_ids = [m.chunk_id for m in selected]
    top_score = fused[0].score if fused else None
    _log(plan, trace_id, query, status, degraded, chunk_ids, top_score, len(fused), timings_out)

    return _result(
        status, selected=selected, policy=policy, degraded=degraded, trace_id=trace_id, low_confidence=low_confidence,
        metadata={
            "top_k": policy["top_k"], "candidates": len(fused), "groups": len(plan.groups),
            "rerank": policy["rerank"], "hybrid_search": policy["hybrid_search"],
            "always_included": len(prompt_matches), "prompt_docs_trimmed": trimmed, "plan_cache": plan_cache, "timings_ms": timings_out,
            "dense_candidates": len(results["dense"].matches or []) if "dense" in results else 0,
            "lexical_candidates": len(results["lexical"].matches or []) if "lexical" in results else 0,
        },
    )


async def _rerank(
    reranker: IReranker, query: str, matches: list[VectorMatch], cfg: RetrievalConfig, time_left: float,
    degraded: list[str], timings: dict[str, float], tenant_id: str | None = None,
) -> list[VectorMatch]:
    # One breaker PER TENANT: a tenant whose own calls time out (a pathological agent, long documents) opens only its
    # own. The shared provider is guarded by rerank_valve, which opens only when several distinct tenants fail.
    breaker = breakers.get(f"rerank:{tenant_id or '-'}", cfg)
    budget = min(cfg.rerank_timeout_s, time_left)
    if budget <= 0.005 or not rerank_valve.allow():
        degraded.append("rerank_skipped")
        return matches
    if not breaker.allow():
        rerank_valve.release_probe()
        degraded.append("rerank_skipped")
        return matches
    t0 = time.monotonic()
    try:
        async with asyncio.timeout(budget):
            ordered = (await reranker.rerank(query, matches, tenant_id=tenant_id) if getattr(reranker, "budgeted", False)
                       else await reranker.rerank(query, matches))
    except asyncio.CancelledError:
        breaker.release_probe()
        rerank_valve.release_probe()
        raise
    except Exception as exc:  # noqa: BLE001 - fused order is the fallback
        if getattr(exc, "breaker_failure", True):
            breaker.record_failure()
            rerank_valve.record_failure(tenant_id or "-")
        else:
            breaker.release_probe()  # a quota or our own budget says nothing about the provider's health
            rerank_valve.release_probe()
        # Fixed reason codes only (RerankError.degraded_reason, or the stage budget); never provider text.
        reason = "rerank_timeout" if isinstance(exc, TimeoutError) else getattr(exc, "degraded_reason", "rerank_error")
        degraded.extend(["rerank_skipped", reason])
        return matches
    finally:
        timings["rerank"] = (time.monotonic() - t0) * 1000
    breaker.record_success()
    rerank_valve.record_success()
    return ordered


def _log(
    plan: plans.RetrievalPlan, trace_id: str, query: str, status: str, degraded: list[str],
    chunk_ids: list[str], top_score: float | None, candidates: int, timings_ms: dict[str, float],
) -> None:
    retrieval_log.record(retrieval_log.RetrievalLogRow(
        tenant_id=plan.tenant_id, agent_id=plan.agent_id, trace_id=trace_id, query=query, status=status,
        degraded=list(degraded), chunk_ids=chunk_ids, top_score=top_score, candidates=candidates,
        timings_ms=timings_ms,
    ))
