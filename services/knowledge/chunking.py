"""Paragraph-first chunking into ~chunk_size-word windows with chunk_overlap words of overlap.
Word count is the token-count proxy throughout."""

from __future__ import annotations

import re
from dataclasses import dataclass

_PARAGRAPH_RE = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class Chunk:
    content: str
    token_count: int


def chunk_text(text: str, chunk_size: int = 200, chunk_overlap: int = 40) -> list[Chunk]:
    paragraphs = [p.strip() for p in _PARAGRAPH_RE.split(text) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[Chunk] = []
    current_words: list[str] = []

    for paragraph in paragraphs:
        para_words = paragraph.split()
        if current_words and len(current_words) + len(para_words) > chunk_size:
            chunks.append(Chunk(content=" ".join(current_words), token_count=len(current_words)))
            overlap_words = current_words[-chunk_overlap:] if chunk_overlap else []
            current_words = overlap_words + para_words
        else:
            current_words.extend(para_words)

        # A single paragraph longer than chunk_size on its own still needs
        # splitting, or it would produce one oversized chunk forever.
        while len(current_words) > chunk_size:
            head, current_words = current_words[:chunk_size], current_words[chunk_size:]
            chunks.append(Chunk(content=" ".join(head), token_count=len(head)))
            if chunk_overlap:
                current_words = head[-chunk_overlap:] + current_words

    if current_words:
        chunks.append(Chunk(content=" ".join(current_words), token_count=len(current_words)))

    return chunks
