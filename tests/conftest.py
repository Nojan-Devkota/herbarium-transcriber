"""Shared fixtures: every test gets an isolated data dir and the offline mock client."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LLM_CLIENT", "mock")
    monkeypatch.setenv("META_API_KEY", "")
    monkeypatch.setenv("GEOCODE_ENABLED", "false")
    from app import config, db

    config.get_settings.cache_clear()
    db.reset_engine()
    yield
    db.reset_engine()
    config.get_settings.cache_clear()


@pytest.fixture
def settings():
    from app.config import get_settings

    return get_settings()


@pytest.fixture
def session():
    from app.db import session_scope

    with session_scope() as s:
        yield s


def make_sheet_jpeg(width: int = 1000, height: int = 1500) -> bytes:
    """A blank cream 'herbarium sheet' with a white label rectangle."""
    im = Image.new("RGB", (width, height), (238, 232, 214))
    label = Image.new("RGB", (int(width * 0.4), int(height * 0.2)), (255, 255, 255))
    im.paste(label, (int(width * 0.56), int(height * 0.73)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


@pytest.fixture
def sheet_jpeg() -> bytes:
    return make_sheet_jpeg()
