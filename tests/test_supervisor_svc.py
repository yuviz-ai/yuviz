import os
import subprocess
from pathlib import Path

import pytest

SVC = Path(__file__).resolve().parents[1] / "deployment" / "svc"


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "supervisorctl").write_text('#!/bin/sh\necho "supervisorctl $*"\n')

    def _run(*args, open_calls="0"):
        psql = "#!/bin/sh\nexit 2\n" if open_calls is None else f"#!/bin/sh\necho {open_calls}\n"
        (bin_dir / "psql").write_text(psql)
        for f in bin_dir.iterdir():
            f.chmod(0o755)
        env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", "FS_PREFIX": str(tmp_path / "no-freeswitch")}
        return subprocess.run([str(SVC), *args], env=env, capture_output=True, text=True, timeout=30)

    return _run


def test_restart_of_a_call_path_service_is_refused_while_calls_are_live(run):
    r = run("restart", "gateway", open_calls="2")
    assert r.returncode == 1 and "Refusing to restart" in r.stderr and "supervisorctl" not in r.stdout


def test_it_goes_ahead_when_no_calls_are_live(run):
    r = run("restart", "gateway", open_calls="0")
    assert r.returncode == 0 and r.stdout.strip().endswith("restart gateway")


def test_services_off_the_call_path_are_not_guarded(run):
    assert run("restart", "toolexec", open_calls="2").returncode == 0
    assert run("status", open_calls=None).returncode == 0


@pytest.mark.parametrize("args", [("restart", "all"), ("stop", "conversation-1"), ("update",), ("stop", "voice:gateway")])
def test_every_way_of_hitting_the_call_path_is_guarded(run, args):
    assert run(*args, open_calls="1").returncode == 1


def test_force_overrides_and_is_not_passed_on(run):
    r = run("restart", "gateway", "--force", open_calls="3")
    assert r.returncode == 0 and "--force" not in r.stdout


def test_unknown_call_count_refuses(run):
    r = run("restart", "gateway", open_calls=None)
    assert r.returncode == 1 and "could not count live calls" in r.stderr
