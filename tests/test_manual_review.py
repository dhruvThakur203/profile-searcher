"""MANUAL_REVIEW: borderline (90-95%) same-record evidence is retained, never auto-accepted."""

from src.query_planner import build_query_plan
from src.scraper.document_investigator import (
    EvidenceGroup,
    GroupEvaluation,
    JointHolderEvidence,
    NameEquivalence,
    ShareholderTarget,
    SourceMatchLevel,
)
from src.scraper.researcher import (
    ResearchStatus,
    ShareholderResearcher,
    final_identity_accepts,
    final_identity_review,
)
from src.scraper.search_client import SearchPolicy, StaticSearchClient
from src.scraper.document_investigator import SourceDocument
from src.tools.search import SearchResult
from tests.test_source_integration import MapSourceFetcher, person


A1 = "12044 FALCON CREST WAY NORTHRIDGE"
A2 = "CALIFORNIA 91326 U.S.A"
FULL_NAME = "MITHOO MURLI RAMCHANDANI"
COMPATIBLE_NAME = "MITHOO M RAMCHANDANI"
# Verified similarities through the real investigator:
A1_AT_91_8 = "12044 FALCON CRST NORTHRIDGE"
A1_AT_93_55 = "12044 FALCON CREST NORTHRIDGE"
A1_AT_84 = "12044 FALCON CREST NORTH"
A1_AT_96_88 = "12044 FALCN CREST WY NORTHRIDGE"
A2_AT_92_68 = "CALIFRNIA 91326 U.S"
A2_AT_95_24 = "CALIFORNIA 91326 USA"


def _record():
    return person(name=FULL_NAME, address1=A1, address2=A2)


def _run(record, text, url="https://records.example/review"):
    client = StaticSearchClient()
    client.register(build_query_plan(record)[0].query, [SearchResult("R", url, "x")])
    doc = SourceDocument(source_url=url, title="Fixture record", document_type="pdf", text=text)
    return ShareholderResearcher(
        client,
        SearchPolicy(delay_seconds=0),
        source_fetcher=MapSourceFetcher({url: doc}),
        use_result_text_when_unfetched=False,
    ).research_person(record)


def _evaluation(name_eq, scores, level=SourceMatchLevel.POSSIBLE_MATCH, joints=None):
    group = EvidenceGroup(
        group_id="group-1",
        text="",
        occurrences=[],
        entity_types_found=[],
        start_position=0,
        end_position=0,
    )
    full = {"address1": None, "address2": None, "address3": None, "address4": None}
    full.update(scores)
    return GroupEvaluation(
        group=group,
        level=level,
        name_equivalence=name_eq,
        address_scores=full,
        joint_holders=joints or [],
    )


TARGET = ShareholderTarget(name=FULL_NAME, address1=A1, address2=A2)


# --- Pure policy (exact similarity values) ---------------------------------


def test_policy_exact_name_address_92_is_manual_review():
    ok, basis, reason = final_identity_review(
        _evaluation(NameEquivalence.EXACT, {"address1": 92.0}), TARGET
    )
    assert ok and basis == "EXACT_NAME + ADDRESS_90_TO_95"
    assert "address1 92%" in reason


def test_policy_compatible_name_one_address_93_is_manual_review():
    ok, basis, _ = final_identity_review(
        _evaluation(NameEquivalence.COMPATIBLE, {"address1": 93.0}), TARGET
    )
    assert ok and basis == "COMPATIBLE_NAME + ADDRESS_90_TO_95"


def test_policy_compatible_name_two_addresses_92_is_manual_review():
    ok, basis, _ = final_identity_review(
        _evaluation(NameEquivalence.COMPATIBLE, {"address1": 92.0, "address2": 92.0}), TARGET
    )
    assert ok and basis == "COMPATIBLE_NAME + ADDRESS_90_TO_95"


def test_policy_strong_cases_are_not_review():
    exact = _evaluation(
        NameEquivalence.EXACT, {"address1": 96.0}, level=SourceMatchLevel.STRONG_MATCH
    )
    compatible = _evaluation(
        NameEquivalence.COMPATIBLE,
        {"address1": 96.0, "address2": 96.0},
        level=SourceMatchLevel.STRONG_MATCH,
    )
    for evaluation in (exact, compatible):
        assert final_identity_accepts(evaluation)[0] is True
        assert final_identity_review(evaluation, TARGET)[0] is False


def test_policy_below_90_without_corroboration_is_not_review():
    ok, _, _ = final_identity_review(
        _evaluation(NameEquivalence.EXACT, {"address1": 89.99}), TARGET
    )
    assert ok is False


def test_policy_partial_or_missing_name_never_qualifies():
    for name_eq in (NameEquivalence.PARTIAL, NameEquivalence.NONE):
        ok, _, _ = final_identity_review(
            _evaluation(name_eq, {"address1": 93.0, "address2": 100.0}), TARGET
        )
        assert ok is False


