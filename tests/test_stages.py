"""Unit tests for stage helpers: licences, ingest, GBIF batches, boxes, spans, confidence."""

from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from app.cache import Cache
from app.models import Check, Field, RegionKind
from app.pipeline.confidence import agreement_score, score_field
from app.pipeline.ingest import fetch_gbif_batch, ingest_image, is_open_license, normalize_license
from app.pipeline.interpret import locate_span
from app.pipeline.segment import Box, dedupe_boxes, reading_order
from app.tools.gbif import GbifClient
from tests.conftest import make_sheet_jpeg


@pytest.mark.parametrize(
    "raw, norm, open_",
    [
        ("CC0_1_0", "CC0-1.0", True),
        ("http://creativecommons.org/publicdomain/zero/1.0/legalcode", "CC0-1.0", True),
        ("CC_BY_4_0", "CC-BY-4.0", True),
        ("http://creativecommons.org/licenses/by/4.0/legalcode", "CC-BY-4.0", True),
        ("CC_BY_NC_4_0", "CC-BY-NC-4.0", False),
        ("https://creativecommons.org/licenses/by-sa/3.0/", "CC-BY-SA-3.0", False),
        ("Copyright by the museum", "Copyright by the museum", False),
        (None, None, False),
    ],
)
def test_licences(raw, norm, open_):
    assert normalize_license(raw) == norm
    assert is_open_license(norm) is open_


def test_ingest_dedupes_and_rejects_non_images(session, settings):
    data = make_sheet_jpeg()
    a, created_a = ingest_image(session, data)
    b, created_b = ingest_image(session, data)
    assert created_a and not created_b and a.id == b.id
    assert (a.width, a.height) == (1000, 1500)
    with pytest.raises(ValueError):
        ingest_image(session, b"<html>Internal Server Error</html>")


def test_ingest_applies_exif_rotation(session):
    im = Image.new("RGB", (300, 200), "white")
    exif = im.getexif()
    exif[0x0112] = 6  # rotate 90° on display
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    sheet, _ = ingest_image(session, buf.getvalue())
    assert (sheet.width, sheet.height) == (200, 300)


def test_gbif_batch_filters_licence_and_skips_broken_media(session, settings, monkeypatch):
    from app.pipeline import ingest

    monkeypatch.setattr(ingest, "_resolve_ips", lambda host: ["93.184.216.34"])  # fake hosts resolve publicly
    img = make_sheet_jpeg(200, 300)

    def handler(request: httpx.Request):
        if request.url.path.endswith("/occurrence/search"):
            return httpx.Response(200, json={"endOfRecords": True, "results": [
                {"key": 1, "license": "CC_BY_NC_4_0", "media": [{"identifier": "https://img/1.jpg"}]},
                {"key": 2, "license": "CC0_1_0", "media": []},
                {"key": 3, "license": "CC0_1_0", "media": [{"identifier": "https://img/broken.jpg"}]},
                {"key": 4, "license": "CC_BY_4_0", "scientificName": "Botrychium lunaria (L.) Sw.",
                 "recordedBy": "N.A. Sørensen", "eventDate": "1950-07-24",
                 "media": [{"identifier": "https://img/4.jpg", "license": "CC_BY_4_0"}]},
            ]})  # fmt: skip
        if request.url.path.endswith("broken.jpg"):
            return httpx.Response(200, content=b"Internal Server Error")
        return httpx.Response(200, content=img)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    gbif = GbifClient(http=http, cache=Cache(settings.cache_dir))
    report = fetch_gbif_batch(session, gbif=gbif, http=http, limit=5)
    assert len(report.added) == 1
    reasons = {s["key"]: s["reason"] for s in report.skipped}
    assert reasons["1"].startswith("licence") and reasons["2"] == "no image" and "download failed" in reasons["3"]
    from app.models import Sheet

    sheet = session.get(Sheet, report.added[0])
    assert sheet.gbif_occurrence_id == "4" and sheet.license == "CC-BY-4.0"
    assert sheet.gbif_record["recordedBy"] == "N.A. Sørensen"


def test_dedupe_boxes_nms_nesting_and_sheet_sized():
    W, H = 1000, 1500
    label = Box(RegionKind.label, "label", "sam", 550, 1100, 970, 1400, 0.7)
    inner = Box(RegionKind.label, "label", "sam", 600, 1150, 800, 1200, 0.9)  # nested text block
    slip_dup = Box(RegionKind.slip, "annotation slip", "sam", 552, 1102, 968, 1398, 0.6)  # same box as label
    ruler = Box(RegionKind.ruler, "ruler", "sam", 0, 1450, 600, 1499, 0.9)
    chart = Box(RegionKind.color_chart, "color chart", "sam", 2, 1451, 598, 1499, 0.7)  # same as ruler
    huge = Box(RegionKind.label, "label", "sam", 0, 0, 999, 1499, 0.99)
    kept = dedupe_boxes([label, inner, slip_dup, ruler, chart, huge], W, H)
    assert kept == [ruler, label] or kept == [label, ruler]


