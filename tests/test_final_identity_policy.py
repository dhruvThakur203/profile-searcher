"""Final identity policy: EXACT+1 address OR COMPATIBLE+2 same-record addresses."""

from pathlib import Path

from src.query_planner import build_query_plan
from src.scraper.document_investigator import (
    NameEquivalence,
    ShareholderTarget,
    SourceDocument,
    SourceMatchLevel,
    investigate,
)
from src.scraper.researcher import ResearchStatus, ShareholderResearcher, final_identity_accepts
from src.scraper.search_client import SearchPolicy, StaticSearchClient
from src.scraper.source_fetcher import extract_pdf_document
from src.tools.search import SearchResult
from tests.test_source_integration import MapSourceFetcher, person


FIXTURES = Path(__file__).parent / "fixtures" / "source_fetcher"


def _run(record, url: str, text: str = "", document: SourceDocument | None = None):
    doc = document or SourceDocument(
        source_url=url,
        title="fixture",
        document_type="text",
        text=text,
    )
    client = StaticSearchClient()
    query = build_query_plan(record)[0].query
    client.register(query, [SearchResult("R", url, "x")])
    return ShareholderResearcher(
        client,
        SearchPolicy(delay_seconds=0),
        source_fetcher=MapSourceFetcher({url: doc}),
        use_result_text_when_unfetched=False,
    ).research_person(record)


def test_compatible_name_plus_one_strong_address_is_not_final_strong():
    record = person(
        name="MITHOO MURLI RAMCHANDANI",
        address1="12044 FALCON CREST WAY NORTHRIDGE",
        address2="CALIFORNIA 91326 U.S.A",
    )
    url = "https://records.example/one-address"
    text = (
        "MITHOO M RAMCHANDANI\n"
        "12044 FALCON CREST WAY NORTHRIDGE\n"
    )
    inv = investigate(
        ShareholderTarget(
            name=record.name,
            address1=record.address1,
            address2=record.address2,
        ),
        SourceDocument(source_url=url, text=text),
    )
    assert inv.level == SourceMatchLevel.STRONG_MATCH
    assert any(
        item.name_equivalence == NameEquivalence.COMPATIBLE for item in inv.evaluations
    )
    strong = next(
        item for item in inv.evaluations if item.level == SourceMatchLevel.STRONG_MATCH
    )
    ok, _ = final_identity_accepts(strong)
    assert ok is False

    outcome = _run(record, url, text=text)
    # Not automatically accepted; retained only for human review.
    assert outcome.status == ResearchStatus.MANUAL_REVIEW
    assert outcome.source_links == []
    assert outcome.matched_name == ""
    assert [item.match_basis for item in outcome.review_findings] == [
        "COMPATIBLE_NAME + ONE_STRONG_ADDRESS"
    ]


def test_compatible_name_plus_two_weak_addresses_is_not_final_strong():
    record = person(
        name="MITHOO MURLI RAMCHANDANI",
        address1="12044 FALCON CREST WAY NORTHRIDGE",
        address2="CALIFORNIA 91326 U.S.A",
    )
    url = "https://records.example/weak-addresses"
    text = (
        "MITHOO M RAMCHANDANI\n"
        "999 OTHER ROAD ANYTOWN\n"
        "NEVADA 89101\n"
    )
    outcome = _run(record, url, text=text)
    assert outcome.status == ResearchStatus.NO_STRONG_MATCH
    assert outcome.source_links == []


def test_compatible_name_with_addresses_from_different_records_is_not_final_strong():
    record = person(
        name="MITHOO MURLI RAMCHANDANI",
        address1="12044 FALCON CREST WAY NORTHRIDGE",
        address2="CALIFORNIA 91326 U.S.A",
    )
    url = "https://records.example/split-records"
    # Name+Address1 in one record; Address2 only in a distant unrelated record.
    text = (
        "MITHOO M RAMCHANDANI\n"
        "12044 FALCON CREST WAY NORTHRIDGE\n"
        "\n" * 40
        + "UNRELATED HOLDER ZZZ\n"
        "CALIFORNIA 91326 U.S.A\n"
    )
    inv = investigate(
        ShareholderTarget(
            name=record.name,
            address1=record.address1,
            address2=record.address2,
        ),
        SourceDocument(source_url=url, text=text),
    )
    for evaluation in inv.evaluations:
        ok, _ = final_identity_accepts(evaluation)
        assert ok is False

    outcome = _run(record, url, text=text)
    assert outcome.status != ResearchStatus.STRONG_MATCH
    assert outcome.source_links == []
    # The distant Address2 is never combined with the name's record.
    for finding in outcome.review_findings:
        assert finding.matched_address_fields == ["address1"]
        assert finding.address2_similarity is None


