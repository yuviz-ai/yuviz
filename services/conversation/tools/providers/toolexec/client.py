"""ToolExecClient — calls toolexec's `/internal/chains/execute` as a service account.
Logs in lazily via Config Service's /auth/login (a different base URL) and re-auths once on 401."""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

_TIMEOUT_S = 10.0  # /auth/login only — the chain-execute call gets its own, derived from the request's own budget

# Round-trip slack on top of chain_budget_ms, so a chain that finishes in budget isn't misread as a timeout.
_CHAIN_TIMEOUT_MARGIN_S = 5.0


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
        # Config Service client, used only for /auth/login.
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
        """Execute a chain, re-authenticating once on 401; timeout derives from chain_budget_ms."""
        timeout = (body["chain_budget_ms"] / 1000) + _CHAIN_TIMEOUT_MARGIN_S
        if self._token is None:
            self._token = await self._login()

        resp = await self._client.post(
            "/internal/chains/execute", json=body, timeout=timeout,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        if resp.status_code == 401:
            self._token = await self._login()
            resp = await self._client.post(
                "/internal/chains/execute", json=body, timeout=timeout,
                headers={"Authorization": f"Bearer {self._token}"},
            )

        resp.raise_for_status()
        return resp.json()
