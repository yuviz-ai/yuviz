"""
SSRF-guard tests for services/toolexec/custom_apis.resolve_and_validate_endpoint
(T5, finding 6). Every rejection case asserts the whole URL is refused, not
merely that a bad record is skipped.
"""

from __future__ import annotations

import pytest

from services.toolexec import custom_apis


@pytest.mark.parametrize("url", [
    # https, not http, so this is denied by the IP deny-list itself, not
    # merely by the http-scheme gate (that gate is exercised separately
    # below, against an ordinary host).
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
    """A host with two A records, only one of which is private, must reject
    the whole URL — never silently dial 'the good one'."""

    async def _resolver(hostname, port):
        return ["8.8.8.8", "10.0.0.5"]

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)
    with pytest.raises(ValueError, match="invalid_endpoint_url"):
        await custom_apis.resolve_and_validate_endpoint("https://multi-record.example.com/api")


@pytest.mark.parametrize("url,expected_reason", [
    # Each of these previously satisfied only the shared "invalid_endpoint_url"
    # prefix, which the DNS-failure path ALSO produces on a network-isolated
    # runner (finding 2: a test passing for the wrong reason). Matching the
    # specific reason text means a runner with no DNS reachability makes
    # these fail loudly instead of silently passing via
    # "DNS resolution failed for ...".
    ("https://169.254.169.254/latest/meta-data/", "resolves to a denied address"),
    ("https://[::1]/", "resolves to a denied address"),
    ("https://100.64.0.1/", "resolves to a denied address"),
    ("https://user:pass@example.com/api", "userinfo is not allowed"),
])
@pytest.mark.asyncio
async def test_rejection_reason_is_specific_not_dns_failure(url, expected_reason):
    """Verified by mutation: temporarily forcing _resolve_addresses to always
    raise socket.gaierror (simulating an unreachable/DNS-isolated runner)
    made the three denied-address cases raise 'DNS resolution failed for'
    instead of 'resolves to a denied address' — this test failed under that
    mutation (match=expected_reason no longer satisfied), then the mutation
    was reverted. The userinfo case is unaffected by DNS at all (it is
    rejected before any lookup), so it pins the userinfo-specific message on
    its own merit."""
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
