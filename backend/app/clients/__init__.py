"""Model clients behind the `LLMClient` interface."""

from __future__ import annotations

from app.clients.base import LLMClient
from app.config import Settings, get_settings


def get_client(settings: Settings | None = None) -> LLMClient:
    settings = settings or get_settings()
    if settings.llm_client == "mock":
        from app.clients.mock import MockClient

        return MockClient()
    from app.clients.meta_api import MetaModelClient

    return MetaModelClient(settings)


__all__ = ["LLMClient", "get_client"]
