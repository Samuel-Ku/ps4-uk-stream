from __future__ import annotations

from .base import BaseProvider, ProviderError

PROVIDERS: dict[str, BaseProvider] = {}


def register(provider: BaseProvider) -> None:
    PROVIDERS[provider.id] = provider


__all__ = ["PROVIDERS", "BaseProvider", "ProviderError", "register"]
