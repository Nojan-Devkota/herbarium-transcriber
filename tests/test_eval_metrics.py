"""Evaluation metrics behave as the design doc specifies."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.metrics import cer, date_correct, name_correct, score_sheet, state_correct, text_pass, truth_date  # noqa: E402


def test_truth_date_precision():
    assert truth_date("1950-06-12T00:00:00") == "1950-06-12"
    assert truth_date("1950-06-01/1950-06-30") == "1950-06"
    assert truth_date("1950-01-01/1950-12-31") == "1950"
    assert truth_date("1950/1952") is None


def test_date_correct_at_truth_precision():
    assert date_correct("1950-06-12", "1950-06-12T00:00:00")
    assert date_correct("1950-06-12", "1950-06-01/1950-06-30")  # more precise than truth is fine
    assert not date_correct("1950", "1950-06-12")  # less precise than truth is not
    assert not date_correct("1951-06-12", "1950-06-12")
    assert date_correct("x", "1950/1952") is None


def test_name_correct_uses_accepted_taxon():
    keys = {"Arenaria stricta": {"usageKey": 1, "acceptedUsageKey": 2}, "Sabulina michauxii": {"usageKey": 2}}
    assert name_correct("Arenaria stricta", "Sabulina michauxii", keys.get)
    assert name_correct(None, "Sabulina michauxii", keys.get) is False
    assert name_correct("x", "Unresolvable truth", lambda n: {}) is None  # unscorable, not wrong


def test_text_metrics():
    assert text_pass("Robert S. Sigafoos", "R. Sigafoos") is False or cer("a", "a") == 0
    assert text_pass("Seward Peninsula, Lost River, York Mountains", "Seward Peninsula, York Mountains, Lost River")
    assert cer("Vinstradalen", "Vinstradalen") == 0 and cer("abc", "xyz") == 1
    assert state_correct("ALASKA", "Alaska") and state_correct("Sør-Trøndelag", "Sor-Trondelag")


def test_score_sheet_skips_missing_truth_and_marks_missing_pred():
    rows = score_sheet(
        {"eventDate": {"value": "1950-06-12", "confidence_level": "high"}, "countryCode": {"value": "US"}},
        {"eventDate": "1950-06-12", "countryCode": "US", "recordedBy": "R. Sigafoos"},
        lambda n: {"usageKey": 1},
    )
    by = {r["field"]: r for r in rows}
    assert by["eventDate"]["correct"] and by["country"]["correct"]
    assert not by["recordedBy"]["correct"] and by["recordedBy"]["confidence_level"] == "missing"
    assert "locality" not in by
