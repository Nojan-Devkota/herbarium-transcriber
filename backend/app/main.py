"""FastAPI app: upload, GBIF batches, sheet detail, field review and Darwin Core export.

Run:  uvicorn app.main:app --reload   (from backend/)

Processing happens in a background worker; endpoints only read finished results.
If the frontend has been built (frontend/dist), it is served at `/`.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlmodel import Session, select

from app.config import REPO_ROOT, get_settings
from app.db import get_engine, get_session
from app.export.dwc_csv import export_csv
from app.models import Check, Edit, Field, FieldStatus, ModelCall, Region, Sheet, SheetStatus, Transcript
from app.pipeline.context import RunOptions
from app.pipeline.ingest import MAX_DOWNLOAD_BYTES, fetch_gbif_batch, ingest_image
from app.pipeline.run import worker
from app.schemas import api

log = logging.getLogger(__name__)

ABLATIONS: dict[str, RunOptions] = {
    "full": RunOptions(),
    "sam+verify": RunOptions(use_second_pass=False),
    "sam-crops": RunOptions(use_verify=False, use_second_pass=False),
    "whole-sheet": RunOptions(use_sam=False, use_verify=False, use_second_pass=False),
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    get_engine()
    worker.start()
    resumed = worker.resume_unfinished()
    if resumed:
        log.info("re-queued %d unfinished sheets", resumed)
    yield


app = FastAPI(title="Herbarium Label Transcriber", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- helpers ---------------------------------------------------------------------
def _summary(session: Session, sheet: Sheet) -> api.SheetSummary:
    fields = session.exec(select(Field).where(Field.sheet_id == sheet.id)).all()
    name = next((f.value for f in fields if f.dwc_term == "scientificName"), None)
    return api.SheetSummary(
        **sheet.model_dump(include=set(api.SheetSummary.model_fields) - {"scientific_name", "fields_total", "fields_flagged"}),
        scientific_name=name,
        fields_total=len(fields),
        fields_flagged=sum(1 for f in fields if f.status == FieldStatus.flagged),
    )


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
    )
    if req.process:
        for sid in report.added:
            worker.enqueue(sid)
    return api.GbifBatchResult(added=report.added, existing=report.existing, skipped=report.skipped)


@app.get("/sheets", response_model=list[api.SheetSummary])
def list_sheets(session: Session = Depends(get_session)) -> list[api.SheetSummary]:
    sheets = session.exec(select(Sheet).order_by(Sheet.id.desc())).all()  # type: ignore[union-attr]
    return [_summary(session, s) for s in sheets]


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
    return api.SheetDetail(
        sheet=_summary(session, sheet),
        regions=[api.RegionOut(**r.model_dump(exclude={"sheet_id", "crop_path"}), has_crop=bool(r.crop_path)) for r in regions],
        transcripts=[api.TranscriptOut(**t.model_dump(include=set(api.TranscriptOut.model_fields))) for t in transcripts],
        fields=[_field_out(f, checks[f.id]) for f in fields],
        ground_truth=sheet.gbif_record,
    )


@app.get("/sheets/{sheet_id}/image")
def sheet_image(sheet_id: int, session: Session = Depends(get_session)) -> FileResponse:
    sheet = _get_sheet(session, sheet_id)
    return FileResponse(sheet.image_path, media_type="image/jpeg")


@app.get("/regions/{region_id}/crop")
def region_crop(region_id: int, session: Session = Depends(get_session)) -> FileResponse:
    region = session.get(Region, region_id)
    if region is None or not region.crop_path:
        raise HTTPException(404, "no crop for this region")
    return FileResponse(region.crop_path, media_type="image/jpeg")


@app.post("/sheets/{sheet_id}/process", response_model=api.SheetSummary)
def reprocess(sheet_id: int, req: api.ProcessRequest | None = None, session: Session = Depends(get_session)) -> api.SheetSummary:
    sheet = _get_sheet(session, sheet_id)
    if sheet.status == SheetStatus.processing:
        raise HTTPException(409, "sheet is being processed")
    worker.enqueue(sheet_id, ABLATIONS[(req or api.ProcessRequest()).ablation])
    session.refresh(sheet)
    return _summary(session, sheet)


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
    sheets = session.exec(select(Sheet)).all()
    done = [s for s in sheets if s.status == SheetStatus.done]
    fields = session.exec(select(Field)).all()
    calls = session.exec(select(ModelCall)).all()
    latency_by_sheet: dict[int, int] = defaultdict(int)
    for c in calls:
        if c.sheet_id is not None:
            latency_by_sheet[c.sheet_id] += c.latency_ms
    done_latency = [latency_by_sheet[s.id] / 1000 for s in done if latency_by_sheet.get(s.id)]
    edited_ids = set(session.exec(select(Edit.field_id).where(Edit.action == "edit")).all())
    reviewed = [f for f in fields if f.status in (FieldStatus.accepted, FieldStatus.edited)]
    edit_rate: dict[str, float | None] = {}
    for level in ("high", "medium", "low"):
        group = [f for f in reviewed if f.confidence_level == level]
        edit_rate[level] = round(sum(1 for f in group if f.status == FieldStatus.edited and f.id in edited_ids) / len(group), 3) if group else None
    total_cost = float(session.exec(select(func.coalesce(func.sum(ModelCall.cost_usd), 0.0))).one())
    return api.Stats(
        sheets_total=len(sheets),
        sheets_by_status=dict(Counter(s.status.value for s in sheets)),
        total_cost_usd=round(total_cost, 4),
        avg_cost_per_sheet_usd=round(sum(s.cost_usd for s in done) / len(done), 4) if done else None,
        avg_model_seconds_per_sheet=round(sum(done_latency) / len(done_latency), 1) if done_latency else None,
        model_calls=len(calls),
        cached_calls=sum(1 for c in calls if c.cached),
        fields_total=len(fields),
        fields_by_status=dict(Counter(f.status.value for f in fields)),
        fields_by_confidence=dict(Counter(f.confidence_level for f in fields)),
        share_needing_review=round(sum(1 for f in fields if f.status == FieldStatus.flagged) / len(fields), 3) if fields else None,
        edit_rate_by_confidence=edit_rate,
    )


# --- built frontend (optional) ------------------------------------------------------------
_DIST = Path(REPO_ROOT) / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")
