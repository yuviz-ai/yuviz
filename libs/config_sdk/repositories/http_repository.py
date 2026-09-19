"""
HttpConfigRepository — calls Config Service's real REST API (the same
endpoints Admin UI uses), authenticated as a service account rather than a
human user. Config Service's routes require a valid JWT on every request
now (see services/config/deps.py) — this repository owns logging in once
and re-authenticating on a 401, so nothing upstream (CacheAsideConfigProvider,
agent_resolver.py) needs to know auth exists at all.

Deliberately does NOT write to Redis on a miss: the GET endpoints it calls
(services/config/{tenants,agents,provider_configs}.py) already populate
Redis themselves as a side effect of the read (their own cache-aside,
unchanged, still the single place that logic lives). Duplicating that write
here would be a second implementation of the same cache-population logic
that could silently drift from the first.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..exceptions import RepositoryUnavailableError

log = logging.getLogger(__name__)


class HttpConfigRepository:
    def __init__(
        self,
        base_url: str,
        service_email: str,
        service_password: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # transport is a testing hook only (e.g. httpx.ASGITransport against
        # services.config.app.app in-process, same convention test_api.py
        # already uses) — production callers never pass it, so this stays a
        # real network client by default; it does not weaken "the SDK
        # exposes abstractions not transport details" for actual consumers.
        self._base_url = base_url.rstrip("/")
        self._email = service_email
        self._password = service_password
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=5.0, transport=transport)
        self._token: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _login(self) -> str:
        resp = await self._client.post(
            "/auth/login", json={"email": self._email, "password": self._password},
        )
        if resp.status_code != 200:
            raise RepositoryUnavailableError(
                f"HttpConfigRepository: service-account login failed status={resp.status_code}",
            )
        return resp.json()["access_token"]

    async def _get(self, path: str) -> dict[str, Any] | None:
        if self._token is None:
            self._token = await self._login()

        try:
            resp = await self._client.get(path, headers={"Authorization": f"Bearer {self._token}"})
        except httpx.HTTPError as exc:
            raise RepositoryUnavailableError(f"HttpConfigRepository: request failed path={path}: {exc}") from exc

        if resp.status_code == 401:
            # Token expired or was never valid — re-authenticate once, not in
            # a loop, so a genuinely broken service account fails loudly
            # (RepositoryUnavailableError) instead of hammering /auth/login.
            self._token = await self._login()
            resp = await self._client.get(path, headers={"Authorization": f"Bearer {self._token}"})

        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RepositoryUnavailableError(
                f"HttpConfigRepository: GET {path} returned status={resp.status_code}",
            )
        return resp.json()

    async def fetch_tenant(self, tenant_slug: str) -> dict[str, Any] | None:
        return await self._get(f"/tenants/{tenant_slug}")

    async def fetch_agent(self, tenant_slug: str, agent_slug: str) -> dict[str, Any] | None:
        return await self._get(f"/tenants/{tenant_slug}/agents/{agent_slug}")

    async def fetch_provider_config(self, provider_id: str) -> dict[str, Any] | None:
        return await self._get(f"/providers/{provider_id}")

    async def fetch_call_flow(self, tenant_slug: str, call_flow_id: str) -> dict[str, Any] | None:
        return await self._get(f"/tenants/{tenant_slug}/call-flows/{call_flow_id}/published")

    async def list_tenants(self) -> list[dict[str, Any]]:
        """Enumeration, not per-call resolution — used only by startup
        prewarming (see services/conversation/__main__.py), never on the
        call path."""
        return await self._get("/tenants") or []

    async def list_agents(self, tenant_slug: str) -> list[dict[str, Any]]:
        return await self._get(f"/tenants/{tenant_slug}/agents") or []
