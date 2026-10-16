"""Obituary / condolence discovery: explicit-only lead extraction, never identity proof."""

from pathlib import Path

from src.models import PersonRecord
from src.query_planner import FAMILY_OBITUARY, build_query_plan
from src.scraper.document_investigator import (
    ShareholderTarget,
    SourceDocument,
    extract_obituary_leads,
)
from src.scraper.researcher import ResearchStatus, ShareholderResearcher
from src.scraper.search_client import SearchPolicy, StaticSearchClient
from src.tools.search import SearchResult
from tests.test_source_integration import MapSourceFetcher


FIXTURE = Path(__file__).parent / "fixtures" / "obituary" / "condolence_page.txt"
URL = "https://condolences.example/notices/1234"


def _doc(text=None, url=URL):
    return SourceDocument(
        source_url=url,
        title="Condolence Messages",
        document_type="html",
        text=text if text is not None else FIXTURE.read_text(encoding="utf-8"),
    )


def _lead(name="PRATAP MALPANI", **target):
    leads = extract_obituary_leads(ShareholderTarget(name=name, **target), _doc())
    assert len(leads) == 1
    return leads[0]


def test_condolence_fixture_captures_only_explicit_entities():
    lead = _lead()
    assert [name.casefold() for name in lead.names] == [
        "sudarshan",
        "ashutosh",
        "pratap",
        "vibha malpani",
    ]
    assert lead.locations == ["BANGALORE, INDIA"]
    assert lead.companies == ["VASP TRADING (S) PTE LTD"]
    assert lead.source_url == URL
    assert lead.title == "Condolence Messages"
    assert {"sympathies", "condolences", "demise"} <= set(lead.keywords)
    assert "THEIR BELOVED PARENTS PRATAP AND VIBHA MALPANI" in lead.excerpt
    # Page chrome around the passage is excluded.
    assert "Privacy" not in lead.excerpt and "Share this notice" not in lead.excerpt


def test_condolence_fixture_relationship_is_the_explicit_parent_wording():
    lead = _lead()
    assert len(lead.relationships) == 1
    relation = lead.relationships[0]
    assert relation.term == "PARENTS"
    assert relation.category == "parent"
    assert relation.named == ["PRATAP", "VIBHA MALPANI"]
    assert relation.sentence_names == ["SUDARSHAN", "ASHUTOSH"]
    assert "THEIR BELOVED PARENTS PRATAP AND VIBHA MALPANI" in relation.phrase


def test_shared_surname_coordination_is_reported_as_such():
    lead = _lead()
    assert lead.primary_name_mention == "COORDINATED_SHARED_SURNAME"
    assert lead.primary_name_text == "PRATAP AND VIBHA MALPANI"
    exact = _lead(name="VIBHA MALPANI")
    assert exact.primary_name_mention == "EXACT"


def test_no_surname_is_attached_to_children_and_company_is_only_a_lead():
    lead = _lead()
    # Sudarshan/Ashutosh are never given the Malpani surname.
    assert not any("SUDARSHAN MALPANI" in query.upper() for query in lead.follow_up_queries)
    assert not any("ASHUTOSH MALPANI" in query.upper() for query in lead.follow_up_queries)
    assert "SUDARSHAN BANGALORE INDIA" in lead.follow_up_queries
    assert "PRATAP MALPANI VASP TRADING (S) PTE LTD" in lead.follow_up_queries
    assert "not identity evidence" in lead.note


def test_joint_holder_mention_is_recorded():
    lead = _lead(joint1="VIBHA MALPANI", joint2="SOMEONE ELSE")
    assert lead.joint_holder_mentions == {"joint1": "EXACT", "joint2": "NOT_MENTIONED"}


def test_page_that_does_not_name_the_holder_is_not_a_lead():
    assert extract_obituary_leads(ShareholderTarget(name="ANIL MOHAN"), _doc()) == []
    # Surname alone is not enough.
    assert extract_obituary_leads(ShareholderTarget(name="RAVI MALPANI"), _doc()) == []


def test_same_page_names_without_relationship_wording_are_not_related():
    text = (
        "Funeral notices this week: PRATAP MALPANI of Pune. "
        "ANITA MALPANI of Mumbai. Services will be held on Monday."
    )
    leads = extract_obituary_leads(ShareholderTarget(name="PRATAP MALPANI"), _doc(text))
    assert leads and leads[0].relationships == []


