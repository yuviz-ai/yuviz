from __future__ import annotations

import pytest

from services.config import cache, provider_configs

pytestmark = pytest.mark.integration


async def test_create_and_get_provider_config(test_tenant):
    created = await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="Deepgram Nova-3", role="stt", engine="deepgram",
        environment="prod", model="nova-3", api_key_ref="k8s:voiceai/deepgram-api-key",
    )
    assert created["role"] == "stt"
    assert created["environment"] == "prod"
    assert created["api_key_ref"] == "k8s:voiceai/deepgram-api-key"

    fetched = await provider_configs.get_provider_config(created["id"])
    assert fetched is not None
    assert fetched["engine"] == "deepgram"


async def test_list_provider_configs_filters_by_role_and_environment(test_tenant):
    await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="Deepgram", role="stt", engine="deepgram", environment="prod",
    )
    await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="Whisper", role="stt", engine="faster_whisper", environment="dev",
    )
    await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="GPT-4o", role="llm", engine="openai", environment="prod",
    )

    stt_only = await provider_configs.list_provider_configs(test_tenant["id"], role="stt")
    assert {p["engine"] for p in stt_only} == {"deepgram", "faster_whisper"}

    prod_stt_only = await provider_configs.list_provider_configs(
        test_tenant["id"], role="stt", environment="prod",
    )
    assert [p["engine"] for p in prod_stt_only] == ["deepgram"]


async def test_update_provider_config_invalidates_cache(test_tenant):
    created = await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="Deepgram", role="stt", engine="deepgram",
    )
    await provider_configs.get_provider_config(created["id"])  # warm cache
    assert await cache.get_json(f"provider:{created['id']}") is not None

    updated = await provider_configs.update_provider_config(created["id"], model="nova-3-medical")
    assert updated["model"] == "nova-3-medical"
    assert await cache.get_json(f"provider:{created['id']}") is None


async def test_update_provider_config_publishes_change_notification(test_tenant):
    """The other half of instant cache invalidation (see cache.py's
    publish() docstring): Conversation Service subscribes to this exact
    channel/message shape to evict its own cached provider client. Uses a
    real Redis Pub/Sub subscription against the real dev Redis, not a
    mock — this is the actual cross-process contract."""
    created = await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="Deepgram", role="stt", engine="deepgram",
    )

    subscriber = cache.get_client().pubsub()
    await subscriber.subscribe(provider_configs.PROVIDER_CONFIG_CHANGED_CHANNEL)
    await subscriber.get_message(timeout=1)  # the subscribe confirmation itself

    await provider_configs.update_provider_config(created["id"], model="nova-3-medical")

    message = await subscriber.get_message(timeout=2)
    assert message is not None
    assert message["type"] == "message"
    assert message["data"] == str(created["id"])

    await subscriber.unsubscribe(provider_configs.PROVIDER_CONFIG_CHANGED_CHANNEL)
    await subscriber.aclose()


async def test_provider_config_role_check_constraint_rejects_bad_role(test_tenant):
    with pytest.raises(Exception):
        await provider_configs.create_provider_config(
            tenant_id=test_tenant["id"], name="Bad", role="not-a-real-role", engine="x",
        )


async def test_soft_delete_repoints_tenant_default_to_sibling(test_tenant, pool):
    """Deleting the tenant's default LLM must not leave a dangling id —
    Conversation would 404 the provider and fall back to env Ollama."""
    from services.config import agents, tenants

    primary = await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="Ollama", role="llm", engine="ollama",
        model="llama3.2",
    )
    backup = await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="Gemini", role="llm", engine="gemini",
        model="gemini-2.5-flash",
    )
    await tenants.update_tenant(
        test_tenant["id"], default_llm_config_id=str(primary["id"]),
    )
    agent = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="chat-bot", name="Chat",
        llm_config_id=str(primary["id"]),
    )

    await provider_configs.soft_delete_provider_config(primary["id"])

    assert await provider_configs.get_provider_config(primary["id"]) is None
    tenant = await tenants.get_tenant(test_tenant["slug"])
    assert tenant is not None
    assert str(tenant["default_llm_config_id"]) == str(backup["id"])
    refreshed = await agents.get_agent_by_id(agent["id"])
    assert refreshed is not None
    assert refreshed["llm_config_id"] is None

    # Fixture teardown hard-deletes provider_configs; clear FKs first.
    await tenants.update_tenant(
        test_tenant["id"],
        default_llm_config_id=None,
        default_stt_config_id=None,
        default_tts_config_id=None,
    )


async def test_audit_log_redacts_api_key_ref(test_tenant, pool):

    created = await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="Deepgram", role="stt", engine="deepgram",
        api_key_ref="k8s:voiceai/deepgram-api-key",
    )
    row = await pool.fetchrow(
        "SELECT * FROM audit_log WHERE entity_type = 'provider_config' AND entity_id = $1 "
        "ORDER BY changed_at DESC LIMIT 1",
        created["id"],
    )
    assert row is not None
    import json
    new_value = json.loads(row["new_value"])
    assert new_value["api_key_ref"] == "[redacted]"
