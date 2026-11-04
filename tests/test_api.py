"""HTTP API: upload, detail, review, export, stats (mock client, offline GBIF)."""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.clients.mock import MockClient
from app.export.dwc_csv import COLUMNS, _safe
from app.pipeline.run import process_sheet
from tests.conftest import make_sheet_jpeg
from tests.test_pipeline import gbif_offline


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def upload(client, data=None, process="false", **form):
    files = [("files", ("sheet.jpg", data or make_sheet_jpeg(), "image/jpeg"))]
    return client.post("/sheets", files=files, data={"process": process, "license": "CC0_1_0", **form})


@pytest.fixture
def processed(client, settings):
    sid = upload(client).json()[0]["id"]
    sheet = process_sheet(sid, client=MockClient(), gbif=gbif_offline(settings))
    assert sheet.status.value == "done"
    return sid


def test_health(client):
    body = client.get("/health").json()
    assert body["ok"] and body["client"] == "mock"


def test_upload_dedupes_and_reports_bad_files(client):
    first = upload(client).json()[0]
    again = upload(client).json()[0]
    assert first["created"] and not again["created"] and first["id"] == again["id"]
    bad = client.post("/sheets", files=[("files", ("x.jpg", b"not an image", "image/jpeg"))], data={"process": "false"})
    assert bad.status_code == 200 and bad.json()[0]["error"] == "file is not a readable image"
    listing = client.get("/sheets").json()
    assert len(listing) == 1 and listing[0]["status"] == "uploaded" and listing[0]["license"] == "CC0-1.0"


def test_upload_with_process_enqueues(client, monkeypatch):
    seen = []
    from app.pipeline import run

    monkeypatch.setattr(run.worker, "enqueue", lambda sid, options=None: seen.append(sid))
    sid = upload(client, process="true").json()[0]["id"]
    assert seen == [sid]


def test_sheet_detail_has_regions_fields_and_trace(client, processed):
    d = client.get(f"/sheets/{processed}").json()
    assert d["sheet"]["status"] == "done" and d["sheet"]["scientific_name"].startswith("Botrychium")
    assert {r["kind"] for r in d["regions"]} == {"label", "slip", "ruler"}
    name = next(f for f in d["fields"] if f["dwc_term"] == "scientificName")
    assert [c["tool"] for c in name["checks"]][:2] == ["grounding", "gbif_name_match"]
    assert name["region_id"] in {r["id"] for r in d["regions"]}
    assert any(t["pass_no"] == 1 for t in d["transcripts"])
    label = next(r for r in d["regions"] if r["kind"] == "label")
    assert client.get(f"/regions/{label['id']}/crop").headers["content-type"] == "image/jpeg"
    assert client.get(f"/sheets/{processed}/image").status_code == 200
    assert client.get("/sheets/999").status_code == 404


def test_review_accept_and_edit_are_logged(client, processed):
    fields = client.get(f"/sheets/{processed}").json()["fields"]
    loc = next(f for f in fields if f["dwc_term"] == "locality")
    date = next(f for f in fields if f["dwc_term"] == "eventDate")

    r = client.patch(f"/fields/{loc['id']}", json={"action": "accept"})
    assert r.json()["status"] == "accepted"
    r = client.patch(f"/fields/{date['id']}", json={"action": "edit", "value": "1897-07-25"})
    assert r.json()["status"] == "edited" and r.json()["value"] == "1897-07-25"
    assert client.patch(f"/fields/{date['id']}", json={"action": "edit"}).status_code == 422

    stats = client.get("/stats").json()
    assert stats["fields_by_status"]["edited"] == 1 and stats["fields_by_status"]["accepted"] == 1
    # eventDate (verified -> high) was edited; locality (unverified -> medium) was accepted
    assert stats["edit_rate_by_confidence"]["high"] == 1.0 and stats["edit_rate_by_confidence"]["medium"] == 0.0


def test_export_csv_is_darwin_core(client, processed):
    resp = client.get("/export.csv")
    assert resp.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert list(rows[0].keys()) == COLUMNS
    row = rows[0]
    assert row["basisOfRecord"] == "PreservedSpecimen"
    assert row["scientificName"] == "Botrychium lunaria (L.) Sw."
    assert row["eventDate"] == "1897-07-24" and row["verbatimEventDate"] == "24. VII. 1897"
    assert row["countryCode"] == "NO" and row["license"] == "CC0-1.0"
    assert row["family"] == "Ophioglossaceae"  # from the GBIF match
    assert row["occurrenceID"].startswith("urn:sha256:")


def test_export_reviewed_only_skips_flagged(client, processed):
    rows = list(csv.DictReader(io.StringIO(client.get("/export.csv?reviewed_only=true").text)))
    flagged = int(list(csv.DictReader(io.StringIO(client.get("/export.csv").text)))[0]["x_fieldsNeedingReview"])
    assert (len(rows) == 0) == (flagged > 0)


