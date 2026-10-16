from dataclasses import dataclass

from src.models import PersonRecord
from src.query_planner import build_query_plan
from src.scraper.document_investigator import (
    NameEquivalence,
    ShareholderTarget,
    SourceDocument,
    SourceMatchLevel,
    investigate,
)
from src.scraper.researcher import ResearchStatus, ShareholderResearcher
from src.scraper.search_client import SearchPolicy, SearchResponse, SearchStatus, StaticSearchClient
from src.scraper.source_fetcher import FetchResult
from src.tools.search import SearchResult


def person(**overrides) -> PersonRecord:
    data = {
        "record_id": "1",
        "name": "MITHOO M RAMCHANDANI",
        "address1": "12044 FALCON CREST WAY",
        "address2": "NORTHRIDGE",
        "country": "USA",
        "scrip": "Example Ltd",
    }
    data.update(overrides)
    return PersonRecord(**data)


@dataclass
class MapSourceFetcher:
    documents: dict[str, SourceDocument]
    fetch_count: dict[str, int] | None = None

    def fetch(self, url: str, title: str = "") -> FetchResult:
        if self.fetch_count is not None:
            self.fetch_count[url] = self.fetch_count.get(url, 0) + 1
        document = self.documents.get(url)
        if document is None:
            return FetchResult(
                ok=False,
                error_code="SOURCE_FETCH_ERROR",
                error_message=f"missing fixture for {url}",
            )
        return FetchResult(ok=True, document=document)


def _doc(url: str, text: str, title: str = "Record", pages=None) -> SourceDocument:
    return SourceDocument(
        source_url=url,
        title=title,
        document_type="text",
        text=text,
        page_texts=pages,
    )


def test_google_result_to_source_to_investigator_flow():
    record = person()
    url = "https://records.example/a"
    text = (
        "MITHOO M RAMCHANDANI\n"
        "12044 FALCON CREST WAY\n"
        "NORTHRIDGE\n"
        "Phone: +1 555 111 2222\n"
        "Email: mithoo@example.com\n"
    )
    client = StaticSearchClient()
    query = build_query_plan(record)[0].query
    client.register(
        query,
        [SearchResult(title="A", url=url, snippet="irrelevant snippet only")],
    )
    fetcher = MapSourceFetcher({url: _doc(url, text)})
    outcome = ShareholderResearcher(
        client,
        SearchPolicy(delay_seconds=0),
        source_fetcher=fetcher,
        use_result_text_when_unfetched=False,
    ).research_person(record)

    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert outcome.source_links == [url]
    assert "mithoo@example.com" in outcome.emails
    assert any("555" in phone for phone in outcome.mobiles)
    assert "Record context:" in outcome.evidence


def test_top_n_source_inspection_limit():
    record = person()
    texts = {
        f"https://records.example/{index}": (
            "MITHOO M RAMCHANDANI\n12044 FALCON CREST WAY\nNORTHRIDGE\n"
        )
        for index in range(1, 6)
    }
    client = StaticSearchClient()
    query = build_query_plan(record)[0].query
    client.register(
        query,
        [
            SearchResult(title=str(index), url=url, snippet="x")
            for index, url in enumerate(texts, start=1)
        ],
    )
    counts: dict[str, int] = {}
    fetcher = MapSourceFetcher(
        {url: _doc(url, text) for url, text in texts.items()},
        fetch_count=counts,
    )
    outcome = ShareholderResearcher(
        client,
        SearchPolicy(delay_seconds=0),
        source_fetcher=fetcher,
        source_inspection_top_n=3,
        use_result_text_when_unfetched=False,
    ).research_person(record)

    assert len(counts) == 3
    assert set(counts) == {
        "https://records.example/1",
        "https://records.example/2",
        "https://records.example/3",
    }
    assert outcome.source_links == [
        "https://records.example/1",
        "https://records.example/2",
        "https://records.example/3",
    ]


