"""ToolPolicyResolver — which registered tools an agent may use, from
agent_tool_policies/tool_provider_configs (direct Postgres + in-process TTL cache).
Note: RuntimeConfig.tools / get_tools() in libs/config_sdk is an unused stub for the same concept."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, replace
from typing import Any

import asyncpg

from libs.tenancy import tenant_conn

from .registry import ToolRegistry
from .types import ToolDefinition

log = logging.getLogger(__name__)

_DEFAULT_CACHE_TTL_S = 30.0


@dataclass(frozen=True)
class ResolvedToolPolicy:
    definition:              ToolDefinition
    tool_provider_config_id: str
    engine:                  str
    api_key_ref:             str | None
    extra:                   dict[str, Any]
    timeout_ms:              int | None
    max_calls_per_turn:      int | None
    # execute_api only; None = platform default.
    max_chain_depth:         int | None = None
    # execute_api only — sensitive caller-param names across the agent's enabled APIs.
    sensitive_arg_keys:      frozenset[str] = frozenset()


class ToolPolicyResolver:
    def __init__(
        self, pool: asyncpg.Pool | None, registry: ToolRegistry, cache_ttl_s: float = _DEFAULT_CACHE_TTL_S,
    ) -> None:
        self._pool = pool
        self._registry = registry
        self._cache_ttl_s = cache_ttl_s
        # Keyed by tenant too, so a cross-tenant agent_id collision can't leak cached policy.
        self._cache: dict[tuple[str, str], tuple[float, list[ResolvedToolPolicy]]] = {}

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
        self, agent_id: str, tenant_slug: str, only: list[str] | None = None,
    ) -> list[ResolvedToolPolicy]:
        """Enabled tools for agent_id in tenant_slug; `only` subsets by name (None = all, [] = none).
        An empty/unresolvable slug raises TenantUnresolved."""
        if not agent_id or self._pool is None:
            return []

        cache_key = (agent_id, tenant_slug)
        cached = self._cache.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < self._cache_ttl_s:
            return _narrow(cached[1], only)

        async with tenant_conn(
            self._pool, explicit_tenant=tenant_slug, reason="conversation-tool-policy",
        ) as conn:
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

        await self._specialize_execute_api_if_present(resolved, agent_id, tenant_slug)

        # Cache unnarrowed; `only` is applied on read (varies per node).
        self._cache[cache_key] = (time.monotonic(), resolved)
        return _narrow(resolved, only)

    async def _specialize_execute_api_if_present(
        self, resolved: list[ResolvedToolPolicy], agent_id: str, tenant_slug: str,
    ) -> None:
        """Specialize execute_api if present; drop it when the agent has no enabled custom APIs."""
        for i, policy in enumerate(resolved):
            if policy.definition.name != "execute_api":
                continue
            specialized = await self._specialize_execute_api(policy.definition, agent_id, tenant_slug)
            if specialized is None:
                del resolved[i]
            else:
                defn, sensitive_arg_keys = specialized
                resolved[i] = replace(policy, definition=defn, sensitive_arg_keys=sensitive_arg_keys)
            return

    async def _specialize_execute_api(
        self, defn: ToolDefinition, agent_id: str, tenant_slug: str,
    ) -> tuple[ToolDefinition, frozenset[str]] | None:
        """Return (defn with api_name enum + param docs, sensitive caller-param names), or None if no APIs.
        Upstream-only APIs are hidden and caller params are gathered across each API's whole chain;
        the agent/API tenant join is the runtime tenant fence."""
        async with tenant_conn(
            self._pool, explicit_tenant=tenant_slug, reason="conversation-tool-policy",
        ) as conn:
            rows = await conn.fetch(
                """
                WITH RECURSIVE enabled AS (
                    SELECT ca.id, ca.name, ca.description
                    FROM agent_custom_apis aca
                    JOIN custom_apis ca ON ca.id = aca.custom_api_id AND ca.deleted_at IS NULL
                    JOIN agents      a  ON a.id  = aca.agent_id AND a.tenant_id = ca.tenant_id
                    WHERE aca.agent_id = $1 AND aca.enabled
                ),
                -- Every API reachable from each enabled API: itself, plus
                -- its transitive upstream dependencies. The depth cap is a
                -- termination guard for a cycle in the data, NOT the chain
                -- limit — services/toolexec's resolve_order() owns that and
                -- raises cycle_detected/depth_limit_exceeded for real.
                chain(root_id, api_id, depth) AS (
                    SELECT id, id, 1 FROM enabled
                  UNION ALL
                    SELECT c.root_id, p.upstream_api_id, c.depth + 1
                    FROM chain c
                    JOIN custom_api_params p
                      ON p.custom_api_id = c.api_id
                     AND p.source = 'upstream'
                     AND p.upstream_api_id IS NOT NULL
                    WHERE c.depth < 8
                )
                SELECT e.id, e.name, e.description,
                       EXISTS (
                           SELECT 1 FROM chain c2
                           WHERE c2.api_id = e.id AND c2.root_id <> e.id
                       ) AS is_intermediate,
                       p.name        AS param_name,
                       p.description AS param_description,
                       p.json_type, p.required,
                       p.sensitive   AS param_sensitive
                FROM enabled e
                LEFT JOIN chain c ON c.root_id = e.id
                LEFT JOIN custom_api_params p
                       ON p.custom_api_id = c.api_id AND p.source = 'caller'
                ORDER BY e.name, p.name
                """,
                agent_id,
            )
        if not rows:
            return None

        offerable = {r["name"] for r in rows if not r["is_intermediate"]}
        if not offerable:
            # Only possible with a dependency cycle; offer all rather than an empty enum.
            log.warning(
                "ToolPolicyResolver: every enabled custom API for agent_id=%s is an upstream of "
                "another (dependency cycle?) — offering all of them unfiltered", agent_id,
            )
            offerable = {r["name"] for r in rows}

        apis: dict[str, dict[str, Any]] = {}
        sensitive_arg_keys: set[str] = set()
        for row in rows:
            if row["name"] not in offerable:
                continue
            api = apis.setdefault(row["name"], {"description": row["description"], "params": {}})
            # Dedup: a diamond in the chain reaches the same leaf twice.
            if row["param_name"] is not None and row["param_name"] not in api["params"]:
                api["params"][row["param_name"]] = row
                if row["param_sensitive"]:
                    sensitive_arg_keys.add(row["param_name"])

        api_docs = "\n".join(
            f"- {name}: {api['description']}" + "".join(
                f"\n    * {p['param_name']} ({p['json_type']}"
                f"{', required' if p['required'] else ''}): {p['param_description']}"
                for p in sorted(api["params"].values(), key=lambda r: r["param_name"])
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


def _narrow(
    resolved: list[ResolvedToolPolicy], only: list[str] | None,
) -> list[ResolvedToolPolicy]:
    """Subset by name — never grants. None means unnarrowed."""
    if only is None:
        return resolved
    allowed = set(only)
    return [p for p in resolved if p.definition.name in allowed]
