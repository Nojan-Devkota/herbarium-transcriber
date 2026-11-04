"""Muse Spark + SAM 3.1 through the Meta Model API (OpenAI-compatible, api.meta.ai/v1).

- SAM 3.1: Responses API, model `sam-3.1`, one noun-phrase concept + one image per
  request. Output is special-token text, e.g.
  `<0f>0<|box;x1=..;y1=..;x2=..;y2=..;w=..;h=..;c=0.96|><|mask;...|>,1<|box;...|>...`
  We only need boxes (the crops are rectangles), so masks are requested in the
  compact `one_bit` encoding and ignored.
- Muse Spark: Chat Completions with `response_format: json_schema` and a
  `reasoning_effort` per call type.
"""

from __future__ import annotations

import re
import time
from typing import Any

from openai import OpenAI

from app.clients.base import Detection, LLMClient, SegmentResult, Usage, jpeg_data_url
from app.config import Settings

_OBJECT_RE = re.compile(r"(\d+)<\|box;([^|]*)\|>")
_FRAME_RE = re.compile(r"<(\d+)f>")


def _kv(token_body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in token_body.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_sam_output(text: str, frame: int = 0) -> tuple[list[Detection], int | None, int | None]:
    """Parse SAM 3.1 special-token output into boxes for one frame.

    Returns (detections, media_width, media_height). Width/height are None when
    there were no matches (an empty output is a valid "zero matches" result).
    """
    detections: list[Detection] = []
    width = height = None
    # Split into frame sections; an image has a single <0f> section.
    sections: list[tuple[int, str]] = []
    marks = list(_FRAME_RE.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        sections.append((int(m.group(1)), text[m.end() : end]))
    if not marks and text.strip():
        sections.append((0, text))
    for idx, body in sections:
        if idx != frame:
            continue
        for _obj_id, box_body in _OBJECT_RE.findall(body):
            kv = _kv(box_body)
            try:
                det = Detection(
                    x1=int(kv["x1"]),
                    y1=int(kv["y1"]),
                    x2=int(kv["x2"]),
                    y2=int(kv["y2"]),
                    score=float(kv["c"]) if "c" in kv else None,
                )
            except (KeyError, ValueError):
                continue  # skip a malformed record rather than failing the sheet
            width = int(kv.get("w", width or 0)) or width
            height = int(kv.get("h", height or 0)) or height
            detections.append(det)
    return detections, width, height


class MetaModelClient(LLMClient):
    name = "meta"

    def __init__(self, settings: Settings):
        if not settings.meta_api_key:
            raise RuntimeError("META_API_KEY is not set; put it in .env or use LLM_CLIENT=mock")
        self.settings = settings
        self._client = OpenAI(
            base_url=settings.meta_base_url,
            api_key=settings.meta_api_key,
            timeout=settings.request_timeout_s,
            max_retries=3,  # SDK retries 429/5xx with backoff
        )

    # --- SAM 3.1 -----------------------------------------------------------
    def segment(self, image_jpeg: bytes, concept: str) -> SegmentResult:
        t0 = time.perf_counter()
        resp = self._client.responses.create(
            model=self.settings.model_sam,
            input=[
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": concept},
                        {"type": "input_image", "image_url": jpeg_data_url(image_jpeg)},
                    ],
                }
            ],
            metadata={"include_confidence": "true", "mask_encoding": "one_bit"},
        )
        text = resp.output_text or ""
        detections, w, h = parse_sam_output(text)
        usage = Usage(
            model=self.settings.model_sam,
            images=1,
            cost_usd=self.settings.price_sam_per_image,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
        return SegmentResult(detections=detections, width=w or 0, height=h or 0, usage=usage, raw=text[:2000])

    # --- Muse Spark ----------------------------------------------------------
    def complete_json(
        self,
        prompt: str,
        images: list[bytes],
        schema: dict[str, Any],
        schema_name: str,
        effort: str,
    ) -> tuple[str, Usage]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content += [{"type": "image_url", "image_url": {"url": jpeg_data_url(img)}} for img in images]
        t0 = time.perf_counter()
        resp = self._client.chat.completions.create(
            model=self.settings.model_spark,
            messages=[{"role": "user", "content": content}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema},
            },
            reasoning_effort=effort,  # type: ignore[arg-type]
        )
        raw = resp.choices[0].message.content or ""
        return raw, self._usage(resp, t0)

    def _usage(self, resp: Any, t0: float) -> Usage:
        s = self.settings
        u = getattr(resp, "usage", None)
        prompt_tokens = getattr(u, "prompt_tokens", 0) or 0
        completion_tokens = getattr(u, "completion_tokens", 0) or 0
        details = getattr(u, "prompt_tokens_details", None)
        cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
        cost = (
            (prompt_tokens - cached) * s.price_spark_input_per_m
            + cached * s.price_spark_cached_input_per_m
            + completion_tokens * s.price_spark_output_per_m
        ) / 1_000_000
        return Usage(
            model=s.model_spark,
            input_tokens=prompt_tokens,
            cached_input_tokens=cached,
            output_tokens=completion_tokens,
            cost_usd=cost,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
