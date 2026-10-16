from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz.fuzz import ratio

from ...normalize import normalize_address, normalize_name
from .models import (
    JOINT_ENTITY_FIELDS,
    EntityType,
    EvidenceGroup,
    GroupEvaluation,
    JointHolderEvidence,
    NameEquivalence,
    OccurrenceKind,
    ShareholderTarget,
    SourceMatchLevel,
)
from .text_search import is_compatible_name, is_ordered_subsequence


@dataclass(frozen=True)
class EvidenceConfig:
    address_strong_threshold: float = 95.0
    address_possible_threshold: float = 85.0


def name_equivalence(target_name: str, candidate_name: str) -> NameEquivalence:
    query = normalize_name(target_name).split()
    window = normalize_name(candidate_name).split()
    if not query or not window:
        return NameEquivalence.NONE
    if query == window:
        return NameEquivalence.EXACT
    if is_compatible_name(query, window):
        return NameEquivalence.COMPATIBLE
    if is_ordered_subsequence(window, query) and len(window) < len(query):
        return NameEquivalence.PARTIAL
    if is_ordered_subsequence(query, window) and len(query) < len(window):
        return NameEquivalence.PARTIAL
    return NameEquivalence.NONE


def evaluate_group(
    target: ShareholderTarget,
    group: EvidenceGroup,
    config: EvidenceConfig | None = None,
) -> GroupEvaluation:
    settings = config or EvidenceConfig()
    name_occ = _best_name(group)
    name_level = (
        name_equivalence(target.name, name_occ.matched_text)
        if name_occ is not None
        else NameEquivalence.NONE
    )

    address_scores: dict[str, float | None] = {
        "address1": _address_score(target.address1, group, EntityType.ADDRESS1),
        "address2": _address_score(target.address2, group, EntityType.ADDRESS2),
        "address3": _address_score(target.address3, group, EntityType.ADDRESS3),
        "address4": _address_score(target.address4, group, EntityType.ADDRESS4),
    }

    strong_addresses = [
        field
        for field, score in address_scores.items()
        if score is not None and score >= settings.address_strong_threshold
    ]
    possible_addresses = [
        field
        for field, score in address_scores.items()
        if score is not None and score >= settings.address_possible_threshold
    ]

    reasons: list[str] = []
    if name_occ is not None:
        reasons.append(
            f"Name occurrence ({name_occ.occurrence_kind.value}): {name_occ.matched_text!r}"
        )
        reasons.append(f"Name equivalence: {name_level.value}")
    else:
        reasons.append("Name occurrence: none in this record group")

    for field, score in address_scores.items():
        if score is None:
            continue
        reasons.append(f"{field} similarity: {score}%")

    name_ok_strong = name_level in {NameEquivalence.EXACT, NameEquivalence.COMPATIBLE}
    name_ok_possible = name_level in {
        NameEquivalence.EXACT,
        NameEquivalence.COMPATIBLE,
        NameEquivalence.PARTIAL,
    }

    if name_ok_strong and strong_addresses:
        reasons.append(
            "STRONG_MATCH: compatible/exact name plus address field(s) "
            f">= {settings.address_strong_threshold}% in the same record group "
            f"({', '.join(strong_addresses)})."
        )
        level = SourceMatchLevel.STRONG_MATCH
    elif name_ok_possible and possible_addresses:
        reasons.append(
            "POSSIBLE_MATCH: name-related occurrence plus nearby address evidence "
            "in the same record group."
        )
        level = SourceMatchLevel.POSSIBLE_MATCH
    elif strong_addresses and name_level == NameEquivalence.NONE:
        reasons.append(
            "POSSIBLE_MATCH: address evidence found in a record group without a usable name."
        )
        level = SourceMatchLevel.POSSIBLE_MATCH
    else:
        reasons.append("NOT_RELEVANT: insufficient same-record identity evidence.")
        level = SourceMatchLevel.NOT_RELEVANT

    joint_holders = _joint_holder_evidence(target, group, name_occ, name_ok_strong)
    for item in joint_holders:
        if item.same_record:
            reasons.append(
                f"Joint holder {item.field} ({item.classification.value}) co-occurs "
                f"in the same record: {item.matched_text!r} (corroboration only)."
            )

    return GroupEvaluation(
        group=group,
        level=level,
        name_equivalence=name_level,
        address_scores=address_scores,
        reasons=reasons,
        joint_holders=joint_holders,
    )


