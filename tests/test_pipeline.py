"""End-to-end pipeline runs with the mock client and an offline GBIF."""

from __future__ import annotations

import httpx
import pytest
from sqlmodel import select

from app.cache import Cache
from app.clients.mock import MOCK_FIELDS, MockClient
from app.models import Check, Field, ModelCall, Region, RegionKind, SheetStatus, Transcript
from app.pipeline.context import RunOptions
from app.pipeline.ingest import ingest_image
from app.pipeline.run import process_sheet
from app.tools.gbif import GbifClient


def gbif_offline(settings, status="ACCEPTED", match_type="EXACT", accepted=None):
    def handler(request: httpx.Request):
        if request.url.path.endswith("/species/match"):
            name = request.url.params["name"]
            return httpx.Response(200, json={
                "matchType": match_type, "confidence": 99, "status": status, "rank": "SPECIES",
                "usageKey": 2650165, "scientificName": name, "family": "Ophioglossaceae",
                "acceptedUsageKey": 42 if accepted else None,
            })  # fmt: skip
        return httpx.Response(200, json={"scientificName": accepted})

    return GbifClient(http=httpx.Client(transport=httpx.MockTransport(handler)), cache=Cache(settings.cache_dir / "gbif-test"))


@pytest.fixture
def sheet_id(session, sheet_jpeg):
    sheet, _ = ingest_image(session, sheet_jpeg, filename="demo.jpg", license="CC0_1_0")
    return sheet.id


def fields_by_term(session, sheet_id):
    return {f.dwc_term: f for f in session.exec(select(Field).where(Field.sheet_id == sheet_id))}


def test_full_pipeline_produces_grounded_verified_record(session, settings, sheet_id):
    client = MockClient()
    sheet = process_sheet(sheet_id, client=client, gbif=gbif_offline(settings))
    assert sheet.status == SheetStatus.done, sheet.error

    regions = session.exec(select(Region).where(Region.sheet_id == sheet_id)).all()
    kinds = sorted(r.kind.value for r in regions)
    assert kinds == ["label", "ruler", "slip"]
    text_regions = sorted((r for r in regions if r.kind.has_text), key=lambda r: r.ordinal)
    assert [r.ordinal for r in text_regions] == [1, 2] and text_regions[0].kind == RegionKind.slip  # slip is higher up
    assert all(r.crop_path for r in text_regions)

    fields = fields_by_term(session, sheet_id)
    for term, verbatim, value in MOCK_FIELDS:
        assert fields[term].value == value, term
        assert fields[term].verbatim == verbatim
        assert fields[term].span_start is not None
    # Identification terms come from the later determination slip
    slip = next(r for r in regions if r.kind == RegionKind.slip)
    assert fields["scientificName"].region_id == slip.id
    # Derived by tools
    assert fields["countryCode"].value == "NO" and fields["countryCode"].derived
    assert fields["taxonID"].value == "2650165"
    # Verified fields are high confidence; every field has a trace
    assert fields["scientificName"].confidence_level == "high"
    assert fields["eventDate"].confidence_level == "high"
    checks = session.exec(select(Check).where(Check.field_id == fields["scientificName"].id)).all()
    assert {c.tool for c in checks} >= {"grounding", "gbif_name_match"}
    assert sheet.cost_usd == 0.0


def test_second_run_is_served_from_cache(session, settings, sheet_id):
    process_sheet(sheet_id, client=MockClient(), gbif=gbif_offline(settings))
    again = MockClient()
    sheet = process_sheet(sheet_id, client=again, gbif=gbif_offline(settings))
    assert sheet.status == SheetStatus.done
    assert again.calls == []  # nothing reached the model
    calls = session.exec(select(ModelCall).where(ModelCall.sheet_id == sheet_id)).all()
    assert calls and all(c.cached for c in calls)
    # Re-run replaced, not duplicated, the results
    assert len(session.exec(select(Field).where(Field.sheet_id == sheet_id, Field.dwc_term == "eventDate")).all()) == 1


def test_agent_rereads_and_fixes_misinterpreted_year(session, settings, sheet_id):
    def interpret(prompt):
        return {"fields": [{"term": "eventDate", "region": 2, "verbatim": "24. VII. 1897", "value": "1997-07-24"}]}

    client = MockClient(overrides={"interpretout": interpret})
    process_sheet(sheet_id, client=client, gbif=gbif_offline(settings))
    f = fields_by_term(session, sheet_id)["eventDate"]
    assert f.value == "1897-07-24" and f.model_value == "1997-07-24"
    trace = session.exec(select(Check).where(Check.field_id == f.id).order_by(Check.step)).all()
    assert [c.tool for c in trace][:4] == ["grounding", "date_check", "reread_crop", "date_check"]
    assert trace[1].passed is False and trace[3].passed is True


