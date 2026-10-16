from src.matching import evaluate_candidate
from src.models import MatchStatus, PersonRecord
from src.query_planner import build_query_plan
from src.scraper.researcher import ResearchStatus, ShareholderResearcher
from src.scraper.result_parser import parse_search_result
from src.scraper.search_client import (
    PoliteSearchClient,
    SearchPolicy,
    SearchResponse,
    SearchStatus,
    StaticSearchClient,
)
from src.tools.search import SearchResult


def person(**overrides) -> PersonRecord:
    data = {
        "record_id": "1",
        "name": "Dhruv Singh Thakur",
        "address1": "123 ABC Road",
        "address2": "Singapore 123456",
        "country": "Singapore",
        "scrip": "Example Ltd",
    }
    data.update(overrides)
    return PersonRecord(**data)


def snippet(name: str, extra: str = "") -> str:
    text = f"{name} 123 ABC Road Singapore 123456"
    if extra:
        text = f"{text} {extra}"
    return text


def result(url: str, text: str, title: str = "Public record") -> SearchResult:
    return SearchResult(title=title, url=url, snippet=text)


def researcher_for(client: StaticSearchClient) -> ShareholderResearcher:
    return ShareholderResearcher(client, SearchPolicy(delay_seconds=0))


def register_q1(client: StaticSearchClient, record: PersonRecord, results) -> None:
    query = build_query_plan(record)[0].query
    client.register(query, results)


def test_exact_name_and_strong_address_is_strong_match():
    record = person()
    client = StaticSearchClient()
    register_q1(
        client,
        record,
        [result("https://source.example/a", snippet(record.name, "mobile 9999999999"))],
    )

    outcome = researcher_for(client).research_person(record)

    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert outcome.matched_name == "Dhruv Singh Thakur"
    assert outcome.source_links == ["https://source.example/a"]
    assert outcome.mobiles == ["9999999999"]
    assert outcome.address1_match_pct == 100.0
    assert outcome.address2_match_pct == 100.0
    assert "name_match: EXACT" in outcome.evidence
    assert "EXACT_NAME + ONE_STRONG_ADDRESS" in outcome.evidence
    assert "Address 1 similarity: 100%." in outcome.evidence
    assert any(
        item.startswith("Q1_FULL_NAME_FULL_ADDRESS") for item in outcome.strategies_used
    )


def test_partial_name_and_perfect_address_is_rejected():
    record = person()
    query = build_query_plan(record)[0].query
    # PARTIAL/NONE names remain rejected even with two strong addresses.
    # Compatible initial forms like "Dhruv S Thakur" are covered separately.
    rejected_names = [
        "Dhruv Thakur",
        "Dhruv Singh",
        "Dhruv Kumar Thakur",
        "D. Singh Thakur",
    ]

    for rejected in rejected_names:
        client = StaticSearchClient()
        client.register(
            query,
            [
                result(
                    "https://source.example/partial",
                    f"{rejected} 123 ABC Road Singapore 123456 mobile 9999999999",
                )
            ],
        )
        outcome = researcher_for(client).research_person(record)
        assert outcome.status == ResearchStatus.NO_STRONG_MATCH
        assert outcome.source_links == []
        assert outcome.mobiles == []
        assert outcome.matched_name == ""


def test_compatible_initial_name_with_two_strong_addresses_is_accepted():
    record = person()
    query = build_query_plan(record)[0].query
    client = StaticSearchClient()
    client.register(
        query,
        [
            result(
                "https://source.example/compatible-initial",
                "Dhruv S Thakur 123 ABC Road Singapore 123456 mobile 9999999999",
            )
        ],
    )
    outcome = researcher_for(client).research_person(record)
    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert outcome.source_links == ["https://source.example/compatible-initial"]
    assert "COMPATIBLE_NAME + TWO_STRONG_ADDRESSES" in outcome.evidence
    assert "name_match: COMPATIBLE" in outcome.evidence


