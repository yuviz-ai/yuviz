"""
httpx logs every request URL at INFO. Executor and provider URLs carry path
values (event_id, spreadsheetId) and the CRM lookups' caller number, so merely
importing the app must quiet the httpx/httpcore loggers, whatever launched it.
A fresh interpreter, so nothing earlier in this process has set them, with the
root logger at INFO as a deployment would have it (at the default WARNING the
assertion could not fail).
"""

from __future__ import annotations

import subprocess
import sys

PROBE = (
    "import logging\n"
    "logging.basicConfig(level=logging.INFO)\n"
    "import services.toolexec.app\n"
    "print(*(logging.getLogger(n).getEffectiveLevel() for n in ('httpx', 'httpcore')))\n"
)


def test_importing_the_app_quiets_the_http_loggers():
    out = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, check=True).stdout
    assert [int(level) for level in out.split()] == [30, 30]
