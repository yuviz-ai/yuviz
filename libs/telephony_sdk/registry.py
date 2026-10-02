"""Name -> provider class registries; providers register themselves on import.

`hidden=True` resolves via get() but is absent from visible() (e.g. the test-only "fake").
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
