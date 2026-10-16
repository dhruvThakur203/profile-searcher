"""Derive postal-code research signals from supplied address lines.

The original Pincode field is never modified; derived fragments are kept
separately together with the address line they came from.
"""

import re
from dataclasses import dataclass

from .models import PersonRecord
from .normalize import normalize_text


ADDRESS_FIELDS = ("address1", "address2", "address3", "address4")

_COUNTRY_PATTERNS = {
    "US": re.compile(r"^\d{5}(?:-\d{4})?$"),
    "IN": re.compile(r"^[1-9]\d{5}$"),
    "SG": re.compile(r"^\d{6}$"),
}
_GENERIC_PATTERN = re.compile(r"^\d{4,6}$")
_SG_LEGACY_PATTERN = re.compile(r"^\d{4}$")
_ALPHA = re.compile(r"^[A-Za-z][A-Za-z.'-]*$")
# Numbers after these words are unit/house/plot numbers, not postal codes.
_UNIT_WORDS = {
    "flat", "apt", "apartment", "unit", "suite", "ste", "no", "block", "blk",
    "floor", "fl", "room", "rm", "house", "plot", "door", "building", "bldg",
    "tower", "box", "lot", "sector", "phase", "street", "st", "road", "rd",
    "lane", "avenue", "ave", "highway", "hwy", "route",
}


@dataclass(frozen=True)
class PostalFragment:
    value: str
    source_field: str
    source_text: str
    country_hint: str = ""
    location: str = ""
    same_as_pincode: bool = False

    def describe(self) -> str:
        parts = [
            f"{self.value} (derived from {self.source_field}: '{self.source_text}'",
            f"country={self.country_hint or 'unknown'}",
        ]
        if self.location:
            parts.append(f"location={self.location}")
        if self.same_as_pincode:
            parts.append("same as Pincode field")
        return "; ".join(parts) + ")"


def country_hint(person: PersonRecord) -> str:
    text = " " + normalize_text(
        " ".join(
            [person.country]
            + [getattr(person, field) or "" for field in ADDRESS_FIELDS]
        )
    ) + " "
    if any(marker in text for marker in (" united states ", " usa ", " u s a ", " america ")):
        return "US"
    if " india " in text:
        return "IN"
    if " singapore " in text:
        return "SG"
    return ""


def derive_postal_fragments(person: PersonRecord) -> list[PostalFragment]:
    """Return postal-code-like fragments found in a postal position.

    A number counts only when it follows an alphabetic token (city/state/
    country), is not the first token of the line (house numbers), is followed
    by at most three alphabetic tokens, and fits the country's postal format.
    """
    hint = country_hint(person)
    pincode = (person.pincode or "").strip()
    fragments: list[PostalFragment] = []
    seen: set[str] = set()
    for field in ADDRESS_FIELDS:
        line = (getattr(person, field) or "").strip()
        if not line:
            continue
        tokens = [token for token in re.split(r"[\s,]+", line) if token]
        for index, token in enumerate(tokens):
            candidate, consumed = _candidate(tokens, index)
            if not candidate or index == 0:
                continue
            previous = tokens[index - 1]
            following = tokens[index + consumed :]
            if not _ALPHA.match(previous):
                continue
            if normalize_text(previous) in _UNIT_WORDS:
                continue
            if len(following) > 3 or not all(_ALPHA.match(item) for item in following):
                continue
            if not _fits_country(candidate, hint, previous):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            fragments.append(
                PostalFragment(
                    value=candidate,
                    source_field=field,
                    source_text=line,
                    country_hint=hint,
                    location=_location_before(tokens, index),
                    same_as_pincode=bool(pincode) and candidate == pincode.replace(" ", ""),
                )
            )
    return fragments


def _candidate(tokens: list[str], index: int) -> tuple[str, int]:
    token = tokens[index].rstrip(".")
    if not token[:1].isdigit():
        return "", 1
    # Indian PIN codes are often written as "560 001".
    if (
        re.fullmatch(r"\d{3}", token)
        and index + 1 < len(tokens)
        and re.fullmatch(r"\d{3}", tokens[index + 1].rstrip("."))
    ):
        return token + tokens[index + 1].rstrip("."), 2
    return token, 1


def _fits_country(value: str, hint: str, previous: str) -> bool:
    if hint == "SG" and _SG_LEGACY_PATTERN.match(value):
        return normalize_text(previous) == "singapore"
    pattern = _COUNTRY_PATTERNS.get(hint)
    if pattern is not None:
        return bool(pattern.match(value))
    return bool(_GENERIC_PATTERN.match(value))


def _location_before(tokens: list[str], index: int) -> str:
    words: list[str] = []
    cursor = index - 1
    while cursor >= 0 and len(words) < 2:
        token = tokens[cursor].strip(".")
        if not _ALPHA.match(token):
            break
        if len(token) < 3:
            # Skip a state abbreviation directly before the code ("CERRITOS CA 90703").
            if not words and cursor == index - 1:
                cursor -= 1
                continue
            break
        words.insert(0, token)
        cursor -= 1
    return " ".join(words)
