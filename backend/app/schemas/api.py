"""Request and response bodies for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Ablation = Literal["full", "sam+verify", "sam-crops", "whole-sheet"]


class SheetSummary(BaseModel):
    id: int
    filename: str | None
    source_url: str | None
    license: str | None
    gbif_occurrence_id: str | None
    status: str
    stage: str | None
    error: str | None
    width: int
    height: int
    cost_usd: float
    created_at: datetime
    updated_at: datetime
    scientific_name: str | None = None
    fields_total: int = 0
    fields_flagged: int = 0


class RegionOut(BaseModel):
    id: int
    kind: str
    concept: str
    source: str
    x1: int
    y1: int
    x2: int
    y2: int
    score: float | None
    ordinal: int
    has_crop: bool


class TranscriptOut(BaseModel):
    id: int
    region_id: int
    pass_no: int
    text: str
    language: str | None
    script: str | None
    model: str
    prompt_version: str


class CheckOut(BaseModel):
    id: int
    step: int
    tool: str
    input: dict[str, Any]
    result: dict[str, Any]
    passed: bool | None


class FieldOut(BaseModel):
    id: int
    dwc_term: str
    verbatim: str | None
    value: str | None
    model_value: str | None
    region_id: int | None
    span_start: int | None
    span_end: int | None
    derived: bool
    confidence: float
    confidence_level: str
    status: str
    evidence: dict[str, Any]
    checks: list[CheckOut] = []


class SheetDetail(BaseModel):
    sheet: SheetSummary
    regions: list[RegionOut]
    transcripts: list[TranscriptOut]
    fields: list[FieldOut]
    ground_truth: dict[str, Any] | None = None


class UploadResult(BaseModel):
    id: int | None
    filename: str | None
    created: bool
    error: str | None = None


class GbifBatchRequest(BaseModel):
    dataset_key: str | None = None
    scientific_name: str | None = None
    taxon_key: int | None = None
    year: str | None = Field(default=None, description="Year or range, e.g. '1850,1950'")
    limit: int = Field(default=10, ge=1, le=50)
    process: bool = True


class GbifBatchResult(BaseModel):
    added: list[int]
    existing: list[int]
    skipped: list[dict[str, Any]]


class FieldPatch(BaseModel):
    action: Literal["accept", "edit"]
    value: str | None = None


class ProcessRequest(BaseModel):
    ablation: Ablation = "full"


class Stats(BaseModel):
    sheets_total: int
    sheets_by_status: dict[str, int]
    total_cost_usd: float
    avg_cost_per_sheet_usd: float | None
    avg_model_seconds_per_sheet: float | None
    model_calls: int
    cached_calls: int
    fields_total: int
    fields_by_status: dict[str, int]
    fields_by_confidence: dict[str, int]
    share_needing_review: float | None
    # Calibration from human review: how often reviewers changed a value, per confidence level
    edit_rate_by_confidence: dict[str, float | None]
