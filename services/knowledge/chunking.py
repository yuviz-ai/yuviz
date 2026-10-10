"""Paragraph-first chunking into ~chunk_size-word windows with chunk_overlap words of overlap.
Word count is the token-count proxy throughout.

Languages written without spaces between words (Chinese, Japanese, Thai, Lao, Khmer,
Myanmar) have no words to count, so they are sized by characters instead: token
counts are non-space characters, and windows are _UNSPACED_CHARS_PER_WORD x larger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from libs.config_sdk.languages import normalize_language

_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_WHITESPACE_RE = re.compile(r"\s+")

# ISO 639-1 codes of languages written without inter-word spaces.
_UNSPACED_LANGUAGES = frozenset({"zh", "ja", "th", "lo", "km", "my"})
# Han, Hiragana/Katakana, Thai, Lao, Myanmar, Khmer, CJK punctuation, full-width forms.
_UNSPACED_SCRIPT_RE = re.compile(
    r"[一-鿿㐀-䶿぀-ヿ฀-๿຀-໿"
    r"က-႟ក-៿　-〿＀-￯]"
)
# Text is treated as unspaced when more than this share of its non-space chars are.
_UNSPACED_SHARE = 0.30
# Roughly one word's worth of characters in unspaced scripts (token-count proxy).
_UNSPACED_CHARS_PER_WORD = 1.5


@dataclass(frozen=True)
class Chunk:
    content: str
    token_count: int


def is_unspaced(text: str, language: str | None = None) -> bool:
    """The document's language decides when known; otherwise sniff the script."""
    lang = normalize_language(language)
    if lang is not None:
        return lang in _UNSPACED_LANGUAGES
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return False
    return sum(1 for c in chars if _UNSPACED_SCRIPT_RE.match(c)) / len(chars) > _UNSPACED_SHARE


def count_tokens(text: str, language: str | None = None) -> int:
    """Token-count proxy: words, or non-space characters for unspaced scripts."""
    if is_unspaced(text, language):
        return sum(1 for c in text if not c.isspace())
    return len(text.split())


def chunk_text(
    text: str, chunk_size: int = 200, chunk_overlap: int = 40, language: str | None = None,
) -> list[Chunk]:
    paragraphs = [p.strip() for p in _PARAGRAPH_RE.split(text) if p.strip()]
    if not paragraphs:
        return []
    if is_unspaced(text, language):
        # Whitespace is kept (collapsed): Thai and Lao use spaces between phrases.
        return _chunk_units(
            [list(_WHITESPACE_RE.sub(" ", p)) for p in paragraphs],
            int(chunk_size * _UNSPACED_CHARS_PER_WORD), int(chunk_overlap * _UNSPACED_CHARS_PER_WORD),
            joiner="", count=lambda units: sum(1 for c in units if not c.isspace()),
        )
    return _chunk_units([p.split() for p in paragraphs], chunk_size, chunk_overlap, joiner=" ", count=len)


def _chunk_units(
    paragraphs: list[list[str]], chunk_size: int, chunk_overlap: int, joiner: str,
    count: Callable[[list[str]], int],
) -> list[Chunk]:
    def emit(units: list[str]) -> None:
        content = joiner.join(units).strip()
        if content:
            chunks.append(Chunk(content=content, token_count=count(units)))

    chunks: list[Chunk] = []
    current: list[str] = []

    for units in paragraphs:
        if current and len(current) + len(units) > chunk_size:
            emit(current)
            overlap = current[-chunk_overlap:] if chunk_overlap else []
            current = overlap + units
        else:
            current.extend(units)

        # A single paragraph longer than chunk_size on its own still needs
        # splitting, or it would produce one oversized chunk forever.
        while len(current) > chunk_size:
            head, current = current[:chunk_size], current[chunk_size:]
            emit(head)
            if chunk_overlap:
                current = head[-chunk_overlap:] + current

    if current:
        emit(current)

    return chunks
