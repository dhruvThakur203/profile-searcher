import re
from dataclasses import dataclass

from ..matching import address_similarity
from ..models import Candidate, PersonRecord
from ..normalize import normalize_address, normalize_name, normalize_text
from ..tools.search import SearchResult
from .phones import PHONE_PATTERN, blocked_digit_strings, phone_key, plausible_phone


EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)


@dataclass
class ParsedCandidate:
    candidate: Candidate
    mobiles: list[str]
    emails: list[str]


def parse_search_result(
    person: PersonRecord, result: SearchResult
) -> ParsedCandidate | None:
    """Extract one candidate from a search title and snippet.

    The candidate name is proposed only when the normalized input full name
    appears as a complete token sequence. Address text is taken from the
    closest snippet window. The query string is not used as evidence.
    """

    text = "\n".join(part for part in (result.title, result.snippet) if part)
    if not exact_name_tokens_present(person.name, text):
        return None
    mobiles, emails = extract_contacts(
        text,
        blocked_digits=blocked_digit_strings(
            person.address1,
            person.address2,
            person.address3,
            person.address4,
            person.pincode,
        ),
    )
    return ParsedCandidate(
        candidate=Candidate(
            name=person.name.strip(),
            address1=best_address_window(person.address1, text),
            address2=best_address_window(person.address2, text),
            address3=best_address_window(person.address3, text),
            address4=best_address_window(person.address4, text),
            source_url=(result.url or "").strip(),
            source_title=(result.title or "").strip(),
            evidence=text.strip(),
        ),
        mobiles=mobiles,
        emails=emails,
    )


def exact_name_tokens_present(name: str, text: str) -> bool:
    name_tokens = normalize_name(name).split()
    if not name_tokens:
        return False
    text_tokens = normalize_text(text).split()
    width = len(name_tokens)
    if len(text_tokens) < width:
        return False
    for index in range(len(text_tokens) - width + 1):
        if text_tokens[index : index + width] == name_tokens:
            return True
    return False


def best_address_window(address: str, text: str) -> str:
    normalized = normalize_address(address)
    if not normalized:
        return ""
    tokens = normalize_text(text).split()
    target_length = len(normalized.split())
    if not tokens or target_length == 0:
        return ""
    lengths = {
        length
        for delta in (-1, 0, 1, 2)
        if (length := target_length + delta) > 0
    }
    best_window = ""
    best_score = -1.0
    for length in sorted(lengths):
        if length > len(tokens):
            continue
        for start in range(len(tokens) - length + 1):
            window = " ".join(tokens[start : start + length])
            score = address_similarity(address, window)
            if score is None or score <= best_score:
                continue
            best_score = score
            best_window = window
    return best_window


def extract_contacts(
    text: str, blocked_digits: set[str] | None = None
) -> tuple[list[str], list[str]]:
    emails = []
    seen_emails: set[str] = set()
    redacted = text
    for match in EMAIL_PATTERN.finditer(text):
        email = match.group(0)
        key = email.casefold()
        if key not in seen_emails:
            seen_emails.add(key)
            emails.append(email)
        redacted = redacted.replace(email, " ")
    phones = []
    seen_phones: set[str] = set()
    for match in PHONE_PATTERN.finditer(redacted):
        display = plausible_phone(match.group(1), blocked_digits)
        if display is None:
            continue
        key = phone_key(display)
        if key in seen_phones:
            continue
        seen_phones.add(key)
        phones.append(display)
    return phones, emails


def dedupe_phones(values: list[str]) -> list[str]:
    output = []
    seen: set[str] = set()
    for value in values:
        display = plausible_phone(value)
        if display is None:
            continue
        key = phone_key(display)
        if key in seen:
            continue
        seen.add(key)
        output.append(display)
    return output


def dedupe_emails(values: list[str]) -> list[str]:
    output = []
    seen: set[str] = set()
    for value in values:
        raw = value.strip()
        key = raw.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(raw)
    return output


def usable_source_url(url: str) -> bool:
    from .urls import is_valid_external_source_url

    return is_valid_external_source_url(url)


