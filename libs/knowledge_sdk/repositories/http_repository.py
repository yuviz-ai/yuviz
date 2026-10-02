"""Knowledge Service internal REST client, authenticated as a service account.

JWTs are minted only by Config Service, so login and API calls use different base URLs.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..exceptions import RepositoryUnavailableError
from ..models import RetrievalPolicy

log = logging.getLogger(__name__)


class HttpKnowledgeRepository:
    def __init__(
        self,
        base_url: str,
        auth_base_url: str,
        service_email: str,
        service_password: str,
        transport: httpx.BaseTransport | None = None,
        auth_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._email = service_email
        self._password = service_password
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=5.0, transport=transport)
        # Config Service client, used only for /auth/login.
        self._auth_client = httpx.AsyncClient(
            base_url=auth_base_url.rstrip("/"), timeout=5.0,
            transport=auth_transport if auth_transport is not None else transport,
        )
        self._token: str | None = None

    async def close(self) -> None:
        await self._client.aclose()
        await self._auth_client.aclose()

    async def _login(self) -> str:
        resp = await self._auth_client.post(
            "/auth/login", json={"email": self._email, "password": self._password},
        )
        if resp.status_code != 200:
            raise RepositoryUnavailableError(
                f"HttpKnowledgeRepository: service-account login failed status={resp.status_code}",
            )
        return resp.json()["access_token"]

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response | None:
        if self._token is None:
            self._token = await self._login()

        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            resp = await self._client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise RepositoryUnavailableError(
                f"HttpKnowledgeRepository: request failed {method} {path}: {exc}",
            ) from exc

        if resp.status_code == 401:
            self._token = await self._login()
            headers = {"Authorization": f"Bearer {self._token}"}
            resp = await self._client.request(method, path, headers=headers, **kwargs)

        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RepositoryUnavailableError(
                f"HttpKnowledgeRepository: {method} {path} returned status={resp.status_code}",
            )
        return resp

    async def has_enabled_kb(self, tenant_slug: str, agent_slug: str) -> bool:
        resp = await self._request(
            "GET", f"/internal/agents/{tenant_slug}/{agent_slug}/has-knowledge",
        )
        if resp is None:
            return False
        return bool(resp.json()["enabled"])

    async def retrieve(
        self,
        tenant_slug: str,
        agent_slug: str,
        query: str,
        policy: RetrievalPolicy,
    ) -> dict[str, Any] | None:
        # None fields are sent explicitly: None means "no per-call override".
        resp = await self._request(
            "POST",
            "/internal/retrieve",
            json={
                "tenant_slug": tenant_slug,
                "agent_slug": agent_slug,
                "query": query,
                "top_k": policy.top_k,
                "max_tokens": policy.max_tokens,
                "minimum_score": policy.minimum_score,
                "rerank": policy.rerank,
                "hybrid_search": policy.hybrid_search,
                "include_citations": policy.include_citations,
            },
        )
        return None if resp is None else resp.json()
