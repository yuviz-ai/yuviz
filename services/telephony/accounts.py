"""AccountStore — telephony accounts keyed (provider, account_ref), plus tenant -> default outbound.
Preloaded and refreshed from Config Service; a failed refresh keeps the last-known-good map.
Also prewarms the (tenant, agent) ownership memo so outbound triggers need no Config Service I/O."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from libs.config_sdk.secrets import decrypt_secret, is_encrypted
from libs.telephony_sdk.interface import ITelephonyProvider
from libs.telephony_sdk.registry import TelephonyProviderRegistry

log = logging.getLogger("telephony.accounts")

CONFIG_SERVICE_URL = os.environ.get("CONFIG_SERVICE_URL", "http://localhost:8000").rstrip("/")
_SERVICE_EMAIL = os.environ.get("CONFIG_SERVICE_EMAIL", "conversation-service@internal.yuviz.ai")
_SERVICE_PASSWORD = os.environ.get("CONFIG_SERVICE_PASSWORD", "")

# Test double, never served over REST.
_HIDDEN_PROVIDERS = {"fake"}


@dataclass(frozen=True)
class Account:
    provider: str
    account_ref: str  # telephony_configs.id
    tenant_id: str
    tenant_slug: str
    is_default_outbound: bool
    credentials: dict[str, Any]  # decrypted
    instance: ITelephonyProvider


class AccountStore:
    def __init__(self) -> None:
        self._accounts: dict[tuple[str, str], Account] = {}
        self._default_outbound: dict[str, Account] = {}  # tenant_slug -> Account
        self._agent_memo: dict[tuple[str, str], bool] = {}  # (tenant_slug, agent_slug) -> exists
        self._loaded = False
        self._jwt_token: str | None = None

    @property
    def loaded(self) -> bool:
        return self._loaded

    def get(self, provider: str, account_ref: str) -> Account | None:
        return self._accounts.get((provider, account_ref))

    def all_accounts(self) -> list[Account]:
        return list(self._accounts.values())

    def default_outbound_for(self, tenant_slug: str) -> Account | None:
        return self._default_outbound.get(tenant_slug)

    def agent_known(self, tenant_slug: str, agent_slug: str) -> bool:
        return (tenant_slug, agent_slug) in self._agent_memo

    def remember_agent(self, tenant_slug: str, agent_slug: str) -> None:
        self._agent_memo[(tenant_slug, agent_slug)] = True

    async def _login(self) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{CONFIG_SERVICE_URL}/auth/login",
                json={"email": _SERVICE_EMAIL, "password": _SERVICE_PASSWORD},
            )
            resp.raise_for_status()
            return resp.json()["access_token"]

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        if self._jwt_token is None:
            self._jwt_token = await self._login()

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{CONFIG_SERVICE_URL}{path}", params=params,
                headers={"Authorization": f"Bearer {self._jwt_token}"},
            )
            if resp.status_code == 401:
                self._jwt_token = await self._login()
                resp = await client.get(
                    f"{CONFIG_SERVICE_URL}{path}", params=params,
                    headers={"Authorization": f"Bearer {self._jwt_token}"},
                )
        resp.raise_for_status()
        return resp.json()

    def _decrypt_field(self, account_ref: str, value: Any) -> Any:
        """Decrypt a scalar or list-valued sensitive field; legacy plaintext passes through with a warning."""
        if isinstance(value, list):
            usable = []
            for entry in value:
                decrypted = self._decrypt_scalar(account_ref, entry)
                if decrypted is not None:
                    usable.append(decrypted)
            return usable
        return self._decrypt_scalar(account_ref, value)

    def _decrypt_scalar(self, account_ref: str, entry: Any) -> str | None:
        if not isinstance(entry, str) or not entry:
            return None
        if not is_encrypted(entry):
            log.warning("telephony: account %s has a non-enc credential field, passing through", account_ref)
            return entry
        try:
            return decrypt_secret(entry)
        except Exception:
            log.exception("telephony: account %s has an undecryptable credential field, skipping", account_ref)
            return None

    async def _fetch_agents(self, tenant_slug: str) -> list[str]:
        try:
            agents = await self._get(f"/tenants/{tenant_slug}/agents")
        except Exception:
            log.warning("telephony: failed to prewarm agents for tenant=%s", tenant_slug, exc_info=True)
            return []
        return [a["slug"] for a in agents]

    async def refresh(self) -> None:
        """Reload all accounts; keeps existing maps on failure and skips rows that fail to construct."""
        providers = [
            name for name in TelephonyProviderRegistry.all() if name not in _HIDDEN_PROVIDERS
        ]

        rows: list[dict[str, Any]] = []
        try:
            for provider in providers:
                rows.extend(await self._get("/telephony-configs", params={"provider": provider}))
        except Exception:
            log.exception("telephony: account refresh failed, keeping last-known-good map")
            return

        accounts: dict[tuple[str, str], Account] = {}
        default_outbound: dict[str, Account] = {}
        for row in rows:
            provider = row["provider"]
            account_ref = str(row["id"])
            credentials = row["credentials"]
            provider_cls = TelephonyProviderRegistry.get(provider)

            decrypted = dict(credentials)
            for field_name in provider_cls.sensitive_credential_fields():
                if field_name in decrypted:
                    decrypted[field_name] = self._decrypt_field(account_ref, decrypted[field_name])

            try:
                instance = provider_cls(decrypted)
            except Exception:
                log.exception("telephony: account %s failed to construct provider %s, skipping", account_ref, provider)
                continue

            account = Account(
                provider=provider,
                account_ref=account_ref,
                tenant_id=str(row["tenant_id"]),
                tenant_slug=row["tenant_slug"],
                is_default_outbound=bool(row["is_default_outbound"]),
                credentials=decrypted,
                instance=instance,
            )
            accounts[(provider, account_ref)] = account
            if account.is_default_outbound:
                default_outbound[account.tenant_slug] = account

        agent_memo: dict[tuple[str, str], bool] = {}
        for tenant_slug in {a.tenant_slug for a in accounts.values()}:
            for agent_slug in await self._fetch_agents(tenant_slug):
                agent_memo[(tenant_slug, agent_slug)] = True

        self._accounts = accounts
        self._default_outbound = default_outbound
        self._agent_memo = agent_memo
        self._loaded = True
        log.info("telephony: loaded %d account(s) across %d provider(s)", len(accounts), len(providers))

    async def refresh_loop(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            await self.refresh()

    async def auth_headers(self) -> dict[str, str]:
        """Service-account bearer header, logging in if needed."""
        if self._jwt_token is None:
            self._jwt_token = await self._login()
        return {"Authorization": f"Bearer {self._jwt_token}"}

    def tenant_id_for_slug(self, tenant_slug: str) -> str | None:
        """Map tenant_slug to tenant_id from loaded accounts (no Postgres)."""
        for account in self._accounts.values():
            if account.tenant_slug == tenant_slug:
                return account.tenant_id
        return None


# Process-wide singleton; lives here so modules can import it without a cycle through app.py.
accounts = AccountStore()