def test_extraction_is_not_specific_to_the_example():
    text = (
        "In loving memory of Ramesh Kumar Shah, who passed away peacefully at home "
        "in Pune on 3 March. He is survived by his wife Meena and son Arjun."
    )
    lead = extract_obituary_leads(ShareholderTarget(name="RAMESH KUMAR SHAH"), _doc(text))[0]
    assert lead.primary_name_mention == "EXACT"
    assert lead.names == ["Ramesh Kumar Shah", "Meena", "Arjun"]
    assert lead.locations == ["Pune"]
    assert [(item.category, item.named) for item in lead.relationships] == [
        ("spouse", ["Meena"]),
        ("child", ["Arjun"]),
    ]
    assert lead.companies == []


def test_obituary_query_strategies():
    record = PersonRecord(
        record_id="O",
        name="PRATAP MALPANI",
        address1="12 MG ROAD INDIRANAGAR",
        address2="BANGALORE 560038",
        country="India",
        joint1="VIBHA MALPANI",
    )
    plans = {plan.strategy: plan for plan in build_query_plan(record)}
    assert plans["Q_OBIT_NAME_OBITUARY"].query == "PRATAP MALPANI obituary"
    assert plans["Q_OBIT_NAME_CONDOLENCES"].query == "PRATAP MALPANI condolences"
    assert plans["Q_OBIT_NAME_DEATH"].query == "PRATAP MALPANI death"
    assert plans["Q_OBIT_NAME_MEMORIAL"].query == "PRATAP MALPANI memorial"
    assert plans["Q_OBIT_NAME_LOCATION_OBITUARY"].query == "PRATAP MALPANI BANGALORE obituary"
    assert plans["Q_OBIT_JOINT_PRIMARY_OBITUARY"].query == (
        "VIBHA MALPANI PRATAP MALPANI obituary"
    )
    assert all(
        plan.family == FAMILY_OBITUARY for name, plan in plans.items() if name.startswith("Q_OBIT_")
    )


def _research(record, document, query):
    client = StaticSearchClient()
    client.register(query, [SearchResult("Condolences", document.source_url, "x")])
    return ShareholderResearcher(
        client,
        SearchPolicy(delay_seconds=0),
        source_fetcher=MapSourceFetcher({document.source_url: document}),
        use_result_text_when_unfetched=False,
    ).research_person(record)


def test_obituary_search_produces_lead_without_strong_match():
    record = PersonRecord(
        record_id="O1",
        name="PRATAP MALPANI",
        address1="12 MG ROAD INDIRANAGAR",
        address2="BANGALORE 560038",
        country="India",
    )
    outcome = _research(record, _doc(), "PRATAP MALPANI obituary")
    assert outcome.status == ResearchStatus.NO_STRONG_MATCH
    assert outcome.source_links == []
    assert outcome.matched_name == ""
    assert len(outcome.obituary_leads) == 1
    lead = outcome.obituary_leads[0]
    assert lead.source_url == URL and lead.original_url == URL
    assert lead.strategies == ["Q_OBIT_NAME_OBITUARY"]
    entry = next(item for item in outcome.log if item.strategy == "Q_OBIT_NAME_OBITUARY")
    assert any("OBITUARY_LEAD" in note for note in entry.investigation_notes)


def test_obituary_with_exact_name_and_location_alone_is_not_strong():
    # Exact holder name and the city are on the page, but no supplied address line.
    record = PersonRecord(
        record_id="O2",
        name="VIBHA MALPANI",
        address1="12 MG ROAD INDIRANAGAR",
        address2="BANGALORE 560038",
        country="India",
        joint1="PRATAP MALPANI",
    )
    outcome = _research(record, _doc(), build_query_plan(record)[0].query)
    assert outcome.status != ResearchStatus.STRONG_MATCH
    assert outcome.status != ResearchStatus.MANUAL_REVIEW
    assert outcome.source_links == []
    assert outcome.obituary_leads and outcome.obituary_leads[0].primary_name_mention == "EXACT"


def test_obituary_queries_are_skipped_once_strong_match_exists():
    record = PersonRecord(
        record_id="O3",
        name="PRATAP MALPANI",
        address1="12 MG ROAD INDIRANAGAR",
        address2="BANGALORE 560038",
        country="India",
    )
    strong = SourceDocument(
        source_url="https://records.example/strong",
        text="PRATAP MALPANI\n12 MG ROAD INDIRANAGAR\nBANGALORE 560038\n",
    )
    client = StaticSearchClient()
    client.register(
        build_query_plan(record)[0].query,
        [SearchResult("R", strong.source_url, "x")],
    )
    outcome = ShareholderResearcher(
        client,
        SearchPolicy(delay_seconds=0),
        source_fetcher=MapSourceFetcher({strong.source_url: strong}),
        use_result_text_when_unfetched=False,
    ).research_person(record)
    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert not any("obituary" in call or "condolences" in call for call in client.calls)
    assert any(
        item.startswith("Q_OBIT_NAME_OBITUARY (not executed: STRONG_MATCH already found")
        for item in outcome.skipped_strategies
    )
