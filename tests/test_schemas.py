"""Strict schemas reject malformed model output; the client retries exactly once."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.clients.base import InvalidModelOutput
from app.clients.mock import MockClient
from app.schemas.darwin_core import ALL_TERMS, LABEL_TERMS, InterpretOut
from app.schemas.transcript import LocateOut, RereadOut, TranscriptOut


def test_transcript_rejects_extra_and_bad_enum():
    TranscriptOut.model_validate({"lines": ["a"], "language": "la", "script": "printed"})
    with pytest.raises(ValidationError):
        TranscriptOut.model_validate({"lines": ["a"], "language": "la", "script": "carved"})
    with pytest.raises(ValidationError):
        TranscriptOut.model_validate({"lines": ["a"], "language": "la", "script": "printed", "x": 1})


def test_interpret_rejects_unknown_term():
    ok = {"fields": [{"term": "eventDate", "region": 1, "verbatim": "1897", "value": "1897"}]}
    assert InterpretOut.model_validate(ok).fields[0].term == "eventDate"
    bad = {"fields": [{"term": "favouriteColour", "region": 1, "verbatim": "x", "value": "x"}]}
    with pytest.raises(ValidationError):
        InterpretOut.model_validate(bad)


def test_locate_box_needs_four_numbers():
    with pytest.raises(ValidationError):
        LocateOut.model_validate({"regions": [{"kind": "label", "box": [1, 2, 3]}]})


def test_terms_are_unique_and_ordered():
    assert len(ALL_TERMS) == len(set(ALL_TERMS))
    assert list(LABEL_TERMS)[2] == "scientificName"


def test_structured_retries_once_then_succeeds():
    attempts = []

    def flaky(prompt: str):
        attempts.append(prompt)
        if len(attempts) == 1:
            return {"answer": 1897}  # wrong type, missing keys
        return {"answer": "1897", "legible": True, "note": "ok"}

    client = MockClient(overrides={"rereadout": flaky})
    result = client.structured("q", [], RereadOut)
    assert result.data.answer == "1897"
    assert result.attempts == 2
    assert "did not match the required JSON schema" in attempts[1]


def test_structured_gives_up_after_second_failure():
    client = MockClient(overrides={"rereadout": {"nope": True}})
    with pytest.raises(InvalidModelOutput):
        client.structured("q", [], RereadOut)


def test_schema_is_json_serializable():
    for model in (TranscriptOut, RereadOut, LocateOut, InterpretOut):
        json.dumps(model.model_json_schema())
