"""FastAPI app: upload, GBIF batches, sheet detail, field review and Darwin Core export.

Run:  uvicorn app.main:app --reload   (from backend/)

Processing happens in a background worker; endpoints only read finished results.
If the frontend has been built (frontend/dist), it is served at `/`.
"""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case, func
from sqlmodel import Session, select

from app.config import REPO_ROOT, get_settings
from app.db import get_engine, get_session
from app.export.dwc_csv import export_csv
from app.models import Check, Edit, Field, FieldStatus, ModelCall, Region, Sheet, SheetStatus, Transcript
from app.pipeline.context import RunOptions
from app.pipeline.ingest import MAX_DOWNLOAD_BYTES, ensure_variant, fetch_gbif_batch, ingest_image
from app.pipeline.run import worker
from app.schemas import api

log = logging.getLogger(__name__)

ABLATIONS: dict[str, RunOptions] = {
    "full": RunOptions(use_second_pass=True),  # opt-in: adds a second reading (slower, costlier)
    "sam+verify": RunOptions(),  # the default pipeline
    "sam-crops": RunOptions(use_verify=False, use_second_pass=False),
    "whole-sheet": RunOptions(use_sam=False, use_verify=False, use_second_pass=False),
}


_POLL_PATH = re.compile(r"^/(sheets(/\d+)?|stats|health)$")


