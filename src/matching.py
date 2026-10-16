from rapidfuzz.fuzz import ratio

from .models import Candidate, MatchResult, MatchStatus, PersonRecord
from .normalize import normalize_address, normalize_name


ADDRESS_STRONG_THRESHOLD = 95.0
# Address similarity in [ADDRESS_REVIEW_THRESHOLD, ADDRESS_STRONG_THRESHOLD) can only
# ever produce MANUAL_REVIEW, never an automatic STRONG_MATCH.
ADDRESS_REVIEW_THRESHOLD = 90.0


def exact_name_match(input_name: str, candidate_name: str) -> bool:
    return normalize_name(input_name) == normalize_name(candidate_name)


def address_similarity(input_address: str, candidate_address: str):
    a = normalize_address(input_address)
    b = normalize_address(candidate_address)
    if not a or not b:
        return None
    return round(float(ratio(a, b)), 2)


def evaluate_candidate(
    person: PersonRecord,
    candidate: Candidate,
    threshold: float = ADDRESS_STRONG_THRESHOLD,
) -> MatchResult:
    reasons = []

    # HARD RULE: a partial/shortened/extended name can never be STRONG.
    name_exact = exact_name_match(person.name, candidate.name)

    a1 = address_similarity(person.address1, candidate.address1)
    a2 = address_similarity(person.address2, candidate.address2)
    a3 = address_similarity(person.address3, candidate.address3)
    a4 = address_similarity(person.address4, candidate.address4)

    if not name_exact:
        return MatchResult(
            status=MatchStatus.REJECTED,
            name_exact=False,
            address1_similarity=a1,
            address2_similarity=a2,
            address3_similarity=a3,
            address4_similarity=a4,
            address_strong=False,
            reasons=[
                "Candidate full name is not an exact normalized match.",
                "Partial/shortened/extended names are rejected as identity matches.",
            ],
        )

    supplied = [x for x in (a1, a2, a3, a4) if x is not None]
    # Strong identity requires at least one supplied address field to match
    # at or above the threshold. Country and scrip are not address fields.
    address_strong = bool(supplied) and any(x >= threshold for x in supplied)

    if a1 is not None:
        reasons.append(f"Address 1 similarity: {a1}%")
    if a2 is not None:
        reasons.append(f"Address 2 similarity: {a2}%")
    if a3 is not None:
        reasons.append(f"Address 3 similarity: {a3}%")
    if a4 is not None:
        reasons.append(f"Address 4 similarity: {a4}%")

    if address_strong:
        reasons.append(f"Full name exact + address similarity >= {threshold}%.")
        return MatchResult(
            status=MatchStatus.STRONG,
            name_exact=True,
            address1_similarity=a1,
            address2_similarity=a2,
            address3_similarity=a3,
            address4_similarity=a4,
            address_strong=True,
            reasons=reasons,
        )

    reasons.append(
        f"Exact name matched, but address evidence is below the {threshold}% strong threshold."
    )
    return MatchResult(
        status=MatchStatus.CANDIDATE,
        name_exact=True,
        address1_similarity=a1,
        address2_similarity=a2,
        address3_similarity=a3,
        address4_similarity=a4,
        address_strong=False,
        reasons=reasons,
    )
