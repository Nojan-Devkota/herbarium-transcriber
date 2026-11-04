"""Content-hash cache behaviour and the SQLite data model."""

from __future__ import annotations

from sqlmodel import select

from app.cache import Cache, cache_key
from app.models import Check, Field, Region, RegionKind, Sheet


def test_cache_key_changes_with_prompt_version_and_extra():
    base = cache_key("abc", "transcribe", "v1", [1, 2, 3, 4])
    assert base == cache_key("abc", "transcribe", "v1", [1, 2, 3, 4])
    assert base != cache_key("abc", "transcribe", "v2", [1, 2, 3, 4])
    assert base != cache_key("abc", "transcribe", "v1", [1, 2, 3, 5])
    assert base != cache_key("abd", "transcribe", "v1", [1, 2, 3, 4])


def test_cache_roundtrip_and_disabled(tmp_path):
    cache = Cache(tmp_path)
    assert cache.get("segment", "k") is None
    cache.set("segment", "k", {"boxes": [1], "name": "Sørensen"})
    assert cache.get("segment", "k") == {"boxes": [1], "name": "Sørensen"}
    off = Cache(tmp_path, enabled=False)
    assert off.get("segment", "k") is None


def test_cache_corrupt_entry_is_a_miss(tmp_path):
    cache = Cache(tmp_path)
    cache.set("s", "deadbeef", {"a": 1})
    path = next((tmp_path / "s").rglob("*.json"))
    path.write_text("{not json")
    assert cache.get("s", "deadbeef") is None


def test_models_roundtrip_with_json_columns(session):
    sheet = Sheet(image_sha256="x" * 64, image_path="/tmp/x.jpg", width=10, height=20, gbif_record={"a": 1})
    session.add(sheet)
    session.commit()
    region = Region(sheet_id=sheet.id, kind=RegionKind.label, concept="label", x1=0, y1=0, x2=5, y2=5)
    session.add(region)
    session.commit()
    field = Field(sheet_id=sheet.id, dwc_term="eventDate", value="1897", region_id=region.id, evidence={"grounding": 1.0})
    session.add(field)
    session.commit()
    session.add(Check(field_id=field.id, tool="date_check", input={"v": "1897"}, result={"ok": True}, passed=True))
    session.commit()

    got = session.exec(select(Field)).one()
    assert got.evidence == {"grounding": 1.0}
    assert session.exec(select(Check)).one().result == {"ok": True}
    assert session.get(Sheet, sheet.id).gbif_record == {"a": 1}
    assert RegionKind.slip.has_text and not RegionKind.ruler.has_text


def test_cache_dir_override(monkeypatch, tmp_path):
    from app import config

    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "shared-cache"))
    config.get_settings.cache_clear()
    assert config.get_settings().cache_dir == tmp_path / "shared-cache"


def test_relative_data_dir_is_anchored_to_backend(monkeypatch, tmp_path):
    from app import config

    monkeypatch.setenv("DATA_DIR", "./some-data")
    for cwd in (config.REPO_ROOT, config.BACKEND_DIR):
        monkeypatch.chdir(cwd)
        config.get_settings.cache_clear()
        s = config.Settings()
        assert s.data_dir == (config.BACKEND_DIR / "some-data").resolve()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert config.Settings().data_dir == tmp_path  # absolute paths are left alone
