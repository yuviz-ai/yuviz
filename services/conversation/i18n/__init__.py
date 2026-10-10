"""Spoken system strings (fillers, goodbyes, fallbacks) per language.

One module per language exposing STRINGS (key -> text) and TOOL_FILLERS
((phrase, approx spoken seconds), ...). Missing languages or keys fall back to
English. Agent-level overrides (farewell_message, transfer_announcement) are
applied by the caller and always win over these.
"""

from __future__ import annotations

from types import ModuleType

from libs.config_sdk.languages import normalize_language

from . import en, hi

_TABLES: dict[str, ModuleType] = {"en": en, "hi": hi}


def t(key: str, language: str | None) -> str:
    """The string for key in language, else English. Raises KeyError for an unknown key."""
    table = _TABLES.get(normalize_language(language) or "en")
    if table is not None and key in table.STRINGS:
        return table.STRINGS[key]
    return en.STRINGS[key]


def tool_fillers(language: str | None) -> tuple[tuple[str, float], ...]:
    table = _TABLES.get(normalize_language(language) or "en")
    return table.TOOL_FILLERS if table is not None and table.TOOL_FILLERS else en.TOOL_FILLERS
