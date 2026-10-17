import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deployment" / "autostart.sh"


@pytest.mark.skipif(sys.platform != "darwin", reason="LaunchAgents are macOS only")
def test_launch_agent_is_interactive_keepalive_and_carries_a_path(tmp_path):
    (tmp_path / "supervisord").write_text("#!/bin/sh\n")
    (tmp_path / "supervisord").chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": str(tmp_path)}
    out = subprocess.run([str(SCRIPT), "print"], env=env, capture_output=True, check=True, timeout=30).stdout
    plist = plistlib.loads(out)
    assert plist["ProcessType"] == "Interactive"  # an untyped job is throttled
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["ProgramArguments"][0] == str(tmp_path / "supervisord")
    assert plist["ProgramArguments"][-1].endswith("deployment/supervisord.conf")
    assert "/venv/bin:" in plist["EnvironmentVariables"]["PATH"]
