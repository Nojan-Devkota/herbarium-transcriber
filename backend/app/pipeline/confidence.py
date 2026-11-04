"""Stage 6 — confidence: evidence -> score, level and review status.

Confidence comes from evidence, not from the model's self-report:

    score = weighted mean of
        grounding            (weight 1)  quoted text found in the transcript
        each external check  (weight 1)  final result of gbif / date / country checks
        agreement^3          (weight 2)  match between two independent readings
     or AGREEMENT_PRIOR      (weight 1)  when no second reading was made

A field whose final external check failed is capped below the medium band, and
a field whose value changed on re-read is discounted slightly (it was unstable).

The second reading is *selective*: it only runs on regions holding fields that
are not already high-confidence, which keeps cost down.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from rapidfuzz import fuzz
from sqlmodel import select

from app.models import Check, Field, FieldStatus, Region, Transcript
from app.pipeline.context import PipelineContext
from app.pipeline.transcribe import transcribe_regions

AGREEMENT_PRIOR = 0.6  # an unconfirmed single reading
AGREEMENT_PASS = 0.9
INFORMATIONAL_TOOLS = {"grounding", "agreement", "reread_crop", "geocode"}


def _normalize(s: str) -> str:
    return " ".join(s.replace("\n", " ").split()).casefold()


def agreement_score(verbatim: str | None, second_reading: str) -> float | None:
    """How well the first reading of a field is reproduced by an independent second reading (0-1)."""
    if not verbatim or not verbatim.strip():
        return None
    if not second_reading.strip():
        return 0.0
    return fuzz.partial_ratio(_normalize(verbatim), _normalize(second_reading)) / 100


def score_field(field: Field, checks: list[Check], high: float, medium: float) -> tuple[float, str, dict[str, Any]]:
    ev = field.evidence or {}
    parts: list[tuple[str, float, float]] = [("grounding", 1.0, float(ev.get("grounding", 0.0)))]

    final: dict[str, bool] = {}
    for c in sorted(checks, key=lambda c: c.step):
        if c.tool not in INFORMATIONAL_TOOLS and c.passed is not None:
            final[c.tool] = c.passed  # last result per tool wins (after re-reads)
    for tool, passed in final.items():
        parts.append((tool, 1.0, 1.0 if passed else 0.0))

    agreement = ev.get("agreement")
    if agreement is None:
        parts.append(("agreement_prior", 1.0, AGREEMENT_PRIOR))
    else:
        parts.append(("agreement", 2.0, float(agreement) ** 3))

    score = sum(w * v for _, w, v in parts) / sum(w for _, w, _ in parts)
    failed = [tool for tool, passed in final.items() if not passed]
    if failed:
        score = min(score, medium - 0.05)
    if ev.get("reread_changed"):
        score *= 0.9
    if ev.get("verification_unavailable"):  # e.g. GBIF was down: the external check never ran
        score = min(score, medium - 0.05)
    score = round(max(0.0, min(1.0, score)), 3)
    level = "high" if score >= high else "medium" if score >= medium else "low"
    components = {name: round(v, 3) for name, _, v in parts}
    if failed:
        components["failed_checks"] = failed
    if ev.get("verification_unavailable"):
        components["verification_unavailable"] = True
    return score, level, components


def apply_confidence(
    ctx: PipelineContext,
    fields: list[Field],
    regions: list[Region],
    pass1: dict[int, Transcript],
) -> None:
    s = ctx.settings
    label_fields = [f for f in fields if not f.derived]
    if not label_fields:
        return
    ids = [f.id for f in label_fields]
    checks: dict[int, list[Check]] = defaultdict(list)
    for c in ctx.session.exec(select(Check).where(Check.field_id.in_(ids))):  # type: ignore[union-attr]
        checks[c.field_id].append(c)

    prelim = {f.id: score_field(f, checks[f.id], s.confidence_high, s.confidence_medium) for f in label_fields}

    if ctx.options.use_second_pass:
        need = {f.region_id for f in label_fields if prelim[f.id][1] != "high" and f.region_id is not None}
        targets = [r for r in regions if r.id in need]
        if targets:
            pass2 = transcribe_regions(ctx, targets, pass_no=2)
            for f in label_fields:
                if f.region_id not in pass2:
                    continue
                second = pass2[f.region_id].text
                a = agreement_score(f.verbatim, second)
                if a is None:
                    continue
                f.evidence = {**(f.evidence or {}), "agreement": round(a, 3)}
                c = Check(
                    field_id=f.id,
                    step=max((x.step for x in checks[f.id]), default=0) + 1,
                    tool="agreement",
                    input={"first_reading": f.verbatim},
                    result={"score": round(a, 3), "second_reading": second[:600]},
                    passed=a >= AGREEMENT_PASS,
                )
                ctx.session.add(c)
                checks[f.id].append(c)

    for f in label_fields:
        score, level, components = score_field(f, checks[f.id], s.confidence_high, s.confidence_medium)
        f.confidence = score
        f.confidence_level = level
        f.evidence = {**(f.evidence or {}), "components": components}
        f.status = FieldStatus.auto if level == "high" else FieldStatus.flagged
        ctx.session.add(f)
    ctx.session.commit()
