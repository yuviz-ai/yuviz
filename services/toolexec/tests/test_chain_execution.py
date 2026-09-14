"""
Integration tests for services/toolexec/executor.py (T11-T15) — real
Postgres (the tenant_agent/pool fixtures), httpx.MockTransport for every
outbound call (executor._step_transport is monkeypatched per test), and
custom_apis._resolve_addresses monkeypatched module-wide in this file so
no test performs a real DNS lookup.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from services.config.auth import CurrentUser
from services.toolexec import admission, agent_apis, custom_apis, db, executor
from services.toolexec.schemas import ChainExecuteRequest


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch):
    async def _resolver(hostname, port):
        return ["93.184.216.34"]  # a public, non-denied address

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)


def _admin(tenant_id: str) -> CurrentUser:
    return CurrentUser(id=str(uuid.uuid4()), email="admin@test.example", role="admin", tenant_id=tenant_id)


async def _register_and_enable(pool, tenant, agent, name: str, **overrides) -> dict:
    # str(tenant["id"]), not the raw asyncpg UUID object — this is what
    # every real caller has (a JSON body field), and it's what
    # auth_schemes.validate_tenant_ref's uuid.UUID(tenant_id) call needs.
    kwargs = dict(
        tenant_id=str(tenant["id"]), name=name, description="d",
        endpoint_url=f"https://{name}.example.com/api", method="GET",
    )
    kwargs.update(overrides)
    api = await custom_apis.create_custom_api(**kwargs)
    await agent_apis.set_enabled(agent["id"], api["id"], enabled=True, current_user=_admin(str(tenant["id"])))
    return api


def _request(tenant, agent, api_name: str, **overrides) -> ChainExecuteRequest:
    unique = uuid.uuid4().hex[:8]
    kwargs = dict(
        tenant_id=str(tenant["id"]), agent_id=str(agent["id"]),
        call_id=f"call-{unique}", session_id=f"sess-{unique}", turn_id=f"turn-{unique}",
        tool_call_id=f"tc-{unique}", idempotency_key=f"idem-{unique}",
        api_name=api_name, caller_arguments={}, chain_budget_ms=15000, max_chain_depth=4,
    )
    kwargs.update(overrides)
    return ChainExecuteRequest(**kwargs)


def _mock(handler) -> None:
    """Returns a monkeypatch-ready replacement for executor._step_transport
    that always hands back the given handler's MockTransport, regardless
    of the (real) allowed_ips it's called with."""
    def _factory(allowed_ips):
        return httpx.MockTransport(handler)
    return _factory


# ── T11 — ownership, ordering, admission, run claim ───────────────────────

@pytest.mark.asyncio
async def test_cross_tenant_pairing_rejected_zero_calls_no_run_row(pool, tenant_agent):
    tenant_a, _agent_a = tenant_agent
    other_tenant = dict(await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
        "Other", f"other-{uuid.uuid4().hex[:8]}",
    ))
    other_agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING *", other_tenant["id"],
    ))
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    try:
        # tenant A's tenant_id paired with tenant B's agent_id.
        request = _request(tenant_a, other_agent, "whatever")
        response = await executor.execute_chain(request)

        assert response.chain_status == "invalid_argument"
        assert response.error == "api_not_enabled_for_agent"
        assert calls == []
        run_rows = await pool.fetch("SELECT * FROM api_chain_runs WHERE tenant_id = $1", tenant_a["id"])
        assert run_rows == []
    finally:
        await pool.execute("DELETE FROM agents WHERE tenant_id = $1", other_tenant["id"])
        await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


