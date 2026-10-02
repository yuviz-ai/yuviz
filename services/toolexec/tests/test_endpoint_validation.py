"""SSRF-guard tests for resolve_and_validate_endpoint; rejections refuse the whole URL."""

from __future__ import annotations

import pytest

from services.toolexec import custom_apis


@pytest.mark.parametrize("url", [
    # https so the IP deny-list, not the http gate, rejects these.
    "https://169.254.169.254/latest/meta-data/",
    "https://[::1]/",
    "https://[::ffff:127.0.0.1]/",
    "https://2130706433/",  # decimal for 127.0.0.1
    "https://0x7f.1/",      # hex for 127.0.0.1
    "https://100.64.0.1/",  # CGNAT
])
@pytest.mark.asyncio
async def test_denied_addresses_rejected(url):
    with pytest.raises(ValueError, match="invalid_endpoint_url"):
        await custom_apis.resolve_and_validate_endpoint(url)


@pytest.mark.asyncio
async def test_http_scheme_rejected_for_non_allowlisted_host(monkeypatch):
    monkeypatch.delenv("TOOLEXEC_HTTP_HOST_ALLOWLIST", raising=False)
    with pytest.raises(ValueError, match="invalid_endpoint_url"):
        await custom_apis.resolve_and_validate_endpoint("http://example.com/api")


@pytest.mark.asyncio
async def test_userinfo_rejected():
    with pytest.raises(ValueError, match="invalid_endpoint_url"):
        await custom_apis.resolve_and_validate_endpoint("https://user:pass@example.com/api")


@pytest.mark.asyncio
async def test_multi_record_one_private_rejects_whole_url(monkeypatch):
    """One private record among several rejects the whole URL."""

    async def _resolver(hostname, port):
        return ["8.8.8.8", "10.0.0.5"]

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)
    with pytest.raises(ValueError, match="invalid_endpoint_url"):
        await custom_apis.resolve_and_validate_endpoint("https://multi-record.example.com/api")


@pytest.mark.parametrize("url,expected_reason", [
    # Specific reasons, so a DNS-isolated runner can't pass via the dns_resolution_failed path.
    ("https://169.254.169.254/latest/meta-data/", "resolves to a denied address"),
    ("https://[::1]/", "resolves to a denied address"),
    ("https://100.64.0.1/", "resolves to a denied address"),
    ("https://user:pass@example.com/api", "userinfo is not allowed"),
])
@pytest.mark.asyncio
async def test_rejection_reason_is_specific_not_dns_failure(url, expected_reason):
    """Each rejection carries its specific reason, not a DNS-failure message."""
    with pytest.raises(ValueError, match=expected_reason):
        await custom_apis.resolve_and_validate_endpoint(url)


@pytest.mark.asyncio
async def test_http_scheme_rejection_reason_is_specific(monkeypatch):
    monkeypatch.delenv("TOOLEXEC_HTTP_HOST_ALLOWLIST", raising=False)
    with pytest.raises(ValueError, match="http requires an allow-listed host"):
        await custom_apis.resolve_and_validate_endpoint("http://example.com/api")


@pytest.mark.asyncio
async def test_accepts_normal_public_host(monkeypatch):
    async def _resolver(hostname, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)
    hostname, allowed_ips = await custom_apis.resolve_and_validate_endpoint("https://public.example.com/api")
    assert hostname == "public.example.com"
    assert allowed_ips == ["93.184.216.34"]
