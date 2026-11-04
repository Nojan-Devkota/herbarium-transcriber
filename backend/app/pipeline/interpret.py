"""Stage 4 — interpret: verbatim transcripts -> Darwin Core fields with provenance.

Spark sees the numbered transcripts (R1, R2, ...) as text only. Each returned
field quotes the substring it is based on; we locate that substring in the
transcript to get a character span (for the UI highlight) and a grounding score
(1.0 = exact; lower = fuzzy; 0 = the model quoted text that isn't there).
"""

from __future__ import annotations

import logging
import re

from rapidfuzz import fuzz

from app.models import Field, FieldStatus, Region, RegionKind, Transcript
from app.pipeline.context import CallRequest, PipelineContext
from app.prompts import load_prompt
from app.schemas.darwin_core import LABEL_TERMS, InterpretedField, InterpretOut

log = logging.getLogger(__name__)

# For these terms a later determination slip supersedes the main label
IDENTIFICATION_TERMS = {"scientificName", "identifiedBy", "dateIdentified", "family"}


def locate_span(verbatim: str | None, text: str) -> tuple[float, int | None, int | None]:
    """Find `verbatim` in `text`. Returns (grounding score 0-1, start, end)."""
    if not verbatim or not text:
        return 0.0, None, None
    flat = text.replace("\n", " ")  # same length, so spans map back 1:1
    idx = flat.find(verbatim)
    if idx >= 0:
        return 1.0, idx, idx + len(verbatim)
    idx = flat.lower().find(verbatim.lower())
    if idx >= 0:
        return 0.98, idx, idx + len(verbatim)
    squashed = re.sub(r"\s+", " ", verbatim).strip()
    align = fuzz.partial_ratio_alignment(squashed, flat, score_cutoff=0)
    if align is None:
        return 0.0, None, None
    score = align.score / 100
    if score >= 0.6:
        return round(score, 3), align.dest_start, align.dest_end
    return round(score, 3), None, None


def build_transcript_block(numbered: list[tuple[int, Region, Transcript]]) -> str:
    parts = []
    for n, region, t in numbered:
        kind = "annotation slip" if region.kind == RegionKind.slip else region.kind.value
        parts.append(f"[R{n}] ({kind}; {t.script}; language={t.language})\n{t.text}")
    return "\n\n".join(parts)


def interpret_fields(ctx: PipelineContext, regions: list[Region], transcripts: dict[int, Transcript]) -> list[Field]:
    numbered: list[tuple[int, Region, Transcript]] = []
    for region in sorted((r for r in regions if r.id in transcripts), key=lambda r: r.ordinal):
        t = transcripts[region.id]
        if t.text.strip():
            numbered.append((len(numbered) + 1, region, t))
    if not numbered:
        return []

    prompt = load_prompt("interpret")
    block = build_transcript_block(numbered)
    terms = "\n".join(f"- {term}: {desc}" for term, desc in LABEL_TERMS.items())
    out: InterpretOut = ctx.structured(
        CallRequest("interpret", prompt, prompt.render(transcripts=block, terms=terms), [], InterpretOut,
                    ctx.settings.effort_interpret, (block,))
    )  # fmt: skip

    by_number = {n: (region, t) for n, region, t in numbered}
    candidates: dict[str, list[InterpretedField]] = {}
    dropped = 0
    for f in out.fields:
        if f.region not in by_number or not f.value.strip():
            dropped += 1  # unknown transcript number (hallucinated region) or empty value
            continue
        candidates.setdefault(f.term, []).append(f)
    if dropped:
        log.warning("sheet %s: dropped %d interpreted field(s) with unknown region or empty value", ctx.sheet_id, dropped)

    fields: list[Field] = []
    for term in LABEL_TERMS:  # export order
        cands = candidates.get(term)
        if not cands:
            continue
        chosen = _choose(term, cands, by_number)
        region, t = by_number[chosen.region]
        grounding, start, end = locate_span(chosen.verbatim, t.text)
        alternatives = [
            {"region_id": by_number[c.region][0].id, "verbatim": c.verbatim, "value": c.value}
            for c in cands if c is not chosen
        ]  # fmt: skip
        fields.append(
            Field(
                sheet_id=ctx.sheet_id,
                dwc_term=term,
                verbatim=chosen.verbatim,
                value=chosen.value.strip(),
                model_value=chosen.value.strip(),
                region_id=region.id,
                span_start=start,
                span_end=end,
                evidence={"grounding": grounding, **({"alternatives": alternatives} if alternatives else {})},
                status=FieldStatus.flagged,
            )
        )
    ctx.session.add_all(fields)
    ctx.session.commit()
    for f in fields:
        ctx.session.refresh(f)
    return fields


def _choose(term: str, cands: list[InterpretedField], by_number: dict[int, tuple[Region, Transcript]]) -> InterpretedField:
    if len(cands) == 1:
        return cands[0]
    if term in IDENTIFICATION_TERMS:
        slips = [c for c in cands if by_number[c.region][0].kind == RegionKind.slip]
        if slips:
            return slips[-1]  # the latest slip in reading order
    return cands[0]
