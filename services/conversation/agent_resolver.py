"""Resolve (tenant_slug, agent_slug) into (RuntimeConfig, ProviderBundle) via the
Config SDK. Never raises: any failure returns None so the call falls back to legacy config."""

from __future__ import annotations

import logging

from libs.config_sdk import IConfigProvider, RuntimeConfig

from .provider_bundle import ProviderBundle, ProviderRegistry

log = logging.getLogger(__name__)


async def resolve_handler_deps(
    tenant_slug: str,
    agent_slug: str,
    registry: ProviderRegistry,
    config: IConfigProvider,
) -> tuple[RuntimeConfig, ProviderBundle] | None:
    try:
        runtime_config = await config.get_runtime_config(tenant_slug, agent_slug)
        if runtime_config is None:
            # Missing/inactive agent or tenant, or incomplete provider assignment.
            log.info(
                "agent_resolver: no runtime config for tenant=%s agent=%s "
                "— falling back to legacy config",
                tenant_slug, agent_slug,
            )
            return None

        bundle = await registry.resolve(runtime_config.providers)
        return runtime_config, bundle
    except Exception:
        log.exception(
            "agent_resolver: resolution failed tenant=%s agent=%s — falling back to legacy config",
            tenant_slug, agent_slug,
        )
        return None
