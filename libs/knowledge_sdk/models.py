"""Knowledge SDK DTOs: the only shapes consumers see.

Separate from RuntimeConfig because retrieval is per-turn and query-dependent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RetrievalPolicy:
    """Per-call retrieval overrides. None = no override.

    Knowledge Service resolves: call override > agent's policy row > system default.
    """
    top_k: int | None = None
    max_tokens: int | None = None
    minimum_score: float | None = None
    rerank: bool | None = None          # not implemented yet
    hybrid_search: bool | None = None   # not implemented yet
    include_citations: bool | None = None


@dataclass(frozen=True)
class ChunkSource:
    """Provenance for one retrieved chunk."""
    kb_id: str
    kb_slug: str
    document_id: str
    document_title: str
    chunk_id: str
    page: int | None
    language: str | None
    tags: dict[str, Any] = field(default_factory=dict)
    version: int = 1


@dataclass(frozen=True)
class RetrievedChunk:
    content: str
    score: float  # cosine similarity, higher is more relevant
    source: ChunkSource


@dataclass(frozen=True)
class RetrievedContext:
    """Result of IKnowledgeProvider.retrieve()."""
    chunks: list[RetrievedChunk]
    sources: list[str]          # deduplicated document titles, for citation/logging
    confidence: float           # top chunk's score, 0.0 if chunks is empty
    latency_ms: float           # SDK-side wall-clock for this retrieve() call
    token_count: int            # sum of chunks' approximate token counts
    include_citations: bool = True  # resolved policy's citation setting
    retrieval_metadata: dict[str, Any] = field(default_factory=dict)
