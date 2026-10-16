from src.matching import evaluate_candidate
from src.models import Candidate, MatchStatus, PersonRecord


def person():
    return PersonRecord(
        record_id="1",
        name="Dhruv Singh Thakur",
        address1="123 ABC Road",
        address2="Singapore 123456",
    )


def test_exact_name_and_exact_addresses_is_strong():
    result = evaluate_candidate(person(), Candidate(
        name="Dhruv Singh Thakur",
        address1="123 ABC Road",
        address2="Singapore 123456",
    ))
    assert result.status == MatchStatus.STRONG


def test_partial_name_is_rejected_even_with_exact_address():
    result = evaluate_candidate(person(), Candidate(
        name="Dhruv Thakur",
        address1="123 ABC Road",
        address2="Singapore 123456",
    ))
    assert result.status == MatchStatus.REJECTED


def test_other_partial_name_is_rejected():
    result = evaluate_candidate(person(), Candidate(
        name="Dhruv Singh",
        address1="123 ABC Road",
        address2="Singapore 123456",
    ))
    assert result.status == MatchStatus.REJECTED


def test_exact_name_but_bad_address_is_only_candidate():
    result = evaluate_candidate(person(), Candidate(
        name="Dhruv Singh Thakur",
        address1="Mumbai",
        address2="",
    ))
    assert result.status == MatchStatus.CANDIDATE


def test_case_and_punctuation_normalization():
    result = evaluate_candidate(person(), Candidate(
        name="DHruv Singh-Thakur",
        address1="123, ABC ROAD",
        address2="Singapore 123456",
    ))
    assert result.status == MatchStatus.STRONG


def test_one_address_is_enough_for_strong():
    result = evaluate_candidate(person(), Candidate(
        name="Dhruv Singh Thakur",
        address1="123 ABC Road",
        address2="Completely Different",
    ))
    assert result.status == MatchStatus.STRONG