class QuietPollingFilter(logging.Filter):
    """Drop successful GETs of the endpoints the UI polls, so the access log shows real activity."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            method, path, status = args[1], str(args[2]).split("?")[0], args[4]
            if method == "GET" and status in (200, 304) and _POLL_PATH.match(path):
                return False
        return True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if os.environ.get("LOG_POLLING", "").lower() not in ("1", "true"):
        logging.getLogger("uvicorn.access").addFilter(QuietPollingFilter())
    get_engine()
    worker.start()
    resumed = worker.resume_unfinished()
    if resumed:
        log.info("re-queued %d unfinished sheets", resumed)
    yield


app = FastAPI(title="Herbarium Label Transcriber", version="0.1.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)  # sheet detail JSON compresses ~5-10x
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- helpers ---------------------------------------------------------------------
_SUMMARY_FIELDS = set(api.SheetSummary.model_fields) - {"scientific_name", "fields_total", "fields_flagged"}


def _field_counts(session: Session, sheet_ids: list[int]) -> dict[int, tuple[int, int, str | None]]:
    """(fields_total, fields_flagged, scientificName) per sheet in two queries, not one per sheet."""
    if not sheet_ids:
        return {}
    flagged = func.sum(case((Field.status == FieldStatus.flagged, 1), else_=0))
    counts = session.exec(
        select(Field.sheet_id, func.count(), flagged).where(Field.sheet_id.in_(sheet_ids)).group_by(Field.sheet_id)  # type: ignore[union-attr]
    ).all()
    names = dict(
        session.exec(
            select(Field.sheet_id, Field.value).where(Field.sheet_id.in_(sheet_ids), Field.dwc_term == "scientificName")  # type: ignore[union-attr]
        ).all()
    )
    out = {sid: (0, 0, names.get(sid)) for sid in sheet_ids}
    for sid, total, n_flagged in counts:
        out[sid] = (total, int(n_flagged or 0), names.get(sid))
    return out


def _summary(sheet: Sheet, counts: tuple[int, int, str | None]) -> api.SheetSummary:
    total, flagged, name = counts
    return api.SheetSummary(
        **sheet.model_dump(include=_SUMMARY_FIELDS), scientific_name=name, fields_total=total, fields_flagged=flagged
    )


def _summary_one(session: Session, sheet: Sheet) -> api.SheetSummary:
    return _summary(sheet, _field_counts(session, [sheet.id])[sheet.id])


def _get_sheet(session: Session, sheet_id: int) -> Sheet:
    sheet = session.get(Sheet, sheet_id)
    if sheet is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")
    return sheet


def _field_out(field: Field, checks: list[Check]) -> api.FieldOut:
    return api.FieldOut(
        **field.model_dump(exclude={"sheet_id"}),
        checks=[api.CheckOut(**c.model_dump(exclude={"field_id", "created_at"})) for c in sorted(checks, key=lambda c: c.step)],
    )


# --- routes ------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    s = get_settings()
    return {"ok": True, "client": s.llm_client, "spark": s.model_spark, "sam": s.model_sam, "worker_pending": worker.pending()}


@app.post("/sheets", response_model=list[api.UploadResult])
async def upload_sheets(
    files: list[UploadFile] = File(...),
    license: str | None = Form(None),
    source_url: str | None = Form(None),
    process: bool = Form(True),
    session: Session = Depends(get_session),
) -> list[api.UploadResult]:
    results = []
    for upload in files:
        data = await upload.read(MAX_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_DOWNLOAD_BYTES:
            results.append(api.UploadResult(id=None, filename=upload.filename, created=False, error="file larger than 60 MB"))
            continue
        try:
            sheet, created = ingest_image(session, data, filename=upload.filename, source_url=source_url, license=license)
        except ValueError as exc:
            results.append(api.UploadResult(id=None, filename=upload.filename, created=False, error=str(exc)))
            continue
        if process and (created or sheet.status in (SheetStatus.uploaded, SheetStatus.error)):
            worker.enqueue(sheet.id)
        results.append(api.UploadResult(id=sheet.id, filename=upload.filename, created=created))
    return results


@app.post("/batches/gbif", response_model=api.GbifBatchResult)
def gbif_batch(req: api.GbifBatchRequest, session: Session = Depends(get_session)) -> api.GbifBatchResult:
    if not (req.dataset_key or req.scientific_name or req.taxon_key):
        raise HTTPException(422, "give a dataset_key, scientific_name or taxon_key")
    report = fetch_gbif_batch(
        session,
        dataset_key=req.dataset_key,
        scientific_name=req.scientific_name,
        taxon_key=req.taxon_key,
        year=req.year,
        limit=req.limit,
        on_added=worker.enqueue if req.process else None,
    )
    return api.GbifBatchResult(added=report.added, existing=report.existing, skipped=report.skipped)


@app.get("/sheets", response_model=list[api.SheetSummary])
def list_sheets(
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> list[api.SheetSummary]:
    sheets = session.exec(select(Sheet).order_by(Sheet.id.desc()).offset(offset).limit(limit)).all()  # type: ignore[union-attr]
    counts = _field_counts(session, [s.id for s in sheets])
    return [_summary(s, counts[s.id]) for s in sheets]


@app.get("/sheets/{sheet_id}", response_model=api.SheetDetail)
def get_sheet(sheet_id: int, session: Session = Depends(get_session)) -> api.SheetDetail:
    sheet = _get_sheet(session, sheet_id)
    regions = session.exec(select(Region).where(Region.sheet_id == sheet_id).order_by(Region.ordinal)).all()
    transcripts = session.exec(select(Transcript).where(Transcript.sheet_id == sheet_id)).all()
    fields = session.exec(select(Field).where(Field.sheet_id == sheet_id).order_by(Field.id)).all()
    checks: dict[int, list[Check]] = defaultdict(list)
    if fields:
        for c in session.exec(select(Check).where(Check.field_id.in_([f.id for f in fields]))):  # type: ignore[union-attr]
            checks[c.field_id].append(c)
    name = next((f.value for f in fields if f.dwc_term == "scientificName"), None)
    flagged = sum(1 for f in fields if f.status == FieldStatus.flagged)
    return api.SheetDetail(
        sheet=_summary(sheet, (len(fields), flagged, name)),
        regions=[api.RegionOut(**r.model_dump(exclude={"sheet_id", "crop_path"}), has_crop=bool(r.crop_path)) for r in regions],
        transcripts=[api.TranscriptOut(**t.model_dump(include=set(api.TranscriptOut.model_fields))) for t in transcripts],
        fields=[_field_out(f, checks[f.id]) for f in fields],
        ground_truth=sheet.gbif_record,
    )


IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}


@app.get("/sheets/{sheet_id}/image")
def sheet_image(
    sheet_id: int,
    size: Literal["thumb", "view", "full"] = "view",
    session: Session = Depends(get_session),
) -> FileResponse:
    """The sheet image. `thumb` (480 px) and `view` (1600 px) are what the UI needs; the
    multi-megabyte original (`full`) is only for downloads. A sheet's image never changes,
    so browsers may cache it forever."""
    sheet = _get_sheet(session, sheet_id)
    if not Path(sheet.image_path).is_file():
        raise HTTPException(404, "image file is missing")
    path = Path(sheet.image_path) if size == "full" else ensure_variant(sheet.image_path, size)
    return FileResponse(path, media_type="image/jpeg", headers=IMMUTABLE)


@app.get("/regions/{region_id}/crop")
def region_crop(region_id: int, session: Session = Depends(get_session)) -> FileResponse:
    region = session.get(Region, region_id)
    if region is None or not region.crop_path or not Path(region.crop_path).is_file():
        raise HTTPException(404, "no crop for this region")
    # Region ids can be reused after a re-run, so revalidate (cheap 304 via ETag) instead of caching blindly
    return FileResponse(region.crop_path, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@app.post("/sheets/{sheet_id}/process", response_model=api.SheetSummary)
def reprocess(sheet_id: int, req: api.ProcessRequest | None = None, session: Session = Depends(get_session)) -> api.SheetSummary:
    sheet = _get_sheet(session, sheet_id)
    if sheet.status == SheetStatus.processing:
        raise HTTPException(409, "sheet is being processed")
    worker.enqueue(sheet_id, ABLATIONS[(req or api.ProcessRequest()).ablation])
    session.refresh(sheet)
    return _summary_one(session, sheet)


@app.patch("/fields/{field_id}", response_model=api.FieldOut)
def review_field(field_id: int, patch: api.FieldPatch, session: Session = Depends(get_session)) -> api.FieldOut:
    field = session.get(Field, field_id)
    if field is None:
        raise HTTPException(404, f"field {field_id} not found")
    old = field.value
    if patch.action == "edit":
        if patch.value is None:
            raise HTTPException(422, "an edit needs a value")
        new = patch.value.strip()
        field.value = new
        field.status = FieldStatus.edited if new != (field.model_value or "") else FieldStatus.accepted
    else:
        field.status = FieldStatus.accepted
    session.add(Edit(field_id=field.id, action=patch.action, old_value=old, new_value=field.value))
    session.add(field)
    session.commit()
    session.refresh(field)
    checks = session.exec(select(Check).where(Check.field_id == field.id)).all()
    return _field_out(field, list(checks))


@app.get("/export.csv", response_class=PlainTextResponse)
def export(
    reviewed_only: bool = False,
    sheet_id: list[int] | None = Query(None),
    session: Session = Depends(get_session),
) -> PlainTextResponse:
    body = export_csv(session, sheet_ids=sheet_id, reviewed_only=reviewed_only)
    return PlainTextResponse(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="darwin_core_export.csv"'},
    )


@app.get("/stats", response_model=api.Stats)
def stats(session: Session = Depends(get_session)) -> api.Stats:
    """Dashboard numbers, computed with SQL aggregates (no full-table loads on each poll)."""
    by_status = {k.value if hasattr(k, "value") else k: n for k, n in session.exec(select(Sheet.status, func.count()).group_by(Sheet.status)).all()}
    done_count, done_cost = session.exec(
        select(func.count(), func.coalesce(func.sum(Sheet.cost_usd), 0.0)).where(Sheet.status == SheetStatus.done)
    ).one()
    per_sheet_latency = (
        select(ModelCall.sheet_id, func.sum(ModelCall.latency_ms).label("ms"))
        .join(Sheet, Sheet.id == ModelCall.sheet_id)
        .where(Sheet.status == SheetStatus.done)
        .group_by(ModelCall.sheet_id)
        .having(func.sum(ModelCall.latency_ms) > 0)
        .subquery()
    )
    avg_ms = session.exec(select(func.avg(per_sheet_latency.c.ms))).one()
    calls, cached, total_cost = session.exec(
        select(func.count(), func.coalesce(func.sum(case((ModelCall.cached, 1), else_=0)), 0), func.coalesce(func.sum(ModelCall.cost_usd), 0.0))
    ).one()
    f_status = {k.value if hasattr(k, "value") else k: n for k, n in session.exec(select(Field.status, func.count()).group_by(Field.status)).all()}
    f_conf = dict(session.exec(select(Field.confidence_level, func.count()).group_by(Field.confidence_level)).all())
    fields_total = sum(f_status.values())
    edit_rate: dict[str, float | None] = {}
    reviewed = (FieldStatus.accepted, FieldStatus.edited)
    for level in ("high", "medium", "low"):
        n, n_edited = session.exec(
            select(func.count(), func.coalesce(func.sum(case((Field.status == FieldStatus.edited, 1), else_=0)), 0))
            .where(Field.confidence_level == level, Field.status.in_(reviewed))  # type: ignore[attr-defined]
        ).one()
        edit_rate[level] = round(n_edited / n, 3) if n else None
    return api.Stats(
        sheets_total=sum(by_status.values()),
        sheets_by_status=by_status,
        total_cost_usd=round(float(total_cost), 4),
        avg_cost_per_sheet_usd=round(float(done_cost) / done_count, 4) if done_count else None,
        avg_model_seconds_per_sheet=round(float(avg_ms) / 1000, 1) if avg_ms else None,
        model_calls=calls,
        cached_calls=int(cached),
        fields_total=fields_total,
        fields_by_status=f_status,
        fields_by_confidence=f_conf,
        share_needing_review=round(f_status.get("flagged", 0) / fields_total, 3) if fields_total else None,
        edit_rate_by_confidence=edit_rate,
    )


# --- built frontend (optional) ------------------------------------------------------------
_DIST = Path(REPO_ROOT) / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")