def test_exact_name_and_weak_address_is_not_strong():
    record = person()
    client = StaticSearchClient()
    register_q1(
        client,
        record,
        [
            result(
                "https://source.example/weak",
                "Dhruv Singh Thakur lives in Mumbai mobile 9999999999",
            )
        ],
    )

    outcome = researcher_for(client).research_person(record)

    assert outcome.status == ResearchStatus.NO_STRONG_MATCH
    assert outcome.source_links == []
    assert outcome.mobiles == []
    assert outcome.address1_match_pct is None


def test_multiple_valid_urls_are_all_preserved():
    record = person()
    urls = [
        "https://source1.example/a",
        "https://source2.example/b",
        "https://source3.example/c",
    ]
    client = StaticSearchClient()
    register_q1(
        client,
        record,
        [result(url, snippet(record.name)) for url in urls],
    )

    outcome = researcher_for(client).research_person(record)

    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert outcome.source_links == urls


def test_duplicate_url_across_queries_is_deduplicated():
    record = person()
    plans = {plan.strategy: plan.query for plan in build_query_plan(record)}
    client = StaticSearchClient()
    text = snippet(record.name)
    client.register(
        plans["Q1_FULL_NAME_FULL_ADDRESS"],
        [result("https://source1.example/a", text)],
    )
    client.register(
        plans["Q2_NAME_ADDRESS1"],
        [result("https://source1.example/a", text)],
    )
    client.register(
        plans["Q3_NAME_ADDRESS2"],
        [result("https://source2.example/b", text)],
    )

    outcome = researcher_for(client).research_person(record)

    assert outcome.source_links == [
        "https://source1.example/a",
        "https://source2.example/b",
    ]


def test_mobile_and_email_from_separate_valid_sources_are_both_preserved():
    record = person()
    plans = {plan.strategy: plan.query for plan in build_query_plan(record)}
    client = StaticSearchClient()
    client.register(
        plans["Q1_FULL_NAME_FULL_ADDRESS"],
        [
            result(
                "https://source-a.example/mobile",
                snippet(record.name, "mobile 9999999999"),
                title="Source A",
            )
        ],
    )
    client.register(
        plans["Q2_NAME_ADDRESS1"],
        [
            result(
                "https://source-b.example/email",
                snippet(record.name, "email person@example.com"),
                title="Source B",
            )
        ],
    )

    outcome = researcher_for(client).research_person(record)

    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert outcome.mobiles == ["9999999999"]
    assert outcome.emails == ["person@example.com"]
    assert outcome.source_links == [
        "https://source-a.example/mobile",
        "https://source-b.example/email",
    ]


def test_empty_source_url_cannot_become_strong_match():
    record = person()
    found = result("", snippet(record.name, "mobile 9999999999"))
    parsed = parse_search_result(record, found)
    assert parsed is not None
    assert evaluate_candidate(record, parsed.candidate).status == MatchStatus.STRONG

    client = StaticSearchClient()
    register_q1(client, record, [found])
    outcome = researcher_for(client).research_person(record)

    assert outcome.status == ResearchStatus.NO_STRONG_MATCH
    assert outcome.source_links == []
    assert outcome.mobiles == []


def test_one_record_error_does_not_stop_another_record():
    first = person(record_id="A", name="Asha Singh")
    broken = person(record_id="B", name="Broken Record")
    third = person(record_id="C", name="Cara Singh")
    blocked = person(record_id="D", name="Blocked Person")

    class SelectiveClient(StaticSearchClient):
        def search(self, query: str) -> SearchResponse:
            if "Broken Record" in query:
                self.calls.append(query)
                raise RuntimeError("provider exploded")
            return super().search(query)

    client = SelectiveClient()
    register_q1(client, first, [result("https://source.example/a", snippet(first.name))])
    register_q1(client, third, [result("https://source.example/c", snippet(third.name))])
    for plan in build_query_plan(blocked):
        client.register(plan.query, status=SearchStatus.BLOCKED, message="HTTP 403")

    outcomes = researcher_for(client).research_people([first, broken, third, blocked])

    assert [item.status for item in outcomes] == [
        ResearchStatus.STRONG_MATCH,
        ResearchStatus.ERROR,
        ResearchStatus.STRONG_MATCH,
        ResearchStatus.BLOCKED,
    ]
    assert outcomes[1].message == "provider exploded"
    assert outcomes[0].source_links == ["https://source.example/a"]
    assert outcomes[2].source_links == ["https://source.example/c"]
    assert outcomes[3].source_links == []
    assert any(call.startswith("Cara Singh") for call in client.calls)


