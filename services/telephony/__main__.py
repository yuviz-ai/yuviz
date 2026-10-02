from __future__ import annotations

# Generated stubs import "voiceai.v1" absolutely; must run before anything imports them.
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.join(
    _os.path.dirname(__file__), "..", "conversation", "generated",
))

import logging
import os

import uvicorn


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    port = int(os.environ.get("PORT", "8750"))
    uvicorn.run("services.telephony.app:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
