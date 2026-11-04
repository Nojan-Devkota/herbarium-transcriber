"""Model clients behind the `LLMClient` interface."""

from __future__ import annotations

import threading

from app.clients.base import LLMClient
from app.config import Settings, get_settings


_shared: dict[tuple, LLMClient] = {}
_lock = threading.Lock()


def get_client(settings: Settings | None = None) -> LLMClient:
    """The model client. The real client is shared process-wide so every sheet reuses one
    connection pool instead of opening (and leaking) a new one per sheet."""
    settings = settings or get_settings()
    if settings.llm_client == "mock":
        from app.clients.mock import MockClient

        return MockClient()
    from app.clients.meta_api import MetaModelClient

    key = (settings.meta_api_key, settings.meta_base_url, settings.request_timeout_s, settings.model_spark, settings.model_sam)
    with _lock:
        if key not in _shared:
            _shared[key] = MetaModelClient(settings)
        return _shared[key]


__all__ = ["LLMClient", "get_client"]
