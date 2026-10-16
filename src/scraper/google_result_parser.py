import re
from html.parser import HTMLParser

from ..tools.search import SearchResult
from .urls import resolve_result_url


class ParsedGoogleResult:
    def __init__(
        self,
        title: str,
        url: str,
        snippet: str,
        rank: int,
        serp_index: int | None = None,
    ) -> None:
        self.title = title
        self.url = url
        self.snippet = snippet
        self.rank = rank
        self.serp_index = serp_index if serp_index is not None else rank

    def as_search_result(self) -> SearchResult:
        return SearchResult(
            title=self.title,
            url=self.url,
            snippet=self.snippet,
            serp_index=self.serp_index,
        )


def clean_google_results(items: list[dict]) -> list[ParsedGoogleResult]:
    results = []
    seen = set()
    for dom_index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())
        snippet = " ".join(str(item.get("snippet") or "").split())
        href = str(item.get("url") or "")
        card_text = " ".join(
            part
            for part in (
                str(item.get("card_text") or ""),
                title,
                snippet,
            )
            if part
        )
        url = resolve_result_url(href, card_text)
        if not url or url in seen:
            continue
        seen.add(url)
        results.append(
            ParsedGoogleResult(
                title=title,
                url=url,
                snippet=snippet,
                rank=len(results) + 1,
                serp_index=dom_index,
            )
        )
    return results


def parse_google_html(html: str) -> list[ParsedGoogleResult]:
    cards = _parse_card_blocks(html or "")
    if cards:
        cleaned = clean_google_results(cards)
        if cleaned:
            return cleaned
    structured = _parse_structured_html(html or "")
    if structured:
        return clean_google_results(structured)
    return clean_google_results(_parse_loose_html(html or ""))


def _strip_tags(html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html or "", flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _parse_card_blocks(html: str) -> list[dict]:
    """Build one item per result card, keeping the card's visible text for /goto recovery."""
    items: list[dict] = []
    patterns = (
        r'<div[^>]*class="[^"]*\bMjjYud\b[^"]*"[^>]*>[\s\S]*?(?=<div[^>]*class="[^"]*\bMjjYud\b|$)',
        r'<div[^>]*class="[^"]*\bg\b[^"]*"[^>]*>[\s\S]*?(?=<div[^>]*class="[^"]*\bg\b|$)',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, html or "", flags=re.I):
            chunk = match.group(0)
            h3 = re.search(r"<h3\b[^>]*>([\s\S]*?)</h3>", chunk, flags=re.I)
            if not h3:
                continue
            title = _strip_tags(h3.group(1))
            href_match = re.search(
                r'<a[^>]+href="([^"]+)"[^>]*>\s*<h3\b',
                chunk,
                flags=re.I,
            )
            if not href_match:
                href_match = re.search(r'<a[^>]+href="([^"]+)"', chunk, flags=re.I)
            href = href_match.group(1) if href_match else ""
            snippet_match = re.search(
                r'class="[^"]*(?:VwiC3b|IsZvec)[^"]*"[^>]*>([\s\S]*?)</(?:div|span)>',
                chunk,
                flags=re.I,
            )
            snippet = _strip_tags(snippet_match.group(1)) if snippet_match else ""
            items.append(
                {
                    "title": title,
                    "url": href,
                    "snippet": snippet,
                    "card_text": _strip_tags(chunk),
                }
            )
        if items:
            return items
    return items


class _ResultHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._hrefs: list[str] = []
        self._in_h3 = False
        self._h3_parts: list[str] = []
        self._h3_href = ""
        self._in_snippet = False
        self._snippet_parts: list[str] = []
        self._pending: dict | None = None
        self._card_parts: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        attr = {key: value for key, value in attrs}
        if tag == "a":
            self._hrefs.append(attr.get("href") or "")
        if tag == "h3":
            self._in_h3 = True
            self._h3_parts = []
            self._h3_href = self._hrefs[-1] if self._hrefs else ""
            self._card_parts = []
        classes = attr.get("class") or ""
        if tag in {"div", "span"} and ("VwiC3b" in classes or "IsZvec" in classes):
            self._in_snippet = True
            self._snippet_parts = []

    def handle_endtag(self, tag) -> None:
        if tag == "a" and self._hrefs:
            self._hrefs.pop()
        if tag == "h3" and self._in_h3:
            self._in_h3 = False
            title = " ".join("".join(self._h3_parts).split())
            if title or self._h3_href:
                self._pending = {
                    "title": title,
                    "url": self._h3_href,
                    "snippet": "",
                    "card_text": title,
                }
                self.items.append(self._pending)
        if tag in {"div", "span"} and self._in_snippet:
            self._in_snippet = False
            snippet = " ".join("".join(self._snippet_parts).split())
            if self._pending is not None and not self._pending.get("snippet"):
                self._pending["snippet"] = snippet
                card = " ".join(
                    part
                    for part in (self._pending.get("card_text") or "", snippet)
                    if part
                )
                self._pending["card_text"] = " ".join(card.split())

    def handle_data(self, data) -> None:
        if self._in_h3:
            self._h3_parts.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)
        elif self._pending is not None and data and data.strip():
            # Capture cite / display-URL text that sits between the title and snippet.
            card = f"{self._pending.get('card_text') or ''} {data}"
            self._pending["card_text"] = " ".join(card.split())


def _parse_structured_html(html: str) -> list[dict]:
    parser = _ResultHTMLParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return []
    return [item for item in parser.items if item.get("title") or item.get("url")]


def _parse_loose_html(html: str) -> list[dict]:
    """Last resort: pair visible title text with the nearest http link."""
    parser = _ResultHTMLParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return []
    return parser.items
