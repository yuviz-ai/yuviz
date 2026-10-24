"""scripts/update_kamailio_ip.sh follows SIP_IP without sudo, and never lands on
0.0.0.0. Runs the real script against a scratch copy of the repo, with no
FreeSWITCH, MySQL or Kamailio of its own, so the machine's stack is untouched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel in ("scripts/update_kamailio_ip.sh", "scripts/lib/env.sh", "scripts/lib/sip.sh",
                "scripts/kamailio/kamailio.cfg.tpl", "scripts/kamailio/dispatcher.list.tpl"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, root / rel)
    (root / ".env").write_text("KAMAILIO_DB_URL=mysql://k:p@localhost/kamailio\nFREESWITCH_ESL_PASSWORD=x\n")
    return root


def _sync(repo: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in os.environ.items() if k not in ("SIP_IP", "SIP_PROXY_HOST")}
    home = repo.parent / "home"
    clean.update(HOME=str(home), KAMAILIO_DIR=str(home / "kamailio"), KAMAILIO_LEGACY_CFG="",
                 YUVIZ_LOGS=str(home / "logs"), FS_PREFIX=str(home / "no-freeswitch"),
                 FS_HOME=str(home / "no-freeswitch"), MYSQL="/nonexistent/mysql")
    clean.update(env)
    return subprocess.run(["bash", str(repo / "scripts/update_kamailio_ip.sh"), *args],
                          env=clean, capture_output=True, text=True, timeout=60, check=False)


def _rendered(repo: Path) -> str:
    return (repo.parent / "home/kamailio/kamailio.cfg").read_text()


def _env(repo: Path) -> list[str]:
    return (repo / ".env").read_text().splitlines()


def test_default_is_loopback(repo):
    proc = _sync(repo)
    assert proc.returncode == 0, proc.stderr
    cfg = _rendered(repo)
    assert "listen=udp:127.0.0.1:5060" in cfg
    assert "__" not in "".join(l for l in cfg.splitlines() if "__LAN_IP__" in l or "__KAMAILIO" in l)
    assert f'"{repo.parent}/home/kamailio/dispatcher.list"' in cfg
    assert "SIP_PROXY_HOST=127.0.0.1" in _env(repo)


def test_fixed_address_from_env_file_reaches_every_target(repo):
    (repo / ".env").write_text((repo / ".env").read_text() + "SIP_IP=10.9.8.7\n")
    proc = _sync(repo)
    assert proc.returncode == 0, proc.stderr
    assert "listen=udp:10.9.8.7:5060" in _rendered(repo)
    assert (repo.parent / "home/kamailio/dispatcher.list").read_text().startswith("1 sip:10.9.8.7:5080")
    assert "SIP_PROXY_HOST=10.9.8.7" in _env(repo)
    again = _sync(repo)
    assert "config already for 10.9.8.7" in again.stdout
    assert _sync(repo, "--if-changed").stdout == ""


@pytest.mark.parametrize("bad", ["0.0.0.0", "10.0.0.1; rm -rf /", "example.com", "1.2.3"])
def test_refuses_anything_but_one_ipv4_interface(repo, bad):
    proc = _sync(repo, SIP_IP=bad)
    assert proc.returncode == 1
    assert "SIP_IP must be" in proc.stderr
    assert not (repo.parent / "home/kamailio/kamailio.cfg").exists()


def test_auto_follows_the_lan_address(repo):
    proc = _sync(repo, SIP_IP="auto")
    if "no usable address" in proc.stderr or "VPN interface" in proc.stderr:
        pytest.skip("offline or VPN default route")
    assert proc.returncode == 0, proc.stderr
    ip = next(l.split("=", 1)[1] for l in _env(repo) if l.startswith("SIP_PROXY_HOST="))
    assert ip not in ("127.0.0.1", "0.0.0.0")
    assert f"listen=udp:{ip}:5060" in _rendered(repo)


def test_auto_keeps_applied_ip_when_egress_is_vpn(repo):
    assert _sync(repo, SIP_IP="10.9.8.7").returncode == 0
    proc = _sync(repo, SIP_IP="auto", YUVIZ_TEST_EGRESS_IFACE="utun3")
    assert proc.returncode == 0, proc.stderr
    assert "VPN interface utun3" in proc.stderr
    assert "keeping SIP on 10.9.8.7" in proc.stderr
    assert "listen=udp:10.9.8.7:5060" in _rendered(repo)
    # --if-changed must not restart onto the tunnel address either.
    again = _sync(repo, "--if-changed", SIP_IP="auto", YUVIZ_TEST_EGRESS_IFACE="utun3")
    assert again.returncode == 0
    assert again.stdout == ""
    assert "listen=udp:10.9.8.7:5060" in _rendered(repo)


def test_auto_refuses_vpn_when_nothing_applied_yet(repo):
    proc = _sync(repo, SIP_IP="auto", YUVIZ_TEST_EGRESS_IFACE="ppp0")
    assert proc.returncode == 1
    assert "VPN interface ppp0" in proc.stderr
    assert not (repo.parent / "home/kamailio/kamailio.cfg").exists()
    # launchd path: stay quiet until a non-VPN route appears.
    quiet = _sync(repo, "--if-changed", SIP_IP="auto", YUVIZ_TEST_EGRESS_IFACE="ipsec0")
    assert quiet.returncode == 0
    assert quiet.stdout == ""


def test_if_changed_repairs_literal_auto_in_vars_xml(repo):
    home = repo.parent / "home"
    fs = home / "fake-fs"
    (fs / "bin").mkdir(parents=True)
    (fs / "bin" / "freeswitch").write_text("#!/bin/sh\n")
    (fs / "bin" / "freeswitch").chmod(0o755)
    conf = home / "fs-home" / "conf"
    conf.mkdir(parents=True)
    (conf / "vars.xml").write_text(
        '<include>\n  <X-PRE-PROCESS cmd="set" data="local_ip_v4=auto"/>\n</include>\n')
    assert _sync(repo, SIP_IP="10.9.8.7", FS_PREFIX=str(fs), FS_HOME=str(home / "fs-home")).returncode == 0
    # Stale pin after a setup_macos.sh that wrote the literal "auto".
    (conf / "vars.xml").write_text(
        '<include>\n  <X-PRE-PROCESS cmd="set" data="local_ip_v4=auto"/>\n</include>\n')
    proc = _sync(repo, "--if-changed", SIP_IP="10.9.8.7",
                 FS_PREFIX=str(fs), FS_HOME=str(home / "fs-home"))
    assert proc.returncode == 0, proc.stderr
    assert 'data="local_ip_v4=10.9.8.7"' in (conf / "vars.xml").read_text()
    assert "local_ip_v4 pinned to 10.9.8.7" in proc.stdout


def test_setup_macos_resolves_sip_ip_before_pinning():
    text = (REPO / "scripts/freeswitch/setup_macos.sh").read_text()
    assert 'data="local_ip_v4=$SIP_IP"' not in text
    assert "_sip_target_ip" in text
    assert "PIN_IP" in text


def test_a_shell_that_only_mentions_a_service_is_never_restarted(repo):
    # Seen live: an unanchored match killed the operator's own shell.
    decoy = subprocess.Popen(
        ["bash", "-c", "sleep 60; true # build/gateway/voice_ai_gateway services.campaigns.app kamailio -f x"],
        cwd=repo)
    try:
        proc = _sync(repo, SIP_IP="10.9.8.7")
        assert proc.returncode == 0, proc.stderr
        assert "restarted" not in proc.stdout
        assert decoy.poll() is None
    finally:
        decoy.kill()


def test_template_listens_only_on_the_rendered_address():
    listens = [l for l in (REPO / "scripts/kamailio/kamailio.cfg.tpl").read_text().splitlines()
               if l.startswith("listen=")]
    # The campaign dial handler listens on loopback only; nothing else may be public.
    assert listens == [
        "listen=udp:__LAN_IP__:5060",
        "listen=tcp:127.0.0.1:5071",
        "listen=tcp:[::1]:5071",
    ]


def test_scripts_never_escalate():
    for rel in ("scripts/update_kamailio_ip.sh", "scripts/lib/sip.sh"):
        assert "sudo" not in (REPO / rel).read_text(), rel


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd is macOS only")
def test_network_sync_agent_is_a_valid_plist(tmp_path):
    out = subprocess.run(["bash", "-c", f'REPO="{REPO}"; . "{REPO}/scripts/lib/sip.sh"; _network_sync_plist'],
                         env={**os.environ, "HOME": str(tmp_path)}, capture_output=True, text=True, check=True).stdout
    lint = subprocess.run(["plutil", "-lint", "-"], input=out, capture_output=True, text=True)
    assert lint.returncode == 0, lint.stdout
    assert f"{REPO}/scripts/update_kamailio_ip.sh" in out and "--if-changed" in out
    assert "/var/run/resolv.conf" in out and "<key>AbandonProcessGroup</key><true/>" in out
    # Seen live: without it, FreeSWITCH restarted by the agent ran at nice 19 and audio crawled.
    assert "<key>ProcessType</key><string>Interactive</string>" in out
