"""Strict schemas for Muse Spark *reading* outputs (verbatim, no interpretation)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TranscriptOut(_Strict):
    """Verbatim reading of one label crop."""

    lines: list[str] = Field(
        description="Every line of text exactly as written, top to bottom. Keep abbreviations, "
        "punctuation, capitalisation and misspellings. Use [?] for an illegible character."
    )
    language: str = Field(description="Main language of the label as an ISO 639-1 code, or 'la' for Latin, 'und' if unknown")
    script: Literal["printed", "typed", "handwritten", "mixed", "none"] = Field(
        description="How the text was produced; 'none' if the crop has no text"
    )


class RereadOut(_Strict):
    """Answer to a targeted re-read question about one field on a crop."""

    answer: str = Field(description="The exact characters written on the label for the asked item, or '' if absent")
    legible: bool = Field(description="False if the characters cannot be read with reasonable certainty")
    note: str = Field(description="One short sentence on what you saw (e.g. 'the second digit has an open top, so 9 not 4')")


class LocatedRegion(_Strict):
    kind: Literal["label", "slip", "barcode", "stamp"]
    box: list[int] = Field(
        description="[x1, y1, x2, y2] on a 0-1000 grid relative to the image width and height",
        min_length=4,
        max_length=4,
    )


class LocateOut(_Strict):
    """Fallback detector: Spark proposes label boxes when SAM finds none."""

    regions: list[LocatedRegion]