def test_policy_generic_single_word_address_cannot_support_review():
    target = ShareholderTarget(name=FULL_NAME, address1="NORTHRIDGE")
    ok, _, _ = final_identity_review(
        _evaluation(NameEquivalence.COMPATIBLE, {"address1": 100.0}), target
    )
    assert ok is False


def test_policy_single_token_name_is_too_generic_for_review():
    target = ShareholderTarget(name="RAMCHANDANI", address1=A1)
    ok, _, _ = final_identity_review(
        _evaluation(NameEquivalence.EXACT, {"address1": 93.0}), target
    )
    assert ok is False


def test_policy_joint_holder_corroboration_without_address_is_review_not_strong():
    joint = JointHolderEvidence(
        field="joint1",
        input_name="SUNITA M RAMCHANDANI",
        matched_text="SUNITA M RAMCHANDANI",
        classification=NameEquivalence.EXACT,
        same_record=True,
        group_id="group-1",
    )
    evaluation = _evaluation(NameEquivalence.EXACT, {"address1": 70.0}, joints=[joint])
    assert final_identity_accepts(evaluation)[0] is False
    ok, basis, reason = final_identity_review(evaluation, TARGET)
    assert ok and basis == "EXACT_NAME + JOINT_HOLDER_SAME_RECORD"
    assert "SUNITA M RAMCHANDANI" in reason


# --- End-to-end through investigator + researcher ---------------------------


def test_exact_name_address_in_review_band_is_manual_review():
    outcome = _run(_record(), f"{FULL_NAME}\n{A1_AT_91_8}\n")
    assert outcome.status == ResearchStatus.MANUAL_REVIEW
    finding = outcome.review_findings[0]
    assert 90 <= finding.address1_similarity < 95
    assert finding.name_equivalence == "EXACT"
    assert finding.match_basis == "EXACT_NAME + ADDRESS_90_TO_95"
    assert finding.matched_address_fields == ["address1"]
    assert finding.url == "https://records.example/review"
    assert finding.title == "Fixture record"
    assert finding.document_type == "pdf"
    assert finding.group_id and "chars" in finding.evidence_span
    assert finding.reason
    assert outcome.manual_review_reason
    assert 90 <= outcome.address1_match_pct < 95


def test_compatible_name_one_address_in_review_band_is_manual_review():
    outcome = _run(_record(), f"{COMPATIBLE_NAME}\n{A1_AT_93_55}\n")
    assert outcome.status == ResearchStatus.MANUAL_REVIEW
    assert outcome.review_findings[0].name_equivalence == "COMPATIBLE"
    assert 90 <= outcome.review_findings[0].address1_similarity < 95


def test_compatible_name_two_addresses_in_review_band_is_manual_review():
    outcome = _run(_record(), f"{COMPATIBLE_NAME}\n{A1_AT_91_8}\n{A2_AT_92_68}\n")
    assert outcome.status == ResearchStatus.MANUAL_REVIEW
    finding = outcome.review_findings[0]
    assert finding.matched_address_fields == ["address1", "address2"]
    assert 90 <= finding.address2_similarity < 95


def test_exact_name_address_above_95_stays_strong():
    outcome = _run(_record(), f"{FULL_NAME}\n{A1_AT_96_88}\n")
    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert outcome.review_findings == []
    assert "EXACT_NAME + ONE_STRONG_ADDRESS" in outcome.evidence


def test_compatible_name_two_addresses_above_95_same_record_stays_strong():
    outcome = _run(_record(), f"{COMPATIBLE_NAME}\n{A1_AT_96_88}\n{A2_AT_95_24}\n")
    assert outcome.status == ResearchStatus.STRONG_MATCH
    assert "COMPATIBLE_NAME + TWO_STRONG_ADDRESSES" in outcome.evidence


def test_address_below_90_without_corroboration_is_no_strong_match():
    outcome = _run(_record(), f"{FULL_NAME}\n{A1_AT_84}\n")
    assert outcome.status == ResearchStatus.NO_STRONG_MATCH
    assert outcome.review_findings == []


def test_generic_shared_address_does_not_establish_identity():
    # The exact supplied address under a different holder's name.
    outcome = _run(_record(), f"SOMEONE ELSE ENTIRELY\n{A1}\n{A2}\n")
    assert outcome.status == ResearchStatus.NO_STRONG_MATCH
    assert outcome.review_findings == []
    assert outcome.source_links == []


def test_manual_review_is_never_labelled_strong():
    outcome = _run(_record(), f"{FULL_NAME}\n{A1_AT_91_8}\n")
    assert outcome.status != ResearchStatus.STRONG_MATCH
    assert outcome.source_links == []
    assert outcome.matched_name == ""
    assert outcome.mobiles == [] and outcome.emails == []
    assert "not a confirmed match" in outcome.evidence
    assert "STRONG_MATCH" not in outcome.evidence
