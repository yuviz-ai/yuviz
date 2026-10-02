#!/usr/bin/env python3
"""Idempotently seed the 'default' tenant's agent and STT/LLM/TTS provider_configs.

Uses the audited services.config write path. Usage: python3 scripts/seed_default_config.py
Requires: POSTGRES_ADMIN_DSN (or POSTGRES_DSN; superuser bypasses RLS) and REDIS_URL.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from libs.tenancy import set_target_tenant  # noqa: E402

from services.config import agents, db, provider_configs, tenants  # noqa: E402

TENANT_SLUG = "default"
AGENT_SLUG = "default"

# Matches config/agents/default.yaml.
GREETING = "Hello! How can I help you today?"
SYSTEM_PROMPT = (
    "You are a helpful voice assistant on a phone call. Answer in at most 2-3 short "
    "spoken sentences. Never enumerate long lists; give the single most likely "
    "answer and offer to go deeper if the caller wants. Plain conversational "
    "speech only - no markdown, no bullet points, no numbered lists."
)

# Under docker-compose the Conversation Service is a container, where
# "localhost" is itself.
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434"

# Must match what the Conversation Service loads, or a second model is fetched.
STT_MODEL = os.environ.get("VOICEAI_STT_MODEL") or "small"
LLM_MODEL = os.environ.get("VOICEAI_LLM_MODEL") or "llama3.2"

# Matches PipelineConfig's defaults (services/conversation/pipeline_config.py).
PROVIDER_DEFAULTS = [
    {"role": "stt", "engine": "faster_whisper", "name": "FasterWhisper (default)",
     "model": STT_MODEL, "extra": {"device": "cpu", "compute_type": "int8"}},
    {"role": "llm", "engine": "ollama", "name": f"Ollama {LLM_MODEL} (default)",
     "model": LLM_MODEL, "extra": {"temperature": 0.7, "base_url": OLLAMA_BASE_URL}},
    # Kokoro, not macOS `say`: it's cross-platform.
    {"role": "tts", "engine": "kokoro", "name": "Kokoro TTS (default)",
     "voice": "af_sarah", "extra": {"lang_code": "a"}},
]


async def main() -> None:
    await db.get_pool(dsn=os.environ.get("POSTGRES_ADMIN_DSN") or os.environ["POSTGRES_DSN"])
    tenant = await tenants.get_tenant(TENANT_SLUG)
    if tenant is None:
        raise RuntimeError(
            f"tenant {TENANT_SLUG!r} not found — apply database/schema.sql first "
            "(it seeds this row)."
        )
    # No request context: set the tenant scope for every tenant_conn() below.
    set_target_tenant(str(tenant["id"]))

    provider_ids: dict[str, str] = {}
    for spec in PROVIDER_DEFAULTS:
        existing = await provider_configs.list_provider_configs(tenant["id"], role=spec["role"])
        match = next((p for p in existing if p["engine"] == spec["engine"]), None)
        if match is not None:
            provider_ids[spec["role"]] = match["id"]
            # Skipping outright would pin an existing install to stale values.
            drift = {k: spec[k] for k in ("name", "model", "voice")
                     if spec.get(k) is not None and match.get(k) != spec[k]}
            want_extra = spec.get("extra") or {}
            have_extra = match.get("extra") or {}
            if isinstance(have_extra, str):
                have_extra = json.loads(have_extra)
            if any(have_extra.get(k) != v for k, v in want_extra.items()):
                drift["extra"] = {**have_extra, **want_extra}
            if drift:
                await provider_configs.update_provider_config(match["id"], **drift)
                print(f"updated provider_config: {spec['role']}/{spec['engine']} {drift}")
            else:
                print(f"provider_config already exists: {spec['role']}/{spec['engine']} ({match['id']})")
            continue
        created = await provider_configs.create_provider_config(
            tenant_id=tenant["id"], name=spec["name"], role=spec["role"], engine=spec["engine"],
            model=spec.get("model"), voice=spec.get("voice"), extra=spec.get("extra"),
        )
        provider_ids[spec["role"]] = created["id"]
        print(f"created provider_config: {spec['role']}/{spec['engine']} ({created['id']})")

    defaults_to_set = {
        "default_stt_config_id": provider_ids["stt"],
        "default_llm_config_id": provider_ids["llm"],
        "default_tts_config_id": provider_ids["tts"],
    }
    if any(tenant.get(k) != v for k, v in defaults_to_set.items()):
        tenant = await tenants.update_tenant(tenant["id"], **defaults_to_set)
        print(f"updated tenant {TENANT_SLUG!r} default providers")
    else:
        print(f"tenant {TENANT_SLUG!r} default providers already set")

    agent = await agents.get_agent(TENANT_SLUG, AGENT_SLUG)
    if agent is None:
        agent = await agents.create_agent(
            tenant_id=tenant["id"], slug=AGENT_SLUG, name="Default Assistant",
            greeting=GREETING, system_prompt=SYSTEM_PROMPT,
        )
        print(f"created agent {AGENT_SLUG!r} ({agent['id']})")
    else:
        print(f"agent {AGENT_SLUG!r} already exists ({agent['id']})")

    print("Seed complete.")


if __name__ == "__main__":
    asyncio.run(main())
