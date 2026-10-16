"""Joint holders: query anchors and same-record corroboration, never identity proof."""

from src.models import PersonRecord
from src.normalize import normalize_text
from src.query_planner import FAMILY_JOINT, build_query_plan
from src.scraper.document_investigator import (
    NameEquivalence,
    ShareholderTarget,
    SourceDocument,
    investigate,
)
from src.scraper.researcher import ResearchStatus, ShareholderResearcher
from src.scraper.search_client import SearchPolicy, StaticSearchClient
from src.tools.search import SearchResult
from tests.test_source_integration import MapSourceFetcher


PRIMARY = "MITHOO MURLI RAMCHANDANI"
JOINT1 = "SUNITA M RAMCHANDANI"
JOINT2 = "KAVITA RAJ SHARMA"
A1 = "12044 FALCON CREST WAY NORTHRIDGE"
A2 = "CALIFORNIA 91326 U.S.A"


def _target(**overrides):
    data = dict(name=PRIMARY, address1=A1, address2=A2, joint1=JOINT1, joint2=JOINT2)
    data.update(overrides)
    return ShareholderTarget(**data)


def _record(**overrides):
    data = dict(
        record_id="J",
        name=PRIMARY,
        address1=A1,
        address2=A2,
        country="United States",
        joint1=JOINT1,
        joint2=JOINT2,
    )
    data.update(overrides)
    return PersonRecord(**data)


def _joint(evaluation, field):
    return next(item for item in evaluation.joint_holders if item.field == field)


def _primary_group(inv):
    return next(
        item
        for item in inv.evaluations
        if item.name_equivalence in {NameEquivalence.EXACT, NameEquivalence.COMPATIBLE}
    )


def test_primary_and_second_holder_co_occur_in_same_record():
    text = f"Folio 0001\t{PRIMARY}\t{JOINT1}\t{A1}\t{A2}\n"
    inv = investigate(_target(joint2=""), SourceDocument(source_url="u", text=text))
    evaluation = _primary_group(inv)
    joint = _joint(evaluation, "joint1")
    assert joint.same_record is True
    assert joint.classification == NameEquivalence.EXACT
    assert joint.matched_text == JOINT1
    assert joint.group_id == evaluation.group.group_id


def test_primary_and_third_holder_co_occur_in_same_record():
    text = f"{PRIMARY}\nJOINT HOLDER: {JOINT2}\n{A1}\n{A2}\n"
    inv = investigate(_target(), SourceDocument(source_url="u", text=text))
    evaluation = _primary_group(inv)
    assert _joint(evaluation, "joint2").same_record is True
    assert _joint(evaluation, "joint2").classification == NameEquivalence.EXACT
    assert _joint(evaluation, "joint1").same_record is False
    assert _joint(evaluation, "joint1").matched_text == ""


def test_compatible_joint_holder_form_is_classified():
    text = f"{PRIMARY}\nJOINT HOLDER: KAVITA R SHARMA\n{A1}\n"
    inv = investigate(_target(), SourceDocument(source_url="u", text=text))
    joint = _joint(_primary_group(inv), "joint2")
    assert joint.classification == NameEquivalence.COMPATIBLE
    assert joint.same_record is True


def test_joint_holder_in_unrelated_record_is_not_linked():
    text = (
        f"{PRIMARY}\n{A1}\n"
        + "\n" * 40
        + f"UNRELATED HOLDER ZZZ\n{JOINT1}\n999 OTHER ROAD ANYTOWN\n"
    )
    inv = investigate(_target(joint2=""), SourceDocument(source_url="u", text=text))
    # Found in the document by the independent search ...
    assert inv.joint_holders_in_source["joint1"] == NameEquivalence.EXACT
    # ... but never attached to the primary holder's record.
    for evaluation in inv.evaluations:
        if evaluation.name_equivalence in {NameEquivalence.EXACT, NameEquivalence.COMPATIBLE}:
            assert _joint(evaluation, "joint1").same_record is False


