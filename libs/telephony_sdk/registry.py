"""
TelephonyProviderRegistry — name -> ITelephonyProvider class. Each provider
module (providers/vobiz.py, and later providers/twilio.py etc.) registers
itself on import; adding a new provider is a new file + one import line in
providers/__init__.py, no edits anywhere else (same "additive, not invasive"
shape as libs/knowledge_sdk's provider registration).

`hidden=True` registers a provider that resolves by name via get() but is
absent from visible() — used by "fake", which exists only for tests and
must never appear in the Admin UI's provider list (AC4).
"""

from __future__ import annotations

from .interface import ISmsProvider, ITelephonyProvider


class TelephonyProviderRegistry:
    _providers: dict[str, type[ITelephonyProvider]] = {}
    _hidden: set[str] = set()

    @classmethod
    def register(cls, name: str, provider_cls: type[ITelephonyProvider], *, hidden: bool = False) -> None:
        cls._providers[name] = provider_cls
        if hidden:
            cls._hidden.add(name)
        else:
            cls._hidden.discard(name)

    @classmethod
    def get(cls, name: str) -> type[ITelephonyProvider]:
        try:
            return cls._providers[name]
        except KeyError:
            raise ValueError(f"Unknown telephony provider: {name!r}") from None

    @classmethod
    def all(cls) -> dict[str, type[ITelephonyProvider]]:
        return dict(cls._providers)

    @classmethod
    def visible(cls) -> dict[str, type[ITelephonyProvider]]:
        return {name: provider_cls for name, provider_cls in cls._providers.items() if name not in cls._hidden}


class SmsProviderRegistry:
    _providers: dict[str, type[ISmsProvider]] = {}
    _hidden: set[str] = set()

    @classmethod
    def register(cls, name: str, provider_cls: type[ISmsProvider], *, hidden: bool = False) -> None:
        cls._providers[name] = provider_cls
        if hidden:
            cls._hidden.add(name)
        else:
            cls._hidden.discard(name)

    @classmethod
    def get(cls, name: str) -> type[ISmsProvider]:
        try:
            return cls._providers[name]
        except KeyError:
            raise ValueError(f"Unknown SMS provider: {name!r}") from None

    @classmethod
    def all(cls) -> dict[str, type[ISmsProvider]]:
        return dict(cls._providers)

    @classmethod
    def visible(cls) -> dict[str, type[ISmsProvider]]:
        return {name: provider_cls for name, provider_cls in cls._providers.items() if name not in cls._hidden}
