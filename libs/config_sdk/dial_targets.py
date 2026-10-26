"""Shapes a tenant-configured transfer destination or caller id may take.

These values end up inside FreeSWITCH ESL commands built by the Gateway
(gateway/src/telephony/EslClient.cpp), which applies the same allowlist as a
final check. Keep the two in sync.
"""

from __future__ import annotations

import re
import socket

_DIAL_NUMBER_RE = re.compile(r"\+?[0-9]{2,15}")
_SIP_URI_RE = re.compile(r"sips?:[A-Za-z0-9._+:;=~-]+@[A-Za-z0-9._+:;=~-]+")

# The platform's own AI entry numbers (scripts/freeswitch/00_voice_ai.xml,
# scripts/kamailio/kamailio.cfg.tpl). A transfer to one loops back into the
# platform and Kamailio refuses it, so it can never connect.
_PLATFORM_AI_NUMBER_RE = re.compile(r"788|500[0-9]")


def is_dial_number(value: str) -> bool:
    return _DIAL_NUMBER_RE.fullmatch(value) is not None


def is_platform_ai_number(value: str) -> bool:
    return _PLATFORM_AI_NUMBER_RE.fullmatch(value) is not None


def is_sip_uri_shape(value: str) -> bool:
    return _SIP_URI_RE.fullmatch(value) is not None


def _sip_uri_host(value: str) -> str:
    host = re.split(r"[:;]", value.split("@", 1)[1], maxsplit=1)[0].lower()
    return host[:-1] if host.endswith(".") else host


def is_loopback_sip_uri(value: str) -> bool:
    """A sip: URI aimed at this host, which lands in FreeSWITCH's own dialplan.
    The Gateway also refuses its own FreeSWITCH/Kamailio addresses, which only
    it knows."""
    if ";maddr" in value.lower():
        return True
    host = _sip_uri_host(value)
    if not host:
        return True
    try:
        # inet_aton, like the resolver, accepts "127.1" and "2130706433" too.
        packed = socket.inet_aton(host)
    except OSError:
        return host == "localhost" or host.endswith(".localhost")
    return packed[0] == 127 or packed == bytes(4)


def is_transfer_destination(value: str) -> bool:
    if value.lower().startswith(("sip:", "sips:")):
        return is_sip_uri_shape(value) and not is_loopback_sip_uri(value)
    return is_dial_number(value) and not is_platform_ai_number(value)
