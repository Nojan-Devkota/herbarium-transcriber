"""Stage 3 — transcribe: verbatim reading of each text crop.

Pass 1 reads every text region. Pass 2 is an independent proofreading reading
with a different prompt; the confidence stage runs it only for regions holding
low-confidence fields and scores agreement between the two readings.
"""

from __future__ import annotations

from pathlib import Path

from app.models import Region, RegionKind, Transcript
from app.pipeline.context import CallRequest, PipelineContext
from app.prompts import load_prompt
from app.schemas.transcript import TranscriptOut

KIND_WORDS = {
    RegionKind.label: "label",
    RegionKind.slip: "annotation slip",
    RegionKind.stamp: "stamp",
    RegionKind.barcode: "barcode label",
    RegionKind.sheet: "sheet (the whole sheet, which may contain several labels)",
}


def transcribe_regions(ctx: PipelineContext, regions: list[Region], pass_no: int = 1) -> dict[int, Transcript]:
    prompt = load_prompt("transcribe" if pass_no == 1 else "transcribe_pass2")
    todo = [r for r in regions if r.kind.has_text and r.crop_path]
    reqs = [
        CallRequest(
            stage=f"transcribe{pass_no}",
            prompt=prompt,
            rendered=prompt.render(kind=KIND_WORDS.get(r.kind, "label")),
            images=[Path(r.crop_path).read_bytes()],
            model_cls=TranscriptOut,
            effort=ctx.settings.effort_transcribe,
            key_extra=(r.x1, r.y1, r.x2, r.y2, ctx.settings.crop_max_side, ctx.settings.crop_padding),
        )
        for r in todo
    ]
    outputs: list[TranscriptOut] = ctx.structured_many(reqs)
    transcripts: dict[int, Transcript] = {}
    for region, out in zip(todo, outputs):
        lines = [ln.rstrip() for ln in out.lines]
        t = Transcript(
            region_id=region.id,
            sheet_id=ctx.sheet_id,
            text="\n".join(lines),
            lines=lines,
            language=out.language,
            script=out.script,
            model=ctx.settings.model_spark if ctx.client.name == "meta" else ctx.client.name,
            prompt_version=prompt.tag,
            pass_no=pass_no,
        )
        ctx.session.add(t)
        transcripts[region.id] = t
    ctx.session.commit()
    for t in transcripts.values():
        ctx.session.refresh(t)
    return transcripts
