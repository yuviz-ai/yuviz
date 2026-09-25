from __future__ import annotations

import pytest

from services.config import cache, provider_configs


async def test_create_and_get_provider_config(test_tenant, scoped):
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


async def test_list_provider_configs_filters_by_role_and_environment(test_tenant, scoped):
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


async def test_update_provider_config_invalidates_cache(test_tenant, scoped):
    created = await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="Deepgram", role="stt", engine="deepgram",
    )
    await provider_configs.get_provider_config(created["id"])  # warm cache
    assert await cache.get_json(f"provider:{created['id']}") is not None

    updated = await provider_configs.update_provider_config(created["id"], model="nova-3-medical")
    assert updated["model"] == "nova-3-medical"
    assert await cache.get_json(f"provider:{created['id']}") is None


async def test_update_provider_config_publishes_change_notification(test_tenant, scoped):
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


async def test_provider_config_role_check_constraint_rejects_bad_role(test_tenant, scoped):
    with pytest.raises(Exception):
        await provider_configs.create_provider_config(
            tenant_id=test_tenant["id"], name="Bad", role="not-a-real-role", engine="x",
        )


async def test_audit_log_redacts_api_key_ref(test_tenant, scoped, pool):
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
