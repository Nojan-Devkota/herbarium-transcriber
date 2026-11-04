"""SQLite tables: Sheet, Region, Transcript, Field, Check, Edit (+ ModelCall for cost).

The data model mirrors the design doc. Every Darwin Core `Field` points at the
`Region` (label crop) it was read from, and every verification step is a `Check`
row, so the review UI can show provenance and the agent trace.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field as Col
from sqlmodel import SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SheetStatus(str, Enum):
    uploaded = "uploaded"  # stored, not queued for processing
    queued = "queued"
    processing = "processing"
    done = "done"
    error = "error"


class RegionKind(str, Enum):
    label = "label"
    slip = "slip"  # annotation / re-identification slip
    barcode = "barcode"
    stamp = "stamp"
    ruler = "ruler"
    color_chart = "color_chart"
    sheet = "sheet"  # whole-sheet fallback region

    @property
    def has_text(self) -> bool:
        return self in (RegionKind.label, RegionKind.slip, RegionKind.stamp, RegionKind.barcode, RegionKind.sheet)


class FieldStatus(str, Enum):
    auto = "auto"  # high confidence, not yet reviewed
    flagged = "flagged"  # needs human review
    accepted = "accepted"  # reviewer confirmed
    edited = "edited"  # reviewer changed the value


class Sheet(SQLModel, table=True):
    id: Optional[int] = Col(default=None, primary_key=True)
    image_sha256: str = Col(index=True, unique=True)
    image_path: str
    width: int
    height: int
    filename: Optional[str] = None
    source_url: Optional[str] = None
    license: Optional[str] = None
    gbif_occurrence_id: Optional[str] = Col(default=None, index=True)
    # Human transcription from GBIF, kept as ground truth for evaluation
    gbif_record: Optional[dict[str, Any]] = Col(default=None, sa_column=Column(JSON))
    status: SheetStatus = SheetStatus.queued
    stage: Optional[str] = None
    error: Optional[str] = None
    cost_usd: float = 0.0
    created_at: datetime = Col(default_factory=utcnow)
    updated_at: datetime = Col(default_factory=utcnow)


class Region(SQLModel, table=True):
    id: Optional[int] = Col(default=None, primary_key=True)
    sheet_id: int = Col(foreign_key="sheet.id", index=True)
    kind: RegionKind
    concept: str  # the SAM concept prompt (or "spark-locate" / "whole sheet")
    source: str = "sam"  # sam | spark | fallback
    # Bounding box in full-resolution sheet pixels, x2/y2 inclusive
    x1: int
    y1: int
    x2: int
    y2: int
    score: Optional[float] = None
    crop_path: Optional[str] = None
    ordinal: int = 0  # reading order among text regions (R1, R2, ...)


class Transcript(SQLModel, table=True):
    id: Optional[int] = Col(default=None, primary_key=True)
    region_id: int = Col(foreign_key="region.id", index=True)
    sheet_id: int = Col(foreign_key="sheet.id", index=True)
    text: str
    lines: list[str] = Col(default_factory=list, sa_column=Column(JSON))
    language: Optional[str] = None
    script: Optional[str] = None
    model: str
    prompt_version: str
    pass_no: int = 1  # 1 = first reading, 2 = independent second reading
    created_at: datetime = Col(default_factory=utcnow)


class Field(SQLModel, table=True):
    id: Optional[int] = Col(default=None, primary_key=True)
    sheet_id: int = Col(foreign_key="sheet.id", index=True)
    dwc_term: str = Col(index=True)
    verbatim: Optional[str] = None  # exact text on the label
    value: Optional[str] = None  # interpreted / normalized value
    model_value: Optional[str] = None  # value before any re-read or human edit
    region_id: Optional[int] = Col(default=None, foreign_key="region.id")
    span_start: Optional[int] = None  # character span of `verbatim` in the transcript
    span_end: Optional[int] = None
    derived: bool = False  # produced by a tool (e.g. acceptedNameUsage), not read from a label
    confidence: float = 0.0
    confidence_level: str = "low"  # high | medium | low
    evidence: dict[str, Any] = Col(default_factory=dict, sa_column=Column(JSON))
    status: FieldStatus = FieldStatus.flagged


class Check(SQLModel, table=True):
    id: Optional[int] = Col(default=None, primary_key=True)
    field_id: int = Col(foreign_key="field.id", index=True)
    step: int = 0  # order within the field's verification trace
    tool: str  # gbif_name_match | date_check | country_check | grounding | reread_crop | geocode | agreement
    input: dict[str, Any] = Col(default_factory=dict, sa_column=Column(JSON))
    result: dict[str, Any] = Col(default_factory=dict, sa_column=Column(JSON))
    passed: Optional[bool] = None  # None = informational step
    created_at: datetime = Col(default_factory=utcnow)


class Edit(SQLModel, table=True):
    id: Optional[int] = Col(default=None, primary_key=True)
    field_id: int = Col(foreign_key="field.id", index=True)
    action: str  # accept | edit
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    at: datetime = Col(default_factory=utcnow)


class ModelCall(SQLModel, table=True):
    """One model invocation (or cache replay); drives cost-per-sheet reporting."""

    id: Optional[int] = Col(default=None, primary_key=True)
    sheet_id: Optional[int] = Col(default=None, foreign_key="sheet.id", index=True)
    stage: str
    model: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    images: int = 0
    cost_usd: float = 0.0
    cached: bool = False
    latency_ms: int = 0
    # What the call cost / took when it was originally made (same as above unless cached);
    # lets the evaluation report true cost per sheet even when replaying from cache.
    nominal_cost_usd: float = 0.0
    nominal_latency_ms: int = 0
    created_at: datetime = Col(default_factory=utcnow)
