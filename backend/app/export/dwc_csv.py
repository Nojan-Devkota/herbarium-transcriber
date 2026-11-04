"""Darwin Core CSV export: one row per processed sheet, importable into collection databases.

Columns are standard Darwin Core terms (https://dwc.tdwg.org/terms/) plus two
`x_` review columns at the end. Values are what the reviewer accepted or
edited; unreviewed values are the pipeline's. Text that a spreadsheet would
execute as a formula is neutralised.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Iterable

from sqlmodel import Session, select

from app.models import Field, FieldStatus, Sheet, SheetStatus

COLUMNS: list[str] = [
    "occurrenceID",
    "basisOfRecord",
    "institutionCode",
    "catalogNumber",
    "scientificName",
    "acceptedNameUsage",
    "taxonID",
    "family",
    "identifiedBy",
    "dateIdentified",
    "recordedBy",
    "recordNumber",
    "eventDate",
    "verbatimEventDate",
    "country",
    "countryCode",
    "stateProvince",
    "county",
    "municipality",
    "locality",
    "habitat",
    "verbatimElevation",
    "verbatimCoordinates",
    "decimalLatitude",
    "decimalLongitude",
    "coordinateUncertaintyInMeters",
    "occurrenceRemarks",
    "associatedMedia",
    "license",
    "x_sheetId",
    "x_fieldsNeedingReview",
]

_FORMULA_PREFIXES = ("=", "+", "@", "\t", "\r")


def _safe(value: object) -> str:
    s = "" if value is None else str(value)
    return "'" + s if s.startswith(_FORMULA_PREFIXES) else s


def sheet_row(sheet: Sheet, fields: Iterable[Field]) -> dict[str, str]:
    by_term = {f.dwc_term: f for f in fields}
    row: dict[str, str] = {c: "" for c in COLUMNS}
    for term, f in by_term.items():
        if term in row:
            row[term] = f.value or ""
    if (ed := by_term.get("eventDate")) and ed.verbatim:
        row["verbatimEventDate"] = ed.verbatim
    if not row["family"] and (sn := by_term.get("scientificName")):
        row["family"] = (sn.evidence or {}).get("family", "")
    row["occurrenceID"] = (
        f"https://www.gbif.org/occurrence/{sheet.gbif_occurrence_id}"
        if sheet.gbif_occurrence_id
        else f"urn:sha256:{sheet.image_sha256}"
    )
    row["basisOfRecord"] = "PreservedSpecimen"
    row["associatedMedia"] = sheet.source_url or ""
    row["license"] = sheet.license or ""
    row["x_sheetId"] = str(sheet.id)
    row["x_fieldsNeedingReview"] = str(sum(1 for f in by_term.values() if f.status == FieldStatus.flagged))
    return {k: _safe(v) for k, v in row.items()}


def export_csv(session: Session, sheet_ids: list[int] | None = None, reviewed_only: bool = False) -> str:
    """CSV text for done sheets. `reviewed_only` skips sheets that still have flagged fields."""
    query = select(Sheet).where(Sheet.status == SheetStatus.done).order_by(Sheet.id)
    if sheet_ids:
        query = query.where(Sheet.id.in_(sheet_ids))  # type: ignore[union-attr]
    sheets = session.exec(query).all()
    fields_by_sheet: dict[int, list[Field]] = defaultdict(list)
    if sheets:
        ids = [s.id for s in sheets]
        for f in session.exec(select(Field).where(Field.sheet_id.in_(ids))):  # type: ignore[union-attr]
            fields_by_sheet[f.sheet_id].append(f)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for sheet in sheets:
        row = sheet_row(sheet, fields_by_sheet[sheet.id])
        if reviewed_only and row["x_fieldsNeedingReview"] != "0":
            continue
        writer.writerow(row)
    return buf.getvalue()
