"""Holder-name combination searches: additive query plans, execution and resume."""

from dataclasses import replace

from src.models import PersonRecord
from src.normalize import normalize_text
from src.query_planner import (
    FAMILY_CORE,
    FAMILY_HOLDER_ADDRESS,
    FAMILY_HOLDERS,
    build_query_plan,
    holder_combination_specs,
)
from src.scraper.checkpoint import SqliteCheckpoint
from src.scraper.document_investigator import SourceDocument
from src.scraper.researcher import ResearchStatus, ShareholderResearcher
from src.scraper.search_client import SearchPolicy, StaticSearchClient
from src.tools.search import SearchResult
from tests.test_source_integration import MapSourceFetcher


PRIMARY = "MITHOO MURLI RAMCHANDANI"
JOINT1 = "SUNITA M RAMCHANDANI"
JOINT2 = "KAVITA RAJ SHARMA"
A1 = "12044 FALCON CREST WAY NORTHRIDGE"
A2 = "CALIFORNIA 91326 U.S.A"
EXISTING_CORE = {
    "Q1_FULL_NAME_FULL_ADDRESS",
    "Q_NAME_ADDRESS1_ADDRESS2",
    "Q2_NAME_ADDRESS1",
    "Q3_NAME_ADDRESS2",
    "Q_NAME_ADDRESS_FRAGMENT",
    "Q_NAME_PINCODE",
    "Q4_NAME_COUNTRY",
}


def _record(**overrides):
    data = dict(
        record_id="H",
        name=PRIMARY,
        address1=A1,
        address2=A2,
        pincode="91326",
        country="United States",
        joint1=JOINT1,
        joint2=JOINT2,
    )
    data.update(overrides)
    return PersonRecord(**data)


def _holder_plans(record):
    return {
        plan.strategy: plan.query
        for plan in build_query_plan(record)
        if plan.family == FAMILY_HOLDERS
    }


def _researcher(client, documents=None, **kwargs):
    checkpoint = kwargs.pop("checkpoint", None)
    extra = {"checkpoint": checkpoint} if checkpoint is not None else {}
    return ShareholderResearcher(
        client,
        SearchPolicy(delay_seconds=0, **kwargs),
        source_fetcher=MapSourceFetcher(documents or {}),
        use_result_text_when_unfetched=False,
        **extra,
    )


def _assert_well_formed(plans):
    keys = [normalize_text(plan.query) for plan in plans]
    assert len(keys) == len(set(keys))
    for plan in plans:
        assert plan.query
        assert plan.query == plan.query.strip()
        assert "  " not in plan.query


def test_three_holders_generate_three_pairs_and_one_triple():
    assert _holder_plans(_record()) == {
        "Q_HOLDER_PAIR_1_2": f"{PRIMARY} {JOINT1}",
        "Q_HOLDER_PAIR_2_3": f"{JOINT1} {JOINT2}",
        "Q_HOLDER_PAIR_3_1": f"{JOINT2} {PRIMARY}",
        "Q_ALL_HOLDERS": f"{PRIMARY} {JOINT1} {JOINT2}",
    }


def test_holder_combinations_are_appended_after_every_existing_strategy():
    plans = build_query_plan(_record())
    families = [plan.family for plan in plans]
    first_holder = families.index(FAMILY_HOLDERS)
    assert FAMILY_HOLDERS not in families[:first_holder]
    assert set(families[first_holder:]) == {FAMILY_HOLDERS}
    assert EXISTING_CORE <= {plan.strategy for plan in plans}
    _assert_well_formed(plans)


def test_existing_name_address_and_pincode_strategies_are_unchanged():
    with_joints = [p for p in build_query_plan(_record()) if p.family == FAMILY_CORE]
    without = [
        p for p in build_query_plan(_record(joint1="", joint2="")) if p.family == FAMILY_CORE
    ]
    assert with_joints == without
    by_strategy = {plan.strategy: plan.query for plan in with_joints}
    assert by_strategy["Q1_FULL_NAME_FULL_ADDRESS"] == f"{PRIMARY} {A1} {A2} 91326"
    assert by_strategy["Q2_NAME_ADDRESS1"] == f"{PRIMARY} {A1}"
    assert by_strategy["Q3_NAME_ADDRESS2"] == f"{PRIMARY} {A2}"
    assert by_strategy["Q_NAME_PINCODE"] == f"{PRIMARY} 91326"
    assert by_strategy["Q4_NAME_COUNTRY"] == f"{PRIMARY} United States"


