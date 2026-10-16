from __future__ import annotations

from dataclasses import dataclass

from .contacts import extract_contacts_for_groups
from .context import ContextConfig, attach_context, joined_text, page_offset_resolver
from .evidence import EvidenceConfig, evaluate_source, name_equivalence
from .models import (
    JOINT_ENTITY_FIELDS,
    NameEquivalence,
    ShareholderTarget,
    SourceDocument,
    SourceInvestigationResult,
    SourceMatchLevel,
)
from .record_grouping import GroupingConfig, group_occurrences
from .text_search import SearchConfig, search_entities


@dataclass(frozen=True)
class InvestigatorConfig:
    context: ContextConfig = ContextConfig()
    search: SearchConfig = SearchConfig()
    grouping: GroupingConfig = GroupingConfig()
    evidence: EvidenceConfig = EvidenceConfig()


def investigate(
    target: ShareholderTarget,
    source: SourceDocument,
    config: InvestigatorConfig | None = None,
) -> SourceInvestigationResult:
    """Investigate one already-available source document in isolation.

    Searches NAME / ADDRESS1..4 independently, groups nearby occurrences into
    record evidence, then evaluates source-level strength. No network I/O.
    """
    settings = config or InvestigatorConfig()
    text = joined_text(source)
    page_of = page_offset_resolver(
        SourceDocument(
            source_url=source.source_url,
            text=text,
            title=source.title,
            document_type=source.document_type,
            page_texts=source.page_texts,
            metadata=source.metadata,
        )
    )

    occurrences = search_entities(
        target,
        text,
        page_of_offset=page_of,
        config=settings.search,
    )
    occurrences = attach_context(occurrences, text, config=settings.context)
    groups = group_occurrences(occurrences, text, config=settings.grouping)
    level, evaluations = evaluate_source(target, groups, config=settings.evidence)
    accepted = [
        item.group
        for item in evaluations
        if item.level
        in {SourceMatchLevel.STRONG_MATCH, SourceMatchLevel.POSSIBLE_MATCH}
    ]
    # Contacts only from groups that carry identity evidence.
    strong_or_possible = [
        item.group
        for item in evaluations
        if item.level == SourceMatchLevel.STRONG_MATCH
    ]
    # Prefer strong groups for contacts; if none, use possible groups.
    contact_groups = strong_or_possible or [
        item.group
        for item in evaluations
        if item.level == SourceMatchLevel.POSSIBLE_MATCH
    ]
    contacts = extract_contacts_for_groups(target, source, contact_groups)

    reasons = []
    for item in evaluations:
        if item.level == SourceMatchLevel.NOT_RELEVANT:
            continue
        reasons.append(f"{item.group.group_id}: {item.level.value}")
        reasons.extend(item.reasons)

    if not reasons:
        reasons = ["No same-record evidence groups supported the shareholder target."]

    return SourceInvestigationResult(
        source_url=source.source_url,
        title=source.title,
        level=level,
        occurrences=occurrences,
        groups=groups,
        evaluations=evaluations,
        contacts=contacts,
        reasons=reasons,
        accepted_groups=accepted,
        joint_holders_in_source=_joint_holders_in_source(target, occurrences),
    )


def _joint_holders_in_source(
    target: ShareholderTarget,
    occurrences,
) -> dict[str, NameEquivalence]:
    rank = {
        NameEquivalence.EXACT: 3,
        NameEquivalence.COMPATIBLE: 2,
        NameEquivalence.PARTIAL: 1,
        NameEquivalence.NONE: 0,
    }
    found: dict[str, NameEquivalence] = {}
    for entity_type, field_name in JOINT_ENTITY_FIELDS.items():
        supplied = (getattr(target, field_name, "") or "").strip()
        if not supplied:
            continue
        best = NameEquivalence.NONE
        for occ in occurrences:
            if occ.entity_type != entity_type:
                continue
            level = name_equivalence(supplied, occ.matched_text)
            if rank[level] > rank[best]:
                best = level
        found[field_name] = best
    return found


def investigate_many(
    target: ShareholderTarget,
    sources: list[SourceDocument],
    config: InvestigatorConfig | None = None,
) -> list[SourceInvestigationResult]:
    """Investigate multiple sources. Does not stop after the first acceptance."""
    return [investigate(target, source, config=config) for source in sources]
