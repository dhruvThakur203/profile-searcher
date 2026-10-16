"""Focused regression tests for SERP/PDF extraction fix (Rows 4 and 5)."""

from pathlib import Path

import pytest

from src.scraper.document_investigator import (
    NameEquivalence,
    ShareholderTarget,
    SourceDocument,
    SourceMatchLevel,
    investigate,
)
from src.query_planner import build_query_plan
from src.scraper.researcher import ResearchStatus, ShareholderResearcher
from src.scraper.search_client import SearchPolicy, StaticSearchClient
from src.scraper.source_fetcher import (
    CACHE_FORMAT_VERSION,
    SourceFetchError,
    SourceFetcher,
    extract_pdf_document,
    looks_like_pdf,
    looks_like_pdf_url,
    reject_empty_document,
)
from src.tools.search import SearchResult
from tests.test_source_integration import MapSourceFetcher, person


FIXTURES = Path(__file__).parent / "fixtures" / "source_fetcher"


def test_pdf_url_detection():
    assert looks_like_pdf_url("https://example.com/file.pdf")
    assert looks_like_pdf_url("https://example.com/path/Dividend-file-2023-24-NOv-2023.pdf")
    assert looks_like_pdf_url("https://cdn.example.com/a.PDF")
    assert looks_like_pdf("https://example.com/file.pdf")
    assert not looks_like_pdf_url("https://example.com/file.html")
    assert not looks_like_pdf("https://example.com/report", "text/html", b"<html>")


def test_pdf_content_type_detection():
    assert looks_like_pdf(
        "https://example.com/download",
        "application/pdf",
        b"%PDF-1.4...",
    )
    assert looks_like_pdf(
        "https://example.com/download;jsessionid=1",
        "application/pdf; charset=binary",
        b"",
    )
    # Magic bytes after redirect without .pdf extension.
    assert looks_like_pdf(
        "https://example.com/download?id=9",
        "application/octet-stream",
        b"%PDF-1.7\n...",
    )
    assert looks_like_pdf(
        "https://example.com/download",
        "",
        b"\r\n%PDF-1.4\n",
    )


def test_pdf_byte_extraction_produces_nonempty_text():
    body = (FIXTURES / "sample.pdf").read_bytes()
    document = extract_pdf_document(
        body,
        source_url="https://records.example/file.pdf",
        title="PDF title",
    )
    assert document.document_type == "pdf"
    assert document.text.strip()
    assert "MITHOO M RAMCHANDANI" in document.text


def test_empty_pdf_extraction_failure():
    body = (FIXTURES / "empty_text.pdf").read_bytes()
    with pytest.raises(SourceFetchError) as exc:
        extract_pdf_document(body, source_url="https://example.com/empty.pdf")
    assert exc.value.code == "PDF_EXTRACTION_ERROR"

    empty_doc = SourceDocument(
        source_url="https://example.com/empty.pdf",
        document_type="pdf",
        text="",
        page_texts=[""],
    )
    rejected = reject_empty_document(empty_doc, document_type_hint="pdf")
    assert rejected is not None
    assert rejected.ok is False
    assert rejected.error_code == "PDF_EXTRACTION_ERROR"


def test_stale_empty_cache_rejection(tmp_path, monkeypatch):
    calls = {"n": 0}
    pdf_body = (FIXTURES / "row4_ador_inder_raj_vig.pdf").read_bytes()

    def fake_download(self, url):
        calls["n"] += 1
        return pdf_body, "application/pdf", url

    monkeypatch.setattr(SourceFetcher, "_download", fake_download)
    cache_dir = tmp_path / "cache"
    fetcher = SourceFetcher(cache_dir=str(cache_dir))
    url = "https://ador.example/shareholder-2025.pdf"

    # Simulate the broken SERP path cache: TYPE=html, empty body, no version.
    stale_path = fetcher._cache_path(url)
    cache_dir.mkdir(parents=True, exist_ok=True)
    stale_path.write_text(
        "URL: https://ador.example/shareholder-2025.pdf\n"
        "TITLE: Ador 2025\n"
        "TYPE: html\n\n",
        encoding="utf-8",
    )

    result = fetcher.fetch(url)
    assert result.ok is True
    assert result.document is not None
    assert result.document.document_type == "pdf"
    assert "INDER RAJ VIG" in result.document.text
    assert calls["n"] == 1

    # Valid versioned PDF cache with text is reusable (no second download).
    second = fetcher.fetch(url)
    assert second.ok is True
    assert calls["n"] == 1
    cached = stale_path.read_text(encoding="utf-8")
    assert f"CACHE_VERSION: {CACHE_FORMAT_VERSION}" in cached
    assert "TYPE: pdf" in cached


