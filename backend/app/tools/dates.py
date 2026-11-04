"""Date parsing and plausibility checks for herbarium labels.

Labels write dates in many ways: `24.VII.1897` (Roman month), `24. 7. 1897`,
`July 24, 1897`, `24 Juli 1897`, `VII.1897`, `1897`. We parse the verbatim text
to an ISO 8601 date at the precision the label gives (year, month or day) and
check that the interpreted value agrees with it and is historically plausible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

ROMAN_MONTHS = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6,
    "vii": 7, "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12,
}  # fmt: skip

# English, German, French, Spanish, Italian, Latin, Scandinavian month names (prefixes)
MONTH_NAMES: dict[str, int] = {}
for _m, _names in {
    1: "january jan januar janvier enero gennaio januarius ianuarius januari janv",
    2: "february feb februar février fevrier febrero febbraio februarius februari févr",
    3: "march mar märz maerz mars marzo martius",
    4: "april apr avril abril aprile aprilis",
    5: "may mai mayo maggio maius",
    6: "june jun juni juin junio giugno junius iunius",
    7: "july jul juli juillet julio luglio julius iulius juil",
    8: "august aug août aout agosto augustus augusti",
    9: "september sep sept septembre septiembre settembre",
    10: "october oct oktober octobre octubre ottobre okt",
    11: "november nov novembre noviembre",
    12: "december dec dezember décembre decembre diciembre dicembre des desember",
}.items():
    for _n in _names.split():
        MONTH_NAMES[_n] = _m

# Digits commonly confused in old handwriting and print (used to suggest re-read candidates)
CONFUSABLE_DIGITS = {"1": "7", "7": "1", "3": "8", "8": "3", "4": "9", "9": "4", "5": "6", "6": "5", "0": "6"}

_ISO_RE = re.compile(r"^\s*(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?\s*$")
_YEAR = r"(1[5-9]\d\d|20\d\d)"


@dataclass
class ParsedDate:
    year: int
    month: int | None = None
    day: int | None = None
    ambiguous: bool = False  # day/month order could not be determined

    @property
    def precision(self) -> str:
        return "day" if self.day else "month" if self.month else "year"

    @property
    def iso(self) -> str:
        if self.day and self.month:
            return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
        if self.month:
            return f"{self.year:04d}-{self.month:02d}"
        return f"{self.year:04d}"


def _valid(y: int, m: int | None, d: int | None) -> bool:
    try:
        date(y, m or 1, d or 1)
        return True
    except ValueError:
        return False


def parse_iso(value: str | None) -> ParsedDate | None:
    """Parse an ISO 8601 value at year, month or day precision."""
    if not value:
        return None
    m = _ISO_RE.match(value)
    if not m:
        return None
    y, mo, d = int(m.group(1)), m.group(2) and int(m.group(2)), m.group(3) and int(m.group(3))
    if not _valid(y, mo or None, d or None):
        return None
    return ParsedDate(y, mo or None, d or None)


def _month_token(tok: str) -> int | None:
    t = tok.lower().strip(".")
    if t in ROMAN_MONTHS:
        return ROMAN_MONTHS[t]
    if t in MONTH_NAMES:
        return MONTH_NAMES[t]
    for name, num in MONTH_NAMES.items():
        if len(t) >= 3 and name.startswith(t):
            return num
    return None


def parse_verbatim_date(text: str | None) -> ParsedDate | None:
    """Best-effort parse of a date as written on a label. Returns None if no year is found."""
    if not text:
        return None
    if iso := parse_iso(text):
        return iso
    s = text.strip()
    year_m = re.search(_YEAR, s)
    if not year_m:
        return None
    year = int(year_m.group(1))
    rest = (s[: year_m.start()] + " " + s[year_m.end() :]).strip()
    tokens = [t for t in re.split(r"[\s.,/\-–]+", rest) if t]

    month = None
    month_idx = None
    for i, tok in enumerate(tokens):
        if not tok.isdigit() and (mm := _month_token(tok)):
            month, month_idx = mm, i
            break
    numbers = [int(t) for i, t in enumerate(tokens) if t.isdigit() and i != month_idx and len(t) <= 2]

    if month is not None:
        day = next((n for n in numbers if 1 <= n <= 31), None)
        if day and not _valid(year, month, day):
            day = None
        return ParsedDate(year, month, day)

    # Numeric day and month: European D.M.Y is the default; M/D/Y only when forced
    if len(numbers) >= 2:
        a, b = numbers[0], numbers[1]
        if a > 12 and b <= 12:
            d, m = a, b
        elif b > 12 and a <= 12:
            d, m = b, a
        else:
            d, m = a, b
        if _valid(year, m, d):
            return ParsedDate(year, m, d, ambiguous=(a <= 12 and b <= 12 and a != b))
    if len(numbers) == 1 and 1 <= numbers[0] <= 12:
        return ParsedDate(year, numbers[0])
    return ParsedDate(year)


def year_alternatives(year: int, earliest: int, latest: int) -> list[int]:
    """Plausible years one confusable digit away from `year` (e.g. 1897 -> 1837, 1891, 1894)."""
    s = str(year)
    alts: set[int] = set()
    for i, ch in enumerate(s):
        if ch in CONFUSABLE_DIGITS and i >= 2:  # century digits are rarely misread
            cand = int(s[:i] + CONFUSABLE_DIGITS[ch] + s[i + 1 :])
            if earliest <= cand <= latest and cand != year:
                alts.add(cand)
    return sorted(alts)


@dataclass
class DateCheck:
    passed: bool
    iso: str | None
    precision: str | None
    verbatim_iso: str | None
    problems: list[str] = field(default_factory=list)
    alternatives: list[int] = field(default_factory=list)
    ambiguous_day_month: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def date_check(
    verbatim: str | None,
    value: str | None,
    *,
    earliest_year: int = 1700,
    today: date | None = None,
    not_after: str | None = None,
) -> DateCheck:
    """Is `value` a valid ISO date, plausible for a herbarium specimen, and consistent with `verbatim`?

    `not_after` is another ISO date the value must not exceed (e.g. a specimen cannot be
    collected after it was identified).
    """
    today = today or date.today()
    problems: list[str] = []
    parsed = parse_iso(value)
    from_verbatim = parse_verbatim_date(verbatim)

    if parsed is None:
        problems.append(f"value {value!r} is not an ISO 8601 date")
    else:
        if parsed.year < earliest_year:
            problems.append(f"year {parsed.year} is before {earliest_year}")
        if date(parsed.year, parsed.month or 1, parsed.day or 1) > today:
            problems.append(f"date {parsed.iso} is in the future")
        if from_verbatim is not None:
            # Compare at the coarser of the two precisions
            if from_verbatim.year != parsed.year:
                problems.append(f"year differs from the label text ({from_verbatim.year} vs {parsed.year})")
            elif from_verbatim.month and parsed.month and from_verbatim.month != parsed.month and not from_verbatim.ambiguous:
                problems.append(f"month differs from the label text ({from_verbatim.month} vs {parsed.month})")
            elif from_verbatim.day and parsed.day and from_verbatim.day != parsed.day and not from_verbatim.ambiguous:
                problems.append(f"day differs from the label text ({from_verbatim.day} vs {parsed.day})")
        elif verbatim and str(parsed.year) not in verbatim:
            problems.append(f"year {parsed.year} does not appear in the label text")
        if not_after and (limit := parse_iso(not_after)):
            if (parsed.year, parsed.month or 0, parsed.day or 0) > (limit.year, limit.month or 99, limit.day or 99):
                problems.append(f"date {parsed.iso} is after {limit.iso}")

    alts: list[int] = []
    if parsed is not None and problems:
        alts = year_alternatives(parsed.year, earliest_year, today.year)
    return DateCheck(
        passed=not problems,
        iso=parsed.iso if parsed else None,
        precision=parsed.precision if parsed else None,
        verbatim_iso=from_verbatim.iso if from_verbatim else None,
        problems=problems,
        alternatives=alts,
        ambiguous_day_month=bool(from_verbatim and from_verbatim.ambiguous),
    )
