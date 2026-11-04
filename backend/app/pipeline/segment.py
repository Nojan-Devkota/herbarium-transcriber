"""Stage 2 — segment: SAM 3.1 concept prompts -> Regions + full-resolution crops.

Labels are a small part of a large sheet, so reading full-resolution crops beats
reading the whole image, and each crop becomes the provenance anchor for the
fields read from it.

Flow:
1. Downscale the sheet to `sam_max_side` and send one SAM request per concept
   (SAM takes one noun phrase per request).
2. Scale boxes back to full resolution, drop low scores, suppress duplicates
   (overlapping label/slip boxes, text blocks nested inside a larger label).
3. If SAM finds no text region, ask Spark to locate labels (risk fallback).
   If that also fails, or SAM is disabled (ablation), use the whole sheet.
4. Number text regions in reading order and save padded crops.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

from app.models import Region, RegionKind
from app.pipeline.context import CallRequest, PipelineContext
from app.pipeline.ingest import downscale, load_image, to_jpeg
from app.prompts import load_prompt
from app.schemas.transcript import LocateOut

# (SAM concept prompt, region kind). One SAM request per concept.
CONCEPTS: list[tuple[str, RegionKind]] = [
    ("label", RegionKind.label),
    ("annotation slip", RegionKind.slip),
    ("barcode", RegionKind.barcode),
    ("stamp", RegionKind.stamp),
    ("ruler", RegionKind.ruler),
    ("color chart", RegionKind.color_chart),
]

# Boxes in the same group compete in non-maximum suppression.
_GROUP = {
    RegionKind.label: "text",
    RegionKind.slip: "text",
    RegionKind.stamp: "text",
    RegionKind.barcode: "barcode",
    RegionKind.ruler: "scale",
    RegionKind.color_chart: "scale",
    RegionKind.sheet: "sheet",
}
_LOCATE_KIND = {"label": RegionKind.label, "slip": RegionKind.slip, "barcode": RegionKind.barcode, "stamp": RegionKind.stamp}


@dataclass
class Box:
    kind: RegionKind
    concept: str
    source: str
    x1: int
    y1: int
    x2: int
    y2: int
    score: float | None = None

    @property
    def area(self) -> int:
        return max(0, self.x2 - self.x1 + 1) * max(0, self.y2 - self.y1 + 1)

    def intersection(self, other: Box) -> int:
        w = min(self.x2, other.x2) - max(self.x1, other.x1) + 1
        h = min(self.y2, other.y2) - max(self.y1, other.y1) + 1
        return max(0, w) * max(0, h)

    def iou(self, other: Box) -> float:
        inter = self.intersection(other)
        union = self.area + other.area - inter
        return inter / union if union else 0.0

    def inside_fraction(self, other: Box) -> float:
        """Fraction of this box's area that lies inside `other`."""
        return self.intersection(other) / self.area if self.area else 0.0


def dedupe_boxes(boxes: list[Box], width: int, height: int, iou_threshold: float = 0.6) -> list[Box]:
    sheet_area = width * height
    # A "label" covering most of the sheet is the sheet itself, not a label.
    boxes = [b for b in boxes if b.area > 0 and not (_GROUP[b.kind] == "text" and b.area > 0.6 * sheet_area)]
    # 1) NMS within each group, highest score first
    kept: list[Box] = []
    for b in sorted(boxes, key=lambda b: -(b.score or 0.0)):
        dup = next((k for k in kept if _GROUP[k.kind] == _GROUP[b.kind] and k.iou(b) > iou_threshold), None)
        if dup is None:
            kept.append(b)
        elif b.kind == RegionKind.label and dup.kind == RegionKind.slip:
            # SAM often scores the main label higher as "annotation slip"; when both
            # concepts hit the same box, it is the label (score picks the geometry).
            dup.kind, dup.concept = RegionKind.label, "label"
    # 2) Text blocks nested inside a larger text region are already in that crop
    result = []
    for b in kept:
        nested = any(
            o is not b and _GROUP[o.kind] == "text" and _GROUP[b.kind] in ("text", "barcode")
            and o.area > b.area and b.inside_fraction(o) >= 0.85
            for o in kept
        )  # fmt: skip
        if not nested:
            result.append(b)
    return result


