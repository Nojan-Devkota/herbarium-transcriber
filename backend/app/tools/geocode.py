"""Place tools: country normalisation (always on) and locality georeferencing (optional).

- `country_lookup` resolves a country as written in English, Latin or a local
  language ("NORVEGIA", "Deutschland", "U.S.A.") to an ISO 3166 code.
- `geocode` turns locality text into coordinates with an uncertainty radius via
  OpenStreetMap Nominatim. It is a nice-to-have, off by default
  (GEOCODE_ENABLED), and its output is always flagged for review.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
import unicodedata
from typing import Any

import httpx
import pycountry

from app.cache import Cache

# Historical, Latin and local names that pycountry's fuzzy search doesn't resolve well
COUNTRY_ALIASES: dict[str, str] = {
    "norvegia": "NO", "norge": "NO", "noreg": "NO", "suecia": "SE", "sverige": "SE",
    "dania": "DK", "danmark": "DK", "fennia": "FI", "suomi": "FI", "islandia": "IS",
    "germania": "DE", "deutschland": "DE", "gallia": "FR", "helvetia": "CH", "schweiz": "CH",
    "suisse": "CH", "austria": "AT", "osterreich": "AT", "italia": "IT", "hispania": "ES",
    "espana": "ES", "lusitania": "PT", "britannia": "GB", "great britain": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "anglia": "GB", "scotia": "GB", "hibernia": "IE",
    "hollandia": "NL", "holland": "NL", "nederland": "NL", "belgica": "BE", "polonia": "PL",
    "bohemia": "CZ", "hungaria": "HU", "graecia": "GR", "rossia": "RU", "russia": "RU",
    "usa": "US", "u.s.a.": "US", "united states": "US", "america": "US", "brasilia": "BR",
    "japonia": "JP", "sina": "CN", "china": "CN", "india orientalis": "IN", "nova zealandia": "NZ",
    "new zealand": "NZ", "australia": "AU", "mexico": "MX", "turkey": "TR", "turkiye": "TR",
}  # fmt: skip


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(s.lower().replace(":", " ").split()).strip(" .,;")


def country_lookup(name: str | None) -> dict[str, Any] | None:
    """Return {'code', 'name'} for a country as written, or None if unknown."""
    if not name or not name.strip():
        return None
    folded = _fold(name)
    code = COUNTRY_ALIASES.get(folded) or COUNTRY_ALIASES.get(folded.replace(".", ""))
    country = pycountry.countries.get(alpha_2=code) if code else None
    if country is None:
        try:
            country = pycountry.countries.lookup(name.strip())
        except LookupError:
            try:
                matches = pycountry.countries.search_fuzzy(folded)
                country = matches[0] if matches else None
            except LookupError:
                country = None
    if country is None:
        return None
    return {"code": country.alpha_2, "name": getattr(country, "common_name", None) or country.name}


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class Geocoder:
    MIN_INTERVAL_S = 1.0  # Nominatim usage policy: at most one request per second

    def __init__(self, user_agent: str, http: httpx.Client | None = None, cache: Cache | None = None):
        self.http = http or httpx.Client(timeout=20.0, headers={"User-Agent": user_agent})
        self.cache = cache
        self._throttle = threading.Lock()
        self._last = 0.0

    def _wait_turn(self) -> None:
        with self._throttle:
            delay = self.MIN_INTERVAL_S - (time.monotonic() - self._last)
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()

    def geocode(self, locality: str, state: str | None = None, country_code: str | None = None) -> dict[str, Any] | None:
        query = ", ".join(p for p in (locality, state) if p)
        key = f"{query}|{country_code}"
        hkey = hashlib.sha256(key.encode()).hexdigest()
        if self.cache and (hit := self.cache.get("geocode", hkey)) is not None:
            return hit.get("result")
        params = {"q": query, "format": "jsonv2", "limit": 1}
        if country_code:
            params["countrycodes"] = country_code.lower()
        self._wait_turn()
        resp = self.http.get("https://nominatim.openstreetmap.org/search", params=params)
        resp.raise_for_status()
        hits = resp.json()
        result = None
        if hits:
            h = hits[0]
            lat, lon = float(h["lat"]), float(h["lon"])
            s, n, w, e = (float(v) for v in h["boundingbox"])
            # Uncertainty = distance from the point to the farthest bounding-box corner
            radius = max(_haversine_m(lat, lon, la, lo) for la in (s, n) for lo in (w, e))
            result = {
                "decimalLatitude": round(lat, 5),
                "decimalLongitude": round(lon, 5),
                "coordinateUncertaintyInMeters": max(int(radius), 100),
                "matched": h.get("display_name"),
            }
        if self.cache:
            self.cache.set("geocode", hkey, {"result": result})
        return result


_shared_geocoders: dict[tuple[str, str], Geocoder] = {}
_geo_lock = threading.Lock()


def shared_geocoder(user_agent: str, cache: Cache | None) -> Geocoder:
    """One geocoder per process so the 1 request/second throttle holds across sheets."""
    key = (user_agent, str(cache.root) if cache else "")
    with _geo_lock:
        if key not in _shared_geocoders:
            _shared_geocoders[key] = Geocoder(user_agent, cache=cache)
        return _shared_geocoders[key]
