"""
Ingestion worker process (separate from the API); polls pending kb_ingestion_jobs forever.

Run: python3 -m services.knowledge --log-level INFO
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import db
from .embedding_manager import EmbeddingProviderManager
from .ingestion_worker import run_loop
from .secret_resolver import CompositeSecretResolver
from .storage import LocalStorageProvider

log = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Knowledge Service ingestion worker")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


async def serve() -> None:
    storage = LocalStorageProvider()
    embedding_manager = EmbeddingProviderManager(CompositeSecretResolver())
    log.info("Ingestion worker started, polling kb_ingestion_jobs")
    try:
        await run_loop(storage, embedding_manager)
    finally:
        await db.close_pool()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(serve())


if __name__ == "__main__":
    main()
