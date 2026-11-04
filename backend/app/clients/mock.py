"""Offline fake client for tests, UI development and zero-cost demos.

Deterministic: SAM returns fixed boxes relative to the image size; Spark returns
a fixed 1890s-style label reading and interprets it by finding known phrases in
the transcripts it is given. Tests can override any response through `overrides`,
keyed by schema name (`transcriptout`, `interpretout`, `rereadout`, `locateout`)
or by `segment:<concept>`.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable
from typing import Any

from PIL import Image

from app.clients.base import Detection, LLMClient, SegmentResult, Usage

MOCK_LABEL_LINES = [
    "HERBARIUM MUSEI DEMONSTRATIONIS",
    "Flora Norvegica",
    "Botrychium lunaria (L.) Sw.",
    "NORVEGIA: Sør-Trøndelag: Oppdal, Vinstradalen",
    "In pratis siccis, alt. 900 m",
    "24. VII. 1897",
    "Leg. N.A. Sørensen   No. 1519",
]
MOCK_SLIP_LINES = ["Det. J. Smith 1952", "Botrychium lunaria (L.) Sw."]

# (term, verbatim phrase to look for, interpreted value)
MOCK_FIELDS: list[tuple[str, str, str]] = [
    ("scientificName", "Botrychium lunaria (L.) Sw.", "Botrychium lunaria (L.) Sw."),
    ("country", "NORVEGIA", "Norway"),
    ("stateProvince", "Sør-Trøndelag", "Sør-Trøndelag"),
    ("municipality", "Oppdal", "Oppdal"),
    ("locality", "Vinstradalen", "Vinstradalen"),
    ("habitat", "In pratis siccis", "In pratis siccis"),
    ("verbatimElevation", "alt. 900 m", "900 m"),
    ("eventDate", "24. VII. 1897", "1897-07-24"),
    ("recordedBy", "N.A. Sørensen", "N.A. Sørensen"),
    ("recordNumber", "No. 1519", "1519"),
    ("identifiedBy", "J. Smith", "J. Smith"),
    ("dateIdentified", "1952", "1952"),
]

# Boxes as fractions of the sheet: (x1, y1, x2, y2, score)
MOCK_BOXES: dict[str, list[tuple[float, float, float, float, float]]] = {
    "label": [(0.55, 0.72, 0.97, 0.94, 0.92)],
    "annotation slip": [(0.55, 0.62, 0.90, 0.69, 0.81)],
    "ruler": [(0.05, 0.96, 0.60, 0.995, 0.88)],
}

Override = Callable[[str], dict[str, Any]] | dict[str, Any]


class MockClient(LLMClient):
    name = "mock"

    def __init__(self, overrides: dict[str, Override] | None = None):
        self.overrides = overrides or {}
        self.calls: list[tuple[str, str]] = []  # (kind, schema/concept) for assertions

    def _override(self, key: str, prompt: str) -> dict[str, Any] | None:
        if key not in self.overrides:
            return None
        o = self.overrides[key]
        return o(prompt) if callable(o) else o

    def segment(self, image_jpeg: bytes, concept: str) -> SegmentResult:
        self.calls.append(("segment", concept))
        w, h = Image.open(io.BytesIO(image_jpeg)).size
        usage = Usage(model="mock-sam", images=1)
        forced = self._override(f"segment:{concept}", concept)
        boxes = forced["boxes"] if forced is not None else MOCK_BOXES.get(concept, [])
        dets = [
            Detection(int(x1 * w), int(y1 * h), int(x2 * w) - 1, int(y2 * h) - 1, score)
            for x1, y1, x2, y2, score in boxes
        ]
        return SegmentResult(dets, w, h, usage)

    def complete_json(
        self,
        prompt: str,
        images: list[bytes],
        schema: dict[str, Any],
        schema_name: str,
        effort: str,
    ) -> tuple[str, Usage]:
        self.calls.append(("spark", schema_name))
        usage = Usage(model="mock-spark", input_tokens=len(prompt) // 4, output_tokens=50)
        forced = self._override(schema_name, prompt)
        if forced is not None:
            return json.dumps(forced), usage
        handler = {
            "transcriptout": self._transcribe,
            "interpretout": self._interpret,
            "rereadout": self._reread,
            "locateout": self._locate,
        }.get(schema_name)
        if handler is None:
            raise ValueError(f"mock client has no response for schema {schema_name!r}")
        return json.dumps(handler(prompt, images)), usage

    # --- canned behaviours ---------------------------------------------------
    @staticmethod
    def _transcribe(_prompt: str, images: list[bytes]) -> dict[str, Any]:
        w, h = Image.open(io.BytesIO(images[0])).size if images else (1, 1)
        lines = MOCK_SLIP_LINES if w / max(h, 1) > 3.2 else MOCK_LABEL_LINES
        return {"lines": lines, "language": "la", "script": "printed"}

    @staticmethod
    def _interpret(prompt: str, _images: list[bytes]) -> dict[str, Any]:
        sections = dict(
            (int(n), body) for n, body in re.findall(r"\[R(\d+)\][^\n]*\n(.*?)(?=\n\[R\d+\]|\Z)", prompt, re.S)
        )
        fields = []
        for term, verbatim, value in MOCK_FIELDS:
            for n, body in sorted(sections.items()):
                if verbatim in body:
                    fields.append({"term": term, "region": n, "verbatim": verbatim, "value": value})
                    break
        return {"fields": fields}

    @staticmethod
    def _reread(prompt: str, _images: list[bytes]) -> dict[str, Any]:
        m = re.search(r"Current reading: «(.*?)»", prompt)
        return {"answer": m.group(1) if m else "", "legible": True, "note": "mock re-read agrees"}

    @staticmethod
    def _locate(_prompt: str, _images: list[bytes]) -> dict[str, Any]:
        return {"regions": [{"kind": "label", "box": [550, 720, 970, 940]}]}