def test_reading_order_rows_then_columns():
    a = Box(RegionKind.label, "", "", 600, 1000, 900, 1200)
    b = Box(RegionKind.slip, "", "", 100, 1010, 400, 1100)
    c = Box(RegionKind.stamp, "", "", 100, 100, 300, 200)
    assert reading_order([a, b, c]) == [c, b, a]


@pytest.mark.parametrize(
    "verbatim, text, score, span",
    [
        ("Vinstradalen", "LOC. Vinstradalen\nHAB.", 1.0, (5, 17)),
        ("vinstradalen", "LOC. Vinstradalen", 0.98, (5, 17)),
        ("Oppdal Vinstradalen", "Oppdal\nVinstradalen", 1.0, (0, 19)),
        ("Mars", "LOC. Vinstradalen", None, None),
    ],
)
def test_locate_span(verbatim, text, score, span):
    g, s, e = locate_span(verbatim, text)
    if score is None:
        assert g < 0.8
    else:
        assert g == score and (s, e) == span


def _check(tool, passed, step):
    return Check(field_id=1, tool=tool, passed=passed, step=step)


def test_score_field_rules():
    f = Field(sheet_id=1, dwc_term="eventDate", evidence={"grounding": 1.0})
    score, level, _ = score_field(f, [_check("date_check", True, 1)], 0.85, 0.6)
    assert level == "high"  # grounded + verified, unconfirmed reading
    score, level, comps = score_field(f, [_check("date_check", True, 1), _check("date_check", False, 3)], 0.85, 0.6)
    assert level == "low" and comps["failed_checks"] == ["date_check"]  # the last result counts
    free = Field(sheet_id=1, dwc_term="locality", evidence={"grounding": 1.0})
    assert score_field(free, [], 0.85, 0.6)[1] == "medium"  # no external check: needs a 2nd reading
    free.evidence = {"grounding": 1.0, "agreement": 1.0}
    assert score_field(free, [], 0.85, 0.6)[1] == "high"
    free.evidence = {"grounding": 1.0, "agreement": 0.8}
    assert score_field(free, [], 0.85, 0.6)[1] != "high"


def test_agreement_score():
    assert agreement_score("Vinstradalen", "LOC. Vinstradalen\nHAB.") == 1.0
    assert agreement_score("Vinstradalen", "LOC. Vinstradolen") < 1.0
    assert agreement_score(None, "x") is None
    assert agreement_score("x", "") == 0.0


def test_label_and_slip_on_same_box_is_a_label():
    slip = Box(RegionKind.slip, "annotation slip", "sam", 100, 100, 500, 400, 0.93)
    label = Box(RegionKind.label, "label", "sam", 105, 102, 498, 398, 0.70)
    kept = dedupe_boxes([slip, label], 1000, 1500)
    assert len(kept) == 1 and kept[0].kind == RegionKind.label and kept[0].score == 0.93


@pytest.mark.parametrize(
    "url, ips",
    [
        ("http://127.0.0.1:8000/sheets", ["127.0.0.1"]),
        ("http://169.254.169.254/latest/meta-data/", ["169.254.169.254"]),
        ("http://intranet.local/x.jpg", ["10.0.0.5"]),
        ("http://[::1]/x.jpg", ["::1"]),
        ("file:///etc/passwd", ["93.184.216.34"]),
        ("ftp://example.org/x.jpg", ["93.184.216.34"]),
    ],
)
def test_ssrf_guard_blocks_non_public_targets(monkeypatch, url, ips):
    from app.pipeline import ingest

    monkeypatch.setattr(ingest, "_resolve_ips", lambda host: ips)
    with pytest.raises(ingest.UnsafeURLError):
        ingest.assert_public_url(url)


def test_ssrf_guard_rechecks_every_redirect(monkeypatch):
    from app.pipeline import ingest

    monkeypatch.setattr(ingest, "_resolve_ips", lambda host: ["10.1.2.3"] if host == "internal" else ["93.184.216.34"])
    hits = []

    def handler(request: httpx.Request):
        hits.append(request.url.host)
        return httpx.Response(302, headers={"location": "http://internal/secret"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ingest.UnsafeURLError):
        ingest.download_image(http, "https://public.example/img.jpg")
    assert hits == ["public.example"]  # the internal host was never contacted


def test_oversized_and_truncated_images_are_clean_errors(session):
    good = make_sheet_jpeg(300, 300)
    with pytest.raises(ValueError, match="not a readable image"):
        ingest_image(session, good[: len(good) // 3])