def test_row4_investigator_regression_from_pdf_bytes():
    body = (FIXTURES / "row4_ador_inder_raj_vig.pdf").read_bytes()
    document = extract_pdf_document(
        body,
        source_url="https://ador.example/unclaimed-dividend-2025.pdf",
        title="Ador 2025",
    )
    assert document.document_type == "pdf"
    assert len(document.text) > 0

    target = ShareholderTarget(
        name="INDER RAJ VIG",
        address1="55 CAIRNHILL ROAD 27 06 CAIRNHILL PLAZA",
        address2="SINGAPORE 0922",
        record_id="row4",
    )
    result = investigate(target, document)
    assert result.level == SourceMatchLevel.STRONG_MATCH
    assert result.evaluations
    evaluation = result.evaluations[0]
    assert evaluation.name_equivalence == NameEquivalence.EXACT
    assert evaluation.address_scores.get("address1", 0) >= 95
    assert evaluation.address_scores.get("address2") is not None
    assert evaluation.address_scores["address2"] >= 95


def test_row5_investigator_regression_from_pdf_bytes():
    body = (FIXTURES / "row5_milkfood_mithoo.pdf").read_bytes()
    document = extract_pdf_document(
        body,
        source_url=(
            "https://milkfoodltd.com/img/downloads/"
            "Dividend-file-2023-24-NOv-2023.pdf"
        ),
        title="Milkfood dividend",
    )
    assert document.document_type == "pdf"
    assert "MITHOO M RAMCHANDANI" in document.text
    assert "12044" in document.text
    assert "FALCON CREST WAY" in document.text
    assert "NORTHRIDGE" in document.text
    assert "91326" in document.text

    target = ShareholderTarget(
        name="MITHOO MURLI RAMCHANDANI",
        address1="12044 FALCON CREST WAY NORTHRIDGE",
        address2="CALIFORNIA 91326 U.S.A",
        record_id="row5",
    )
    result = investigate(target, document)
    assert result.level == SourceMatchLevel.STRONG_MATCH
    evaluation = next(
        item
        for item in result.evaluations
        if item.level == SourceMatchLevel.STRONG_MATCH
    )
    assert evaluation.name_equivalence == NameEquivalence.COMPATIBLE
    assert evaluation.address_scores.get("address1", 0) >= 95
    assert evaluation.address_scores.get("address2", 0) >= 90


def test_row5_compatible_name_with_two_strong_addresses_is_final_strong():
    body = (FIXTURES / "row5_milkfood_mithoo.pdf").read_bytes()
    url = (
        "https://milkfoodltd.com/img/downloads/"
        "Dividend-file-2023-24-NOv-2023.pdf"
    )
    document = extract_pdf_document(body, source_url=url, title="Milkfood")
    # Document-level evidence is strong via COMPATIBLE name.
    inv = investigate(
        ShareholderTarget(
            name="MITHOO MURLI RAMCHANDANI",
            address1="12044 FALCON CREST WAY NORTHRIDGE",
            address2="CALIFORNIA 91326 U.S.A",
        ),
        document,
    )
    assert inv.level == SourceMatchLevel.STRONG_MATCH
    assert any(
        item.name_equivalence == NameEquivalence.COMPATIBLE for item in inv.evaluations
    )

    record = person(
        name="MITHOO MURLI RAMCHANDANI",
        address1="12044 FALCON CREST WAY NORTHRIDGE",
        address2="CALIFORNIA 91326 U.S.A",
    )
    client = StaticSearchClient()
    query = build_query_plan(record)[0].query
    client.register(query, [SearchResult("Milkfood dividend PDF", url, "snippet")])
    outcome = ShareholderResearcher(
        client,
        SearchPolicy(delay_seconds=0),
        source_fetcher=MapSourceFetcher({url: document}),
        use_result_text_when_unfetched=False,
    ).research_person(record)
    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert url in outcome.source_links
    assert "COMPATIBLE_NAME + TWO_STRONG_ADDRESSES" in outcome.evidence
    assert "name_match: COMPATIBLE" in outcome.evidence