def test_csv_formula_injection_is_neutralised():
    assert _safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert _safe("-2+3+cmd|' /C calc'!A0").startswith("'")  # leading '-' formula
    assert _safe(" =1+1").startswith("'") and _safe("@SUM(A1)").startswith("'")
    assert _safe("-12.5") == "-12.5" and _safe("+1") == "+1" and _safe(None) == ""  # plain numbers untouched


def test_reprocess_validates_ablation(client, processed, monkeypatch):
    from app.pipeline import run

    seen = []
    monkeypatch.setattr(run.worker, "enqueue", lambda sid, options=None: seen.append(options))
    assert client.post(f"/sheets/{processed}/process", json={"ablation": "whole-sheet"}).status_code == 200
    assert seen[0].use_sam is False
    assert client.post(f"/sheets/{processed}/process", json={"ablation": "nope"}).status_code == 422


def test_gbif_batch_needs_a_filter(client):
    assert client.post("/batches/gbif", json={"limit": 2}).status_code == 422


def test_stats_on_empty_db(client):
    s = client.get("/stats").json()
    assert s["sheets_total"] == 0 and s["avg_cost_per_sheet_usd"] is None


def test_polling_requests_are_kept_out_of_the_access_log():
    import logging

    from app.main import QuietPollingFilter

    f = QuietPollingFilter()

    def rec(method, path, status):
        return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                 ("127.0.0.1:1", method, path, "1.1", status), None)  # fmt: skip

    assert not f.filter(rec("GET", "/sheets", 200))
    assert not f.filter(rec("GET", "/sheets/12", 304))
    assert not f.filter(rec("GET", "/stats", 200))
    assert f.filter(rec("POST", "/sheets", 200))  # uploads stay visible
    assert f.filter(rec("GET", "/sheets/12/image", 200))
    assert f.filter(rec("GET", "/sheets", 500))  # errors stay visible


def test_writes_from_other_machines_are_refused(client, monkeypatch):
    from starlette.testclient import TestClient as RawClient

    from app.main import app

    remote = RawClient(app, client=("203.0.113.9", 5555))
    assert remote.get("/health").status_code == 200  # reads stay open
    assert remote.post("/batches/gbif", json={"scientific_name": "x"}).status_code == 403
    monkeypatch.setenv("ALLOW_REMOTE_WRITES", "true")
    from app import config

    config.get_settings.cache_clear()
    assert remote.post("/batches/gbif", json={"limit": 1}).status_code == 422  # now reaches validation


def test_input_limits(client, processed):
    assert client.post("/batches/gbif", json={"scientific_name": "x", "year": "18xx"}).status_code == 422
    field_id = client.get(f"/sheets/{processed}").json()["fields"][0]["id"]
    assert client.patch(f"/fields/{field_id}", json={"action": "edit", "value": "x" * 5000}).status_code == 422
    files = [("files", (f"{i}.jpg", b"x", "image/jpeg")) for i in range(21)]
    assert client.post("/sheets", files=files, data={"process": "false"}).status_code == 413
    bad_url = client.post("/sheets", files=[("files", ("a.jpg", make_sheet_jpeg(), "image/jpeg"))], data={"process": "false", "source_url": "javascript:alert(1)"})
    assert bad_url.status_code == 422


def test_reprocess_and_review_conflicts(client, processed, monkeypatch):
    from app.db import session_scope
    from app.models import Sheet, SheetStatus

    with session_scope() as s:
        sheet = s.get(Sheet, processed)
        sheet.status = SheetStatus.queued
        s.add(sheet)
        s.commit()
    assert client.post(f"/sheets/{processed}/process", json={}).status_code == 409
    field_id = client.get(f"/sheets/{processed}").json()["fields"][0]["id"]
    assert client.patch(f"/fields/{field_id}", json={"action": "accept"}).status_code == 409


def test_image_variants_and_cache_headers(client, processed):
    thumb = client.get(f"/sheets/{processed}/image?size=thumb")
    view = client.get(f"/sheets/{processed}/image")
    full = client.get(f"/sheets/{processed}/image?size=full")
    assert len(thumb.content) < len(view.content) <= len(full.content)
    assert "immutable" in thumb.headers["cache-control"]
    assert client.get(f"/sheets/{processed}/image?size=huge").status_code == 422


def test_sheet_list_query_count_does_not_grow_with_sheets(client, settings):
    from sqlalchemy import event

    from app.db import get_engine

    for i in range(6):
        upload(client, data=make_sheet_jpeg(200 + i, 300))
    statements = []
    listener = lambda *a: statements.append(a[2])  # noqa: E731
    event.listen(get_engine(), "before_cursor_execute", listener)
    try:
        assert len(client.get("/sheets").json()) == 6
    finally:
        event.remove(get_engine(), "before_cursor_execute", listener)
    assert len([q for q in statements if q.lstrip().upper().startswith("SELECT")]) <= 3
