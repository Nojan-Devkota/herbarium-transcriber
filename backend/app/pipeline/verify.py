"""Stage 5 — verify: the bounded verification agent.

For each field the agent runs the checks that apply, and when an external check
fails it asks Spark a *targeted* question about the crop ("is the year 1897 or
1891?") instead of retrying the whole sheet. At most `max_rereads_per_field`
re-reads per field keeps cost predictable. Every tool call is a `Check` row, so
the UI can replay the agent's trace.

Tools:
- grounding       — is the field's quoted text actually in the transcript?
- gbif_name_match — is scientificName in the GBIF Backbone? synonym -> accepted name
- date_check      — valid ISO, plausible year, consistent with the label text,
                    not collected after it was identified
- country_check   — does the country resolve to an ISO 3166 code?
- reread_crop     — Spark re-reads the crop to answer one question
- geocode         — (optional) locality -> coordinates + uncertainty
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from app.models import Check, Field, FieldStatus, Region, Transcript
from app.pipeline.context import CallRequest, PipelineContext
from app.prompts import load_prompt
from app.schemas.transcript import RereadOut
from app.tools.dates import date_check, parse_verbatim_date
from app.tools.gbif import name_check_passed
from app.tools.geocode import country_lookup

log = logging.getLogger(__name__)

GROUNDING_PASS = 0.8


class Trace:
    """Appends numbered Check rows for one field."""

    def __init__(self, ctx: PipelineContext, field: Field):
        self.ctx, self.field, self.step = ctx, field, 0

    def add(self, tool: str, input: dict[str, Any], result: dict[str, Any], passed: bool | None) -> Check:
        self.step += 1
        check = Check(field_id=self.field.id, step=self.step, tool=tool, input=input, result=result, passed=passed)
        self.ctx.session.add(check)
        return check


def _norm(s: str | None) -> str:
    return " ".join((s or "").split()).casefold()


def _set_evidence(field: Field, **updates: Any) -> None:
    field.evidence = {**(field.evidence or {}), **updates}  # reassign: JSON columns aren't mutation-tracked


class Verifier:
    def __init__(self, ctx: PipelineContext, regions: dict[int, Region], transcripts: dict[int, Transcript]):
        self.ctx = ctx
        self.s = ctx.settings
        self.regions = regions
        self.transcripts = transcripts
        self.derived: list[Field] = []

    # --- entry point -------------------------------------------------------------
    def run(self, fields: list[Field]) -> list[Field]:
        by_term = {f.dwc_term: f for f in fields}
        for f in fields:
            trace = Trace(self.ctx, f)
            g = (f.evidence or {}).get("grounding", 0.0)
            trace.add(
                "grounding",
                {"verbatim": f.verbatim, "region_id": f.region_id},
                {"score": g, "span": [f.span_start, f.span_end]},
                passed=g >= GROUNDING_PASS,
            )
            if not self.ctx.options.use_verify:
                continue
            if f.dwc_term == "scientificName":
                self._verify_name(f, trace)
            elif f.dwc_term == "eventDate":
                ident = by_term.get("dateIdentified")
                self._verify_date(f, trace, not_after=ident.value if ident else None)
            elif f.dwc_term == "dateIdentified":
                self._verify_date(f, trace)
            elif f.dwc_term == "country":
                self._verify_country(f, trace)
        if self.ctx.options.use_verify and self.ctx.geocoder and (loc := by_term.get("locality")):
            self._georeference(loc, by_term)
        self.ctx.session.add_all(self.derived)
        self.ctx.session.commit()
        for d in self.derived:
            self.ctx.session.refresh(d)
        return self.derived

    # --- re-read tool ----------------------------------------------------------------
    def _context_line(self, f: Field) -> str:
        t = self.transcripts.get(f.region_id or -1)
        if not t:
            return ""
        if f.span_start is not None:
            start = t.text.rfind("\n", 0, f.span_start) + 1
            end = t.text.find("\n", f.span_end or f.span_start)
            return t.text[start : end if end >= 0 else len(t.text)]
        return next((ln for ln in t.lines if f.verbatim and f.verbatim[:6] in ln), "")

    def reread(self, f: Field, trace: Trace, question: str, current: str) -> str | None:
        region = self.regions.get(f.region_id or -1)
        if region is None or not region.crop_path:
            return None
        prompt = load_prompt("reread")
        rendered = prompt.render(term=f.dwc_term, current=current, context=self._context_line(f), question=question)
        out: RereadOut = self.ctx.structured(
            CallRequest("reread", prompt, rendered, [Path(region.crop_path).read_bytes()], RereadOut,
                        self.s.effort_reread, (region.x1, region.y1, region.x2, region.y2, f.dwc_term, current, question))
        )  # fmt: skip
        trace.add("reread_crop", {"question": question, "current": current}, out.model_dump(), passed=None)
        answer = out.answer.strip()
        return answer if out.legible and answer else None

    # --- scientific name ---------------------------------------------------------------
    def _verify_name(self, f: Field, trace: Trace) -> None:
        rereads = 0
        while True:
            self.ctx.release_db()
            try:
                match = self.ctx.gbif.name_match(f.value or "")
            except (httpx.HTTPError, ValueError) as exc:  # ValueError: non-JSON reply
                log.warning("GBIF name match unavailable for field %s: %s", f.id, exc)
                trace.add("gbif_name_match", {"name": f.value}, {"error": f"GBIF unavailable: {type(exc).__name__}"}, None)
                # The check never ran: never let such a field be auto-accepted
                _set_evidence(f, verification_unavailable=True)
                return
            passed = name_check_passed(match, self.s.gbif_min_match_confidence)
            trace.add("gbif_name_match", {"name": f.value}, match, passed)
            exact = passed and match.get("matchType") == "EXACT"
            if exact or rereads >= self.s.max_rereads_per_field:
                break
            candidates = [n for n in [match.get("scientificName")] + [a["scientificName"] for a in match.get("alternatives", [])] if n]
            question = (
                f"GBIF's taxonomy does not exactly recognise «{f.value}»"
                + (f"; the closest known names are: {'; '.join(candidates[:3])}" if candidates else "")
                + ". Read the plant's scientific name (genus, epithet and author) letter by letter as written."
            )
            answer = self.reread(f, trace, question, current=f.value or "")
            rereads += 1
            if answer is None or _norm(answer) == _norm(f.value):
                break  # the model stands by its reading (or cannot read it)
            f.value = answer
            _set_evidence(f, reread_changed=True)

        _set_evidence(f, gbif={"matchType": match.get("matchType"), "confidence": match.get("confidence"),
                                "status": match.get("status")})  # fmt: skip
        if not passed:
            return
        conf = (match.get("confidence") or 0) / 100
        if match.get("usageKey"):
            self._derive("taxonID", str(match["usageKey"]), f, conf, note="GBIF Backbone usage key")
        if match.get("acceptedName") and _norm(match["acceptedName"]) != _norm(f.value):
            self._derive("acceptedNameUsage", match["acceptedName"], f, conf, note=f"label name is a {match.get('status', 'synonym').lower()}")
        if match.get("family"):
            _set_evidence(f, family=match["family"])

    # --- dates -------------------------------------------------------------------------
    def _verify_date(self, f: Field, trace: Trace, not_after: str | None = None) -> None:
        rereads = 0
        while True:
            r = date_check(f.verbatim, f.value, earliest_year=self.s.earliest_plausible_year, not_after=not_after)
            trace.add("date_check", {"verbatim": f.verbatim, "value": f.value, "not_after": not_after}, r.to_dict(), r.passed)
            if r.passed or rereads >= self.s.max_rereads_per_field:
                return
            year = (r.iso or "")[:4]
            question = "What is this date exactly as written on the label?"
            if year and r.alternatives:
                question += f" Look closely at the year: is it {year} or {' or '.join(map(str, r.alternatives[:3]))}?"
            if r.ambiguous_day_month:
                question += " Also check which number is the day and which is the month."
            answer = self.reread(f, trace, question, current=f.verbatim or f.value or "")
            rereads += 1
            parsed = parse_verbatim_date(answer) if answer else None
            if parsed is None:
                return
            if _norm(answer) == _norm(f.verbatim) and parsed.iso == f.value:
                return  # re-read confirms the same reading; the problem is real, leave it flagged
            if _norm(answer) != _norm(f.verbatim):
                f.verbatim = answer
                _set_evidence(f, reread_changed=True)
            f.value = parsed.iso

    # --- country -----------------------------------------------------------------------
    def _verify_country(self, f: Field, trace: Trace) -> None:
        rereads = 0
        while True:
            hit = country_lookup(f.value) or country_lookup(f.verbatim)
            trace.add("country_check", {"value": f.value, "verbatim": f.verbatim}, hit or {"error": "unknown country"}, hit is not None)
            if hit or rereads >= 1:
                break
            answer = self.reread(f, trace, "Which country is named on this label? Give the word as written.", current=f.verbatim or "")
            rereads += 1
            if not answer or _norm(answer) == _norm(f.verbatim):
                break
            f.verbatim = answer
            if found := country_lookup(answer):
                f.value = found["name"]
            _set_evidence(f, reread_changed=True)
        if hit:
            self._derive("countryCode", hit["code"], f, 0.95, note=f"ISO 3166 code for {hit['name']}")

    # --- georeference (optional) -------------------------------------------------------
    def _georeference(self, loc: Field, by_term: dict[str, Field]) -> None:
        trace = Trace(self.ctx, loc)
        trace.step = 100  # after the field's other checks
        state = by_term.get("municipality") or by_term.get("stateProvince")
        cc = next((d.value for d in self.derived if d.dwc_term == "countryCode"), None)
        self.ctx.release_db()
        try:
            result = self.ctx.geocoder.geocode(loc.value or "", state.value if state else None, cc)  # type: ignore[union-attr]
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("geocoding unavailable for field %s: %s", loc.id, exc)
            trace.add("geocode", {"locality": loc.value}, {"error": f"{type(exc).__name__}: {exc}"[:200]}, None)
            return
        trace.add("geocode", {"locality": loc.value, "state": state.value if state else None, "countryCode": cc},
                  result or {"error": "no match"}, result is not None)  # fmt: skip
        if result:
            for term in ("decimalLatitude", "decimalLongitude", "coordinateUncertaintyInMeters"):
                # Georeferences always need a human look
                self._derive(term, str(result[term]), loc, 0.5, note=result.get("matched") or "")

    # --- helpers -------------------------------------------------------------------------
    def _derive(self, term: str, value: str, source: Field, confidence: float, note: str = "") -> None:
        high = confidence >= self.s.confidence_high
        self.derived.append(
            Field(
                sheet_id=source.sheet_id,
                dwc_term=term,
                verbatim=None,
                value=value,
                model_value=value,
                region_id=source.region_id,
                derived=True,
                confidence=round(confidence, 3),
                confidence_level="high" if high else "medium" if confidence >= self.s.confidence_medium else "low",
                evidence={"derived_from": source.dwc_term, "note": note},
                status=FieldStatus.auto if high else FieldStatus.flagged,
            )
        )


def verify_fields(
    ctx: PipelineContext, fields: list[Field], regions: list[Region], transcripts: dict[int, Transcript]
) -> list[Field]:
    """Run the agent over all fields; returns newly derived fields."""
    verifier = Verifier(ctx, {r.id: r for r in regions}, transcripts)
    derived = verifier.run(fields)
    ctx.session.commit()
    return derived
