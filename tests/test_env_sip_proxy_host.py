"""SIP_PROXY_HOST stays blank until update_kamailio_ip.sh writes Kamailio's IP;
a shipped default would defeat the Gateway's sip_proxy_host_unset refusal.
Drives scripts/lib/env.sh in bash and zsh against a scratch .env.example.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENV_SH = REPO / "scripts" / "lib" / "env.sh"
SHELLS = [s for s in ("bash", "zsh") if shutil.which(s)]


def _proc(shell: str, repo: Path, script: str, **env: str) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in os.environ.items() if k != "SIP_PROXY_HOST"}
    clean.update(env)
    clean["REPO"] = str(repo)
    proc = subprocess.run(
        [shell, "-c", f'source "{ENV_SH}"; {script}'],
        env=clean, capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc


def _run(shell: str, repo: Path, script: str, **env: str) -> str:
    return _proc(shell, repo, script, **env).stdout


@pytest.fixture
def scratch_repo(tmp_path: Path) -> Path:
    shutil.copy(REPO / ".env.example", tmp_path / ".env.example")
    return tmp_path


def test_shipped_env_example_leaves_sip_proxy_host_blank():
    lines = [l for l in (REPO / ".env.example").read_text().splitlines() if l.startswith("SIP_PROXY_HOST=")]
    assert lines == ["SIP_PROXY_HOST="]


@pytest.mark.parametrize("shell", SHELLS)
def test_env_init_then_load_env_does_not_export_a_sip_proxy_host(shell, scratch_repo):
    out = _run(shell, scratch_repo,
               '_env_init >/dev/null; _load_env; printf "%s" "${SIP_PROXY_HOST-<unset>}"')
    assert out == "<unset>"


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("initial", [
    "A=1\nSIP_PROXY_HOST=127.0.0.1\nB=2\n",   # present (an old .env)
    "A=1\nSIP_PROXY_HOST=\nB=2\n",            # present, blank
    "A=1\nB=2\n",                             # missing
    "A=1\nB=2",                               # missing, no trailing newline
    "",                                       # empty file
])
def test_env_put_adds_or_replaces_and_keeps_other_lines(shell, scratch_repo, initial):
    (scratch_repo / ".env").write_text(initial)
    out = _run(shell, scratch_repo, '_env_put SIP_PROXY_HOST 10.1.2.3 && _env_get SIP_PROXY_HOST')
    assert out == "10.1.2.3\n"
    lines = (scratch_repo / ".env").read_text().splitlines()
    assert lines.count("SIP_PROXY_HOST=10.1.2.3") == 1
    assert [l for l in lines if l.startswith("SIP_PROXY_HOST=")] == ["SIP_PROXY_HOST=10.1.2.3"]
    for kept in ("A=1", "B=2"):
        if kept in initial:
            assert kept in lines


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("present", [True, False])
def test_env_put_writes_special_characters_literally(shell, scratch_repo, present):
    (scratch_repo / ".env").write_text("SIP_PROXY_HOST=old\n" if present else "A=1\n")
    value = r"a&b|c\d$e`f"
    _run(shell, scratch_repo, '_env_put SIP_PROXY_HOST "$V"', V=value)
    assert f"SIP_PROXY_HOST={value}" in (scratch_repo / ".env").read_text().splitlines()


# _load_env never replaces a variable the shell already exports, so a tab that
# sourced start_local.sh before update_kamailio_ip.sh rewrote .env keeps the
# old host. Whatever starts from it must say so instead of dialing it silently.
@pytest.mark.parametrize("shell", SHELLS)
def test_warn_env_drift_flags_a_stale_exported_sip_proxy_host(shell, scratch_repo):
    (scratch_repo / ".env").write_text("SIP_PROXY_HOST=10.1.2.3\n")
    proc = _proc(shell, scratch_repo, "_warn_env_drift SIP_PROXY_HOST", SIP_PROXY_HOST="127.0.0.1")
    assert "SIP_PROXY_HOST=127.0.0.1" in proc.stderr
    assert "SIP_PROXY_HOST=10.1.2.3" in proc.stderr
    assert "new tab" in proc.stderr and "unset SIP_PROXY_HOST" in proc.stderr


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("exported", [None, "10.1.2.3"])
def test_warn_env_drift_is_silent_when_the_shell_agrees_or_has_nothing(shell, scratch_repo, exported):
    (scratch_repo / ".env").write_text("SIP_PROXY_HOST=10.1.2.3\n")
    env = {"SIP_PROXY_HOST": exported} if exported else {}
    proc = _proc(shell, scratch_repo, "_warn_env_drift SIP_PROXY_HOST", **env)
    assert proc.stderr == "" and proc.stdout == ""


@pytest.mark.parametrize("shell", SHELLS)
def test_warn_env_drift_flags_an_export_when_env_is_blank(shell, scratch_repo):
    (scratch_repo / ".env").write_text("SIP_PROXY_HOST=\n")
    proc = _proc(shell, scratch_repo, "_warn_env_drift SIP_PROXY_HOST", SIP_PROXY_HOST="127.0.0.1")
    assert "WARNING" in proc.stderr


@pytest.mark.parametrize("path, func", [
    ("scripts/start_local.sh", "start_gateway"),
    ("scripts/start_local.sh", "start_campaigns_service"),
])
def test_launchers_check_for_a_stale_sip_proxy_host(path, func):
    text = (REPO / path).read_text()
    body = text[text.index(f"{func}() {{"):]
    body = body[: body.index("\n}\n")]
    assert "_warn_env_drift SIP_PROXY_HOST" in body


def test_update_kamailio_ip_warns_about_the_calling_shell():
    text = (REPO / "scripts" / "update_kamailio_ip.sh").read_text()
    assert text.index("_warn_env_drift SIP_PROXY_HOST") > text.index("_env_put SIP_PROXY_HOST")
