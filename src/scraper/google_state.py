from enum import Enum


class GooglePageState(str, Enum):
    SERP = "SERP"
    NO_RESULTS = "NO_RESULTS"
    CONSENT = "CONSENT"
    CAPTCHA = "CAPTCHA"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


def detect_google_page(url: str, html: str, title: str = "") -> GooglePageState:
    """Classify a Google page. A CAPTCHA/block is reported and never solved."""
    current_url = (url or "").lower()
    text = f"{title}\n{html}".lower()
    if (
        "/sorry" in current_url
        or "unusual traffic" in text
        or "detected unusual traffic" in text
        or "g-recaptcha" in text
        or 'id="captcha' in text
        or "our systems have detected" in text
    ):
        return GooglePageState.CAPTCHA
    if _looks_like_serp(text):
        if "did not match any documents" in text or "no results found" in text:
            return GooglePageState.NO_RESULTS
        return GooglePageState.SERP
    if (
        "consent.google." in current_url
        or "before you continue" in text
        or "accept all" in text
    ):
        return GooglePageState.CONSENT
    if "that's an error" in text or "this page isn’t working" in text or "this page isn't working" in text:
        return GooglePageState.ERROR
    if "did not match any documents" in text:
        return GooglePageState.NO_RESULTS
    return GooglePageState.UNKNOWN


def _looks_like_serp(text: str) -> bool:
    return 'id="search"' in text or 'id="rso"' in text or 'id="rhs"' in text
