"""Path A dial: write the dial intent to Redis, then ask Kamailio's loopback dial handler to send the trigger INVITE.

The intent (callee, caller ID, tenant, time limit) is what FreeSWITCH dials from; the signed HTTP body and the
SIP headers carry only identifiers. Wire formats are pinned by tests/vectors, shared with the Kamailio side."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Literal

import httpx
import redis.asyncio as redis

log = logging.getLogger(__name__)

INTENT_TTL_S = 60
EGRESS_GRACE_S = 120
HTTP_TIMEOUT_S = 3.0

_KEY = re.compile(r"[0-9a-f-]{36}_[0-9]{1,3}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_E164 = re.compile(r"\+[1-9][0-9]{6,14}")
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_MAX_CALL_S = re.compile(r"[1-9][0-9]{1,3}")

SendResult = Literal["accepted", "duplicate", "rejected", "no_node", "timeout"]


def is_e164(value: str) -> bool:
    return _E164.fullmatch(value) is not None


class InvalidDialField(ValueError):
    """A value that could not be placed safely in a SIP header, JSON body or originate string."""


def _mac(secret: bytes, message: bytes) -> str:
    """Unpadded base64url, the form Kamailio's crypto_hmac_sha256 produces."""
    return base64.urlsafe_b64encode(hmac.new(secret, message, hashlib.sha256).digest()).rstrip(b"=").decode()


def egress_token(
    *, key: str, callee: str, caller_id: str, tenant_id: str, max_call_s: int, exp: int, secret: bytes,
) -> str:
    """`key.exp.tenant_id.caller_id.max_call_s.mac`, authorising PSTN egress once, for exactly this callee."""
    for name, value in (("key", key), ("tenant_id", tenant_id), ("caller_id", caller_id), ("max_call_s", max_call_s)):
        if "." in str(value):
            raise InvalidDialField(f"{name} must not contain '.'")
    mac = _mac(secret, f"{key}|{callee}|{caller_id}|{tenant_id}|{max_call_s}|{exp}".encode())
    return f"{key}.{exp}.{tenant_id}.{caller_id}.{max_call_s}.{mac}"


def verify_egress_token(token: str, *, callee: str, secret: bytes, now: int | None = None) -> bool:
    """Python twin of the Kamailio check: expiry, then MAC over the token's own fields plus the dialled callee."""
    parts = token.split(".")
    if len(parts) != 6 or not parts[1].isdigit() or not parts[4].isdigit():
        return False
    key, exp, tenant_id, caller_id, max_call_s, mac = parts
    if int(exp) < (int(time.time()) if now is None else now):
        return False
    expected = _mac(secret, f"{key}|{callee}|{caller_id}|{tenant_id}|{max_call_s}|{exp}".encode())
    return hmac.compare_digest(mac, expected)


@dataclass(frozen=True)
class DialIntent:
    key: str
    tenant_id: str
    campaign_id: str
    contact_id: str
    call_uuid: str
    callee: str
    caller_id: str
    tenant_slug: str
    agent_slug: str
    exp: int
    egress_token: str
    max_call_s: int

    def __post_init__(self) -> None:
        patterns = (
            ("key", self.key, _KEY), ("tenant_id", self.tenant_id, _UUID), ("campaign_id", self.campaign_id, _UUID),
            ("contact_id", self.contact_id, _UUID), ("call_uuid", self.call_uuid, _UUID),
            ("callee", self.callee, _E164), ("caller_id", self.caller_id, _E164),
            ("tenant_slug", self.tenant_slug, _SLUG), ("agent_slug", self.agent_slug, _SLUG),
            ("max_call_s", str(self.max_call_s), _MAX_CALL_S),
        )
        for name, value, pattern in patterns:
            if not isinstance(value, str) or not pattern.fullmatch(value):
                raise InvalidDialField(f"{name} is not in the accepted form")


def build_intent(
    *, key: str, tenant_id: str, campaign_id: str, contact_id: str, call_uuid: str, callee: str, caller_id: str,
    tenant_slug: str, agent_slug: str, max_call_s: int, egress_secret: bytes, now: int | None = None,
) -> DialIntent:
    exp = (int(time.time()) if now is None else now) + INTENT_TTL_S
    token = egress_token(
        key=key, callee=callee, caller_id=caller_id, tenant_id=tenant_id, max_call_s=max_call_s,
        exp=exp + EGRESS_GRACE_S, secret=egress_secret,
    )
    return DialIntent(
        key=key, tenant_id=tenant_id, campaign_id=campaign_id, contact_id=contact_id, call_uuid=call_uuid,
        callee=callee, caller_id=caller_id, tenant_slug=tenant_slug,
        agent_slug=agent_slug, exp=exp, egress_token=token, max_call_s=max_call_s,
    )


def canonical_body(intent: DialIntent, ts: int) -> bytes:
    """Fixed key order and no whitespace: Kamailio rebuilds this and requires byte equality."""
    fields = {
        "key": intent.key, "campaign_id": intent.campaign_id, "contact_id": intent.contact_id,
        "callee": intent.callee, "caller_id": intent.caller_id,
        "tenant_slug": intent.tenant_slug, "agent_slug": intent.agent_slug, "ts": ts,
    }
    return json.dumps(fields, separators=(",", ":")).encode()


class DialRequestClient:
    def __init__(self, dial_url: str, hmac_secret: bytes, redis_client: redis.Redis) -> None:
        self._dial_url = dial_url.rstrip("/")
        self._secret = hmac_secret
        self._redis = redis_client
        self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)

    async def close(self) -> None:
        await self._http.aclose()

    async def send(self, intent: DialIntent, *, first: bool) -> SendResult:
        intent_key = f"campaign:intent:{intent.key}"
        if first:
            if not await self._redis.set(intent_key, json.dumps(asdict(intent)), nx=True, ex=INTENT_TTL_S):
                return "duplicate"
        elif not await self._redis.exists(intent_key) or await self._redis.exists(f"campaign:claim:{intent.key}"):
            return "duplicate"

        try:
            resp = await self._post(self._dial_url, canonical_body(intent, int(time.time())))
        except httpx.HTTPError:
            log.warning("dial request to Kamailio got no answer key=%s", intent.key)
            return "timeout"
        if resp.status_code == 200:
            return "accepted"
        if resp.status_code >= 500 and resp.status_code != 503:
            return "timeout"
        # Nothing was sent: make sure the key can never dial.
        await self._redis.delete(intent_key)
        if resp.status_code == 503:
            return "no_node"
        log.error("Kamailio dial handler refused key=%s status=%s", intent.key, resp.status_code)
        return "rejected"

    async def probe(self) -> bool:
        try:
            body = json.dumps({"ts": int(time.time())}, separators=(",", ":")).encode()
            resp = await self._post(f"{self._dial_url}/health", body)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def _post(self, url: str, body: bytes) -> httpx.Response:
        return await self._http.post(
            url, content=body,
            headers={"X-Campaign-Signature": _mac(self._secret, body), "Content-Type": "application/json"},
        )
