from __future__ import annotations

import logging
import os

import uvicorn


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    port = int(os.environ.get("PORT", "8600"))  # 8500 is vobiz, 8400 is campaigns
    uvicorn.run("services.toolexec.app:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
