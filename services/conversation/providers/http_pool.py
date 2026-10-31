"""Connection-pool limits for provider HTTP clients.

httpx defaults to 100 connections per client; one busy tenant's LLM/TTS/STT client
then queues requests behind the pool and first-audio latency balloons. Size it to
expected concurrency, overridable via env.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

DEFAULT_MAX_CONNECTIONS = 1000
DEFAULT_MAX_KEEPALIVE = 200


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer - using %s", name, raw, default)
        return default
    if val < 1:
        log.warning("%s=%r must be >= 1 - using %s", name, raw, default)
        return default
    return val


def provider_http_limits() -> httpx.Limits:
    max_conn = _env_int("PROVIDER_HTTP_MAX_CONNECTIONS", DEFAULT_MAX_CONNECTIONS)
    keepalive = min(_env_int("PROVIDER_HTTP_MAX_KEEPALIVE", DEFAULT_MAX_KEEPALIVE), max_conn)
    return httpx.Limits(max_connections=max_conn, max_keepalive_connections=keepalive)