def test_reread_is_bounded_when_problem_persists(session, settings, sheet_id):
    def interpret(prompt):
        return {"fields": [{"term": "eventDate", "region": 2, "verbatim": "24. VII. 1897", "value": "2097-07-24"}]}

    rereads = []

    def reread(prompt):
        rereads.append(prompt)
        return {"answer": f"24. VII. 20{97 - len(rereads)}", "legible": True, "note": "keeps changing"}

    client = MockClient(overrides={"interpretout": interpret, "rereadout": reread})
    process_sheet(sheet_id, client=client, gbif=gbif_offline(settings))
    assert len(rereads) <= settings.max_rereads_per_field


def test_synonym_yields_accepted_name(session, settings, sheet_id):
    gbif = gbif_offline(settings, status="SYNONYM", accepted="Botrychium accepted Sw.")
    process_sheet(sheet_id, client=MockClient(), gbif=gbif)
    fields = fields_by_term(session, sheet_id)
    assert fields["acceptedNameUsage"].value == "Botrychium accepted Sw."
    assert fields["scientificName"].value == "Botrychium lunaria (L.) Sw."  # label name is kept


def test_unknown_name_triggers_targeted_reread(session, settings, sheet_id):
    questions = []

    def reread(prompt):
        questions.append(prompt)
        return {"answer": "Botrychium lunaria (L.) Sw.", "legible": True, "note": "same"}

    gbif = gbif_offline(settings, match_type="NONE")
    process_sheet(sheet_id, client=MockClient(overrides={"rereadout": reread}), gbif=gbif)
    f = fields_by_term(session, sheet_id)["scientificName"]
    assert len(questions) == 1 and "does not exactly recognise" in questions[0]
    assert f.status.value == "flagged" and f.confidence_level != "high"


def test_spark_locate_fallback_when_sam_finds_nothing(session, settings, sheet_id):
    empty = {"boxes": []}
    client = MockClient(overrides={"segment:label": empty, "segment:annotation slip": empty, "segment:ruler": empty})
    process_sheet(sheet_id, client=client, gbif=gbif_offline(settings))
    regions = session.exec(select(Region).where(Region.sheet_id == sheet_id)).all()
    assert [r.source for r in regions] == ["spark"]
    assert ("spark", "locateout") in client.calls


def test_whole_sheet_ablation_skips_sam(session, settings, sheet_id):
    client = MockClient()
    opts = RunOptions(use_sam=False, use_verify=False, use_second_pass=False)
    sheet = process_sheet(sheet_id, client=client, options=opts, gbif=gbif_offline(settings))
    assert sheet.status == SheetStatus.done
    regions = session.exec(select(Region).where(Region.sheet_id == sheet_id)).all()
    assert [r.kind for r in regions] == [RegionKind.sheet]
    assert not any(kind == "segment" for kind, _ in client.calls)
    f = fields_by_term(session, sheet_id)["eventDate"]
    tools = {c.tool for c in session.exec(select(Check).where(Check.field_id == f.id))}
    assert tools == {"grounding"}


def test_second_reading_is_opt_in(session, settings, sheet_id):
    client = MockClient()
    process_sheet(sheet_id, client=client, gbif=gbif_offline(settings))
    assert ("spark", "transcriptout") in client.calls
    pass2 = session.exec(select(Transcript).where(Transcript.sheet_id == sheet_id, Transcript.pass_no == 2)).all()
    assert pass2 == []  # default pipeline makes no second-reading calls
    f = fields_by_term(session, sheet_id)["locality"]
    assert "agreement" not in f.evidence and f.confidence_level == "medium" and f.status.value == "flagged"


def test_selective_second_pass_only_for_unverified_fields(session, settings, sheet_id):
    process_sheet(sheet_id, client=MockClient(), options=RunOptions(use_second_pass=True), gbif=gbif_offline(settings))
    pass2 = session.exec(select(Transcript).where(Transcript.sheet_id == sheet_id, Transcript.pass_no == 2)).all()
    assert pass2  # free-text fields (locality, recordedBy) need a second reading
    f = fields_by_term(session, sheet_id)["locality"]
    assert f.evidence["agreement"] == 1.0 and f.confidence_level == "high"


def test_model_failure_marks_sheet_error_without_crashing(session, settings, sheet_id):
    def boom(prompt):
        raise RuntimeError("API down")

    sheet = process_sheet(sheet_id, client=MockClient(overrides={"interpretout": boom}), gbif=gbif_offline(settings))
    assert sheet.status == SheetStatus.error and "API down" in sheet.error


def test_cached_calls_keep_nominal_cost(session, settings, sheet_id):
    priced = MockClient()
    original = priced.complete_json

    def with_cost(*args, **kwargs):
        raw, usage = original(*args, **kwargs)
        usage.cost_usd = 0.01
        return raw, usage

    priced.complete_json = with_cost
    first = process_sheet(sheet_id, client=priced, gbif=gbif_offline(settings))  # default options
    second = process_sheet(sheet_id, client=MockClient(), gbif=gbif_offline(settings))
    assert first.cost_usd > 0 and second.cost_usd == 0
    calls = session.exec(select(ModelCall).where(ModelCall.sheet_id == sheet_id)).all()
    assert sum(c.nominal_cost_usd for c in calls) == pytest.approx(first.cost_usd)


