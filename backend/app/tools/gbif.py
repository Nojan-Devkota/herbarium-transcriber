"""GBIF API tools: taxonomic name matching and occurrence search.

- `name_match` checks a label's scientific name against the GBIF Backbone
  Taxonomy (exact / fuzzy / higher-rank / none) and resolves synonyms to the
  accepted name, which is how the agent catches outdated names.
- `occurrence_search` pulls preserved specimens with images for batch ingest and
  for building the evaluation gold set.

Every response is cached on disk, so repeated runs never hit GBIF rate limits.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

import httpx

from app.cache import Cache

GBIF_API = "https://api.gbif.org/v1"


class GbifClient:
    def __init__(self, http: httpx.Client | None = None, cache: Cache | None = None, base_url: str = GBIF_API):
        self.http = http or httpx.Client(timeout=30.0, headers={"User-Agent": "herbarium-label-transcriber/0.1"})
        self.cache = cache
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str, params: dict[str, Any] | list[tuple[str, Any]] | None = None, use_cache: bool = True) -> Any:
        key = hashlib.sha256(json.dumps([path, params], sort_keys=True, default=str).encode()).hexdigest()
        if use_cache and self.cache and (hit := self.cache.get("gbif", key)) is not None:
            return hit["body"]
        resp = self.http.get(f"{self.base_url}{path}", params=params)
        resp.raise_for_status()
        body = resp.json()
        if use_cache and self.cache:
            self.cache.set("gbif", key, {"body": body})
        return body

    # --- taxonomy ------------------------------------------------------------
    def name_match(self, name: str, kingdom: str = "Plantae") -> dict[str, Any]:
        """Match `name` against the GBIF Backbone. Adds `acceptedName` for synonyms."""
        body = self._get("/species/match", {"name": name, "kingdom": kingdom, "verbose": "true"})
        result = {
            "query": name,
            "matchType": body.get("matchType", "NONE"),
            "confidence": body.get("confidence", 0),
            "status": body.get("status"),
            "rank": body.get("rank"),
            "usageKey": body.get("usageKey"),
            "scientificName": body.get("scientificName"),
            "canonicalName": body.get("canonicalName"),
            "family": body.get("family"),
            "acceptedUsageKey": body.get("acceptedUsageKey"),
            "acceptedName": None,
            "alternatives": [
                {"scientificName": a.get("scientificName"), "status": a.get("status"), "confidence": a.get("confidence")}
                for a in (body.get("alternatives") or [])[:3]
            ],
        }
        if result["status"] in ("SYNONYM", "HETEROTYPIC_SYNONYM", "HOMOTYPIC_SYNONYM", "PROPARTE_SYNONYM"):
            if key := result["acceptedUsageKey"]:
                accepted = self._get(f"/species/{key}")
                result["acceptedName"] = accepted.get("scientificName")
            elif body.get("species"):
                result["acceptedName"] = body.get("species")
        return result

    # --- occurrences -------------------------------------------------------------
    def occurrence_search(
        self,
        *,
        dataset_key: str | None = None,
        scientific_name: str | None = None,
        taxon_key: int | None = None,
        licenses: tuple[str, ...] = ("CC0_1_0", "CC_BY_4_0"),
        year: str | None = None,
        limit: int = 20,
        offset: int = 0,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Preserved specimens with still images, filtered to open licences."""
        params: list[tuple[str, Any]] = [
            ("basisOfRecord", "PRESERVED_SPECIMEN"),
            ("mediaType", "StillImage"),
            ("limit", min(limit, 300)),
            ("offset", offset),
        ]
        params += [("license", lic) for lic in licenses]
        if dataset_key:
            params.append(("datasetKey", dataset_key))
        if scientific_name:
            params.append(("scientificName", scientific_name))
        if taxon_key:
            params.append(("taxonKey", taxon_key))
        if year:
            params.append(("year", year))
        for k, v in (extra or {}).items():
            params.append((k, v))
        return self._get("/occurrence/search", params)

    def occurrence(self, key: int | str) -> dict[str, Any]:
        return self._get(f"/occurrence/{key}")


def name_check_passed(match: dict[str, Any], min_confidence: int = 90) -> bool:
    """A name passes when GBIF matches it at species rank or below with good confidence."""
    if match.get("matchType") not in ("EXACT", "FUZZY"):
        return False
    if match.get("matchType") == "FUZZY" and (match.get("confidence") or 0) < min_confidence:
        return False
    return (match.get("rank") or "").upper() in {"SPECIES", "SUBSPECIES", "VARIETY", "FORM", "SUBVARIETY", "SUBFORM"}


_shared: dict[tuple[str, bool], GbifClient] = {}
_shared_lock = threading.Lock()


def shared_gbif(cache: Cache | None) -> GbifClient:
    """One GBIF client (and connection pool) per cache location, reused across sheets."""
    key = (str(cache.root) if cache else "", bool(cache and cache.enabled))
    with _shared_lock:
        if key not in _shared:
            _shared[key] = GbifClient(cache=cache)
        return _shared[key]