def reading_order(boxes: list[Box]) -> list[Box]:
    """Top-to-bottom, then left-to-right, with a tolerance band for rows."""
    if not boxes:
        return []
    band = max(1, int(sum(b.y2 - b.y1 for b in boxes) / len(boxes) / 2))
    return sorted(boxes, key=lambda b: (b.y1 // band, b.x1))


def _sam_boxes(ctx: PipelineContext, sheet_img: Image.Image) -> list[Box]:
    s = ctx.settings
    small = downscale(sheet_img, s.sam_max_side)
    jpeg = to_jpeg(small, quality=88)
    sx, sy = sheet_img.width / small.width, sheet_img.height / small.height
    boxes: list[Box] = []
    results = ctx.segment_many([c for c, _k in CONCEPTS], jpeg, key_extra=(s.sam_max_side,))
    for (concept, kind), res in zip(CONCEPTS, results):
        for d in res.detections:
            if d.score is not None and d.score < s.sam_min_score:
                continue
            boxes.append(
                Box(
                    kind, concept, "sam",
                    int(d.x1 * sx), int(d.y1 * sy),
                    min(sheet_img.width - 1, int((d.x2 + 1) * sx) - 1),
                    min(sheet_img.height - 1, int((d.y2 + 1) * sy) - 1),
                    d.score,
                )
            )  # fmt: skip
    return boxes


def _spark_locate(ctx: PipelineContext, sheet_img: Image.Image) -> list[Box]:
    prompt = load_prompt("locate")
    jpeg = to_jpeg(sheet_img, max_side=ctx.settings.sam_max_side)
    out: LocateOut = ctx.structured(
        CallRequest("locate", prompt, prompt.render(), [jpeg], LocateOut, ctx.settings.effort_locate, (ctx.settings.sam_max_side,))
    )
    W, H = sheet_img.size
    boxes = []
    for r in out.regions:
        x1, y1, x2, y2 = (max(0, min(1000, v)) for v in r.box)
        if x2 <= x1 or y2 <= y1:
            continue
        boxes.append(
            Box(_LOCATE_KIND[r.kind], "spark-locate", "spark", int(x1 / 1000 * W), int(y1 / 1000 * H),
                min(W - 1, int(x2 / 1000 * W)), min(H - 1, int(y2 / 1000 * H)))
        )  # fmt: skip
    return boxes


def save_crop(ctx: PipelineContext, sheet_img: Image.Image, box: Box, ordinal: int) -> str:
    s = ctx.settings
    pad = int(s.crop_padding * max(sheet_img.size))
    x1, y1 = max(0, box.x1 - pad), max(0, box.y1 - pad)
    x2, y2 = min(sheet_img.width, box.x2 + 1 + pad), min(sheet_img.height, box.y2 + 1 + pad)
    crop = sheet_img.crop((x1, y1, x2, y2))
    path = s.crops_dir / f"{ctx.sheet_sha[:16]}_{ordinal:02d}_{box.kind.value}.jpg"
    path.write_bytes(to_jpeg(crop, max_side=s.crop_max_side, quality=90))
    return str(path)


def segment_sheet(ctx: PipelineContext) -> list[Region]:
    sheet_img = load_image(ctx.sheet.image_path)
    W, H = sheet_img.size
    boxes: list[Box] = []
    if ctx.options.use_sam:
        boxes = dedupe_boxes(_sam_boxes(ctx, sheet_img), W, H)
        if not any(_GROUP[b.kind] == "text" for b in boxes):
            boxes += dedupe_boxes(_spark_locate(ctx, sheet_img), W, H)
    if not any(_GROUP[b.kind] == "text" for b in boxes):
        boxes.append(Box(RegionKind.sheet, "whole sheet", "fallback", 0, 0, W - 1, H - 1))

    text_boxes = reading_order([b for b in boxes if b.kind.has_text])
    other_boxes = [b for b in boxes if not b.kind.has_text]
    regions: list[Region] = []
    for ordinal, b in enumerate(text_boxes, start=1):
        regions.append(_region(ctx, b, ordinal, crop_path=save_crop(ctx, sheet_img, b, ordinal)))
    for b in other_boxes:
        regions.append(_region(ctx, b, 0, crop_path=None))
    ctx.session.add_all(regions)
    ctx.session.commit()
    for r in regions:
        ctx.session.refresh(r)
    return regions


def _region(ctx: PipelineContext, b: Box, ordinal: int, crop_path: str | None) -> Region:
    return Region(
        sheet_id=ctx.sheet_id, kind=b.kind, concept=b.concept, source=b.source,
        x1=b.x1, y1=b.y1, x2=b.x2, y2=b.y2, score=b.score, crop_path=crop_path, ordinal=ordinal,
    )  # fmt: skip
