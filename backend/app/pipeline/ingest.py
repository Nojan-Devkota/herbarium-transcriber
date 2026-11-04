"""Stage 1 — ingest: uploads and GBIF batches become `Sheet` rows.

Images are de-duplicated by SHA-256 of the original bytes, EXIF-rotated,
converted to RGB JPEG and stored under data/images/. Only openly licensed
images (CC0 / CC-BY) are accepted from GBIF; the licence is stored per sheet.
"""

from __future__ import annotations

import hashlib
import io
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.models import Sheet, SheetStatus
from app.cache import Cache
from app.tools.gbif import GbifClient, shared_gbif

log = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024

# GBIF fields kept as ground truth for evaluation
TRUTH_FIELDS = [
    "gbifID", "occurrenceID", "catalogNumber", "institutionCode", "datasetName",
    "scientificName", "acceptedScientificName", "family", "recordedBy", "recordNumber",
    "eventDate", "year", "month", "day", "verbatimEventDate", "country", "countryCode",
    "stateProvince", "county", "municipality", "locality", "verbatimLocality", "habitat",
    "elevation", "verbatimElevation", "decimalLatitude", "decimalLongitude",
    "identifiedBy", "dateIdentified",
]  # fmt: skip


# --- licences ---------------------------------------------------------------------
def normalize_license(raw: str | None) -> str | None:
    """Map GBIF licence codes/URLs to SPDX-like ids: CC0-1.0, CC-BY-4.0, CC-BY-NC-4.0, ..."""
    if not raw:
        return None
    r = raw.strip().lower().replace("_", "-")
    if "cc0" in r or "publicdomain/zero" in r or "public domain" in r:
        return "CC0-1.0"
    if not ("creativecommons" in r or r.startswith("cc")) or "by" not in r:
        return raw.strip()  # not a Creative Commons licence we can classify
    parts = ["CC", "BY"]
    for flag in ("nc", "nd", "sa"):
        if f"-{flag}" in r or f"/{flag}" in r or f"by-{flag}" in r:
            parts.append(flag.upper())
    version = next((v for v in ("4.0", "3.0", "2.5", "2.0", "1.0") if v in r), None)
    version = version or ("4.0" if "4-0" in r else "3.0" if "3-0" in r else "4.0")
    return "-".join(parts) + f"-{version}"


def is_open_license(lic: str | None) -> bool:
    """CC0 or plain CC-BY only (design doc: 'Licensed data only')."""
    return lic is not None and (lic == "CC0-1.0" or (lic.startswith("CC-BY-") and lic.count("-") == 2))


# --- images -----------------------------------------------------------------------
# Herbarium scans reach ~150 MP; anything far beyond that is a decompression bomb.
Image.MAX_IMAGE_PIXELS = 200_000_000
# Decoding a 150 MP scan takes ~450 MB of RAM; cap how many happen at once.
_DECODE_SLOTS = threading.BoundedSemaphore(2)

# Downscaled copies served to the UI instead of the multi-megabyte original
VARIANT_SIDES = {"thumb": 480, "view": 1600}


def _decode(data: bytes) -> Image.Image:
    try:
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im)
        return im.convert("RGB")
    except Image.DecompressionBombError as exc:
        raise ValueError("image is too large (over 200 megapixels)") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ValueError("file is not a readable image") from exc


def downscale(im: Image.Image, max_side: int) -> Image.Image:
    """A resized copy no larger than max_side (never a full-resolution copy first)."""
    if max(im.size) <= max_side:
        return im
    return ImageOps.contain(im, (max_side, max_side), Image.Resampling.LANCZOS)


def normalize_image(data: bytes) -> tuple[bytes, int, int]:
    """Decode any common image format, apply EXIF rotation, return RGB JPEG bytes."""
    with _DECODE_SLOTS:
        im = _decode(data)
        return to_jpeg(im, quality=92), im.width, im.height


def load_image(path: str) -> Image.Image:
    with Image.open(path) as im:
        return im.convert("RGB")


def to_jpeg(im: Image.Image, max_side: int | None = None, quality: int = 88) -> bytes:
    if max_side:
        im = downscale(im, max_side)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def variant_path(image_path: str, size: str) -> Path:
    p = Path(image_path)
    return p.with_name(f"{p.stem}_{size}.jpg")


def write_variants(im: Image.Image, image_path: str) -> None:
    for size, side in VARIANT_SIDES.items():
        variant_path(image_path, size).write_bytes(to_jpeg(im, max_side=side, quality=85))


def ensure_variant(image_path: str, size: str) -> Path:
    """Path of a downscaled copy, created on first request for sheets ingested before variants existed."""
    path = variant_path(image_path, size)
    if not path.exists():
        with _DECODE_SLOTS:
            write_variants(load_image(image_path), image_path)
    return path