def _joint_holder_evidence(
    target: ShareholderTarget,
    group: EvidenceGroup,
    primary_occ,
    primary_name_ok: bool,
) -> list[JointHolderEvidence]:
    evidence: list[JointHolderEvidence] = []
    for entity_type, field_name in JOINT_ENTITY_FIELDS.items():
        supplied = (getattr(target, field_name, "") or "").strip()
        if not supplied:
            continue
        item = JointHolderEvidence(
            field=field_name,
            input_name=supplied,
            group_id=group.group_id,
        )
        candidates = [
            occ
            for occ in group.occurrences
            if occ.entity_type == entity_type
            and not _overlaps(occ, primary_occ)
        ]
        best = None
        best_level = NameEquivalence.NONE
        rank = {
            NameEquivalence.EXACT: 3,
            NameEquivalence.COMPATIBLE: 2,
            NameEquivalence.PARTIAL: 1,
            NameEquivalence.NONE: 0,
        }
        for occ in candidates:
            level = name_equivalence(supplied, occ.matched_text)
            if rank[level] > rank[best_level]:
                best, best_level = occ, level
        if best is not None:
            item.matched_text = best.matched_text
            item.classification = best_level
            item.page_number = best.page_number
            item.same_record = primary_name_ok and best_level in {
                NameEquivalence.EXACT,
                NameEquivalence.COMPATIBLE,
            }
        evidence.append(item)
    return evidence


def _overlaps(left, right) -> bool:
    if left is None or right is None:
        return False
    return left.character_start < right.character_end and right.character_start < left.character_end


def evaluate_source(
    target: ShareholderTarget,
    groups: list[EvidenceGroup],
    config: EvidenceConfig | None = None,
) -> tuple[SourceMatchLevel, list[GroupEvaluation]]:
    evaluations = [evaluate_group(target, group, config=config) for group in groups]
    if any(item.level == SourceMatchLevel.STRONG_MATCH for item in evaluations):
        return SourceMatchLevel.STRONG_MATCH, evaluations
    if any(item.level == SourceMatchLevel.POSSIBLE_MATCH for item in evaluations):
        return SourceMatchLevel.POSSIBLE_MATCH, evaluations
    return SourceMatchLevel.NOT_RELEVANT, evaluations


def _best_name(group: EvidenceGroup):
    names = [item for item in group.occurrences if item.entity_type == EntityType.NAME]
    if not names:
        return None
    rank = {
        OccurrenceKind.EXACT: 0,
        OccurrenceKind.COMPATIBLE: 1,
        OccurrenceKind.POTENTIAL: 2,
    }
    return sorted(
        names,
        key=lambda item: (rank.get(item.occurrence_kind, 9), -item.confidence),
    )[0]


def _address_score(
    target_value: str,
    group: EvidenceGroup,
    entity_type: EntityType,
) -> float | None:
    target = (target_value or "").strip()
    if not target:
        return None
    matches = [item for item in group.occurrences if item.entity_type == entity_type]
    if not matches:
        # Fall back to comparing the whole group text window for soft support.
        group_norm = normalize_address(group.text)
        target_norm = normalize_address(target)
        if not group_norm or target_norm not in group_norm:
            return None
        return 100.0
    best = 0.0
    target_norm = normalize_address(target)
    for item in matches:
        score = float(ratio(target_norm, normalize_address(item.matched_text)))
        if score > best:
            best = score
    return round(best, 2)