def test_original_full_names_are_preserved_verbatim():
    record = _record(name="Dr. A.K. Sharma-Rao", joint1="Smt. Mira O'Neil", joint2="")
    assert _holder_plans(record) == {
        "Q_HOLDER_PAIR_1_2": "Dr. A.K. Sharma-Rao Smt. Mira O'Neil",
    }


def test_duplicate_and_empty_holder_fields_do_not_create_extra_queries():
    cases = [
        (dict(joint2=JOINT1.lower()), {"Q_HOLDER_PAIR_1_2"}),
        (dict(joint1=f"  {PRIMARY.title()} ", joint2=JOINT2), {"Q_HOLDER_PAIR_3_1"}),
        (dict(joint1="   ", joint2=""), set()),
        (dict(joint1=PRIMARY, joint2=PRIMARY), set()),
    ]
    for overrides, expected in cases:
        record = _record(**overrides)
        assert set(_holder_plans(record)) == expected, overrides
        _assert_well_formed(build_query_plan(record))


def test_two_holders_generate_only_their_pair():
    plans = _holder_plans(_record(joint2=""))
    assert plans == {"Q_HOLDER_PAIR_1_2": f"{PRIMARY} {JOINT1}"}
    assert all(query == query.strip() for query in plans.values())


def test_one_holder_generates_no_combination_but_keeps_existing_strategies():
    plans = build_query_plan(_record(joint1="", joint2=""))
    assert not any(plan.family == FAMILY_HOLDERS for plan in plans)
    assert EXISTING_CORE <= {plan.strategy for plan in plans}


def test_missing_primary_still_combines_the_joint_holders():
    record = _record(name="", joint1=JOINT1, joint2=JOINT2)
    assert holder_combination_specs(record) == [("Q_HOLDER_PAIR_2_3", [JOINT1, JOINT2])]
    plans = build_query_plan(record)
    assert _holder_plans(record) == {"Q_HOLDER_PAIR_2_3": f"{JOINT1} {JOINT2}"}
    # Joint-only address/company queries would masquerade as the shareholder.
    assert not any(plan.strategy.startswith("Q_JOINT_PRIMARY") for plan in plans)
    _assert_well_formed(plans)


def test_holder_queries_run_through_the_google_client_and_investigation():
    record = _record()
    strong_url = "https://records.example/holders-strong"
    names_only_url = "https://records.example/holders-names-only"
    second_url = "https://records.example/holders-second"
    documents = {
        strong_url: SourceDocument(
            source_url=strong_url,
            text=f"Folio 0001\t{PRIMARY}\t{JOINT1}\t{JOINT2}\t{A1}\t{A2}\n",
        ),
        second_url: SourceDocument(
            source_url=second_url,
            text=f"Folio 0099\t{PRIMARY}\t{A1}\t{A2}\n",
        ),
        names_only_url: SourceDocument(
            source_url=names_only_url,
            text=f"Meeting attendees: {PRIMARY}, {JOINT1} and {JOINT2}.\n",
        ),
    }
    holders = _holder_plans(record)
    client = StaticSearchClient()
    client.register(
        holders["Q_ALL_HOLDERS"],
        [
            SearchResult("All holders", strong_url, "folio snippet", serp_index=1),
            SearchResult("Second", second_url, "second snippet", serp_index=2),
        ],
    )
    client.register(
        holders["Q_HOLDER_PAIR_1_2"],
        [SearchResult("Attendees", names_only_url, "names only", serp_index=1)],
    )
    outcome = _researcher(client, documents).research_person(record)

    for query in holders.values():
        assert query in client.calls
    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert strong_url in outcome.source_links
    assert second_url in outcome.source_links
    assert "Q_ALL_HOLDERS" in outcome.source_strategies[strong_url]
    # Holder names alone never qualify a source.
    assert names_only_url not in outcome.source_links
    assert all(f.url != names_only_url for f in outcome.strong_findings)

    entry = next(item for item in outcome.log if item.strategy == "Q_ALL_HOLDERS")
    assert entry.result_details[0] == {
        "rank": 1,
        "url": strong_url,
        "title": "All holders",
        "snippet": "folio snippet",
    }
    assert entry.accepted_source_urls == [strong_url, second_url]


