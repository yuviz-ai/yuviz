"""Native services listen on loopback unless .env says otherwise.

Config, Conversation, the Gateway's audio WebSocket and the rest trust their
callers, so a LAN-wide bind lets any peer on the network act as any tenant.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[1]
_LAUNCHERS = ["scripts/start_local.sh", "scripts/start_web_test.sh"]
_ENTRY_POINTS = sorted(
    p for p in (_REPO / "services").glob("*/*.py") if p.name in {"__main__.py", "app.py"}
)


def test_launchers_never_bind_all_interfaces():
    for rel in _LAUNCHERS:
        text = (_REPO / rel).read_text()
        assert "0.0.0.0" not in text, f"{rel} binds 0.0.0.0; use \"$LISTEN_HOST\""
        hosts = re.findall(r"--host (\S+)", text)
        assert hosts and all(h == '"$LISTEN_HOST"' for h in hosts), f"{rel}: {hosts}"


def test_service_entry_points_default_to_loopback():
    assert _ENTRY_POINTS, "found no service entry points"
    offenders = [str(p.relative_to(_REPO)) for p in _ENTRY_POINTS
                 if re.search(r"""["'](0\.0\.0\.0|\[::\])""", p.read_text())]
    assert not offenders, f"hardcoded all-interfaces bind: {offenders}"
    readers = [p for p in _ENTRY_POINTS if 'os.environ.get("LISTEN_HOST", "127.0.0.1")' in p.read_text()]
    assert len(readers) >= 4, "conversation, telephony, toolexec and webcall must read LISTEN_HOST"


def test_gateway_websocket_defaults_to_loopback():
    ws = yaml.safe_load((_REPO / "config/gateway.yaml").read_text())["gateway"]["websocket"]
    assert "host" not in ws, "the bind comes from GATEWAY_LISTEN_HOST in .env, not gateway.yaml"
    header = (_REPO / "gateway/include/config/Config.h").read_text()
    assert 'std::string host{"127.0.0.1"};   // GATEWAY_LISTEN_HOST' in header


def test_every_envoy_listener_binds_loopback():
    listeners = yaml.safe_load((_REPO / "config/envoy.yaml").read_text())["static_resources"]["listeners"]
    assert listeners
    exposed = {l["name"]: l["address"]["socket_address"]["address"] for l in listeners
               if l["address"]["socket_address"]["address"] not in {"127.0.0.1", "::1"}}
    assert not exposed, f"Envoy listeners must bind loopback: {exposed}"


def test_containers_listening_widely_publish_only_to_loopback_by_default():
    services = yaml.safe_load((_REPO / "deployment/docker/docker-compose.yml").read_text())["services"]
    wide = [name for name, svc in services.items()
            if (svc.get("environment") or {}).get("LISTEN_HOST") == "0.0.0.0"]
    assert wide, "no container sets LISTEN_HOST"
    for name in wide:
        for port in services[name].get("ports", []):
            assert re.match(r"\$\{[A-Z_]*BIND_ADDR:-127\.0\.0\.1\}:", str(port)), f"{name}: {port}"


_KAMAILIO_TPL = (_REPO / "scripts/kamailio/kamailio.cfg.tpl").read_text()


def _dial_branch() -> str:
    start = _KAMAILIO_TPL.index("\tif ($Rp == 5071) {\n", _KAMAILIO_TPL.index("event_route[xhttp:request]"))
    depth, i = 0, _KAMAILIO_TPL.index("{", start)
    for end in range(i, len(_KAMAILIO_TPL)):
        depth += {"{": 1, "}": -1}.get(_KAMAILIO_TPL[end], 0)
        if depth == 0:
            return _KAMAILIO_TPL[i + 1:end]
    raise AssertionError("unbalanced dial branch")


def test_kamailio_dial_listens_are_exactly_the_two_loopback_lines():
    listens = re.findall(r"^listen=(\S+)$", _KAMAILIO_TPL, re.M)
    assert [l for l in listens if l.endswith(":5071")] == ["tcp:127.0.0.1:5071", "tcp:[::1]:5071"]
    assert listens[0] == "udp:__LAN_IP__:5060" and len(listens) == 3
    assert not [l for l in listens if l.endswith(":5071") and ("0.0.0.0" in l or "__LAN_IP__" in l)]


def test_kamailio_drops_sip_on_the_dial_socket_before_anything_else():
    body = _KAMAILIO_TPL.split("request_route {\n", 1)[1]
    assert re.match(r"\n#!ifdef WITH_CAMPAIGN_DIAL\n    if \(\$Rp == 5071\) \{\n        drop;\n    \}\n#!endif\n", body)


def test_every_kamailio_dial_path_answers_and_exits_and_never_relays():
    branch = _dial_branch()
    replies = re.findall(r"xhttp_reply\([^;]*\);\n\s*(\S+)", branch)
    assert len(replies) >= 12, "found too few xhttp_reply calls"
    assert set(replies) == {"exit;"}, "an xhttp_reply that does not exit"
    assert branch.rstrip().endswith("exit;")
    assert not re.search(r"t_relay|jsonrpc|route\(|sl_send_reply|ds_is_from", branch)
    assert branch.index('"CAMPAIGNS_DIAL_HMAC"') < branch.index("ds_select_dst") < branch.index("uac_req_send")