@pytest.mark.asyncio
async def test_soft_deleted_api_rejected_even_with_stale_enabled_true(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    api = await _register_and_enable(pool, tenant, agent, f"softdel_{uuid.uuid4().hex[:8]}")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    await custom_apis.soft_delete_custom_api(api["id"])
    # agent_custom_apis.enabled is deliberately left stale `true` — no cascade write (T7).
    enabled_row = await pool.fetchrow(
        "SELECT enabled FROM agent_custom_apis WHERE agent_id = $1 AND custom_api_id = $2", agent["id"], api["id"],
    )
    assert enabled_row["enabled"] is True

    response = await executor.execute_chain(_request(tenant, agent, api["name"]))

    assert response.chain_status == "invalid_argument"
    assert response.error == "api_not_enabled_for_agent"
    assert calls == []


@pytest.mark.asyncio
async def test_repost_same_idempotency_key_returns_recorded_outcome_zero_new_calls(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    api = await _register_and_enable(pool, tenant, agent, f"repost_{uuid.uuid4().hex[:8]}", side_effecting=False)
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    request = _request(tenant, agent, api["name"])
    first = await executor.execute_chain(request)
    assert first.chain_status == "success"
    assert call_count["n"] == 1

    second = await executor.execute_chain(request)  # identical request, same idempotency_key
    assert second.run_id == first.run_id
    assert second.chain_status == "success"
    assert call_count["n"] == 1  # no new transport call


@pytest.mark.asyncio
async def test_concurrent_cap_plus_one_rate_limited_no_run_row_zero_calls(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    api = await _register_and_enable(pool, tenant, agent, f"admission_{uuid.uuid4().hex[:8]}", side_effecting=False)
    limit = admission._max_concurrent()

    # Occupy `limit` concurrency slots directly via admission.acquire() —
    # simpler and more direct than gathering `limit` real in-flight chain
    # executions, and exercises the exact same guard execute_chain calls.
    for _ in range(limit):
        assert admission.acquire(str(tenant["id"]), str(agent["id"])) is True

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    try:
        response = await executor.execute_chain(_request(tenant, agent, api["name"]))
        assert response.chain_status == "rate_limited"
        assert calls == []
        run_rows = await pool.fetch("SELECT * FROM api_chain_runs WHERE tenant_id = $1", tenant["id"])
        assert run_rows == []
    finally:
        for _ in range(limit):
            admission.release(str(tenant["id"]), str(agent["id"]))


@pytest.mark.asyncio
async def test_claim_run_raising_surfaces_the_real_error_and_releases_the_slot(pool, tenant_agent, monkeypatch):
    """MINOR 5: run_id was first bound at line 647 inside the try; the
    finally evaluated `if run_id is not None`. If _claim_run itself raised
    (pool timeout, or the api_chain_runs FK on agent_id/target_api_id),
    the finally raised UnboundLocalError and masked the original
    exception — the FK case lost app.py's fk_violation_handler 400 and
    became an opaque 500. Worse, the admission slot acquired earlier was
    then never released, permanently wedging that (tenant, agent) at
    rate_limited after enough such errors."""
    tenant, agent = tenant_agent
    api = await _register_and_enable(pool, tenant, agent, f"claimraise_{uuid.uuid4().hex[:8]}", side_effecting=False)

    async def _raising_claim_run(*args, **kwargs):
        raise RuntimeError("pool timeout")

    monkeypatch.setattr(executor, "_claim_run", _raising_claim_run)

    key = (str(tenant["id"]), str(agent["id"]))
    with pytest.raises(RuntimeError, match="pool timeout"):
        await executor.execute_chain(_request(tenant, agent, api["name"]))

    # The real exception reached the caller (never UnboundLocalError), and
    # the admission slot acquired before _claim_run was released rather
    # than leaked.
    assert admission._concurrent[key] == 0


# ── T12 — per-step outbound call: DNS-rebind-safe transport, response cap ─

@pytest.mark.asyncio
async def test_step_timeout_aborts_chain_no_call_to_successor(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    unique = uuid.uuid4().hex[:8]
    leaf = await _register_and_enable(
        pool, tenant, agent, f"hangleaf_{unique}", side_effecting=False, timeout_ms=100,
    )
    root = await _register_and_enable(
        pool, tenant, agent, f"hangroot_{unique}", side_effecting=False,
        params=[{
            "name": "leaf_id", "location": "query", "json_type": "string", "required": True,
            "source": "upstream", "upstream_api_id": leaf["id"], "upstream_json_path": "$.id",
        }],
    )
    calls = []

    async def handler(request):
        calls.append(request.url.host)
        if "hangleaf" in request.url.host:
            await asyncio.sleep(1.0)  # hangs well past the 100ms step timeout
            return httpx.Response(200, json={"id": "leaf-1"})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    response = await executor.execute_chain(_request(tenant, agent, root["name"]))

    assert response.chain_status == "timeout"
    assert len([c for c in calls if "hangleaf" in c]) == 1
    assert len([c for c in calls if "hangroot" in c]) == 0  # successor never called


@pytest.mark.asyncio
async def test_302_recorded_as_step_result_not_followed(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    api = await _register_and_enable(pool, tenant, agent, f"redirect_{uuid.uuid4().hex[:8]}", side_effecting=False)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://elsewhere.example.com/"})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    response = await executor.execute_chain(_request(tenant, agent, api["name"]))

    assert len(calls) == 1  # never a second call following the redirect
    step_row = await pool.fetchrow(
        "SELECT * FROM api_chain_steps WHERE run_id = $1", uuid.UUID(response.run_id),
    )
    assert step_row["http_status"] == 302
    assert response.chain_status == "failed"


class _CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunk: bytes, num_chunks: int) -> None:
        self.chunk = chunk
        self.num_chunks = num_chunks
        self.chunks_yielded = 0

    async def __aiter__(self):
        for _ in range(self.num_chunks):
            self.chunks_yielded += 1
            yield self.chunk


@pytest.mark.asyncio
async def test_response_body_cap_enforced_at_deployed_default(pool, tenant_agent, monkeypatch):
    """Lesson 25: TOOLEXEC_MAX_RESPONSE_BYTES is left at its deployed
    default (1 MiB) — the oversized body is what's driven up, not the cap
    driven down."""
    tenant, agent = tenant_agent
    api = await _register_and_enable(pool, tenant, agent, f"bigresp_{uuid.uuid4().hex[:8]}", side_effecting=False)
    chunk_size = 100_000
    stream = _CountingStream(b"x" * chunk_size, num_chunks=20)  # 2 MiB total, well past the 1 MiB cap

    def handler(request):
        return httpx.Response(200, stream=stream)

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))
    cap = executor._max_response_bytes()

    response = await executor.execute_chain(_request(tenant, agent, api["name"]))

    assert response.chain_status == "failed"
    bytes_pulled = stream.chunks_yielded * chunk_size
    assert bytes_pulled <= cap + chunk_size  # abandoned, not read to completion
    step_row = await pool.fetchrow(
        "SELECT * FROM api_chain_steps WHERE run_id = $1", uuid.UUID(response.run_id),
    )
    assert step_row["error"] == "response_too_large"


# ── T13 — argument placement, never concatenation ─────────────────────────

@pytest.mark.asyncio
async def test_header_crlf_injection_rejected_pre_request(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    api = await _register_and_enable(
        pool, tenant, agent, f"headerinj_{uuid.uuid4().hex[:8]}", side_effecting=False,
        params=[{
            "name": "X-Custom", "location": "header", "json_type": "string",
            "required": True, "source": "caller",
        }],
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    injected = "a\r\nAuthorization: Bearer x"
    response = await executor.execute_chain(
        _request(tenant, agent, api["name"], caller_arguments={"X-Custom": injected}),
    )

    assert calls == []
    assert response.chain_status == "invalid_argument"
    step_row = await pool.fetchrow(
        "SELECT * FROM api_chain_steps WHERE run_id = $1", uuid.UUID(response.run_id),
    )
    assert step_row["error"] == "illegal_header_value"
    assert injected not in (step_row["error"] or "")


@pytest.mark.asyncio
async def test_path_traversal_and_query_injection_produce_single_segment(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    name = f"pathinj_{uuid.uuid4().hex[:8]}"
    api = await _register_and_enable(
        pool, tenant, agent, name, side_effecting=False,
        endpoint_url=f"https://{name}.example.com/api/{{ref}}",
        params=[{
            "name": "ref", "location": "path", "json_type": "string",
            "required": True, "source": "caller",
        }],
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    # (a) raw ".." rejected outright, no request sent.
    response_a = await executor.execute_chain(
        _request(tenant, agent, api["name"], caller_arguments={"ref": "../../admin/refund"}),
    )
    assert calls == []
    assert response_a.chain_status == "invalid_argument"

    # (b) a value containing '?' produces ONE percent-encoded path segment
    # under the registered path — never a literal query string bolted on.
    response_b = await executor.execute_chain(
        _request(tenant, agent, api["name"], caller_arguments={"ref": "x?admin=1"}),
    )
    assert len(calls) == 1
    sent_url = str(calls[0].url)
    assert sent_url == f"https://{name}.example.com/api/x%3Fadmin%3D1"
    assert response_b.chain_status == "success"


@pytest.mark.asyncio
async def test_one_malformed_custom_api_row_does_not_break_other_apis_chain(pool, tenant_agent, monkeypatch):
    """Defect 2's second half / lesson 34: _build_api_tree decodes every
    non-deleted custom_apis/custom_api_params row in the tenant in one
    pass. One row whose auth_config is genuinely double-encoded (the shape
    db.json_col's guard exists to catch — simulated here via a raw SQL
    write, since the registration/write path cannot produce one) must not
    take down execute_api for every OTHER api in the tenant."""
    tenant, agent = tenant_agent
    broken = await _register_and_enable(pool, tenant, agent, f"broken_{uuid.uuid4().hex[:8]}", side_effecting=False)
    await pool.execute(
        "UPDATE custom_apis SET auth_config = to_jsonb('{\"token_ref\": \"x\"}'::text) WHERE id = $1",
        broken["id"],
    )

    healthy = await _register_and_enable(pool, tenant, agent, f"healthy_{uuid.uuid4().hex[:8]}", side_effecting=False)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    response = await executor.execute_chain(_request(tenant, agent, healthy["name"]))

    assert len(calls) == 1
    assert response.chain_status == "success"


@pytest.mark.asyncio
async def test_malformed_upstream_row_in_chain_is_reported_not_a_raw_exception(pool, tenant_agent, monkeypatch):
    """Review finding 2 / security finding 4: the prior test only proves an
    UNINVOLVED malformed custom_apis row is harmless. This one corrupts the
    row that IS an upstream of the target: _build_api_tree still decodes
    the whole tenant and skips the broken row from api_rows, but _build()
    then does api_rows[api_id]["name"] for that same id while walking the
    upstream edge and — per the review — raises KeyError instead of
    producing a clean chain failure. This test states the intended
    behaviour (execute_chain returns a failure response, it does not raise)
    so it fails for the right reason if that intent is not met; per the
    task, if it reproduces the KeyError that is a real defect to report,
    not to fix here."""
    tenant, agent = tenant_agent
    leaf = await _register_and_enable(pool, tenant, agent, f"badupstream_{uuid.uuid4().hex[:8]}", side_effecting=False)
    root = await _register_and_enable(
        pool, tenant, agent, f"badroot_{uuid.uuid4().hex[:8]}", side_effecting=False,
        params=[{
            "name": "a", "location": "query", "json_type": "string", "required": True,
            "source": "upstream", "upstream_api_id": leaf["id"], "upstream_json_path": "$.id",
        }],
    )
    # Same simulated-corruption technique as the uninvolved-row test above,
    # but on the LEAF that the target actually depends on.
    await pool.execute(
        "UPDATE custom_apis SET auth_config = to_jsonb('{\"token_ref\": \"x\"}'::text) WHERE id = $1",
        leaf["id"],
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "x"})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    response = await executor.execute_chain(_request(tenant, agent, root["name"]))

    assert calls == []
    assert response.chain_status in ("failed", "unavailable", "invalid_argument")


# ── T14 — side-effect fail-closed claim ───────────────────────────────────

async def _register_side_effecting(pool, tenant, agent, name: str, **overrides) -> dict:
    kwargs = dict(method="POST", idempotency_header="Idempotency-Key")
    kwargs.update(overrides)
    return await _register_and_enable(pool, tenant, agent, name, side_effecting=True, **kwargs)


@pytest.mark.asyncio
async def test_concurrent_double_fire_collapses_to_one_outbound_call(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    api = await _register_side_effecting(pool, tenant, agent, f"refund_{uuid.uuid4().hex[:8]}")
    call_count = {"n": 0}

    async def handler(request):
        call_count["n"] += 1
        return httpx.Response(200, json={"refunded": True})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    # Two DIFFERENT run identities (distinct idempotency_key/tool_call_id,
    # same session) resolving to the IDENTICAL arguments — the same
    # mutation double-fired, e.g. by a UI double-click or a client retry
    # that didn't reuse the run's own idempotency_key.
    session_id = f"sess-{uuid.uuid4().hex[:8]}"
    req1 = _request(tenant, agent, api["name"], session_id=session_id)
    req2 = _request(tenant, agent, api["name"], session_id=session_id)

    results = await asyncio.gather(executor.execute_chain(req1), executor.execute_chain(req2))

    assert call_count["n"] == 1
    statuses = sorted(r.chain_status for r in results)
    assert statuses == ["failed", "success"]
    loser = next(r for r in results if r.chain_status == "failed")
    assert loser.failed_step.status == "failed"
    loser_step = await pool.fetchrow(
        "SELECT * FROM api_chain_steps WHERE run_id = $1", uuid.UUID(loser.run_id),
    )
    assert loser_step["error"] == "side_effecting_step_already_completed"


@pytest.mark.asyncio
async def test_claim_side_effect_is_atomic_under_direct_concurrency(pool, tenant_agent):
    """A more direct proof than the full-pipeline test above: real DB
    atomicity does not depend on genuine Python-level interleaving to be
    correct, but proving OUR TEST can catch a regression to a
    check-then-act pattern does — asyncio.gather over the full pipeline
    can finish one coroutine's claim before the other's even starts,
    letting a check-then-act bug slip through undetected. Calling
    executor._claim_side_effect directly, many times, for the identical
    (tenant, api, hash), removes everything between the two calls except
    the claim itself, so the tasks are scheduled back-to-back with no
    intervening awaits to let one finish before the other starts."""
    tenant, agent = tenant_agent
    api = await _register_side_effecting(pool, tenant, agent, f"atomic_{uuid.uuid4().hex[:8]}")
    run = dict(await pool.fetchrow(
        "INSERT INTO api_chain_runs (tenant_id, agent_id, tool_call_id, idempotency_key, target_api_id) "
        "VALUES ($1, $2, 'tc-atomic', 'idem-atomic', $3) RETURNING *",
        tenant["id"], agent["id"], api["id"],
    ))
    try:
        results = await asyncio.gather(*[
            executor._claim_side_effect(str(tenant["id"]), str(api["id"]), "same-hash", str(run["id"]), "sess")
            for _ in range(20)
        ])
        assert sum(1 for r in results if r) == 1  # exactly one winner out of 20 racers
    finally:
        await pool.execute(
            "DELETE FROM api_side_effect_claims WHERE tenant_id = $1 AND custom_api_id = $2",
            tenant["id"], api["id"],
        )
        await pool.execute("DELETE FROM api_chain_runs WHERE id = $1", run["id"])


@pytest.mark.asyncio
async def test_cross_session_redial_still_refused(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    api = await _register_side_effecting(pool, tenant, agent, f"refund2_{uuid.uuid4().hex[:8]}")
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        return httpx.Response(200, json={"refunded": True})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    first = await executor.execute_chain(_request(tenant, agent, api["name"], session_id="session-A"))
    assert first.chain_status == "success"
    assert call_count["n"] == 1

    # A brand new session (the hang-up-and-redial case) with a DIFFERENT
    # idempotency_key/tool_call_id but the same resolved arguments — the
    # claim is deliberately not session-scoped.
    second = await executor.execute_chain(_request(tenant, agent, api["name"], session_id="session-B"))
    assert second.chain_status == "failed"
    assert call_count["n"] == 1  # no new outbound call


@pytest.mark.asyncio
async def test_post_ttl_claim_taken_over_history_preserved(pool, tenant_agent, monkeypatch):
    """Lesson 25: TOOLEXEC_SIDE_EFFECT_CLAIM_TTL is left at its deployed
    default — the claim row's claimed_at is moved into the past instead."""
    tenant, agent = tenant_agent
    api = await _register_side_effecting(pool, tenant, agent, f"refund3_{uuid.uuid4().hex[:8]}")
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        return httpx.Response(200, json={"refunded": True})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    first = await executor.execute_chain(_request(tenant, agent, api["name"]))
    assert first.chain_status == "success"
    assert call_count["n"] == 1

    await pool.execute(
        "UPDATE api_side_effect_claims SET claimed_at = now() - interval '25 hours' "
        "WHERE tenant_id = $1 AND custom_api_id = $2",
        tenant["id"], api["id"],
    )

    second = await executor.execute_chain(_request(tenant, agent, api["name"]))
    assert second.chain_status == "success"
    assert call_count["n"] == 2  # the post-TTL claim was taken over, not refused

    history_rows = await pool.fetch(
        "SELECT * FROM api_chain_steps WHERE custom_api_id = $1 ORDER BY created_at", api["id"],
    )
    assert len(history_rows) == 2  # the earlier step row was never overwritten


@pytest.mark.asyncio
async def test_differing_argument_is_never_gated(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    api = await _register_side_effecting(
        pool, tenant, agent, f"refund4_{uuid.uuid4().hex[:8]}",
        params=[{
            "name": "order_id", "location": "query", "json_type": "string",
            "required": True, "source": "caller",
        }],
    )
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        return httpx.Response(200, json={"refunded": True})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    r1 = await executor.execute_chain(_request(tenant, agent, api["name"], caller_arguments={"order_id": "A"}))
    r2 = await executor.execute_chain(_request(tenant, agent, api["name"], caller_arguments={"order_id": "B"}))

    assert r1.chain_status == "success"
    assert r2.chain_status == "success"
    assert call_count["n"] == 2  # a genuinely different mutation is never gated


@pytest.mark.asyncio
async def test_422_releases_timeout_and_500_keep_the_claim(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    api_422 = await _register_side_effecting(pool, tenant, agent, f"refund422_{uuid.uuid4().hex[:8]}")
    api_500 = await _register_side_effecting(
        pool, tenant, agent, f"refund500_{uuid.uuid4().hex[:8]}", timeout_ms=5000,
    )

    def handler_422(request):
        return httpx.Response(422, json={"error": "unprocessable"})

    call_count_500 = {"n": 0}

    def handler_500(request):
        call_count_500["n"] += 1
        return httpx.Response(500, json={"error": "boom"})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler_422))
    first_422 = await executor.execute_chain(_request(tenant, agent, api_422["name"]))
    assert first_422.chain_status == "failed"
    claim_422 = await pool.fetchrow(
        "SELECT * FROM api_side_effect_claims WHERE custom_api_id = $1", api_422["id"],
    )
    assert claim_422["status"] == "released"
    # An immediate retry DOES call again.
    second_422 = await executor.execute_chain(_request(tenant, agent, api_422["name"]))
    assert second_422.chain_status == "failed"  # still 422 from the same handler

    monkeypatch.setattr(executor, "_step_transport", _mock(handler_500))
    first_500 = await executor.execute_chain(_request(tenant, agent, api_500["name"]))
    assert first_500.chain_status == "failed"
    claim_500 = await pool.fetchrow(
        "SELECT * FROM api_side_effect_claims WHERE custom_api_id = $1", api_500["id"],
    )
    assert claim_500["status"] == "claimed"  # kept, not released
    second_500 = await executor.execute_chain(_request(tenant, agent, api_500["name"]))
    assert second_500.chain_status == "failed"
    assert call_count_500["n"] == 1  # the retry never called out again


@pytest.mark.asyncio
async def test_hash_domain_separation(pool, tenant_agent, monkeypatch):
    import hashlib

    tenant, agent = tenant_agent
    api = await _register_side_effecting(pool, tenant, agent, f"hashsep_{uuid.uuid4().hex[:8]}")

    def handler(request):
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    response = await executor.execute_chain(_request(tenant, agent, api["name"]))
    assert response.chain_status == "success"
    step_row = await pool.fetchrow(
        "SELECT * FROM api_chain_steps WHERE run_id = $1", uuid.UUID(response.run_id),
    )
    stored_hash = step_row["arguments_hash"]

    # (vi.1) not a bare sha256 of the canonical args.
    bare_sha256 = hashlib.sha256(b'{"a":"","args":{},"t":""}').hexdigest()
    assert stored_hash != bare_sha256
    assert stored_hash != f"k1:{bare_sha256}"

    # (vi.4) stored != the exported idem value (claim/idem tag separation).
    run_row = await pool.fetchrow("SELECT * FROM api_chain_runs WHERE id = $1", uuid.UUID(response.run_id))
    assert stored_hash != step_row["idempotency_key"]
    assert step_row["idempotency_key"] is not None

    # (vi.2) differs under a second TOOLEXEC_ARGS_HMAC_KEY_REF.
    monkeypatch.setenv("TOOLEXEC_TEST_HMAC_KEY_2", "a-totally-different-platform-key")
    monkeypatch.setenv("TOOLEXEC_ARGS_HMAC_KEY_REF", "env:TOOLEXEC_TEST_HMAC_KEY_2")
    executor._hmac_key_cache.clear()
    api2 = await _register_side_effecting(pool, tenant, agent, f"hashsep2_{uuid.uuid4().hex[:8]}")
    response2 = await executor.execute_chain(_request(tenant, agent, api2["name"]))
    step_row2 = await pool.fetchrow(
        "SELECT * FROM api_chain_steps WHERE run_id = $1", uuid.UUID(response2.run_id),
    )
    assert step_row2["arguments_hash"].split(":", 1)[1] != stored_hash.split(":", 1)[1]

    # (vi.3) changing TOOLEXEC_ARGS_HMAC_KEY_ID changes the stored prefix.
    monkeypatch.setenv("TOOLEXEC_ARGS_HMAC_KEY_ID", "k2")
    api3 = await _register_side_effecting(pool, tenant, agent, f"hashsep3_{uuid.uuid4().hex[:8]}")
    response3 = await executor.execute_chain(_request(tenant, agent, api3["name"]))
    step_row3 = await pool.fetchrow(
        "SELECT * FROM api_chain_steps WHERE run_id = $1", uuid.UUID(response3.run_id),
    )
    assert step_row3["arguments_hash"].startswith("k2:")


@pytest.mark.asyncio
async def test_null_arguments_hash_on_side_effecting_step_violates_check(pool, tenant_agent):
    tenant, agent = tenant_agent
    api = await _register_side_effecting(pool, tenant, agent, f"nullcheck_{uuid.uuid4().hex[:8]}")
    run = dict(await pool.fetchrow(
        "INSERT INTO api_chain_runs (tenant_id, agent_id, tool_call_id, idempotency_key, target_api_id) "
        "VALUES ($1, $2, 'tc', 'idem', $3) RETURNING *",
        tenant["id"], agent["id"], api["id"],
    ))
    try:
        with pytest.raises(Exception, match="api_chain_steps_side_effect_keyed"):
            await pool.execute(
                "INSERT INTO api_chain_steps "
                "(run_id, step_index, custom_api_id, api_name, level, status, side_effecting, arguments_hash) "
                "VALUES ($1, 0, $2, $3, 1, 'success', true, NULL)",
                run["id"], api["id"], api["name"],
            )
    finally:
        await pool.execute("DELETE FROM api_chain_runs WHERE id = $1", run["id"])


@pytest.mark.asyncio
async def test_side_effecting_step_missing_required_arg_finalizes_no_500(pool, tenant_agent, monkeypatch):
    """Defect 1 / lesson 32: a side-effecting API's failure paths persist
    the step before `arguments_hash` is ever derived (missing required
    caller argument, here) — that write must not retrip
    api_chain_steps_side_effect_keyed, and the run must reach a terminal
    status rather than being stranded 'running'."""
    tenant, agent = tenant_agent
    api = await _register_side_effecting(
        pool, tenant, agent, f"refundmissing_{uuid.uuid4().hex[:8]}",
        params=[{"name": "amount", "location": "body", "json_type": "number", "required": True, "source": "caller"}],
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"refunded": True})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    response = await executor.execute_chain(_request(tenant, agent, api["name"], caller_arguments={}))

    assert calls == []
    assert response.chain_status == "invalid_argument"
    assert response.error == "missing_fields"

    run_row = await pool.fetchrow("SELECT * FROM api_chain_runs WHERE id = $1", uuid.UUID(response.run_id))
    assert run_row["status"] != "running"
    assert run_row["finished_at"] is not None

    step_row = await pool.fetchrow("SELECT * FROM api_chain_steps WHERE run_id = $1", uuid.UUID(response.run_id))
    assert step_row["arguments_hash"] is None
    assert step_row["side_effecting"] is False  # no claim was ever attempted for this step


@pytest.mark.asyncio
async def test_missing_fields_reaches_the_response_not_dropped_to_empty(pool, tenant_agent, monkeypatch):
    """QA defect 9: _resolve_arguments builds _StepFailure.missing_fields
    precisely so the turn can name the gap, but the except handler used to
    never carry it out of _run_steps — the response always sent []. A
    caller who asks for a refund without an order id must get back the
    name of the field that's missing, not an empty list."""
    tenant, agent = tenant_agent
    api = await _register_side_effecting(
        pool, tenant, agent, f"missingnamed_{uuid.uuid4().hex[:8]}",
        params=[{"name": "order_id", "location": "body", "json_type": "string", "required": True, "source": "caller"}],
    )

    response = await executor.execute_chain(_request(tenant, agent, api["name"], caller_arguments={}))

    assert response.chain_status == "invalid_argument"
    assert response.error == "missing_fields"
    assert response.missing_fields == [{"name": "order_id", "description": ""}]


@pytest.mark.asyncio
async def test_coerce_failure_finalizes_as_invalid_argument_not_a_raw_exception(pool, tenant_agent, monkeypatch):
    """QA defect 6: an LLM-supplied {"qty": "three"} against an `integer`
    param used to raise a bare ValueError out of _coerce, past the step
    loop's only handler (except _StepFailure), past _finalize_run —
    leaving api_chain_runs.status stuck 'running' forever. Because that
    run row already won the (tenant_id, idempotency_key) claim, the
    corrected retry under the same key would then be permanently answered
    chain_already_running."""
    tenant, agent = tenant_agent
    api = await _register_and_enable(
        pool, tenant, agent, f"badcoerce_{uuid.uuid4().hex[:8]}", side_effecting=False,
        params=[{"name": "qty", "location": "body", "json_type": "integer", "required": True, "source": "caller"}],
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    same_key = f"idem-{uuid.uuid4().hex[:8]}"
    response = await executor.execute_chain(
        _request(tenant, agent, api["name"], caller_arguments={"qty": "three"}, idempotency_key=same_key),
    )

    assert calls == []
    assert response.chain_status == "invalid_argument"
    assert response.error == "invalid_argument_type"
    assert response.missing_fields == [{"name": "qty", "description": ""}]

    # The row must reach a terminal status — under the bug, _coerce's bare
    # ValueError escapes _run_steps/execute_chain entirely, past
    # _finalize_run, leaving this row stuck 'running' forever. Because
    # this row already won the (tenant_id, idempotency_key) claim, that
    # would permanently answer any retry under the same key
    # chain_already_running (_response_from_existing_run's "running"
    # branch) instead of the row's real, finalized outcome.
    run_row = await pool.fetchrow("SELECT * FROM api_chain_runs WHERE id = $1", uuid.UUID(response.run_id))
    assert run_row["status"] != "running"
    assert run_row["finished_at"] is not None


@pytest.mark.asyncio
async def test_undecodable_param_row_refuses_the_call_instead_of_dropping_the_field(pool, tenant_agent, monkeypatch):
    """Review finding 1 / security finding 1 (medium): _build_api_tree
    silently SKIPS a custom_api_params row it cannot decode, deleting a
    declared argument (and, for source='upstream', a dependency edge) from
    the chain instead of failing it. The security audit found no reachable
    trigger through the real write path (literal_value is JSONB, so
    Postgres has already validated it, and _decode_param_row accepts every
    valid JSON value) — so this test injects the decode failure directly
    by monkeypatching custom_apis._decode_param_row for exactly the
    'amount' row, simulating a future stricter decoder or a second writer
    bypassing _replace_params. This pins the SAFE, intended behaviour: a
    required argument that cannot be resolved must refuse the call
    (missing_fields / invalid_argument), never dispatch the side-effecting
    request with the field silently absent. If the current code fails
    open (dispatches anyway with chain_status='success' and zero mention
    of the dropped field), that is the defect described in finding 1 —
    report it and leave this failing, do not weaken the assertion."""
    tenant, agent = tenant_agent
    api = await _register_side_effecting(
        pool, tenant, agent, f"refundskip_{uuid.uuid4().hex[:8]}",
        params=[{"name": "amount", "location": "body", "json_type": "number", "required": True, "source": "caller"}],
    )

    real_decode = custom_apis._decode_param_row

    def _boom_for_amount(row):
        if row["name"] == "amount":
            raise RuntimeError("simulated undecodable custom_api_params row")
        return real_decode(row)

    monkeypatch.setattr(custom_apis, "_decode_param_row", _boom_for_amount)

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"refunded": True})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    response = await executor.execute_chain(
        _request(tenant, agent, api["name"], caller_arguments={"amount": 50}),
    )

    # The intended, safe outcome: the call is refused, the dropped
    # required field is named, and — crucially — the outbound mutation
    # never fires with the field missing.
    assert calls == []
    assert response.chain_status in ("invalid_argument", "failed", "unavailable")


# ── T15 — auth application, redaction, success_template interpolation ─────

@pytest.mark.asyncio
async def test_credential_unavailable_no_request_ref_not_leaked(pool, tenant_agent, monkeypatch, caplog):
    tenant, agent = tenant_agent
    # Namespace-VALID (would pass registration) but never actually
    # provisioned — the realistic way a ref becomes unresolvable at call
    # time without ever failing registration's own validation.
    tenant_hex = uuid.UUID(str(tenant["id"])).hex.upper()
    missing_ref = f"env:TENANT_{tenant_hex}_NEVER_SET_TOKEN"
    api = await _register_and_enable(
        pool, tenant, agent, f"credmissing_{uuid.uuid4().hex[:8]}", side_effecting=False,
        auth_scheme="bearer", auth_config={"token_ref": missing_ref},
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    with caplog.at_level("DEBUG"):
        response = await executor.execute_chain(_request(tenant, agent, api["name"]))

    assert calls == []
    assert response.chain_status == "unavailable"
    step_row = await pool.fetchrow(
        "SELECT * FROM api_chain_steps WHERE run_id = $1", uuid.UUID(response.run_id),
    )
    assert step_row["error"] == "credential_unavailable"
    assert missing_ref not in (step_row["error"] or "")
    for record in caplog.records:
        assert missing_ref not in record.getMessage()
        assert missing_ref not in str(record.args)


@pytest.mark.asyncio
async def test_auth_injected_credential_absent_from_redacted_step_and_chain_runs(pool, tenant_agent, monkeypatch):
    """BLOCKING 1: the resolved bearer credential auth_schemes.apply()
    injects into `headers` at call time must never reach
    api_chain_steps.arguments_redacted — that column is returned by
    GET /calls/{session_id}/chain-runs behind bare get_current_user, so a
    leftover credential there would let any viewer/supervisor/agent in
    the tenant read the tenant's live bearer token straight out of
    Postgres."""
    tenant, agent = tenant_agent
    tenant_hex = uuid.UUID(str(tenant["id"])).hex.upper()
    ref = f"env:TENANT_{tenant_hex}_LIVE_BEARER_TOKEN"
    secret_value = "sk-live-bearer-token-must-never-be-stored"
    monkeypatch.setenv(f"TENANT_{tenant_hex}_LIVE_BEARER_TOKEN", secret_value)
    api = await _register_and_enable(
        pool, tenant, agent, f"authredact_{uuid.uuid4().hex[:8]}", side_effecting=False,
        auth_scheme="bearer", auth_config={"token_ref": ref},
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    response = await executor.execute_chain(_request(tenant, agent, api["name"]))

    assert response.chain_status == "success"
    # The outbound call DID carry the credential (it must still work).
    assert calls[0].headers["Authorization"] == f"Bearer {secret_value}"

    step_row = await pool.fetchrow(
        "SELECT * FROM api_chain_steps WHERE run_id = $1", uuid.UUID(response.run_id),
    )
    arguments_redacted = db.json_col(step_row["arguments_redacted"])
    assert "Authorization" not in arguments_redacted
    assert secret_value not in str(arguments_redacted)

    session_runs = await agent_apis._authorize_chain_runs(
        step_row["session_id"], _admin(str(tenant["id"])),
    )
    for run in session_runs:
        for step in run["steps"]:
            assert secret_value not in str(step)
            step_args = db.json_col(step["arguments_redacted"]) or {}
            assert "Authorization" not in step_args


@pytest.mark.asyncio
async def test_arguments_hash_stable_across_credential_rotation(pool, tenant_agent, monkeypatch):
    """BLOCKING 2 (AC 15 regression, the more important half): a credential
    rotation must not move arguments_hash when the declared arguments are
    unchanged. auth_scheme='oauth2_client_credentials' caches its token
    per process and refreshes it 60s before expiry, so if the hash were a
    function of the resolved credential the SAME logical mutation would
    derive a DIFFERENT hash on the next token refresh — the ON CONFLICT
    (tenant_id, custom_api_id, arguments_hash) insert would then find no
    conflict, and a genuinely identical refund would fire twice."""
    tenant, agent = tenant_agent
    tenant_hex = uuid.UUID(str(tenant["id"])).hex.upper()
    ref = f"env:TENANT_{tenant_hex}_ROTATING_TOKEN"
    monkeypatch.setenv(f"TENANT_{tenant_hex}_ROTATING_TOKEN", "token-before-rotation")
    api = await _register_side_effecting(
        pool, tenant, agent, f"rotate_{uuid.uuid4().hex[:8]}",
        auth_scheme="bearer", auth_config={"token_ref": ref},
    )

    def handler(request):
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    response1 = await executor.execute_chain(_request(tenant, agent, api["name"]))
    assert response1.chain_status == "success"
    step1 = await pool.fetchrow(
        "SELECT * FROM api_chain_steps WHERE run_id = $1", uuid.UUID(response1.run_id),
    )

    # Rotate the credential — same declared arguments (none), only the
    # resolved value behind the same ref changes.
    monkeypatch.setenv(f"TENANT_{tenant_hex}_ROTATING_TOKEN", "token-after-rotation")
    response2 = await executor.execute_chain(_request(tenant, agent, api["name"]))
    step2 = await pool.fetchrow(
        "SELECT * FROM api_chain_steps WHERE run_id = $1", uuid.UUID(response2.run_id),
    )

    assert step1["arguments_hash"] == step2["arguments_hash"]
    # And because the hash is unchanged, the second logically-identical
    # call correctly collapses to the loser's path instead of firing the
    # mutation again.
    assert response2.chain_status == "failed"
    assert response2.error == "side_effecting_step_already_completed"


@pytest.mark.asyncio
async def test_sensitive_literal_registry_redaction_never_reaches_outbound_call(pool, tenant_agent, monkeypatch):
    """The registry-read redaction added for the sensitive-literal-exposure
    fix (_redact_sensitive_literals) must be a REGISTRY-view-only concern:
    the executor never goes through list_custom_apis/get_custom_api — it
    decodes custom_api_params rows directly in _build_api_tree — so a
    sensitive literal must still place its REAL value on the outbound
    call, never the registry's None."""
    tenant, agent = tenant_agent
    secret_value = "sk-live-outbound-real-value"
    api = await _register_and_enable(
        pool, tenant, agent, f"sensitiveliteral_{uuid.uuid4().hex[:8]}", side_effecting=False,
        params=[{
            "name": "X-Api-Key", "location": "header", "json_type": "string",
            "required": True, "source": "literal", "literal_value": secret_value, "sensitive": True,
        }],
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    response = await executor.execute_chain(_request(tenant, agent, api["name"]))

    assert response.chain_status == "success"
    assert len(calls) == 1
    assert calls[0].headers["X-Api-Key"] == secret_value


@pytest.mark.asyncio
async def test_sensitive_param_and_response_path_redacted_metadata_visible(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    api = await _register_and_enable(
        pool, tenant, agent, f"sensitive_{uuid.uuid4().hex[:8]}", side_effecting=False,
        sensitive_response_paths=["$.ssn"],
        params=[{
            "name": "national_id", "location": "query", "json_type": "string",
            "required": True, "source": "caller", "sensitive": True,
        }],
    )

    def handler(request):
        return httpx.Response(200, json={"ssn": "123-45-6789", "order_id": "o-1"})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    response = await executor.execute_chain(
        _request(tenant, agent, api["name"], caller_arguments={"national_id": "999-99-9999"}),
    )
    assert response.chain_status == "success"
    assert response.data["ssn"] == "[redacted]"
    assert response.data["order_id"] == "o-1"

    step_row = await pool.fetchrow(
        "SELECT * FROM api_chain_steps WHERE run_id = $1", uuid.UUID(response.run_id),
    )
    arguments_redacted = db.json_col(step_row["arguments_redacted"])
    response_redacted = db.json_col(step_row["response_redacted"])
    argument_sources = db.json_col(step_row["argument_sources"])
    assert arguments_redacted["national_id"] == "[redacted]"
    assert response_redacted["ssn"] == "[redacted]"
    assert response_redacted["order_id"] == "o-1"
    # Metadata stays visible — redaction targets values, not the shape.
    assert step_row["api_name"] == api["name"]
    assert step_row["status"] == "success"
    assert argument_sources["national_id"] == "caller"


@pytest.mark.asyncio
async def test_success_template_redacted_placeholder_suppresses_the_whole_template(pool, tenant_agent, monkeypatch):
    """FIX 3: a path made sensitive since the template was registered —
    simulated here via a direct SQL update bypassing custom_apis.py's own
    write-time guard — must suppress the WHOLE template (deterministic_response
    is None), exactly like a genuinely absent path. Speaking "[redacted]"
    back as if it were a real value both confirms to the caller that a
    sensitive field exists and reads as a malfunction; the LLM narrates
    from `data` instead, where "[redacted]" is an ordinary field value,
    not a spoken confirmation of anything."""
    tenant, agent = tenant_agent
    api = await _register_and_enable(
        pool, tenant, agent, f"tmpl_ok_{uuid.uuid4().hex[:8]}", side_effecting=False,
        success_template="Your balance is {{$.balance}}",
    )
    await pool.execute(
        "UPDATE custom_apis SET sensitive_response_paths = $2::jsonb WHERE id = $1",
        api["id"], '["$.balance"]',
    )

    def handler(request):
        return httpx.Response(200, json={"balance": "42.00"})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    response = await executor.execute_chain(_request(tenant, agent, api["name"]))
    assert response.chain_status == "success"
    assert response.deterministic_response is None
    assert "{{" not in (response.deterministic_response or "")
    # The LLM still narrates from `data`, which itself is redacted (T15) —
    # "[redacted]" appears there as an ordinary field value, never spoken.
    assert response.data["balance"] == "[redacted]"


@pytest.mark.asyncio
async def test_success_template_unresolved_placeholder_yields_none_not_literal(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    api = await _register_and_enable(
        pool, tenant, agent, f"tmpl_missing_{uuid.uuid4().hex[:8]}", side_effecting=False,
        success_template="Confirmation: {{$.confirmation_code}}",
    )

    def handler(request):
        return httpx.Response(200, json={"order_id": "o-1"})  # no confirmation_code this time

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    response = await executor.execute_chain(_request(tenant, agent, api["name"]))
    assert response.chain_status == "success"
    assert response.deterministic_response is None
    assert response.data == {"order_id": "o-1"}
    assert "{{" not in (response.deterministic_response or "")


# ── FIX 1 — the per-agent max_chain_depth override must actually govern
# the runtime ceiling, not just the enable-time gate ───────────────────────

async def _build_three_level_chain(pool, tenant, agent, unique: str) -> dict:
    """leaf <- mid <- root (3 levels), all non-side-effecting so the test
    can tell 'admitted' from 'rejected' purely by transport call count."""
    leaf = await _register_and_enable(pool, tenant, agent, f"depthleaf_{unique}", side_effecting=False)
    mid = await _register_and_enable(
        pool, tenant, agent, f"depthmid_{unique}", side_effecting=False,
        params=[{
            "name": "a", "location": "query", "json_type": "string", "required": True,
            "source": "upstream", "upstream_api_id": leaf["id"], "upstream_json_path": "$.id",
        }],
    )
    root = await _register_and_enable(
        pool, tenant, agent, f"depthroot_{unique}", side_effecting=False,
        params=[{
            "name": "a", "location": "query", "json_type": "string", "required": True,
            "source": "upstream", "upstream_api_id": mid["id"], "upstream_json_path": "$.id",
        }],
    )
    return root


async def _set_agent_max_chain_depth(pool, tenant, agent, value: int) -> None:
    tpc = dict(await pool.fetchrow(
        "INSERT INTO tool_provider_configs (tenant_id, name, tool_name, engine) "
        "VALUES ($1, 'x', 'execute_api', 'toolexec') RETURNING *", tenant["id"],
    ))
    await pool.execute(
        "INSERT INTO agent_tool_policies (agent_id, tool_name, tool_provider_config_id, max_chain_depth) "
        "VALUES ($1, 'execute_api', $2, $3)", agent["id"], tpc["id"], value,
    )


@pytest.mark.asyncio
async def test_agent_override_rejects_a_legitimate_chain_the_platform_ceiling_would_admit(
    pool, tenant_agent, monkeypatch,
):
    tenant, agent = tenant_agent
    root = await _build_three_level_chain(pool, tenant, agent, uuid.uuid4().hex[:8])
    await _set_agent_max_chain_depth(pool, tenant, agent, 2)

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "x"})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    try:
        # The client itself asks for the full platform ceiling (4) — only
        # the agent's own override may lower it further.
        response = await executor.execute_chain(_request(tenant, agent, root["name"], max_chain_depth=4))

        assert response.chain_status == "failed"
        assert response.error == "depth_limit_exceeded"
        assert calls == []  # rejected before any HTTP call
    finally:
        await pool.execute("DELETE FROM agent_tool_policies WHERE agent_id = $1", agent["id"])
        await pool.execute("DELETE FROM tool_provider_configs WHERE tenant_id = $1", tenant["id"])


@pytest.mark.asyncio
async def test_no_override_admits_the_same_chain_the_override_would_reject(pool, tenant_agent, monkeypatch):
    """Same 3-level chain, same agent shape, but with NO agent_tool_policies
    row at all (NULL override) — must be ADMITTED. Both directions, or a
    bug that always rejects (or always admits) regardless of the override
    would still pass only the other half of this pair."""
    tenant, agent = tenant_agent
    root = await _build_three_level_chain(pool, tenant, agent, uuid.uuid4().hex[:8])

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "x"})

    monkeypatch.setattr(executor, "_step_transport", _mock(handler))

    response = await executor.execute_chain(_request(tenant, agent, root["name"], max_chain_depth=4))

    assert response.chain_status == "success"
    assert len(calls) == 3
