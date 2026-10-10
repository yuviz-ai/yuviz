"""Resolve (tenant_slug, agent_slug) into (RuntimeConfig, ProviderBundle) via the
Config SDK. Never raises: any failure returns None and the caller decides the fallback."""

from __future__ import annotations

import logging

from libs.config_sdk import ConfigUnavailableError, IConfigProvider, RuntimeConfig

from .provider_bundle import ProviderBundle, ProviderRegistry

log = logging.getLogger(__name__)


async def resolve_handler_deps(
    tenant_slug: str,
    agent_slug: str,
    registry: ProviderRegistry,
    config: IConfigProvider,
    *,
    include_inactive: bool = False,
) -> tuple[RuntimeConfig, ProviderBundle] | None:
    try:
        runtime_config = await config.get_runtime_config(
            tenant_slug, agent_slug, include_inactive=include_inactive
        )
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
    except ConfigUnavailableError:
        log.warning("agent_resolver: config unavailable tenant=%s agent=%s", tenant_slug, agent_slug)
        return None
    except Exception:
        log.exception(
            "agent_resolver: resolution failed tenant=%s agent=%s — falling back to legacy config",
            tenant_slug, agent_slug,
        )
        return None