def test_address_evidence_must_come_from_the_holder_record():
    url = "https://records.example/joint-split"
    # Primary + joint in one record without an address; the supplied address
    # only appears under a different holder far away.
    text = (
        f"{PRIMARY}\nJOINT HOLDER: {JOINT1}\n"
        + "\n" * 40
        + f"UNRELATED HOLDER ZZZ\n{A1}\n{A2}\n"
    )
    record = _record(joint2="")
    client = StaticSearchClient()
    client.register(build_query_plan(record)[0].query, [SearchResult("R", url, "x")])
    outcome = ShareholderResearcher(
        client,
        SearchPolicy(delay_seconds=0),
        source_fetcher=MapSourceFetcher({url: SourceDocument(source_url=url, text=text)}),
        use_result_text_when_unfetched=False,
    ).research_person(record)
    # Joint co-occurrence alone never becomes STRONG_MATCH.
    assert outcome.status == ResearchStatus.MANUAL_REVIEW
    finding = outcome.review_findings[0]
    assert finding.match_basis == "EXACT_NAME + JOINT_HOLDER_SAME_RECORD"
    assert finding.address1_similarity is None and finding.address2_similarity is None
    assert finding.joint_holders[0]["same_record"] is True


def test_strong_match_records_joint_holder_corroboration():
    url = "https://records.example/joint-strong"
    text = f"Folio 0001\t{PRIMARY}\t{JOINT1}\t{A1}\t{A2}\n"
    record = _record(joint2="")
    client = StaticSearchClient()
    client.register(build_query_plan(record)[0].query, [SearchResult("R", url, "x")])
    outcome = ShareholderResearcher(
        client,
        SearchPolicy(delay_seconds=0),
        source_fetcher=MapSourceFetcher({url: SourceDocument(source_url=url, text=text)}),
        use_result_text_when_unfetched=False,
    ).research_person(record)
    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert outcome.strong_findings[0].joint_holders[0]["classification"] == "EXACT"
    assert "Joint holders: joint1" in outcome.evidence


def test_joint_holder_query_strategies():
    plans = build_query_plan(_record(scrip="Example Ltd"))
    by_strategy = {plan.strategy: plan for plan in plans}
    assert by_strategy["Q_HOLDER_PAIR_1_2"].query == f"{PRIMARY} {JOINT1}"
    assert by_strategy["Q_HOLDER_PAIR_3_1"].query == f"{JOINT2} {PRIMARY}"
    assert by_strategy["Q_HOLDER_PAIR_2_3"].query == f"{JOINT1} {JOINT2}"
    assert by_strategy["Q_JOINT_PRIMARY_JOINT_ADDRESS"].query == (
        f"{PRIMARY} {JOINT1} FALCON CREST WAY NORTHRIDGE"
    )
    assert by_strategy["Q_JOINT_PRIMARY_JOINT_SCRIP"].query == f"{PRIMARY} {JOINT1} Example Ltd"
    assert all(by_strategy[name].family == FAMILY_JOINT for name in by_strategy if name.startswith("Q_JOINT_"))
    # Core strategies still come first and are unchanged.
    assert plans[0].strategy == "Q1_FULL_NAME_FULL_ADDRESS"
    assert JOINT1 not in plans[0].query


def test_missing_joint_fields_generate_no_malformed_queries():
    for joint1, joint2 in (("", ""), ("   ", None), (None, "")):
        record = _record(joint1=joint1 or "", joint2=joint2 or "")
        plans = build_query_plan(record)
        assert not any(plan.family == FAMILY_JOINT for plan in plans)
        for plan in plans:
            assert plan.query == plan.query.strip()
            assert "  " not in plan.query
            assert plan.query


def test_only_third_holder_supplied_is_used_as_the_joint_anchor():
    plans = build_query_plan(_record(joint1="", joint2=JOINT2))
    strategies = {plan.strategy: plan.query for plan in plans}
    assert strategies["Q_HOLDER_PAIR_3_1"] == f"{JOINT2} {PRIMARY}"
    assert strategies["Q_JOINT_PRIMARY_JOINT_ADDRESS"].startswith(f"{PRIMARY} {JOINT2} ")
    assert "Q_HOLDER_PAIR_1_2" not in strategies
    assert "Q_HOLDER_PAIR_2_3" not in strategies


def test_duplicate_joint_holder_queries_are_removed():
    record = _record(joint1=JOINT1, joint2=JOINT1.lower(), scrip="")
    plans = build_query_plan(record)
    keys = [normalize_text(plan.query) for plan in plans]
    assert len(keys) == len(set(keys))
    strategies = {plan.strategy for plan in plans}
    assert "Q_HOLDER_PAIR_1_2" in strategies
    assert not strategies & {"Q_HOLDER_PAIR_2_3", "Q_HOLDER_PAIR_3_1", "Q_ALL_HOLDERS"}

    same_as_primary = build_query_plan(_record(joint1=PRIMARY.title(), joint2=""))
    assert not any(plan.family == FAMILY_JOINT for plan in same_as_primary)
