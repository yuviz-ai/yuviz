"""Config Service REST client, authenticated as a service account (re-logs in on 401).

Doesn't write to Redis: the GET endpoints it calls populate the cache themselves.
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
        # transport is a test hook (e.g. httpx.ASGITransport); production never passes it.
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
            # Re-authenticate once, not in a loop, so a broken account fails loudly.
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
        """Startup prewarming only; never on the call path."""
        return await self._get("/tenants") or []

    async def list_agents(self, tenant_slug: str) -> list[dict[str, Any]]:
        return await self._get(f"/tenants/{tenant_slug}/agents") or []