def test_query_cache_prevents_duplicate_identical_searches():
    first = person(record_id="1")
    second = person(record_id="2")
    client = StaticSearchClient()
    register_q1(
        client,
        first,
        [result("https://source.example/a", snippet(first.name))],
    )
    plans = build_query_plan(first)

    outcomes = researcher_for(client).research_people([first, second])

    assert [item.status for item in outcomes] == [
        ResearchStatus.STRONG_MATCH,
        ResearchStatus.STRONG_MATCH,
    ]
    # Obituary discovery is skipped once a STRONG_MATCH exists.
    assert client.calls == [plan.query for plan in plans if plan.family != "OBITUARY"]
    assert client.calls[0] == plans[0].query
    assert all("not executed" in item for item in outcomes[0].skipped_strategies)
    assert any(item.startswith("Q_OBIT_") for item in outcomes[0].skipped_strategies)


def test_search_policy_has_safe_defaults():
    policy = SearchPolicy()

    assert policy.delay_seconds == 1.0
    assert policy.max_retries == 2
    assert policy.backoff_base_seconds == 1.0
    assert policy.backoff_multiplier == 2.0
    assert policy.max_backoff_seconds == 30.0
    assert policy.max_consecutive_failures == 3
    assert policy.max_consecutive_provider_failures == 10
    assert policy.timeout_seconds == 20.0
    assert policy.max_searches_per_person == 12
    assert policy.max_concurrency == 1


def test_temporary_errors_use_exponential_backoff_and_then_cache():
    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def search(self, query: str) -> SearchResponse:
            self.calls += 1
            if self.calls < 3:
                return SearchResponse(
                    results=[],
                    status=SearchStatus.ERROR,
                    message="temporary",
                    retryable=True,
                )
            return SearchResponse(results=[], status=SearchStatus.OK)

    flaky = Flaky()
    sleeps = []
    client = PoliteSearchClient(
        flaky,
        SearchPolicy(
            delay_seconds=0,
            max_retries=2,
            backoff_base_seconds=1.0,
            backoff_multiplier=2.0,
        ),
        sleeper=sleeps.append,
    )

    assert client.search("same query").status == SearchStatus.OK
    assert client.search("same query").status == SearchStatus.OK

    assert flaky.calls == 3
    assert sleeps == [1.0, 2.0]


def test_blocked_query_is_not_retried_or_refetched():
    class Once:
        def __init__(self) -> None:
            self.calls = 0

        def search(self, query: str) -> SearchResponse:
            self.calls += 1
            return SearchResponse(
                results=[],
                status=SearchStatus.BLOCKED,
                message="HTTP 403",
                retryable=True,
            )

    inner = Once()
    client = PoliteSearchClient(inner, SearchPolicy(delay_seconds=0, max_retries=2))

    assert client.search("blocked query").status == SearchStatus.BLOCKED
    assert client.search("blocked query").status == SearchStatus.BLOCKED
    assert inner.calls == 1


def test_queries_union_unique_source_urls():
    record = person(
        record_id="T",
        name="TEST PERSON",
        address1="123 TEST STREET",
        address2="UNIT 4",
        country="",
        scrip="",
    )
    plans = {plan.strategy: plan.query for plan in build_query_plan(record)}
    text = "TEST PERSON 123 TEST STREET"
    client = StaticSearchClient()
    client.register(
        plans["Q1_FULL_NAME_FULL_ADDRESS"],
        [result("https://source.example/a", text, title="A")],
    )
    client.register(
        plans["Q2_NAME_ADDRESS1"],
        [
            result("https://source.example/a", text, title="A"),
            result("https://source.example/b", text, title="B"),
        ],
    )
    client.register(
        plans["Q3_NAME_ADDRESS2"],
        [
            result("https://source.example/b?utm_source=google", text, title="B"),
            result("https://source.example/c", text, title="C"),
        ],
    )

    outcome = researcher_for(client).research_person(record)

    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert outcome.source_links == [
        "https://source.example/a",
        "https://source.example/b",
        "https://source.example/c",
    ]
    assert outcome.source_strategies["https://source.example/a"] == [
        "Q1_FULL_NAME_FULL_ADDRESS",
        "Q2_NAME_ADDRESS1",
    ]
    assert "Q2_NAME_ADDRESS1" in outcome.source_strategies["https://source.example/b"]
    assert "Q3_NAME_ADDRESS2" in outcome.source_strategies["https://source.example/b"]
    assert outcome.source_strategies["https://source.example/c"] == ["Q3_NAME_ADDRESS2"]


