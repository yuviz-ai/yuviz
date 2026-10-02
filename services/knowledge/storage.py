"""StorageProvider — where document bytes live; kb_documents.source_ref is an opaque pointer, never raw bytes."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Protocol


class StorageProvider(Protocol):
    async def save(self, tenant_id: str, kb_id: str, filename: str, content: bytes) -> str:
        """Return an opaque source_ref; never parse or construct it elsewhere."""
        ...

    async def read(self, source_ref: str) -> bytes: ...

    async def delete(self, source_ref: str) -> None: ...


class LocalStorageProvider:
    """Local filesystem storage at {root}/{tenant_id}/{kb_id}/{uuid}-{filename} (single-node only)."""

    def __init__(self, root: str | None = None) -> None:
        self._root = Path(root or os.environ.get("KNOWLEDGE_STORAGE_ROOT", "./data/knowledge_documents"))

    async def save(self, tenant_id: str, kb_id: str, filename: str, content: bytes) -> str:
        directory = self._root / tenant_id / kb_id
        directory.mkdir(parents=True, exist_ok=True)
        safe_name = f"{uuid.uuid4()}-{Path(filename).name}"
        path = directory / safe_name
        path.write_bytes(content)
        return str(path)

    async def read(self, source_ref: str) -> bytes:
        return Path(source_ref).read_bytes()

    async def delete(self, source_ref: str) -> None:
        Path(source_ref).unlink(missing_ok=True)
