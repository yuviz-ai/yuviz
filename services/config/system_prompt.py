"""
generate_system_prompt() — one-shot LLM call that turns the agent-creation
wizard's structured inputs (identity/purpose/tone/transfer rule) into prose,
using the tenant's own configured LLM provider_config. Modeled directly on
provider_configs.list_elevenlabs_voices()'s pattern: resolve api_key_ref via
the already-injected SecretResolver, make one outbound httpx call, never
return the resolved key or the raw vendor body to the caller.

The wizard's own deterministic template (admin-ui's systemPromptBuilder.ts)
still owns the actual anti-hallucination guardrail wording — this endpoint's
meta-prompt requires the model to reproduce it near-verbatim rather than
trusting the model to invent equivalent wording, so a paraphrase can't
quietly drop a guardrail. Only openai/anthropic engines are supported today;
anything else is a clear 400, not a silent fallback.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .provider_configs import get_provider_config
from .secret_resolver import SecretResolver

log = logging.getLogger(__name__)

_TIMEOUT_S = 20.0

# The guardrail sentences the model is required to reproduce close to
# verbatim — kept identical to admin-ui/lib/systemPromptBuilder.ts's fixed
# lines, so a from-scratch LLM draft carries the same non-negotiable rules
# as the deterministic one.
_GUARDRAILS = (
    "Never invent facts, prices, policies, order details, or availability. If you do not have "
    "verified information to answer something, say so plainly and offer to check or transfer the "
    "caller — do not guess or make up an answer."
)
_SPOKEN_STYLE = (
    "Answer in at most 2-3 short spoken sentences. Plain conversational speech only — no markdown, "
    "no lists, no headings."
)


def _meta_prompt(inputs: dict[str, Any]) -> str:
    facts = [
        f"Agent name: {inputs['name']}",
        f"Purpose: {inputs['purpose'] or '(not specified)'}",
        f"Identity/persona: {inputs['persona'] or '(not specified)'}",
        f"Tone: {inputs['tone'] or '(not specified)'}",
        f"Language: {inputs['language'] or '(derive from voice/provider)'}",
        f"Has a knowledge base attached: {'yes' if inputs['has_knowledge_base'] else 'no'}",
        f"Transfer rule: {inputs['transfer_condition'] or '(none configured)'}",
        f"Fallback response when it doesn't know something: {inputs.get('fallback_response') or '(none specified)'}",
        f"Compliance rules it must always follow: {inputs.get('compliance_instructions') or '(none specified)'}",
    ]
    return (
        "Write a system prompt for a real-time voice AI agent, for the facts below. "
        "Write it as instructions addressed to the agent (second person), in prose, one "
        "paragraph, no headings or bullet points.\n\n"
        + "\n".join(facts)
        + "\n\nThe prompt you write MUST include, reproduced close to verbatim, these exact "
        "rules (translate only if the target language is not English; do not paraphrase or "
        "soften them):\n"
        f"1. {_GUARDRAILS}\n"
        f"2. {_SPOKEN_STYLE}\n"
        + ("3. If a knowledge base is attached, add one sentence requiring every factual claim "
           "to be grounded in it, and to admit not knowing rather than improvising when it "
           "doesn't cover the question.\n" if inputs["has_knowledge_base"] else "")
        + (f"4. When it doesn't know something, it must say, verbatim: \"{inputs['fallback_response']}\"\n"
           if inputs.get("fallback_response") else "")
        + (f"5. It must always follow these rules, verbatim: {inputs['compliance_instructions']}\n"
           if inputs.get("compliance_instructions") else "")
        + "Return only the system prompt text, nothing else — no preamble, no quotes."
    )


async def _call_openai(api_key: str, model: str, prompt: str) -> str:
    async with httpx.AsyncClient(base_url="https://api.openai.com", timeout=_TIMEOUT_S) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "temperature": 0.4,
            },
        )
    if resp.status_code != 200:
        log.warning("OpenAI chat/completions returned %s: %s", resp.status_code, resp.text[:200])
        raise ValueError(f"OpenAI returned {resp.status_code}")
    return resp.json()["choices"][0]["message"]["content"].strip()


async def _call_anthropic(api_key: str, model: str, prompt: str) -> str:
    async with httpx.AsyncClient(base_url="https://api.anthropic.com", timeout=_TIMEOUT_S) as client:
        resp = await client.post(
            "/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 400,
                "stream": False,
            },
        )
    if resp.status_code != 200:
        log.warning("Anthropic messages returned %s: %s", resp.status_code, resp.text[:200])
        raise ValueError(f"Anthropic returned {resp.status_code}")
    return resp.json()["content"][0]["text"].strip()


_CALLERS = {"openai": _call_openai, "anthropic": _call_anthropic}
_DEFAULT_MODEL = {"openai": "gpt-4o-mini", "anthropic": "claude-3-5-haiku-20241022"}


async def generate_system_prompt(
    tenant_id: Any, llm_config_id: Any, inputs: dict[str, Any], *, secret_resolver: SecretResolver,
) -> str:
    cfg = await get_provider_config(llm_config_id)
    if cfg is None:
        raise LookupError(f"provider_config {llm_config_id} not found")
    if str(cfg["tenant_id"]) != str(tenant_id):
        raise ValueError(f"llm_config_id={llm_config_id!r} belongs to a different tenant")
    if cfg["role"] != "llm":
        raise ValueError(f"provider_config {llm_config_id} is role={cfg['role']!r}, expected 'llm'")
    caller = _CALLERS.get(cfg["engine"])
    if caller is None:
        raise ValueError(
            f"system-prompt generation isn't supported for engine {cfg['engine']!r} yet — "
            f"supported: {sorted(_CALLERS)}"
        )
    if not cfg["api_key_ref"]:
        raise ValueError(f"provider_config {llm_config_id} has no api_key_ref configured")

    api_key = await secret_resolver.resolve(cfg["api_key_ref"])
    prompt = _meta_prompt(inputs)
    try:
        return await caller(api_key, cfg["model"] or _DEFAULT_MODEL[cfg["engine"]], prompt)
    except httpx.RequestError as exc:
        raise ValueError(f"could not reach the LLM provider: {exc.__class__.__name__}") from exc
