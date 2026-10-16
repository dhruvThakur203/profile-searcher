import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "gclid",
    "fbclid",
    "srsltid",
    "ved",
    "usg",
    "sa",
    "ei",
    "oq",
    "source",
    "sxsrf",
}

GOOGLE_HOST_SUFFIXES = (
    "google.com",
    "google.co.in",
    "google.co.uk",
    "gstatic.com",
    "googleusercontent.com",
    "googleapis.com",
)

VISIBLE_HTTP_URL_PATTERN = re.compile(r"https?://[^\s<>\"'\)\]\}]+", re.IGNORECASE)
BLOCKED_SCHEMES = {"data", "blob", "javascript", "about", "file", "chrome", "chrome-extension"}


def unwrap_google_href(url: str) -> str:
    candidate = (url or "").strip()
    if candidate.startswith("/url?") or candidate.startswith("/search?"):
        candidate = "https://www.google.com" + candidate
    parts = urlsplit(candidate)
    host = (parts.hostname or "").lower()
    if "google." in host and parts.path == "/url":
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        target = params.get("q") or params.get("url") or ""
        if target.startswith("http://") or target.startswith("https://"):
            return target
    return (url or "").strip()


def is_google_navigation_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return any(host == suffix or host.endswith("." + suffix) for suffix in GOOGLE_HOST_SUFFIXES)


def _absolute_google_candidate(url: str) -> str:
    candidate = (url or "").strip()
    if candidate.startswith("/"):
        return "https://www.google.com" + candidate
    return candidate


def is_opaque_google_href(url: str) -> bool:
    """True when a result href is a Google wrapper that does not yield an http(s) target."""
    candidate = (url or "").strip()
    if not candidate:
        return False
    if candidate.startswith("/goto"):
        return True
    absolute = _absolute_google_candidate(candidate)
    parts = urlsplit(absolute)
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    if "google." in host and path.startswith("/goto"):
        return True
    if "google." in host and path == "/url":
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        target = params.get("q") or params.get("url") or ""
        if target and not (
            target.startswith("http://") or target.startswith("https://")
        ):
            return True
    return False


def is_resource_or_invalid_source_url(url: str) -> bool:
    """Reject browser/SVG/resource URLs that must never become research sources."""
    cleaned = (url or "").strip()
    if not cleaned:
        return True
    if "\\" in cleaned:
        return True
    lowered = cleaned.casefold()
    for scheme in BLOCKED_SCHEMES:
        if lowered.startswith(scheme + ":"):
            return True
    parts = urlsplit(cleaned)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        return True
    host = (parts.hostname or "").lower()
    if not host or "." not in host:
        return True
    path = (parts.path or "").lower()
    if host in {"w3.org", "www.w3.org"} and "/2000/svg" in path:
        return True
    if path.endswith("/svg") and host in {"w3.org", "www.w3.org"}:
        return True
    return False


def is_valid_external_source_url(url: str) -> bool:
    cleaned = (url or "").strip()
    if not cleaned.startswith("http://") and not cleaned.startswith("https://"):
        return False
    if is_resource_or_invalid_source_url(cleaned):
        return False
    if is_google_navigation_url(cleaned):
        return False
    return True


def extract_visible_destination_url(text: str) -> str:
    """Return the first visible external http(s) URL in result-card text.

    Does not invent a scheme for bare domains. Does not decode /goto payloads.
    """
    for match in VISIBLE_HTTP_URL_PATTERN.finditer(text or ""):
        candidate = match.group(0).rstrip(".,;:!?\\")
        if is_resource_or_invalid_source_url(candidate):
            continue
        if is_google_navigation_url(candidate):
            continue
        parts = urlsplit(candidate)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            continue
        return candidate
    return ""


def resolve_result_url(href: str, card_text: str = "") -> str:
    """Resolve a SERP href, falling back to a same-card visible destination URL."""
    resolved = canonical_url(href)
    if resolved:
        return resolved
    visible = extract_visible_destination_url(card_text)
    if not visible:
        return ""
    return canonical_url(visible)


def canonical_url(url: str) -> str:
    unwrapped = unwrap_google_href(url)
    if is_resource_or_invalid_source_url(unwrapped):
        return ""
    parts = urlsplit(unwrapped)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return ""
    if is_google_navigation_url(unwrapped):
        return ""
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
        ]
    )
    path = parts.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    cleaned = urlunsplit((parts.scheme.lower(), parts.hostname.lower(), path, query, ""))
    if not is_valid_external_source_url(cleaned):
        return ""
    return cleaned
