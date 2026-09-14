"""
ToolPolicyResolver — DB-aware agent tool enablement, deliberately separate
from ToolRegistry (design review point 7): the registry knows what tools
exist in code; this class knows which of them a given agent may actually
use, resolved from agent_tool_policies/tool_provider_configs.

v1 simplification, flagged explicitly rather than silently done: this
queries Postgres directly with a simple in-process TTL cache, mirroring
TranscriptBuilder's own direct-asyncpg precedent in this same service —
NOT the full Config SDK cache-aside (HTTP + Redis) pattern RuntimeConfig
uses. Promoting this to that pattern (so tool-policy changes propagate the
same way agent config changes do) is the natural v2 hardening step, not
done here to avoid an invasive change to shared libs/config_sdk for a
single small table.

KNOWN CONFLICT, found while building this (2026-07-22), not resolved here:
libs/config_sdk/models.py already has RuntimeConfig.tools: list[ToolSpec],
fed by IConfigProvider.get_tools() — an earlier, pre-existing stub
explicitly labeled "Phase 6b concept, not built yet," always returning []
in both CacheAsideConfigProvider and MockProvider, with no real backing
implementation anywhere. This class is the actual, working implementation
of that same "which tools can an agent use" concept, but it does NOT go
through that seam — agent_tool_policies/tool_provider_configs are an
entirely separate, parallel mechanism. Whoever does the v2 hardening above
should also resolve this: either retire ToolSpec/get_tools() in favor of
this resolver's shape, or re-architect this resolver to flow through that
existing Config SDK seam instead of a direct Postgres query. Left as two
concepts today rather than guessing which one the rest of the codebase
will actually standardize on.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, replace
from typing import Any

import asyncpg

from .registry import ToolRegistry
from .types import ToolDefinition

log = logging.getLogger(__name__)

_DEFAULT_CACHE_TTL_S = 30.0

# cancel_appointment/reschedule_appointment are deliberately NOT
# independently configurable (2026-07-23 design decision): they're the
# natural counterparts of book_appointment, not separate features a tenant
# would want without it — there's no real scenario where a business offers
# booking but not cancellation/rescheduling. So neither is ever something an
# admin adds/configures via its own tool_provider_config/agent_tool_policies
# row; both are automatically derived here, reusing whichever
# tool_provider_config book_appointment already uses (same Cal.com
# account/event type — CalComCalendarProvider already implements
# check_availability/book_appointment, find_upcoming_bookings/
# cancel_appointment, AND reschedule_appointment on the same instance).
# Disabling book_appointment (agent_tool_policies.enabled=false, or
# removing it entirely) automatically disables both derived tools too, with
# no separate action needed — they simply never appear in `rows` to derive
# from.
_AUTO_DERIVED_COMPANIONS = {"book_appointment": ["cancel_appointment", "reschedule_appointment"]}


@dataclass(frozen=True)
class ResolvedToolPolicy:
    definition:              ToolDefinition
    tool_provider_config_id: str
    engine:                  str
    api_key_ref:             str | None
    extra:                   dict[str, Any]
    timeout_ms:              int | None
    max_calls_per_turn:      int | None
    # execute_api only — the agent's effective chain-depth ceiling from
    # agent_tool_policies.max_chain_depth (NULL = platform default, same
    # NULL-means-framework-default contract as timeout_ms above). None for
    # every other tool.
    max_chain_depth:         int | None = None
    # execute_api only — union of custom_api_params.name WHERE sensitive AND
    # source='caller', across the agent's enabled APIs (see
    # _specialize_execute_api). Empty frozenset for every legacy tool, so
    # their logging stays byte-identical to today (middleware.py finding 8).
    sensitive_arg_keys:      frozenset[str] = frozenset()


class ToolPolicyResolver:
    def __init__(
        self, pool: asyncpg.Pool | None, registry: ToolRegistry, cache_ttl_s: float = _DEFAULT_CACHE_TTL_S,
    ) -> None:
        self._pool = pool
        self._registry = registry
        self._cache_ttl_s = cache_ttl_s
        self._cache: dict[str, tuple[float, list[ResolvedToolPolicy]]] = {}

    @classmethod
    async def connect(cls, database_url: str | None, registry: ToolRegistry) -> "ToolPolicyResolver":
        if not database_url:
            return cls(None, registry)
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
        return cls(pool, registry)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def enabled_tools(
        self, agent_id: str, only: list[str] | None = None,
    ) -> list[ResolvedToolPolicy]:
        """Return enabled tools for agent_id. `only` subsets by name (never
        grants); None = unnarrowed; [] = none this stage."""
        if not agent_id or self._pool is None:
            return []

        cached = self._cache.get(agent_id)
        if cached is not None and time.monotonic() - cached[0] < self._cache_ttl_s:
            return _narrow(cached[1], only)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT atp.tool_name, atp.timeout_ms, atp.max_calls_per_turn, atp.max_chain_depth,
                       tpc.id AS tool_provider_config_id, tpc.engine, tpc.api_key_ref, tpc.extra
                FROM agent_tool_policies atp
                JOIN tool_provider_configs tpc ON tpc.id = atp.tool_provider_config_id
                WHERE atp.agent_id = $1 AND atp.enabled = true AND tpc.deleted_at IS NULL
                """,
                agent_id,
            )

        resolved: list[ResolvedToolPolicy] = []
        for row in rows:
            defn = self._registry.resolve(row["tool_name"])
            if defn is None:
                log.warning(
                    "ToolPolicyResolver: agent_tool_policies references unknown tool_name=%r agent_id=%s — skipping",
                    row["tool_name"], agent_id,
                )
                continue
            raw_extra = row["extra"]
            # asyncpg may return JSONB as str without a codec — accept either.
            extra = json.loads(raw_extra) if isinstance(raw_extra, str) else (raw_extra or {})
            resolved.append(ResolvedToolPolicy(
                definition=defn,
                tool_provider_config_id=str(row["tool_provider_config_id"]),
                engine=row["engine"],
                api_key_ref=row["api_key_ref"],
                extra=extra,
                timeout_ms=row["timeout_ms"],
                max_calls_per_turn=row["max_calls_per_turn"],
                max_chain_depth=row["max_chain_depth"],
            ))

        self._add_auto_derived_companions(resolved, agent_id)
        await self._specialize_execute_api_if_present(resolved, agent_id)

        # Cache unnarrowed; `only` is applied on read (varies per node).
        self._cache[agent_id] = (time.monotonic(), resolved)
        return _narrow(resolved, only)

    async def _specialize_execute_api_if_present(
        self, resolved: list[ResolvedToolPolicy], agent_id: str,
    ) -> None:
        """Runs the execute_api specialization query only when an
        agent_tool_policies row for it is actually present (AC 9 — an agent
        that never enabled execute_api issues no second query at all).
        Drops the policy entirely when the agent has zero enabled custom
        APIs, so the LLM never sees an execute_api with an empty enum."""
        for i, policy in enumerate(resolved):
            if policy.definition.name != "execute_api":
                continue
            specialized = await self._specialize_execute_api(policy.definition, agent_id)
            if specialized is None:
                del resolved[i]
            else:
                defn, sensitive_arg_keys = specialized
                resolved[i] = replace(policy, definition=defn, sensitive_arg_keys=sensitive_arg_keys)
            return

    async def _specialize_execute_api(
        self, defn: ToolDefinition, agent_id: str,
    ) -> tuple[ToolDefinition, frozenset[str]] | None:
        """Returns (defn with api_name.enum + per-API leaf-input docs, the
        union of sensitive caller-param names across those APIs), or None
        when the agent has zero enabled custom APIs.

        The query is the runtime tenant fence for AC 10 — an agent_custom_apis
        row can only resolve if the agent and the API share a tenant,
        independent of the write-time check in services/toolexec."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ca.id, ca.name, ca.description, ca.chain_levels,
                       p.name AS param_name, p.description AS param_description,
                       p.json_type, p.required, p.sensitive AS param_sensitive
                FROM agent_custom_apis aca
                JOIN custom_apis ca ON ca.id = aca.custom_api_id AND ca.deleted_at IS NULL
                JOIN agents      a  ON a.id  = aca.agent_id AND a.tenant_id = ca.tenant_id
                LEFT JOIN custom_api_params p ON p.custom_api_id = ca.id AND p.source = 'caller'
                WHERE aca.agent_id = $1 AND aca.enabled
                ORDER BY ca.name, p.name
                """,
                agent_id,
            )
        if not rows:
            return None

        apis: dict[str, dict[str, Any]] = {}
        sensitive_arg_keys: set[str] = set()
        for row in rows:
            api = apis.setdefault(row["name"], {"description": row["description"], "params": []})
            if row["param_name"] is not None:
                api["params"].append(row)
                if row["param_sensitive"]:
                    sensitive_arg_keys.add(row["param_name"])

        api_docs = "\n".join(
            f"- {name}: {api['description']}" + "".join(
                f"\n    * {p['param_name']} ({p['json_type']}"
                f"{', required' if p['required'] else ''}): {p['param_description']}"
                for p in api["params"]
            )
            for name, api in apis.items()
        )
        specialized = replace(
            defn,
            description=f"{defn.description}\n\nAvailable APIs:\n{api_docs}",
            parameters_schema={
                **defn.parameters_schema,
                "properties": {
                    **defn.parameters_schema["properties"],
                    "api_name": {"type": "string", "enum": sorted(apis)},
                },
            },
        )
        return specialized, frozenset(sensitive_arg_keys)

    def _add_auto_derived_companions(self, resolved: list[ResolvedToolPolicy], agent_id: str) -> None:
        """Fill gaps from _AUTO_DERIVED_COMPANIONS; explicit rows win."""
        present = {p.definition.name for p in resolved}
        for source_name, derived_names in _AUTO_DERIVED_COMPANIONS.items():
            if source_name not in present:
                continue
            source = next(p for p in resolved if p.definition.name == source_name)
            for derived_name in derived_names:
                if derived_name in present:
                    continue
                derived_defn = self._registry.resolve(derived_name)
                if derived_defn is None:
                    log.warning(
                        "ToolPolicyResolver: _AUTO_DERIVED_COMPANIONS references unknown tool_name=%r agent_id=%s",
                        derived_name, agent_id,
                    )
                    continue
                resolved.append(ResolvedToolPolicy(
                    definition=derived_defn,
                    tool_provider_config_id=source.tool_provider_config_id,
                    engine=source.engine,
                    api_key_ref=source.api_key_ref,
                    extra=source.extra,
                    timeout_ms=source.timeout_ms,
                    max_calls_per_turn=source.max_calls_per_turn,
                ))
                present.add(derived_name)


def _narrow(
    resolved: list[ResolvedToolPolicy], only: list[str] | None,
) -> list[ResolvedToolPolicy]:
    """Subset by name; companions ride along when their source is allowed."""
    if only is None:
        return resolved
    allowed = set(only)
    for source_name, derived_names in _AUTO_DERIVED_COMPANIONS.items():
        if source_name in allowed:
            allowed.update(derived_names)
    return [p for p in resolved if p.definition.name in allowed]