def test_no_write_lock_is_held_during_model_calls(session, settings, sheet_id):
    """Regression: concurrent eval workers hit 'database is locked' because a session
    held a write transaction open across slow model calls."""
    import sqlite3
    import threading

    violations = []
    probe_lock = threading.Lock()  # concurrent model calls must not trip over each other's probes

    def assert_db_writable():
        with probe_lock:
            _probe()

    def _probe():
        con = sqlite3.connect(settings.db_path, timeout=0)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.rollback()
        except sqlite3.OperationalError as exc:
            violations.append(str(exc))
        finally:
            con.close()

    class LockCheckingClient(MockClient):
        def segment(self, image_jpeg, concept):
            assert_db_writable()
            return super().segment(image_jpeg, concept)

        def complete_json(self, *args, **kwargs):
            assert_db_writable()
            return super().complete_json(*args, **kwargs)

    gbif = gbif_offline(settings, match_type="NONE")  # forces re-reads, the path that held locks
    original = gbif.name_match
    gbif.name_match = lambda *a, **k: (assert_db_writable(), original(*a, **k))[1]
    sheet = process_sheet(sheet_id, client=LockCheckingClient(), options=RunOptions(use_second_pass=True), gbif=gbif)
    assert sheet.status == SheetStatus.done
    assert violations == []


def test_sam_concepts_run_concurrently(session, settings, sheet_id):
    import threading
    import time

    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    class SlowSam(MockClient):
        def segment(self, image_jpeg, concept):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return super().segment(image_jpeg, concept)

    sheet = process_sheet(sheet_id, client=SlowSam(), gbif=gbif_offline(settings))
    assert sheet.status == SheetStatus.done
    assert state["peak"] > 1
    calls = session.exec(select(ModelCall).where(ModelCall.sheet_id == sheet_id, ModelCall.stage == "segment")).all()
    assert len(calls) == 6  # every concept still logged exactly once


def test_cost_of_finished_calls_is_kept_when_a_sibling_call_fails(session, settings, sheet_id):
    from app.pipeline.context import CallRequest, PipelineContext
    from app.models import Sheet
    from app.prompts import load_prompt
    from app.schemas.transcript import TranscriptOut

    class HalfBroken(MockClient):
        def complete_json(self, prompt, *args, **kwargs):
            if "BOOM" in prompt:
                raise RuntimeError("API down")
            raw, usage = super().complete_json(prompt, *args, **kwargs)
            usage.cost_usd = 0.02
            return raw, usage

    from tests.conftest import make_sheet_jpeg

    sheet = session.get(Sheet, sheet_id)
    ctx = PipelineContext(session, sheet, HalfBroken(), settings)
    p = load_prompt("transcribe")
    img = make_sheet_jpeg(200, 100)
    reqs = [CallRequest("transcribe1", p, "ok", [img], TranscriptOut, "low", ("a",)),
            CallRequest("transcribe1", p, "BOOM", [img], TranscriptOut, "low", ("b",))]  # fmt: skip
    with pytest.raises(RuntimeError):
        ctx.structured_many(reqs)
    session.commit()
    calls = session.exec(select(ModelCall).where(ModelCall.sheet_id == sheet_id)).all()
    assert [c.cost_usd for c in calls] == [0.02]


def test_stale_cache_entry_is_a_miss(session, settings, sheet_id):
    process_sheet(sheet_id, client=MockClient(), gbif=gbif_offline(settings))
    # Corrupt every cached Spark entry's shape (e.g. written by an older schema)
    import json

    for path in settings.cache_dir.rglob("*.json"):
        if "transcribe1" in path.parts:
            path.write_text(json.dumps({"data": {"unexpected": 1}, "usage": {"model": "x"}}))
    client = MockClient()
    sheet = process_sheet(sheet_id, client=client, gbif=gbif_offline(settings))
    assert sheet.status == SheetStatus.done
    assert ("spark", "transcriptout") in client.calls  # re-fetched instead of crashing


def test_gbif_outage_flags_the_name_instead_of_passing_it(session, settings, sheet_id):
    def down(request):
        raise httpx.ConnectError("GBIF down")

    gbif = GbifClient(http=httpx.Client(transport=httpx.MockTransport(down)), cache=Cache(settings.cache_dir / "down"))
    sheet = process_sheet(sheet_id, client=MockClient(), gbif=gbif)
    assert sheet.status == SheetStatus.done
    f = fields_by_term(session, sheet_id)["scientificName"]
    assert f.evidence["verification_unavailable"] is True
    assert f.status.value == "flagged" and f.confidence_level != "high"


def test_failed_sheet_keeps_cost_of_calls_already_made(session, settings, sheet_id):
    class FailsAtInterpret(MockClient):
        def complete_json(self, prompt, images, schema, schema_name, effort):
            if schema_name == "interpretout":
                raise RuntimeError("API down")
            raw, usage = super().complete_json(prompt, images, schema, schema_name, effort)
            usage.cost_usd = 0.01
            return raw, usage

    sheet = process_sheet(sheet_id, client=FailsAtInterpret(), gbif=gbif_offline(settings))
    assert sheet.status == SheetStatus.error and sheet.cost_usd > 0  # transcription was paid for
