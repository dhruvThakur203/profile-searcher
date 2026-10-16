"""Fetch Google-discovered source URLs into SourceDocument text.

This module performs no web searching. It only opens already-discovered URLs.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .document_investigator.models import SourceDocument
from .urls import is_valid_external_source_url


logger = logging.getLogger("shareholder.source")

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_BYTES = 5_000_000
DEFAULT_USER_AGENT = "shareholder-research/0.1 (+local; source-fetch)"
MIN_USABLE_TEXT_CHARS = 40
CACHE_FORMAT_VERSION = "2"


class SourceFetchError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class FetchResult:
    ok: bool
    document: SourceDocument | None = None
    error_code: str = ""
    error_message: str = ""


def looks_like_pdf_url(url: str) -> bool:
    path = (urlsplit(url or "").path or "").lower()
    return path.endswith(".pdf") or ".pdf?" in ((url or "").lower())


def looks_like_pdf(
    url: str = "",
    content_type: str = "",
    body: bytes | None = None,
) -> bool:
    """Detect PDF from URL extension, Content-Type, and/or magic bytes."""
    if looks_like_pdf_url(url):
        return True
    lowered = (content_type or "").lower()
    if "application/pdf" in lowered or lowered.strip() == "pdf":
        return True
    if "pdf" in lowered and "html" not in lowered:
        return True
    if body and body[:5] == b"%PDF-":
        return True
    # Some servers prepend whitespace/BOM before the magic header.
    if body:
        sample = body.lstrip()[:8]
        if sample.startswith(b"%PDF-"):
            return True
    return False


def usable_text_length(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def document_has_usable_text(document: SourceDocument | None, *, minimum: int = MIN_USABLE_TEXT_CHARS) -> bool:
    if document is None:
        return False
    if usable_text_length(document.text) >= minimum:
        return True
    if document.page_texts and any(usable_text_length(page) >= minimum for page in document.page_texts):
        return True
    return False


def reject_empty_document(
    document: SourceDocument | None,
    *,
    document_type_hint: str = "",
) -> FetchResult | None:
    """Return a failure FetchResult when the document has no usable text."""
    if document is None:
        return FetchResult(
            ok=False,
            error_code="SOURCE_FETCH_ERROR",
            error_message="Source produced no document.",
        )
    if document_has_usable_text(document):
        return None
    dtype = (document.document_type or document_type_hint or "").lower()
    if dtype == "pdf" or looks_like_pdf_url(document.source_url):
        return FetchResult(
            ok=False,
            error_code="PDF_EXTRACTION_ERROR",
            error_message="PDF contained no usable extractable text.",
        )
    return FetchResult(
        ok=False,
        error_code="SOURCE_FETCH_ERROR",
        error_message="Source HTML/text contained no usable extractable content.",
    )


def log_fetch_result(
    *,
    url: str,
    final_url: str = "",
    content_type: str = "",
    document: SourceDocument | None = None,
    status: str,
    bytes_count: int | None = None,
) -> None:
    dtype = ""
    text_chars = 0
    if document is not None:
        dtype = document.document_type or ""
        text_chars = len(document.text or "")
    logger.info(
        "[FETCH] url=%s final_url=%s content_type=%s document_type=%s bytes=%s text_chars=%s status=%s",
        url,
        final_url or url,
        content_type or "",
        dtype,
        bytes_count if bytes_count is not None else "",
        text_chars,
        status,
    )


def log_pdf_result(
    *,
    url: str,
    bytes_count: int,
    pages: int | None,
    text_chars: int,
    extraction_status: str,
) -> None:
    logger.info(
        "[PDF] url=%s bytes=%s pages=%s text_chars=%s extraction_status=%s",
        url,
        bytes_count,
        pages if pages is not None else "",
        text_chars,
        extraction_status,
    )


@dataclass
class SourceFetcher:
    """Sequential source fetcher with in-memory URL cache."""

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_bytes: int = DEFAULT_MAX_BYTES
    cache_dir: str = "data/source_cache"
    user_agent: str = DEFAULT_USER_AGENT
    _cache: dict[str, FetchResult] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "SourceFetcher":
        return cls(
            timeout_seconds=float(os.environ.get("SOURCE_FETCH_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)),
            max_bytes=int(os.environ.get("SOURCE_FETCH_MAX_BYTES", DEFAULT_MAX_BYTES)),
            cache_dir=os.environ.get("SOURCE_CACHE_DIR", "data/source_cache"),
        )

    def fetch(self, url: str, title: str = "") -> FetchResult:
        cleaned = (url or "").strip()
        if cleaned in self._cache:
            cached = self._cache[cleaned]
            if cached.ok and cached.document is not None and document_has_usable_text(cached.document):
                return cached
            if cached.ok:
                # Drop stale empty success from memory.
                self._cache.pop(cleaned, None)
        disk = self._load_disk_cache(cleaned)
        if disk is not None:
            self._cache[cleaned] = disk
            return disk
        result = self._fetch_uncached(cleaned, title=title)
        self._cache[cleaned] = result
        return result

    def store(self, url: str, result: FetchResult) -> None:
        """Persist a successful fetch (e.g. browser-opened PDF) into cache."""
        cleaned = (url or "").strip()
        if not cleaned:
            return
        if result.ok and result.document is not None and document_has_usable_text(result.document):
            self._cache[cleaned] = result
            self._persist_cache(cleaned, result.document)
            if result.document.source_url and result.document.source_url != cleaned:
                self._cache[result.document.source_url] = result
                self._persist_cache(result.document.source_url, result.document)

    def _fetch_uncached(self, url: str, title: str = "") -> FetchResult:
        if not is_valid_external_source_url(url):
            return FetchResult(
                ok=False,
                error_code="SOURCE_FETCH_ERROR",
                error_message=f"Rejected invalid or non-external source URL: {url}",
            )
        try:
            body, content_type, final_url = self._download(url)
        except SourceFetchError as exc:
            log_fetch_result(url=url, status=exc.code, content_type="")
            return FetchResult(ok=False, error_code=exc.code, error_message=exc.message)
        except Exception as exc:
            log_fetch_result(url=url, status="SOURCE_FETCH_ERROR")
            return FetchResult(
                ok=False,
                error_code="SOURCE_FETCH_ERROR",
                error_message=f"Failed to fetch source: {exc}",
            )

        if looks_like_pdf(final_url, content_type, body):
            try:
                document = extract_pdf_document(body, source_url=final_url, title=title)
            except SourceFetchError as exc:
                log_pdf_result(
                    url=final_url,
                    bytes_count=len(body),
                    pages=None,
                    text_chars=0,
                    extraction_status=exc.code,
                )
                log_fetch_result(
                    url=url,
                    final_url=final_url,
                    content_type=content_type,
                    status=exc.code,
                    bytes_count=len(body),
                )
                return FetchResult(ok=False, error_code=exc.code, error_message=exc.message)
            except Exception as exc:
                log_pdf_result(
                    url=final_url,
                    bytes_count=len(body),
                    pages=None,
                    text_chars=0,
                    extraction_status="PDF_EXTRACTION_ERROR",
                )
                return FetchResult(
                    ok=False,
                    error_code="PDF_EXTRACTION_ERROR",
                    error_message=f"PDF extraction failed: {exc}",
                )
            empty = reject_empty_document(document, document_type_hint="pdf")
            if empty is not None:
                log_pdf_result(
                    url=final_url,
                    bytes_count=len(body),
                    pages=len(document.page_texts or []) if document else None,
                    text_chars=0,
                    extraction_status="PDF_EXTRACTION_ERROR",
                )
                return empty
            log_pdf_result(
                url=final_url,
                bytes_count=len(body),
                pages=len(document.page_texts or []),
                text_chars=len(document.text or ""),
                extraction_status="OK",
            )
            log_fetch_result(
                url=url,
                final_url=final_url,
                content_type=content_type,
                document=document,
                status="OK",
                bytes_count=len(body),
            )
            self._persist_cache(final_url, document)
            return FetchResult(ok=True, document=document)

        document = extract_html_document(body, source_url=final_url, title=title)
        empty = reject_empty_document(document, document_type_hint="html")
        if empty is not None:
            log_fetch_result(
                url=url,
                final_url=final_url,
                content_type=content_type,
                document=document,
                status=empty.error_code,
                bytes_count=len(body),
            )
            return empty
        log_fetch_result(
            url=url,
            final_url=final_url,
            content_type=content_type,
            document=document,
            status="OK",
            bytes_count=len(body),
        )
        self._persist_cache(final_url, document)
        return FetchResult(ok=True, document=document)

    def _download(self, url: str) -> tuple[bytes, str, str]:
        request = Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                content_type = response.headers.get("Content-Type", "")
                final_url = response.geturl() or url
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise SourceFetchError(
                            "SOURCE_FETCH_ERROR",
                            f"Source exceeded maximum size of {self.max_bytes} bytes.",
                        )
                    chunks.append(chunk)
        except HTTPError as exc:
            raise SourceFetchError("SOURCE_FETCH_ERROR", f"HTTP {exc.code} for {url}") from exc
        except URLError as exc:
            raise SourceFetchError("SOURCE_FETCH_ERROR", f"Network error for {url}: {exc}") from exc
        return b"".join(chunks), content_type, final_url

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        return Path(self.cache_dir) / f"{digest}.txt"

    def _load_disk_cache(self, url: str) -> FetchResult | None:
        path = self._cache_path(url)
        if not path.is_file():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
            headers, _, body = raw.partition("\n\n")
            meta = {}
            for line in headers.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip().upper()] = value.strip()
            version = meta.get("CACHE_VERSION", "1")
            source_url = meta.get("URL") or url
            document_type = (meta.get("TYPE") or "text").lower()
            page_marker = "\n\n-----PAGE-----\n\n"
            page_texts = None
            text = body
            if page_marker in body:
                page_texts = body.split(page_marker)
                text = "\n".join(page_texts)

            # Reject stale empty PDF-as-HTML cache entries from the broken SERP path.
            if version != CACHE_FORMAT_VERSION:
                if looks_like_pdf_url(source_url) or looks_like_pdf_url(url):
                    return None
                if document_type == "html" and looks_like_pdf_url(source_url):
                    return None
                if not usable_text_length(text):
                    return None

            document = SourceDocument(
                source_url=source_url,
                title=meta.get("TITLE") or "",
                document_type=meta.get("TYPE") or "text",
                text=text,
                page_texts=page_texts,
            )
            if not document_has_usable_text(document):
                return None
            if looks_like_pdf_url(source_url) and document.document_type != "pdf":
                return None
            return FetchResult(ok=True, document=document)
        except Exception:
            return None

    def _persist_cache(self, url: str, document: SourceDocument) -> None:
        if not document_has_usable_text(document):
            return
        try:
            root = Path(self.cache_dir)
            root.mkdir(parents=True, exist_ok=True)
            path = self._cache_path(url)
            if document.page_texts:
                body = "\n\n-----PAGE-----\n\n".join(document.page_texts)
            else:
                body = document.text
            payload = (
                f"CACHE_VERSION: {CACHE_FORMAT_VERSION}\n"
                f"URL: {document.source_url}\n"
                f"TITLE: {document.title}\n"
                f"TYPE: {document.document_type}\n\n"
                f"{body}"
            )
            path.write_text(payload, encoding="utf-8")
        except Exception:
            return


def extract_html_document(body: bytes, *, source_url: str, title: str = "") -> SourceDocument:
    text = body.decode("utf-8", errors="replace")
    parser = _VisibleTextHTMLParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        parser = _VisibleTextHTMLParser()
    visible = "\n".join(line for line in parser.lines if line.strip())
    page_title = parser.title.strip() or title or source_url
    return SourceDocument(
        source_url=source_url,
        title=page_title,
        document_type="html",
        text=visible,
        metadata={"content_type": "text/html"},
    )


def extract_pdf_document(body: bytes, *, source_url: str, title: str = "") -> SourceDocument:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise SourceFetchError(
            "PDF_EXTRACTION_ERROR",
            "pypdf is required for PDF source extraction.",
        ) from exc
    if not body:
        raise SourceFetchError("PDF_EXTRACTION_ERROR", "PDF body was empty.")
    try:
        reader = PdfReader(io.BytesIO(body))
    except Exception as exc:
        raise SourceFetchError("PDF_EXTRACTION_ERROR", f"Could not open PDF: {exc}") from exc

    page_texts: list[str] = []
    for page in reader.pages:
        try:
            page_texts.append(page.extract_text() or "")
        except Exception:
            page_texts.append("")
    joined = "\n".join(page_texts)
    if usable_text_length(joined) < MIN_USABLE_TEXT_CHARS:
        raise SourceFetchError(
            "PDF_EXTRACTION_ERROR",
            "PDF contained no extractable text.",
        )
    meta_title = ""
    try:
        meta = reader.metadata or {}
        meta_title = str(getattr(meta, "title", None) or meta.get("/Title") or "")
    except Exception:
        meta_title = ""
    return SourceDocument(
        source_url=source_url,
        title=(meta_title or title or source_url).strip(),
        document_type="pdf",
        text=joined,
        page_texts=page_texts,
        metadata={"page_count": len(page_texts), "byte_count": len(body)},
    )


class _VisibleTextHTMLParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False
        self._parts: list[str] = []
        self._title_parts: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        name = tag.lower()
        if name in self.SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if name == "title":
            self._in_title = True
        if name in {"p", "div", "tr", "li", "br", "h1", "h2", "h3", "h4", "section", "article"}:
            self._flush()

    def handle_endtag(self, tag) -> None:
        name = tag.lower()
        if name in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if name == "title":
            self._in_title = False
            self.title = " ".join("".join(self._title_parts).split())
            self._title_parts = []
        if name in {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "section", "article", "td", "th"}:
            self._flush()

    def handle_data(self, data) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        if data and data.strip():
            self._parts.append(data)

    def _flush(self) -> None:
        if not self._parts:
            return
        line = " ".join("".join(self._parts).split())
        self._parts = []
        if line:
            self.lines.append(line)
