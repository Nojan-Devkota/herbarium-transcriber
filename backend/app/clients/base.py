"""The one interface every pipeline stage talks to.

`LLMClient` hides whether we're calling the Meta Model API or the offline mock.
Structured calls are validated against a Pydantic model; invalid JSON is
rejected and retried exactly once (design doc: "Strict schemas").
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class InvalidModelOutput(RuntimeError):
    """The model returned output that failed schema validation twice."""


@dataclass
class Detection:
    """One SAM match, in the pixel space of the image that was sent."""

    x1: int
    y1: int
    x2: int
    y2: int
    score: float | None = None


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    images: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            model=self.model,
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            images=self.images + other.images,
            cost_usd=self.cost_usd + other.cost_usd,
            latency_ms=self.latency_ms + other.latency_ms,
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Usage:
        return cls(**d)


@dataclass
class SegmentResult:
    detections: list[Detection]
    width: int
    height: int
    usage: Usage
    raw: str = ""


@dataclass
class StructuredResult(Generic[T]):
    data: T
    usage: Usage
    raw: str = ""
    attempts: int = 1
    errors: list[str] = field(default_factory=list)


def jpeg_data_url(image_jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(image_jpeg).decode("ascii")


class LLMClient(ABC):
    name: str = "base"

    @abstractmethod
    def segment(self, image_jpeg: bytes, concept: str) -> SegmentResult:
        """SAM 3.1 concept segmentation: every instance of `concept` as a box."""

    @abstractmethod
    def complete_json(
        self,
        prompt: str,
        images: list[bytes],
        schema: dict[str, Any],
        schema_name: str,
        effort: str,
    ) -> tuple[str, Usage]:
        """Muse Spark call constrained to `schema`; returns raw JSON text and usage."""

    def structured(
        self,
        prompt: str,
        images: list[bytes],
        model_cls: type[T],
        effort: str = "low",
        schema_name: str | None = None,
    ) -> StructuredResult[T]:
        name = schema_name or model_cls.__name__.lower()
        schema = model_cls.model_json_schema()
        raw, usage = self.complete_json(prompt, images, schema, name, effort)
        try:
            return StructuredResult(model_cls.model_validate_json(raw), usage, raw)
        except ValidationError as first_error:
            err1 = _short_error(first_error)
        retry_prompt = (
            f"{prompt}\n\nYour previous reply did not match the required JSON schema "
            f"({err1}). Reply again with JSON that matches the schema exactly."
        )
        raw2, usage2 = self.complete_json(retry_prompt, images, schema, name, effort)
        total = usage + usage2
        try:
            return StructuredResult(model_cls.model_validate_json(raw2), total, raw2, attempts=2, errors=[err1])
        except ValidationError as second_error:
            raise InvalidModelOutput(
                f"{name}: invalid output after retry: {_short_error(second_error)}"
            ) from second_error


def _short_error(err: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}" for e in err.errors()[:3])