def test_partial_name_plus_two_strong_addresses_is_not_final_strong():
    record = person(
        name="MITHOO MURLI RAMCHANDANI",
        address1="12044 FALCON CREST WAY NORTHRIDGE",
        address2="CALIFORNIA 91326 U.S.A",
    )
    url = "https://records.example/partial-name"
    text = (
        "MITHOO RAMCHANDANI\n"
        "12044 FALCON CREST WAY NORTHRIDGE\n"
        "CALIFORNIA 91326 U.S.A\n"
    )
    outcome = _run(record, url, text=text)
    assert outcome.status == ResearchStatus.NO_STRONG_MATCH
    assert outcome.source_links == []


def test_exact_name_plus_one_strong_address_is_final_strong():
    record = person(
        name="INDER RAJ VIG",
        address1="55 CAIRNHILL ROAD 27 06 CAIRNHILL PLAZA",
        address2="SINGAPORE 0922",
    )
    url = "https://records.example/exact-one"
    text = (
        "INDER RAJ VIG\n"
        "55 CAIRNHILL ROAD 27 06 CAIRNHILL PLAZA\n"
    )
    outcome = _run(record, url, text=text)
    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert url in outcome.source_links
    assert "EXACT_NAME + ONE_STRONG_ADDRESS" in outcome.evidence
    assert "name_match: EXACT" in outcome.evidence


def test_row5_compatible_name_two_strong_addresses_is_final_strong():
    body = (FIXTURES / "row5_milkfood_mithoo.pdf").read_bytes()
    url = (
        "https://milkfoodltd.com/img/downloads/"
        "Dividend-file-2023-24-NOv-2023.pdf"
    )
    document = extract_pdf_document(body, source_url=url, title="Milkfood")
    record = person(
        name="MITHOO MURLI RAMCHANDANI",
        address1="12044 FALCON CREST WAY NORTHRIDGE",
        address2="CALIFORNIA 91326 U.S.A",
    )
    inv = investigate(
        ShareholderTarget(
            name=record.name,
            address1=record.address1,
            address2=record.address2,
        ),
        document,
    )
    assert inv.level == SourceMatchLevel.STRONG_MATCH
    evaluation = next(
        item
        for item in inv.evaluations
        if item.level == SourceMatchLevel.STRONG_MATCH
    )
    assert evaluation.name_equivalence == NameEquivalence.COMPATIBLE
    assert evaluation.address_scores.get("address1", 0) >= 95
    assert evaluation.address_scores.get("address2", 0) >= 95
    ok, basis = final_identity_accepts(evaluation)
    assert ok is True
    assert basis == "COMPATIBLE_NAME + TWO_STRONG_ADDRESSES"

    outcome = _run(record, url, document=document)
    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert url in outcome.source_links
    assert "COMPATIBLE_NAME + TWO_STRONG_ADDRESSES" in outcome.evidence
    assert "name_match: COMPATIBLE" in outcome.evidence
    assert outcome.address1_match_pct is not None and outcome.address1_match_pct >= 95
    assert outcome.address2_match_pct is not None and outcome.address2_match_pct >= 95


def test_row4_exact_name_path_still_strong():
    body = (FIXTURES / "row4_ador_inder_raj_vig.pdf").read_bytes()
    url = "https://ador.example/unclaimed-dividend-2025.pdf"
    document = extract_pdf_document(body, source_url=url, title="Ador 2025")
    record = person(
        name="INDER RAJ VIG",
        address1="55 CAIRNHILL ROAD 27 06 CAIRNHILL PLAZA",
        address2="SINGAPORE 0922",
        country="Singapore",
    )
    outcome = _run(record, url, document=document)
    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert url in outcome.source_links
    assert "EXACT_NAME + ONE_STRONG_ADDRESS" in outcome.evidence or (
        "name_match: EXACT" in outcome.evidence
    )
    assert outcome.address1_match_pct is not None and outcome.address1_match_pct >= 95