def test_contacts_merge_across_accepted_sources_without_overwrite():
    record = person()
    plans = {plan.strategy: plan.query for plan in build_query_plan(record)}
    client = StaticSearchClient()
    client.register(
        plans["Q1_FULL_NAME_FULL_ADDRESS"],
        [result("https://source.example/a", snippet(record.name, "email ravi@example.com"))],
    )
    client.register(
        plans["Q2_NAME_ADDRESS1"],
        [result("https://source.example/b", snippet(record.name, "mobile +1 555 123456"))],
    )
    client.register(
        plans["Q3_NAME_ADDRESS2"],
        [
            result(
                "https://source.example/c",
                snippet(record.name, "email ravi@example.com mobile +1 555 999999"),
            )
        ],
    )

    outcome = researcher_for(client).research_person(record)

    assert outcome.emails == ["ravi@example.com"]
    assert outcome.mobiles == ["+1 555 123456", "+1 555 999999"]
    assert outcome.source_links == [
        "https://source.example/a",
        "https://source.example/b",
        "https://source.example/c",
    ]


def test_invalid_svg_source_cannot_produce_strong_match():
    record = person()
    client = StaticSearchClient()
    register_q1(
        client,
        record,
        [result("http://www.w3.org/2000/svg\\", snippet(record.name))],
    )
    outcome = researcher_for(client).research_person(record)
    assert outcome.status != ResearchStatus.STRONG_MATCH
    assert outcome.source_links == []


def test_valid_exact_name_address_and_source_is_strong_match():
    record = person()
    client = StaticSearchClient()
    register_q1(
        client,
        record,
        [result("https://records.example/person", snippet(record.name))],
    )
    outcome = researcher_for(client).research_person(record)
    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert outcome.source_links == ["https://records.example/person"]


def test_strategies_used_lists_only_executed_ok_searches_with_counts():
    record = person()
    plans = {plan.strategy: plan.query for plan in build_query_plan(record)}

    class MixedClient:
        manages_browser = True

        def __init__(self) -> None:
            self.calls = []

        def search(self, query: str) -> SearchResponse:
            self.calls.append(query)
            if query == plans["Q1_FULL_NAME_FULL_ADDRESS"]:
                return SearchResponse(
                    results=[
                        result("https://source.example/a", snippet(record.name)),
                        result("https://source.example/b", snippet(record.name)),
                    ],
                    status=SearchStatus.OK,
                )
            if query == plans["Q2_NAME_ADDRESS1"]:
                return SearchResponse(
                    results=[],
                    status=SearchStatus.ERROR,
                    message="search box failed",
                )
            return SearchResponse(
                results=[result("https://source.example/c", snippet(record.name))],
                status=SearchStatus.OK,
            )

    outcome = ShareholderResearcher(
        MixedClient(),
        SearchPolicy(delay_seconds=0, max_consecutive_failures=5),
    ).research_person(record)

    assert "Q1_FULL_NAME_FULL_ADDRESS (2 results)" in outcome.strategies_used
    assert "Q3_NAME_ADDRESS2 (1 results)" in outcome.strategies_used
    assert all("Q2_NAME_ADDRESS1" not in item for item in outcome.strategies_used)
    assert outcome.log[0].result_count == 2
    assert outcome.log[1].status == SearchStatus.ERROR
    assert outcome.log[1].result_count == 0
