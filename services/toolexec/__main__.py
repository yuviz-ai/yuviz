from __future__ import annotations

import logging
import os

import uvicorn

from .app import quiet_http_loggers


def configure_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    quiet_http_loggers()


def main() -> None:
    configure_logging()
    port = int(os.environ.get("PORT", "8600"))  # 8400 is campaigns, 8750 is telephony
    uvicorn.run("services.toolexec.app:app", host=os.environ.get("LISTEN_HOST", "127.0.0.1"), port=port, log_level="info")


if __name__ == "__main__":
    main()
