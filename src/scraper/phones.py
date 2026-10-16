import re


DATE_PATTERN = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b")
PHONE_PATTERN = re.compile(r"(?<!\w)(\(?\+?\d[\d\s().-]{6,}\d\)?)(?!\w)")


def blocked_digit_strings(*values: str) -> set[str]:
    blocked = set()
    for value in values:
        digits = re.sub(r"\D", "", value or "")
        if digits:
            blocked.add(digits)
    return blocked


def plausible_phone(raw: str, blocked_digits: set[str] | None = None) -> str | None:
    """Keep visibly formatted phone numbers and ordinary 10–12 digit mobiles.

    Solid numbers outside that range are left unclassified. They often are
    folio numbers, account numbers, dates, or postal codes.
    """
    display = re.sub(r"\s+", " ", (raw or "")).strip()
    if not display or DATE_PATTERN.fullmatch(display):
        return None
    digits = re.sub(r"\D", "", display)
    if digits in (blocked_digits or set()):
        return None
    has_plus = display.startswith("+")
    has_separator = bool(re.search(r"[\s().-]", display))
    if has_plus:
        if not 8 <= len(digits) <= 15:
            return None
    elif has_separator:
        if not 10 <= len(digits) <= 15:
            return None
    elif not 10 <= len(digits) <= 12:
        return None
    return display


def phone_key(display: str) -> str:
    return re.sub(r"\D", "", display)
