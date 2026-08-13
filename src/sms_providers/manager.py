"""Registry and factory for :class:`~sms_providers.base.BaseSmsProvider` instances."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from typing import Any, ClassVar, TypeVar

from .base import BaseSmsProvider, ProviderNotRegistered

P = TypeVar("P", bound=BaseSmsProvider)

__all__ = ["SmsProviderManager"]


class SmsProviderManager:
    """Registers and looks up SMS providers, by instance and by class.

    Two independent registries are kept: a class-level registry (used by
    :meth:`register`/:meth:`from_config` to know what provider types exist),
    and a per-instance registry of live, configured providers.
    """

    _classes: ClassVar[dict[str, type[BaseSmsProvider]]] = {}

    def __init__(self, providers: Mapping[str, BaseSmsProvider] | None = None) -> None:
        self._providers: dict[str, BaseSmsProvider] = {}
        if providers:
            for name, provider in providers.items():
                self.register_provider(name, provider)

    # --- instances ---

    def register_provider(
        self, name: str, provider: BaseSmsProvider, *, replace: bool = False
    ) -> BaseSmsProvider:
        if not isinstance(provider, BaseSmsProvider):
            raise TypeError(f"provider must be a BaseSmsProvider instance, got {type(provider)!r}")
        if name in self._providers and not replace:
            raise ValueError(f"provider {name!r} is already registered; pass replace=True")
        self._providers[name] = provider
        return provider

    def unregister_provider(self, name: str) -> None:
        self._providers.pop(name, None)

    def get(self, name: str) -> BaseSmsProvider:
        try:
            return self._providers[name]
        except KeyError:
            raise ProviderNotRegistered(f"provider {name!r} is not registered") from None

    def __getitem__(self, name: str) -> BaseSmsProvider:
        return self.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._providers

    def __iter__(self) -> Iterator[str]:
        return iter(self._providers)

    def __len__(self) -> int:
        return len(self._providers)

    def names(self) -> list[str]:
        return list(self._providers)

    async def aclose_all(self) -> None:
        names = list(self._providers)
        results = await asyncio.gather(
            *(self._providers[name].aclose() for name in names), return_exceptions=True
        )
        errors = [
            (name, result)
            for name, result in zip(names, results, strict=True)
            if isinstance(result, Exception)
        ]
        if errors:
            failed_names = ", ".join(name for name, _ in errors)
            raise RuntimeError(f"failed to close provider(s): {failed_names}") from errors[0][1]

    # --- classes ---

    @classmethod
    def register(cls, name: str | None = None) -> Callable[[type[P]], type[P]]:
        """Class decorator registering a provider class for use with :meth:`from_config`."""

        def decorator(provider_cls: type[P]) -> type[P]:
            registered_name = name if name is not None else provider_cls.name
            cls._classes[registered_name] = provider_cls
            return provider_cls

        return decorator

    @classmethod
    def registered_classes(cls) -> dict[str, type[BaseSmsProvider]]:
        return dict(cls._classes)

    @classmethod
    def provider_class(cls, name: str) -> type[BaseSmsProvider]:
        try:
            return cls._classes[name]
        except KeyError:
            available = ", ".join(sorted(cls._classes)) or "<none>"
            raise ProviderNotRegistered(
                f"provider class {name!r} is not registered; available: {available}"
            ) from None

    # --- config ---

    @classmethod
    def from_config(cls, config: Mapping[str, Mapping[str, Any]]) -> SmsProviderManager:
        manager = cls()
        for key, params in config.items():
            params = dict(params)
            provider_name = params.pop("provider", key)
            provider_cls = cls.provider_class(provider_name)
            try:
                provider = provider_cls(**params)
            except TypeError as exc:
                raise ValueError(f"invalid config for {key!r} ({provider_name!r}): {exc}") from exc
            manager.register_provider(key, provider)
        return manager
