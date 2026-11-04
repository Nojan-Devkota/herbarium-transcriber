"""Shared state for one pipeline run over one sheet.

Stages receive a `PipelineContext` and never talk to the model client, cache or
cost log directly. Every model call goes through `structured_many()` /
`segment()`, which (1) checks the content-hash cache, (2) calls the model
in parallel where possible, and (3) logs a `ModelCall` row (cost 0 on a cache hit)
on the main thread, since SQLite sessions are not thread-safe.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
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
from app.tools.gbif import GbifClient, shared_gbif
from app.tools.geocode import Geocoder, shared_geocoder

T = TypeVar("T", bound=BaseModel)
log = logging.getLogger(__name__)

SEGMENT_CACHE_VERSION = "sam-concepts@v1"


@dataclass(frozen=True)
class RunOptions:
    """Pipeline switches; the evaluation ablations toggle these."""

    use_sam: bool = True  # False = whole sheet straight to Spark (ablation 1)
    use_verify: bool = True  # GBIF / date / country checks + bounded re-reads
    # Independent second reading of low-confidence fields. Off by default: on the gold set it
    # cost ~$0.016 and ~23 s per sheet with no accuracy gain (eval/report.md). Opt-in ablation.
    use_second_pass: bool = False
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
        self.gbif = gbif or shared_gbif(self.cache)
        self.geocoder = geocoder
        if self.geocoder is None and settings.geocode_enabled:
            self.geocoder = shared_geocoder(settings.geocode_user_agent, self.cache)
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
                nominal_cost_usd=usage.cost_usd,
                nominal_latency_ms=usage.latency_ms,
            )
        )

    def release_db(self) -> None:
        """End any open write transaction before slow network I/O.

        SQLite allows one writer at a time; a session that autoflushed and then
        waited 10+ s on a model call would block every other worker thread
        ("database is locked"). Committing first keeps write locks short.
        """
        if self.session.in_transaction():
            self.session.commit()

    def _key(self, stage: str, version: str, *extra: Any) -> str:
        # Client name is part of the key so mock output never pollutes the real cache.
        return cache_key(self.sheet_sha, stage, version, self.client.name, *extra)

    # --- shared plumbing -------------------------------------------------------------
    def _run_parallel(self, stage_of: Callable[[int], str], jobs: list[Callable[[], tuple[Any, Usage, bool]]]) -> list[Any]:
        """Run independent model calls concurrently; log every finished call even if another fails."""
        self.release_db()  # no write lock may be held during network I/O
        if len(jobs) == 1:
            outcomes: list[Any] = []
            try:
                outcomes.append(jobs[0]())
            except Exception as exc:  # noqa: BLE001 - re-raised below after bookkeeping
                outcomes.append(exc)
        else:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(jobs))) as pool:
                futures = [pool.submit(job) for job in jobs]
                outcomes = []
                for fut in futures:
                    try:
                        outcomes.append(fut.result())
                    except Exception as exc:  # noqa: BLE001
                        outcomes.append(exc)
        # Record on this thread: SQLite sessions are not thread-safe. Paid calls are logged
        # even when a sibling call failed, so cost per sheet is never under-reported.
        for i, out in enumerate(outcomes):
            if not isinstance(out, Exception):
                _data, usage, hit = out
                self.record(stage_of(i), usage, cached=hit)
        if errors := [o for o in outcomes if isinstance(o, Exception)]:
            raise errors[0]
        return [out[0] for out in outcomes]

    def _cached(self, stage: str, key: str, parse: Callable[[dict[str, Any]], Any]) -> tuple[Any, Usage] | None:
        hit = self.cache.get(stage, key)
        if hit is None:
            return None
        try:
            return parse(hit), Usage.from_dict(hit["usage"])
        except (KeyError, TypeError, ValueError) as exc:  # entry from an older schema: treat as a miss
            log.warning("ignoring stale %s cache entry %s: %s", stage, key[:12], exc)
            return None

    # --- SAM ---------------------------------------------------------------------
    def segment_many(self, concepts: list[str], image_jpeg: bytes, key_extra: tuple[Any, ...] = ()) -> list[SegmentResult]:
        """One SAM request per concept (the API takes one noun phrase each), run concurrently."""

        def job(concept: str) -> Callable[[], tuple[SegmentResult, Usage, bool]]:
            def run() -> tuple[SegmentResult, Usage, bool]:
                key = self._key("segment", SEGMENT_CACHE_VERSION, self.settings.model_sam, concept, *key_extra)
                parse = lambda h: SegmentResult([Detection(**d) for d in h["detections"]], h["width"], h["height"], Usage.from_dict(h["usage"]))  # noqa: E731
                if (hit := self._cached("segment", key, parse)) is not None:
                    return hit[0], hit[1], True
                result = self.client.segment(image_jpeg, concept)
                self.cache.set("segment", key, {
                    "detections": [d.__dict__ for d in result.detections],
                    "width": result.width, "height": result.height, "usage": result.usage.to_dict(),
                })  # fmt: skip
                return result, result.usage, False

            return run

        return self._run_parallel(lambda _i: "segment", [job(c) for c in concepts])

    def segment(self, concept: str, image_jpeg: bytes, key_extra: tuple[Any, ...] = ()) -> SegmentResult:
        return self.segment_many([concept], image_jpeg, key_extra)[0]

    # --- Spark -------------------------------------------------------------------
    def structured(self, req: CallRequest) -> Any:
        return self.structured_many([req])[0]

    def structured_many(self, reqs: list[CallRequest]) -> list[Any]:
        if not reqs:
            return []

        def job(req: CallRequest) -> Callable[[], tuple[Any, Usage, bool]]:
            def run() -> tuple[Any, Usage, bool]:
                key = self._key(req.stage, req.prompt.tag, self.settings.model_spark, req.effort, *req.key_extra)
                if (hit := self._cached(req.stage, key, lambda h: req.model_cls.model_validate(h["data"]))) is not None:
                    return hit[0], hit[1], True
                res = self.client.structured(req.rendered, req.images, req.model_cls, effort=req.effort)
                self.cache.set(req.stage, key, {"data": res.data.model_dump(), "usage": res.usage.to_dict()})
                return res.data, res.usage, False

            return run

        return self._run_parallel(lambda i: reqs[i].stage, [job(r) for r in reqs])
