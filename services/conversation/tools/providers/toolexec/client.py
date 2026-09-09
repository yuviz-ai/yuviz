"""
ToolExecClient — calls Tool Execution Service's internal
`/internal/chains/execute` route, authenticated as a service account.
Copies `libs/knowledge_sdk/repositories/http_repository.py` verbatim in
shape: lazy login, one re-authentication on a 401, a `transport`/
`auth_transport` testing hook for ASGITransport/MockTransport. Reuses the
same JWT mechanism (services.config.auth) Tool Execution Service validates
directly (via services.config.deps.get_current_user) — login and the
execute call target two different base URLs, since JWTs are only ever
minted by Config Service's /auth/login.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

_TIMEOUT_S = 10.0


class ToolExecClient:
    def __init__(
        self,
        base_url: str,
        auth_base_url: str,
        service_email: str,
        service_password: str,
        transport: httpx.BaseTransport | None = None,
        auth_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._email = service_email
        self._password = service_password
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=_TIMEOUT_S, transport=transport)
        # A second client for auth_base_url (Config Service) — only
        # /auth/login is ever called on it; auth_transport defaults to
        # `transport` when both services are the same ASGI app under test.
        self._auth_client = httpx.AsyncClient(
            base_url=auth_base_url.rstrip("/"), timeout=_TIMEOUT_S,
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
        resp.raise_for_status()
        return resp.json()["access_token"]

    async def execute_chain(self, body: dict[str, Any]) -> dict[str, Any]:
        """POSTs the chain-execute request once, re-authenticating exactly
        once on a 401 (an expired/invalid token) before giving up."""
        if self._token is None:
            self._token = await self._login()

        resp = await self._client.post(
            "/internal/chains/execute", json=body,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        if resp.status_code == 401:
            self._token = await self._login()
            resp = await self._client.post(
                "/internal/chains/execute", json=body,
                headers={"Authorization": f"Bearer {self._token}"},
            )

        resp.raise_for_status()
        return resp.json()
