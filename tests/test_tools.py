"""Date parsing/plausibility, GBIF name matching (offline) and country lookup."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.cache import Cache
from app.tools.dates import date_check, parse_iso, parse_verbatim_date, year_alternatives
from app.tools.gbif import GbifClient, name_check_passed
from app.tools.geocode import country_lookup


@pytest.mark.parametrize(
    "text, iso",
    [
        ("24.VII.1897", "1897-07-24"),
        ("24. VII. 1897", "1897-07-24"),
        ("VII.1897", "1897-07"),
        ("24.7.1897", "1897-07-24"),
        ("7/24/1897", "1897-07-24"),
        ("July 24, 1897", "1897-07-24"),
        ("24 Juli 1897", "1897-07-24"),
        ("12 juillet 1902", "1902-07-12"),
        ("Aug. 1950", "1950-08"),
        ("1897", "1897"),
        ("1950-08-03", "1950-08-03"),
        ("Leg. 15 Jun 1923 N.A. Sørensen", "1923-06-15"),
    ],
)
def test_parse_verbatim_date(text, iso):
    assert parse_verbatim_date(text).iso == iso


def test_parse_verbatim_date_without_year():
    assert parse_verbatim_date("no date here") is None
    assert parse_verbatim_date("") is None


def test_day_month_ambiguity_is_flagged():
    assert parse_verbatim_date("3.4.1910").ambiguous
    assert not parse_verbatim_date("13.4.1910").ambiguous


def test_parse_iso_rejects_invalid():
    assert parse_iso("1897-02-30") is None
    assert parse_iso("24.VII.1897") is None
    assert parse_iso("1897-07").precision == "month"


def test_date_check_passes_consistent_value():
    r = date_check("24.VII.1897", "1897-07-24", today=date(2026, 11, 3))
    assert r.passed and r.precision == "day" and r.problems == []


def test_date_check_catches_misread_year_and_suggests_alternatives():
    r = date_check("24.VII.1897", "1997-07-24", today=date(2026, 11, 3))
    assert not r.passed
    assert any("year differs" in p for p in r.problems)


def test_date_check_future_and_too_early():
    assert not date_check(None, "2099", today=date(2026, 11, 3)).passed
    assert not date_check(None, "1650", earliest_year=1700).passed


def test_date_check_not_after_identification():
    r = date_check("1960", "1960", not_after="1952", today=date(2026, 1, 1))
    assert not r.passed and any("after 1952" in p for p in r.problems)


def test_year_alternatives():
    assert 1891 in year_alternatives(1897, 1700, 2026)
    assert all(1700 <= y <= 2026 for y in year_alternatives(1997, 1700, 2026))


def _gbif(tmp_path, handler):
    return GbifClient(http=httpx.Client(transport=httpx.MockTransport(handler)), cache=Cache(tmp_path))


def test_name_match_resolves_synonym_to_accepted_name(tmp_path):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        if request.url.path.endswith("/species/match"):
            return httpx.Response(
                200,
                json={"matchType": "EXACT", "confidence": 98, "status": "SYNONYM", "rank": "SPECIES",
                      "usageKey": 1, "acceptedUsageKey": 2, "scientificName": "Arenaria stricta Michx."},
            )  # fmt: skip
        return httpx.Response(200, json={"scientificName": "Sabulina michauxii (Fenzl) Dillenb. & Kadereit"})

    client = _gbif(tmp_path, handler)
    m = client.name_match("Arenaria stricta")
    assert m["acceptedName"].startswith("Sabulina michauxii")
    assert name_check_passed(m)
    # Second call is served from the cache
    client.name_match("Arenaria stricta")
    assert len(calls) == 2


def test_name_match_none_and_higher_rank_fail(tmp_path):
    client = _gbif(tmp_path, lambda r: httpx.Response(200, json={"matchType": "NONE", "confidence": 100}))
    assert not name_check_passed(client.name_match("Xyzzy foo"))
    assert not name_check_passed({"matchType": "HIGHERRANK", "rank": "GENUS", "confidence": 99})
    assert not name_check_passed({"matchType": "FUZZY", "rank": "SPECIES", "confidence": 70})
    assert name_check_passed({"matchType": "FUZZY", "rank": "SPECIES", "confidence": 95})


def test_occurrence_search_sends_licence_filters(tmp_path):
    seen = {}

    def handler(request: httpx.Request):
        seen["params"] = request.url.params.multi_items()
        return httpx.Response(200, json={"results": [], "count": 0})

    _gbif(tmp_path, handler).occurrence_search(dataset_key="abc", limit=5)
    params = seen["params"]
    assert ("license", "CC0_1_0") in params and ("license", "CC_BY_4_0") in params
    assert ("basisOfRecord", "PRESERVED_SPECIMEN") in params and ("datasetKey", "abc") in params


@pytest.mark.parametrize(
    "written, code",
    [("NORVEGIA", "NO"), ("Norway", "NO"), ("Deutschland", "DE"), ("U.S.A.", "US"), ("Sverige", "SE"), ("France", "FR")],
)
def test_country_lookup(written, code):
    assert country_lookup(written)["code"] == code


def test_country_lookup_unknown():
    assert country_lookup("Atlantis Minor") is None
    assert country_lookup("") is None
