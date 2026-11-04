"""Orchestrates the stages for one sheet, plus the background worker.

Each stage writes its results to SQLite and every model call is cached, so the
review UI reads finished results and never waits on a model. Re-running a sheet
clears its previous results first; cached model calls make re-runs free.
"""

from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timezone

from sqlalchemy import delete, func
from sqlmodel import Session, select

from app.clients import get_client
from app.clients.base import LLMClient
from app.config import get_settings
from app.db import session_scope
from app.models import Check, Edit, Field, ModelCall, Region, Sheet, SheetStatus, Transcript
from app.pipeline.confidence import apply_confidence
from app.pipeline.context import PipelineContext, RunOptions
from app.pipeline.interpret import interpret_fields
from app.pipeline.segment import segment_sheet
from app.pipeline.transcribe import transcribe_regions
from app.pipeline.verify import verify_fields
from app.tools.gbif import GbifClient
from app.tools.geocode import Geocoder

log = logging.getLogger(__name__)


def clear_results(session: Session, sheet_id: int) -> None:
    field_ids = select(Field.id).where(Field.sheet_id == sheet_id)
    session.exec(delete(Check).where(Check.field_id.in_(field_ids)))  # type: ignore[union-attr]
    session.exec(delete(Edit).where(Edit.field_id.in_(field_ids)))  # type: ignore[union-attr]
    session.exec(delete(Field).where(Field.sheet_id == sheet_id))  # type: ignore[arg-type]
    session.exec(delete(Transcript).where(Transcript.sheet_id == sheet_id))  # type: ignore[arg-type]
    session.exec(delete(Region).where(Region.sheet_id == sheet_id))  # type: ignore[arg-type]
    session.exec(delete(ModelCall).where(ModelCall.sheet_id == sheet_id))  # type: ignore[arg-type]
    session.commit()


def _stage(session: Session, sheet: Sheet, name: str) -> None:
    sheet.stage = name
    sheet.updated_at = datetime.now(timezone.utc)
    session.add(sheet)
    session.commit()


def process_sheet(
    sheet_id: int,
    *,
    client: LLMClient | None = None,
    options: RunOptions | None = None,
    gbif: GbifClient | None = None,
    geocoder: Geocoder | None = None,
) -> Sheet:
    settings = get_settings()
    client = client or get_client(settings)
    options = options or RunOptions()
    with session_scope() as session:
        sheet = session.get(Sheet, sheet_id)
        if sheet is None:
            raise KeyError(f"sheet {sheet_id} not found")
        clear_results(session, sheet_id)
        sheet.status, sheet.error = SheetStatus.processing, None
        ctx = PipelineContext(session, sheet, client, settings, options, gbif=gbif, geocoder=geocoder)
        try:
            _stage(session, sheet, "segment")
            regions = segment_sheet(ctx)
            _stage(session, sheet, "transcribe")
            pass1 = transcribe_regions(ctx, regions, pass_no=1)
            _stage(session, sheet, "interpret")
            fields = interpret_fields(ctx, regions, pass1)
            _stage(session, sheet, "verify")
            derived = verify_fields(ctx, fields, regions, pass1)
            _stage(session, sheet, "confidence")
            apply_confidence(ctx, fields + derived, regions, pass1)
            sheet.status, sheet.stage = SheetStatus.done, None
        except Exception as exc:  # a failed sheet must not take the worker down
            log.exception("sheet %s failed", sheet_id)
            session.rollback()
            sheet = session.get(Sheet, sheet_id)
            sheet.status = SheetStatus.error
            sheet.error = f"{type(exc).__name__}: {exc}"[:1000]
        sheet.cost_usd = float(
            session.exec(select(func.coalesce(func.sum(ModelCall.cost_usd), 0.0)).where(ModelCall.sheet_id == sheet_id)).one()
        )
        sheet.updated_at = datetime.now(timezone.utc)
        session.add(sheet)
        session.commit()
        session.refresh(sheet)
        session.expunge(sheet)
        return sheet


class Worker:
    """Single background thread processing queued sheets in order (keeps SQLite simple)."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[int, RunOptions | None]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.current: int | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="pipeline-worker", daemon=True)
                self._thread.start()

    def enqueue(self, sheet_id: int, options: RunOptions | None = None) -> None:
        with session_scope() as session:
            sheet = session.get(Sheet, sheet_id)
            if sheet is not None:
                sheet.status, sheet.stage, sheet.error = SheetStatus.queued, None, None
                session.add(sheet)
                session.commit()
        self._queue.put((sheet_id, options))
        self.start()

    def pending(self) -> int:
        return self._queue.qsize() + (1 if self.current is not None else 0)

    def resume_unfinished(self) -> int:
        """Re-queue sheets left queued/processing by a previous process."""
        with session_scope() as session:
            ids = session.exec(
                select(Sheet.id).where(Sheet.status.in_([SheetStatus.queued, SheetStatus.processing]))  # type: ignore[attr-defined]
            ).all()
        for sid in ids:
            self.enqueue(sid)
        return len(ids)

    def _loop(self) -> None:
        while True:
            sheet_id, options = self._queue.get()
            self.current = sheet_id
            try:
                process_sheet(sheet_id, options=options)
            except Exception:
                log.exception("worker failed on sheet %s", sheet_id)
            finally:
                self.current = None
                self._queue.task_done()

    def join(self) -> None:
        self._queue.join()


worker = Worker()