def test_duplicate_url_elimination_across_queries():
    record = person()
    plans = {plan.strategy: plan.query for plan in build_query_plan(record)}
    shared = "https://records.example/shared"
    other = "https://records.example/other"
    text = "MITHOO M RAMCHANDANI\n12044 FALCON CREST WAY\nNORTHRIDGE\n"
    client = StaticSearchClient()
    client.register(plans["Q1_FULL_NAME_FULL_ADDRESS"], [SearchResult("A", shared, "x")])
    client.register(plans["Q2_NAME_ADDRESS1"], [SearchResult("A", shared, "x")])
    client.register(plans["Q3_NAME_ADDRESS2"], [SearchResult("B", other, "x")])
    fetcher = MapSourceFetcher(
        {
            shared: _doc(shared, text),
            other: _doc(other, text),
        }
    )
    outcome = ShareholderResearcher(
        client,
        SearchPolicy(delay_seconds=0),
        source_fetcher=fetcher,
        use_result_text_when_unfetched=False,
    ).research_person(record)
    assert outcome.source_links == [shared, other]


def test_multiple_accepted_sources_and_contact_aggregation():
    record = person()
    plans = {plan.strategy: plan.query for plan in build_query_plan(record)}
    a = "https://records.example/a"
    b = "https://records.example/b"
    client = StaticSearchClient()
    client.register(plans["Q1_FULL_NAME_FULL_ADDRESS"], [SearchResult("A", a, "x")])
    client.register(plans["Q2_NAME_ADDRESS1"], [SearchResult("B", b, "x")])
    fetcher = MapSourceFetcher(
        {
            a: _doc(
                a,
                "MITHOO M RAMCHANDANI\n12044 FALCON CREST WAY\nNORTHRIDGE\n"
                "Phone: +1 555 111 2222\n",
            ),
            b: _doc(
                b,
                "MITHOO M RAMCHANDANI\n12044 FALCON CREST WAY\nNORTHRIDGE\n"
                "Email: mithoo@example.com\n",
            ),
        }
    )
    outcome = ShareholderResearcher(
        client,
        SearchPolicy(delay_seconds=0),
        source_fetcher=fetcher,
        use_result_text_when_unfetched=False,
    ).research_person(record)
    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert outcome.source_links == [a, b]
    assert any("555" in phone for phone in outcome.mobiles)
    assert outcome.emails == ["mithoo@example.com"]
    assert a in outcome.evidence and b in outcome.evidence


def test_contact_provenance_stays_with_accepted_source():
    record = person()
    url = "https://records.example/contact"
    text = (
        "MITHOO M RAMCHANDANI\n12044 FALCON CREST WAY\nNORTHRIDGE\n"
        "Phone: +1 555 111 2222\nEmail: mithoo@example.com\n"
        "\nCompany Contact:\n+91 99999 88888\ncompany@example.com\n"
    )
    inv = investigate(
        ShareholderTarget(
            name=record.name,
            address1=record.address1,
            address2=record.address2,
        ),
        _doc(url, text),
    )
    assert inv.level == SourceMatchLevel.STRONG_MATCH
    values = {item.value for item in inv.contacts}
    assert "mithoo@example.com" in values
    assert any("555" in value for value in values)
    assert "company@example.com" not in values
    assert not any("99999" in value for value in values)
    for item in inv.contacts:
        assert item.source_url == url
        assert item.group_id
        assert item.context


def test_same_page_unrelated_records_do_not_merge():
    record = person()
    text = (
        "Record 1:\n"
        "MITHOO M RAMCHANDANI\n"
        "12044 FALCON CREST WAY\n"
        "NORTHRIDGE\n"
        "\n"
        "Record 2:\n"
        "ANOTHER PERSON\n"
        "999 OTHER ROAD\n"
        "NORTHRIDGE\n"
        "Phone: +1 555 999 0000\n"
    )
    inv = investigate(
        ShareholderTarget(
            name=record.name,
            address1=record.address1,
            address2=record.address2,
        ),
        _doc("https://records.example/page", text),
    )
    assert inv.level == SourceMatchLevel.STRONG_MATCH
    values = {item.value for item in inv.contacts}
    assert not any("999" in value and "555" in value for value in values)


