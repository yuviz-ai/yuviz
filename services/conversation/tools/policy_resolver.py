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

from libs.tenancy import tenant_conn

from .registry import ToolRegistry
from .types import ToolDefinition

log = logging.getLogger(__name__)

_DEFAULT_CACHE_TTL_S = 30.0

# The auto-derived-companion machinery (book_appointment silently granting
# cancel_appointment/reschedule_appointment on the same Cal.com config)
# was removed on 2026-09-18 along with the calendar built-ins themselves.
# Nothing replaces it: execute_api is the only DB-gated tool left, and a
# custom API never implies another custom API — if two of them are related,
# that relationship is an upstream edge in custom_api_params, resolved by
# services/toolexec, not a second tool grant here.


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
        # Keyed by (agent_id, tenant_slug), not agent_id alone — an agent_id
        # collision across tenants can never serve one tenant's cached
        # tool policy back to another.
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
        """Return enabled tools for agent_id, scoped to tenant_slug. `only`
        subsets by name (never grants); None = unnarrowed; [] = none this
        stage. tenant_slug is passed straight to tenant_conn() below — an
        empty/unresolvable slug (the legacy YAML fallback's placeholder,
        see agent_config.py's to_runtime_config()) raises TenantUnresolved
        rather than silently resolving zero tools under the wrong scope."""
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
        """Runs the execute_api specialization query only when an
        agent_tool_policies row for it is actually present (AC 9 — an agent
        that never enabled execute_api issues no second query at all).
        Drops the policy entirely when the agent has zero enabled custom
        APIs, so the LLM never sees an execute_api with an empty enum."""
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
        """Returns (defn with api_name.enum + per-API leaf-input docs, the
        union of sensitive caller-param names across those APIs), or None
        when the agent has zero enabled custom APIs.

        The query is the runtime tenant fence for AC 10 — an agent_custom_apis
        row can only resolve if the agent and the API share a tenant,
        independent of the write-time check in services/toolexec.

        TWO THINGS THIS GETS RIGHT THAT THE FLAT PER-API QUERY DID NOT
        (fixed 2026-09-18 after the model kept picking the wrong API):

        1. An API that is another enabled API's upstream is NOT offered.
           services/toolexec calls it automatically as a chain step, so
           offering it invites the model to call a half-chain directly. In
           the reference tenant that was 5 of 8 names in the enum.

        2. An API's caller params are collected across its WHOLE chain, not
           just its own row. A terminal API usually declares no caller
           params of its own — its inputs live on the leaf it depends on
           (get_product_details needs `q`, which belongs to
           search_products). The flat query therefore documented exactly
           the wrong three APIs as taking no arguments at all, while the
           intermediate ones advertised the `q` the caller had just said.
           A model shown that will pick the intermediate one, correctly,
           given what it was told.

        Together these make the enum mean "things you can ask for" and the
        params mean "what you must supply", which is what execute_api's own
        description has always promised."""
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

        # A name is offerable unless it is some other enabled API's
        # upstream. Collected first so the params pass can skip the rest.
        offerable = {r["name"] for r in rows if not r["is_intermediate"]}
        if not offerable:
            # Every enabled API is an upstream of another — only reachable
            # through a cycle in the data, which resolve_order() will
            # refuse anyway. Offer everything rather than silently handing
            # the model an empty enum, and say so in the log.
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
            # Keyed by param name: a diamond in the chain reaches the same
            # leaf twice, and the model must be told about it once.
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