def test_names_only_holder_source_is_not_accepted():
    record = _record()
    url = "https://records.example/names-only"
    client = StaticSearchClient()
    client.register(
        _holder_plans(record)["Q_ALL_HOLDERS"], [SearchResult("N", url, "x")]
    )
    documents = {
        url: SourceDocument(
            source_url=url,
            text=f"Board list: {PRIMARY}; {JOINT1}; {JOINT2}. Contact 9876543210.\n",
        )
    }
    outcome = _researcher(client, documents).research_person(record)
    assert outcome.status != ResearchStatus.STRONG_MATCH
    assert outcome.source_links == []
    assert outcome.mobiles == []


def test_holder_budget_does_not_displace_existing_strategies():
    record = _record(scrip="Example Ltd", address3="NEAR PARK", address4="LA COUNTY")
    plans = build_query_plan(record)
    separate = {FAMILY_HOLDERS, FAMILY_HOLDER_ADDRESS}
    primary = [plan for plan in plans if plan.family not in separate]
    client = StaticSearchClient()
    _researcher(client).research_person(record)
    baseline = StaticSearchClient()
    _researcher(
        baseline, max_holder_combination_searches=0, max_searches_per_additional_holder=0
    ).research_person(record)
    extra_queries = {plan.query for plan in plans if plan.family in separate}
    assert [call for call in client.calls if call not in extra_queries] == baseline.calls
    assert baseline.calls == [plan.query for plan in primary][:12]


def test_old_finished_checkpoint_is_reopened_only_for_new_holder_queries(tmp_path):
    record = _record()
    store = SqliteCheckpoint(tmp_path / "cp.sqlite")
    first_client = StaticSearchClient()
    first = _researcher(
        first_client,
        checkpoint=store,
        max_holder_combination_searches=0,
        max_searches_per_additional_holder=0,
    ).research_person(record)
    new_families = {FAMILY_HOLDERS, FAMILY_HOLDER_ADDRESS}
    assert not any(entry.strategy.endswith(("_HOLDER2", "_HOLDER3")) for entry in first.log)
    assert not any(entry.strategy.startswith(("Q_HOLDER", "Q_ALL")) for entry in first.log)

    # Simulate a checkpoint written before these queries existed whose
    # completed list happens to contain the new labels.
    saved = store.load(record.record_id)
    assert saved.finished
    legacy = replace(
        saved,
        completed_strategies=saved.completed_strategies
        + ["Q_HOLDER_PAIR_1_2", "Q_ALL_HOLDERS", "Q2_NAME_ADDRESS1_HOLDER2"],
        completed_queries={},
    )
    store.save(record.record_id, legacy)

    resumed_client = StaticSearchClient()
    resumed = _researcher(resumed_client, checkpoint=store).research_person(record)
    new_plans = [p for p in build_query_plan(record) if p.family in new_families]
    assert resumed_client.calls == [plan.query for plan in new_plans]
    assert [entry.strategy for entry in resumed.log][: len(first.log)] == [
        entry.strategy for entry in first.log
    ]
    assert len(resumed.log) == len(first.log) + len(new_plans)

    class Exploding:
        def search(self, query):
            raise AssertionError("completed holder queries must not run again")

    again = ShareholderResearcher(
        Exploding(), SearchPolicy(delay_seconds=0), checkpoint=store
    ).research_person(record)
    assert len(again.log) == len(resumed.log)


def test_changed_holder_name_reruns_only_the_affected_queries(tmp_path):
    store = SqliteCheckpoint(tmp_path / "cp.sqlite")
    _researcher(StaticSearchClient(), checkpoint=store).research_person(_record())

    edited = _record(joint2="KAVITA R SHARMA")
    client = StaticSearchClient()
    _researcher(client, checkpoint=store).research_person(edited)
    expected = _holder_plans(edited)
    third_holder_address = [
        plan.query
        for plan in build_query_plan(edited)
        if plan.family == FAMILY_HOLDER_ADDRESS and plan.holder == 3
    ]
    assert third_holder_address
    assert client.calls == third_holder_address + [
        expected["Q_HOLDER_PAIR_2_3"],
        expected["Q_HOLDER_PAIR_3_1"],
        expected["Q_ALL_HOLDERS"],
    ]