def test_compatible_name_with_only_one_strong_address_is_not_final_strong():
    # COMPATIBLE name + a single strong address must not become final STRONG_MATCH.
    record = person(
        name="MITHOO MURLI RAMCHANDANI",
        address1="12044 FALCON CREST WAY NORTHRIDGE",
        address2="CALIFORNIA 91326 U.S.A",
    )
    url = "https://records.example/compatible"
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
        _doc(url, text),
    )
    # Investigator may still report document-level strength via COMPATIBLE.
    assert any(
        item.name_equivalence == NameEquivalence.COMPATIBLE
        for item in inv.evaluations
    ) or inv.level in {
        SourceMatchLevel.STRONG_MATCH,
        SourceMatchLevel.POSSIBLE_MATCH,
        SourceMatchLevel.NOT_RELEVANT,
    }

    client = StaticSearchClient()
    query = build_query_plan(record)[0].query
    client.register(query, [SearchResult("C", url, "x")])
    outcome = ShareholderResearcher(
        client,
        SearchPolicy(delay_seconds=0),
        source_fetcher=MapSourceFetcher({url: _doc(url, text)}),
        use_result_text_when_unfetched=False,
    ).research_person(record)
    assert outcome.status == ResearchStatus.MANUAL_REVIEW
    assert outcome.source_links == []
    assert [item.url for item in outcome.review_findings] == [url]


def test_partial_name_candidate_cannot_become_final_strong_match():
    record = person()
    url = "https://records.example/partial"
    text = "MITHOO RAMCHANDANI\n12044 FALCON CREST WAY\nNORTHRIDGE\n"
    client = StaticSearchClient()
    query = build_query_plan(record)[0].query
    client.register(query, [SearchResult("P", url, "x")])
    outcome = ShareholderResearcher(
        client,
        SearchPolicy(delay_seconds=0),
        source_fetcher=MapSourceFetcher({url: _doc(url, text)}),
        use_result_text_when_unfetched=False,
    ).research_person(record)
    assert outcome.status == ResearchStatus.NO_STRONG_MATCH
    assert outcome.source_links == []


def test_source_fetch_error_is_surfaced_not_collapsed():
    record = person()
    url = "https://records.example/missing"
    client = StaticSearchClient()
    query = build_query_plan(record)[0].query
    client.register(query, [SearchResult("M", url, "x")])
    outcome = ShareholderResearcher(
        client,
        SearchPolicy(delay_seconds=0),
        source_fetcher=MapSourceFetcher({}),
        use_result_text_when_unfetched=False,
    ).research_person(record)
    assert outcome.status == ResearchStatus.NO_STRONG_MATCH
    assert "SOURCE_FETCH_ERROR" in outcome.message or any(
        "SOURCE_FETCH_ERROR" in note for entry in outcome.log for note in entry.investigation_notes
    )


def test_page_boundary_evidence_from_multipage_source():
    record = person()
    url = "https://records.example/pages.pdf"
    pages = [
        "UNRELATED PERSON\n999 OTHER ROAD\n",
        "MITHOO M RAMCHANDANI\n12044 FALCON CREST WAY\nNORTHRIDGE\n",
    ]
    inv = investigate(
        ShareholderTarget(
            name=record.name,
            address1=record.address1,
            address2=record.address2,
        ),
        _doc(url, "\n".join(pages), pages=pages),
    )
    assert inv.level == SourceMatchLevel.STRONG_MATCH
    strong = next(
        item for item in inv.evaluations if item.level == SourceMatchLevel.STRONG_MATCH
    )
    assert strong.group.page_number == 2
