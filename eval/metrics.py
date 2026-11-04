"""Field-level metrics against GBIF's human transcriptions (design doc: Evaluation plan).

- scientificName: correct after GBIF name matching — the same accepted taxon
  counts as correct, so a synonym of the right accepted name is not an error.
- eventDate: exact match at the precision the ground truth gives.
- recordedBy, locality: character error rate (CER) plus a pass rate at a fixed
  token-set similarity threshold (GBIF often reformats names, e.g.
  "Robert S. Sigafoos" vs "R. Sigafoos", so exact match would be unfair).
- country: ISO code match. stateProvince: case/accent-insensitive match.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Any

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from app.tools.dates import parse_iso
from app.tools.geocode import country_lookup

TEXT_PASS = 80  # token-set similarity (0-100) counted as a pass
EVAL_FIELDS = ["scientificName", "eventDate", "recordedBy", "locality", "country", "stateProvince"]


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = re.sub(r"[.,;:()\[\]\"']", " ", s)
    return " ".join(s.split())


def fold(s: str | None) -> str:
    return unicodedata.normalize("NFKD", norm(s)).encode("ascii", "ignore").decode()


def cer(pred: str | None, truth: str | None) -> float:
    return Levenshtein.normalized_distance(norm(pred), norm(truth))


def text_pass(pred: str | None, truth: str | None) -> bool:
    return bool(pred) and fuzz.token_set_ratio(norm(pred), norm(truth)) >= TEXT_PASS


def truth_date(raw: str | None) -> str | None:
    """GBIF eventDate ('1950-06-12', '1950-06-12T00:00:00', '1950-06-01/1950-06-30') -> ISO at its precision."""
    if not raw:
        return None
    start, _, end = raw.partition("/")
    start, end = start.split("T")[0], (end.split("T")[0] if end else "")
    if end and end != start:
        if start[:7] == end[:7]:
            return start[:7]
        if start[:4] == end[:4]:
            return start[:4]
        return None  # a multi-year range can't be scored
    return start


def date_correct(pred: str | None, truth_raw: str | None) -> bool | None:
    t = truth_date(truth_raw)
    if t is None or parse_iso(t) is None:
        return None
    p = parse_iso(pred)
    return p is not None and len(p.iso) >= len(t) and p.iso[: len(t)] == t


def taxon_key(match: dict[str, Any]) -> int | None:
    return match.get("acceptedUsageKey") or match.get("usageKey")


def name_correct(pred: str | None, truth: str | None, name_match: Callable[[str], dict[str, Any]]) -> bool:
    if not pred or not truth:
        return False
    kp, kt = taxon_key(name_match(pred)), taxon_key(name_match(truth))
    return kp is not None and kp == kt


def country_correct(pred_code: str | None, pred_name: str | None, truth_code: str | None) -> bool | None:
    if not truth_code:
        return None
    code = pred_code or ((country_lookup(pred_name) or {}).get("code") if pred_name else None)
    return bool(code) and code.upper() == truth_code.upper()


def state_correct(pred: str | None, truth: str | None) -> bool:
    return bool(pred) and (fold(pred) == fold(truth) or fuzz.ratio(fold(pred), fold(truth)) >= 90)


def score_sheet(pred: dict[str, dict[str, Any]], truth: dict[str, Any], name_match: Callable[[str], dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare one sheet's predicted fields ({term: {value, verbatim, confidence_level}}) with GBIF truth."""
    def val(term: str) -> str | None:
        return (pred.get(term) or {}).get("value")

    def level(term: str) -> str:
        return (pred.get(term) or {}).get("confidence_level", "missing")

    rows: list[dict[str, Any]] = []

    def add(term: str, correct: bool | None, truth_value: Any, cer_value: float | None = None, level_term: str | None = None):
        if correct is None:
            return
        rows.append({"field": term, "correct": bool(correct), "pred": val(term), "truth": truth_value,
                     "cer": cer_value, "confidence_level": level(level_term or term)})  # fmt: skip

    if truth.get("scientificName"):
        add("scientificName", name_correct(val("scientificName"), truth["scientificName"], name_match), truth["scientificName"])
    if truth.get("eventDate"):
        add("eventDate", date_correct(val("eventDate"), truth["eventDate"]), truth["eventDate"])
    if truth.get("recordedBy"):
        add("recordedBy", text_pass(val("recordedBy"), truth["recordedBy"]), truth["recordedBy"], cer(val("recordedBy"), truth["recordedBy"]))
    loc_truth = truth.get("verbatimLocality") or truth.get("locality")
    if loc_truth:
        add("locality", text_pass(val("locality"), loc_truth), loc_truth, cer(val("locality"), loc_truth))
    if truth.get("countryCode"):
        add("country", country_correct(val("countryCode"), val("country"), truth["countryCode"]), truth["countryCode"])
    if truth.get("stateProvince"):
        add("stateProvince", state_correct(val("stateProvince"), truth["stateProvince"]), truth["stateProvince"])
    return rows
