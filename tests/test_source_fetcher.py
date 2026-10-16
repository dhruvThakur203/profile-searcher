from pathlib import Path

from src.scraper.document_investigator import SourceDocument, investigate, SourceMatchLevel
from src.scraper.source_fetcher import (
    SourceFetcher,
    extract_html_document,
    extract_pdf_document,
)


FIXTURES = Path(__file__).parent / "fixtures" / "source_fetcher"


def test_html_source_extracts_visible_text_not_raw_markup():
    body = (FIXTURES / "sample.html").read_bytes()
    document = extract_html_document(
        body,
        source_url="https://records.example/directory",
        title="fallback",
    )
    assert document.document_type == "html"
    assert document.title == "Shareholder Directory"
    assert "MITHOO M RAMCHANDANI" in document.text
    assert "12044 FALCON CREST WAY" in document.text
    assert "<script>" not in document.text
    assert "console.log" not in document.text
    assert ".nav" not in document.text


def test_pdf_source_preserves_page_boundaries():
    body = (FIXTURES / "sample.pdf").read_bytes()
    document = extract_pdf_document(
        body,
        source_url="https://records.example/file.pdf",
        title="PDF title",
    )
    assert document.document_type == "pdf"
    assert document.page_texts is not None
    assert len(document.page_texts) == 2
    assert "UNRELATED PERSON" in document.page_texts[0]
    assert "MITHOO M RAMCHANDANI" in document.page_texts[1]
    assert "12044 FALCON CREST WAY" in document.page_texts[1]


def test_invalid_urls_are_rejected_without_fetch():
    fetcher = SourceFetcher(cache_dir="data/test_source_cache_invalid")
    rejected = [
        "javascript:alert(1)",
        "data:text/plain,hello",
        "blob:https://example.com/1",
        "https://www.google.com/search?q=test",
        "http://www.w3.org/2000/svg",
        "not-a-url",
    ]
    for url in rejected:
        result = fetcher.fetch(url)
        assert result.ok is False
        assert result.error_code == "SOURCE_FETCH_ERROR"


def test_source_document_created_from_html_bytes():
    body = b"<html><head><title>T</title></head><body><p>Hello World</p></body></html>"
    document = extract_html_document(body, source_url="https://example.com/a")
    assert isinstance(document, SourceDocument)
    assert document.source_url == "https://example.com/a"
    assert "Hello World" in document.text


def test_document_investigator_receives_source_document_from_html():
    from src.scraper.document_investigator import ShareholderTarget

    body = (FIXTURES / "sample.html").read_bytes()
    document = extract_html_document(
        body,
        source_url="https://records.example/directory",
    )
    target = ShareholderTarget(
        name="MITHOO M RAMCHANDANI",
        address1="12044 FALCON CREST WAY",
        address2="NORTHRIDGE",
    )
    result = investigate(target, document)
    assert result.level == SourceMatchLevel.STRONG_MATCH
    assert any(contact.value == "mithoo@example.com" for contact in result.contacts)


def test_source_fetcher_memory_and_disk_cache(tmp_path, monkeypatch):
    calls = {"n": 0}
    body = (FIXTURES / "sample.html").read_bytes()

    def fake_download(self, url):
        calls["n"] += 1
        return body, "text/html", url

    monkeypatch.setattr(SourceFetcher, "_download", fake_download)
    fetcher = SourceFetcher(cache_dir=str(tmp_path / "cache"))
    url = "https://records.example/cached"
    first = fetcher.fetch(url)
    second = fetcher.fetch(url)
    assert first.ok and second.ok
    assert calls["n"] == 1

    # New fetcher instance should reload from disk cache.
    other = SourceFetcher(cache_dir=str(tmp_path / "cache"))
    third = other.fetch(url)
    assert third.ok
    assert calls["n"] == 1
    assert "MITHOO M RAMCHANDANI" in third.document.text
