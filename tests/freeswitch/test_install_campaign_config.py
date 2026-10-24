"""install_campaign_config.sh registers the Path A hooks and the 3pcc param, once, in valid XML."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/freeswitch/install_campaign_config.sh"

LUA_CONF = """<configuration name="lua.conf" description="LUA Configuration">
  <settings>
    <!--<hook event="CUSTOM" subclass="conference::maintenance" script="catch-event.lua"/>-->
  </settings>
</configuration>
"""
EXTERNAL = """<profile name="external">
  <settings>
    <param name="sip-port" value="$${external_sip_port}"/>
    <!--<param name="enable-3pcc" value="true"/>-->
  </settings>
</profile>
"""


def _install(conf: Path) -> subprocess.CompletedProcess:
    return subprocess.run([str(SCRIPT), str(conf)], capture_output=True, text=True)


@pytest.fixture
def conf(tmp_path):
    (tmp_path / "autoload_configs").mkdir()
    (tmp_path / "sip_profiles").mkdir()
    (tmp_path / "autoload_configs/lua.conf.xml").write_text(LUA_CONF)
    (tmp_path / "sip_profiles/external.xml").write_text(EXTERNAL)
    return tmp_path


def _lint(path: Path) -> None:
    if shutil.which("xmllint"):
        proc = subprocess.run(["xmllint", "--noout", str(path)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


def test_registers_both_hooks_and_3pcc_proxy_in_valid_xml(conf):
    assert _install(conf).returncode == 0
    lua = (conf / "autoload_configs/lua.conf.xml").read_text()
    external = (conf / "sip_profiles/external.xml").read_text()
    assert lua.count('<hook event="CHANNEL_ANSWER" script="campaign_event.lua"/>') == 1
    assert lua.count('<hook event="CHANNEL_HANGUP_COMPLETE" script="campaign_event.lua"/>') == 1
    assert external.count('<param name="enable-3pcc" value="proxy"/>') == 1
    assert "catch-event.lua" in lua
    _lint(conf / "autoload_configs/lua.conf.xml")
    _lint(conf / "sip_profiles/external.xml")


def test_a_second_run_changes_nothing(conf):
    _install(conf)
    before = {p.name: p.read_text() for p in conf.rglob("*.xml")}
    proc = _install(conf)
    assert proc.returncode == 0 and proc.stdout == ""
    assert {p.name: p.read_text() for p in conf.rglob("*.xml")} == before


def test_an_existing_3pcc_value_is_replaced_not_duplicated(conf):
    path = conf / "sip_profiles/external.xml"
    path.write_text(EXTERNAL.replace("<!--<param", "<param").replace('"true"/>-->', '"true"/>'))
    assert _install(conf).returncode == 0
    text = path.read_text()
    assert text.count("enable-3pcc") == 1 and 'value="proxy"' in text
    _lint(path)


def test_a_config_without_a_single_settings_end_is_refused(conf):
    (conf / "autoload_configs/lua.conf.xml").write_text("<configuration/>\n")
    proc = _install(conf)
    assert proc.returncode == 1 and "</settings>" in proc.stderr
