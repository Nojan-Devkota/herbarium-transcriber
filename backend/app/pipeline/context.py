"""Shared state for one pipeline run over one sheet.

Stages receive a `PipelineContext` and never talk to the model client, cache or
cost log directly. Every model call goes through `structured_many()` /
`segment()`, which (1) checks the content-hash cache, (2) calls the model
in parallel where possible, and (3) logs a `ModelCall` row (cost 0 on a cache hit)
on the main thread, since SQLite sessions are not thread-safe.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlmodel import Session

from app.cache import Cache, cache_key
from app.clients.base import Detection, LLMClient, SegmentResult, Usage
from app.config import Settings
from app.models import ModelCall, Sheet
from app.prompts import Prompt
from app.tools.gbif import GbifClient
from app.tools.geocode import Geocoder

T = TypeVar("T", bound=BaseModel)

SEGMENT_CACHE_VERSION = "sam-concepts@v1"


@dataclass(frozen=True)
class RunOptions:
    """Pipeline switches; the evaluation ablations toggle these."""

    use_sam: bool = True  # False = whole sheet straight to Spark (ablation 1)
    use_verify: bool = True  # GBIF / date / country checks + bounded re-reads
    use_second_pass: bool = True  # independent second reading for low-confidence fields
    use_cache: bool = True

    @property
    def label(self) -> str:
        if not self.use_sam:
            return "whole-sheet"
        if not self.use_verify:
            return "sam-crops"
        if not self.use_second_pass:
            return "sam+verify"
        return "full"


@dataclass
class CallRequest:
    stage: str
    prompt: Prompt
    rendered: str
    images: list[bytes]
    model_cls: type[BaseModel]
    effort: str
    key_extra: tuple[Any, ...] = field(default_factory=tuple)


class PipelineContext:
    def __init__(
        self,
        session: Session,
        sheet: Sheet,
        client: LLMClient,
        settings: Settings,
        options: RunOptions | None = None,
        cache: Cache | None = None,
        gbif: GbifClient | None = None,
        geocoder: Geocoder | None = None,
        max_workers: int = 4,
    ):
        self.session = session
        self.sheet = sheet
        # Plain copies: worker threads must never touch (possibly expired) ORM attributes
        self.sheet_id: int = sheet.id  # type: ignore[assignment]
        self.sheet_sha: str = sheet.image_sha256
        self.client = client
        self.settings = settings
        self.options = options or RunOptions()
        self.cache = cache or Cache(settings.cache_dir, enabled=self.options.use_cache)
        self.gbif = gbif or GbifClient(cache=self.cache)
        self.geocoder = geocoder
        if self.geocoder is None and settings.geocode_enabled:
            self.geocoder = Geocoder(settings.geocode_user_agent, cache=self.cache)
        self.max_workers = max_workers

    # --- bookkeeping ---------------------------------------------------------
    def record(self, stage: str, usage: Usage, cached: bool) -> None:
        self.session.add(
            ModelCall(
                sheet_id=self.sheet_id,
                stage=stage,
                model=usage.model,
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                images=usage.images,
                cost_usd=0.0 if cached else usage.cost_usd,
                cached=cached,
                latency_ms=0 if cached else usage.latency_ms,
            )
        )

    def _key(self, stage: str, version: str, *extra: Any) -> str:
        # Client name is part of the key so mock output never pollutes the real cache.
        return cache_key(self.sheet_sha, stage, version, self.client.name, *extra)

    # --- SAM ---------------------------------------------------------------------
    def segment(self, concept: str, image_jpeg: bytes, key_extra: tuple[Any, ...] = ()) -> SegmentResult:
        key = self._key("segment", SEGMENT_CACHE_VERSION, self.settings.model_sam, concept, *key_extra)
        if (hit := self.cache.get("segment", key)) is not None:
            usage = Usage.from_dict(hit["usage"])
            self.record("segment", usage, cached=True)
            return SegmentResult([Detection(**d) for d in hit["detections"]], hit["width"], hit["height"], usage)
        result = self.client.segment(image_jpeg, concept)
        self.cache.set(
            "segment",
            key,
            {
                "detections": [d.__dict__ for d in result.detections],
                "width": result.width,
                "height": result.height,
                "usage": result.usage.to_dict(),
            },
        )
        self.record("segment", result.usage, cached=False)
        return result

    # --- Spark -------------------------------------------------------------------
    def structured(self, req: CallRequest) -> Any:
        return self.structured_many([req])[0]

    def structured_many(self, reqs: list[CallRequest]) -> list[Any]:
        if not reqs:
            return []

        def compute(req: CallRequest) -> tuple[Any, Usage, bool]:
            key = self._key(req.stage, req.prompt.tag, self.settings.model_spark, req.effort, *req.key_extra)
            if (hit := self.cache.get(req.stage, key)) is not None:
                return req.model_cls.model_validate(hit["data"]), Usage.from_dict(hit["usage"]), True
            res = self.client.structured(req.rendered, req.images, req.model_cls, effort=req.effort)
            self.cache.set(req.stage, key, {"data": res.data.model_dump(), "usage": res.usage.to_dict()})
            return res.data, res.usage, False

        if len(reqs) == 1:
            results = [compute(reqs[0])]
        else:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(reqs))) as pool:
                results = list(pool.map(compute, reqs))
        for req, (_data, usage, hit) in zip(reqs, results):
            self.record(req.stage, usage, cached=hit)
        return [data for data, _u, _h in results]