def ingest_image(
    session: Session,
    data: bytes,
    *,
    filename: str | None = None,
    source_url: str | None = None,
    license: str | None = None,
    gbif_occurrence_id: str | None = None,
    gbif_record: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> tuple[Sheet, bool]:
    """Store an image as a Sheet. Returns (sheet, created); duplicates return the existing row."""
    settings = settings or get_settings()
    sha = hashlib.sha256(data).hexdigest()
    existing = session.exec(select(Sheet).where(Sheet.image_sha256 == sha)).first()
    if existing is not None:
        return existing, False
    path = settings.images_dir / f"{sha}.jpg"
    with _DECODE_SLOTS:
        im = _decode(data)
        width, height = im.size
        path.write_bytes(to_jpeg(im, quality=92))
        write_variants(im, str(path))
        del im
    sheet = Sheet(
        image_sha256=sha,
        image_path=str(path),
        width=width,
        height=height,
        filename=filename,
        source_url=source_url,
        license=normalize_license(license) if license else None,
        gbif_occurrence_id=gbif_occurrence_id,
        gbif_record=gbif_record,
        status=SheetStatus.uploaded,
    )
    session.add(sheet)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent upload of the same image won the race; return its row.
        session.rollback()
        return session.exec(select(Sheet).where(Sheet.image_sha256 == sha)).one(), False
    session.refresh(sheet)
    return sheet, True


# --- GBIF batches -------------------------------------------------------------------
def gbif_truth(occ: dict[str, Any]) -> dict[str, Any]:
    return {k: occ[k] for k in TRUTH_FIELDS if occ.get(k) not in (None, "")}


def pick_media(occ: dict[str, Any]) -> tuple[str, str | None] | None:
    """First still image with an identifier, and its licence (falls back to the record's)."""
    for m in occ.get("media") or []:
        if m.get("type", "StillImage") == "StillImage" and m.get("identifier"):
            return m["identifier"], m.get("license") or occ.get("license")
    return None


@dataclass
class BatchReport:
    added: list[int] = field(default_factory=list)
    existing: list[int] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)


_download_client: httpx.Client | None = None
_download_lock = threading.Lock()


def download_client() -> httpx.Client:
    """Shared HTTP client for image downloads (one connection pool for the process)."""
    global _download_client
    with _download_lock:
        if _download_client is None:
            _download_client = httpx.Client(headers={"User-Agent": "herbarium-label-transcriber/0.1"})
        return _download_client


# Many museum media servers are slow or hang; fail fast and move on to the next record.
DOWNLOAD_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
DOWNLOAD_DEADLINE_S = 90.0


def download_image(http: httpx.Client, url: str, deadline_s: float = DOWNLOAD_DEADLINE_S) -> bytes:
    start = time.monotonic()
    with http.stream("GET", url, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT) as resp:
        resp.raise_for_status()
        chunks, size = [], 0
        for chunk in resp.iter_bytes():
            size += len(chunk)
            if size > MAX_DOWNLOAD_BYTES:
                raise ValueError("image larger than 60 MB")
            if time.monotonic() - start > deadline_s:
                raise ValueError(f"download slower than {deadline_s:.0f} s")
            chunks.append(chunk)
    return b"".join(chunks)


def fetch_gbif_batch(
    session: Session,
    *,
    gbif: GbifClient | None = None,
    http: httpx.Client | None = None,
    dataset_key: str | None = None,
    scientific_name: str | None = None,
    taxon_key: int | None = None,
    year: str | None = None,
    limit: int = 10,
    max_pages: int = 5,
    require: tuple[str, ...] = (),
    settings: Settings | None = None,
    on_added: Callable[[int], None] | None = None,
) -> BatchReport:
    """Pull up to `limit` openly licensed specimen images from GBIF into Sheets.

    `require` lists GBIF fields that must be present (e.g. recordedBy, eventDate)
    — used when building the evaluation set so every sheet has ground truth.
    `on_added(sheet_id)` is called as soon as each new sheet is stored, so
    processing can start while the rest of the batch is still downloading.
    """
    settings = settings or get_settings()
    gbif = gbif or shared_gbif(Cache(settings.cache_dir))
    http = http or download_client()
    report = BatchReport()
    page_size = min(max(limit * 2, 20), 100)
    for page in range(max_pages):
        body = gbif.occurrence_search(
            dataset_key=dataset_key,
            scientific_name=scientific_name,
            taxon_key=taxon_key,
            year=year,
            limit=page_size,
            offset=page * page_size,
        )
        results = body.get("results") or []
        for occ in results:
            if len(report.added) >= limit:
                return report
            key = str(occ.get("key"))
            media = pick_media(occ)
            if media is None:
                report.skipped.append({"key": key, "reason": "no image"})
                continue
            url, raw_lic = media
            lic = normalize_license(raw_lic)
            if not is_open_license(lic):
                report.skipped.append({"key": key, "reason": f"licence {lic}"})
                continue
            if missing := [f for f in require if not occ.get(f)]:
                report.skipped.append({"key": key, "reason": f"missing {', '.join(missing)}"})
                continue
            try:
                data = download_image(http, url)
                sheet, created = ingest_image(
                    session,
                    data,
                    filename=url.rsplit("/", 1)[-1][:120],
                    source_url=url,
                    license=lic,
                    gbif_occurrence_id=key,
                    gbif_record=gbif_truth(occ),
                    settings=settings,
                )
            except (httpx.HTTPError, ValueError) as exc:
                report.skipped.append({"key": key, "reason": f"download failed: {type(exc).__name__}"})
                continue
            (report.added if created else report.existing).append(sheet.id)
            if created and on_added is not None:
                on_added(sheet.id)
        if body.get("endOfRecords", True) or not results:
            break
    return report
