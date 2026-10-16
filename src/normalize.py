import re
import unicodedata


def normalize_text(value: str) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalize_name(value: str) -> str:
    return normalize_text(value)


def normalize_address(value: str) -> str:
    return normalize_text(value)
