from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class EntityType(str, Enum):
    NAME = "NAME"
    ADDRESS1 = "ADDRESS1"
    ADDRESS2 = "ADDRESS2"
    ADDRESS3 = "ADDRESS3"
    ADDRESS4 = "ADDRESS4"
    JOINT1 = "JOINT1"
    JOINT2 = "JOINT2"


JOINT_ENTITY_FIELDS = {
    EntityType.JOINT1: "joint1",
    EntityType.JOINT2: "joint2",
}


class OccurrenceKind(str, Enum):
    EXACT = "EXACT"
    COMPATIBLE = "COMPATIBLE"
    POTENTIAL = "POTENTIAL"
    ADDRESS = "ADDRESS"


class SourceMatchLevel(str, Enum):
    NOT_RELEVANT = "NOT_RELEVANT"
    POSSIBLE_MATCH = "POSSIBLE_MATCH"
    STRONG_MATCH = "STRONG_MATCH"


class NameEquivalence(str, Enum):
    NONE = "NONE"
    PARTIAL = "PARTIAL"
    COMPATIBLE = "COMPATIBLE"
    EXACT = "EXACT"


@dataclass
class ShareholderTarget:
    """Shareholder fields used for isolated source investigation."""

    name: str
    address1: str = ""
    address2: str = ""
    address3: str = ""
    address4: str = ""
    pincode: str = ""
    country: str = ""
    scrip: str = ""
    joint1: str = ""
    joint2: str = ""
    record_id: str = ""


@dataclass
class SourceDocument:
    """A source already available as text. No downloading is performed here."""

    source_url: str
    text: str
    title: str = ""
    document_type: str = "text"
    page_texts: list[str] | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.page_texts:
            joined = "\n".join(self.page_texts)
            if not self.text:
                self.text = joined


@dataclass
class EntityOccurrence:
    entity_type: EntityType
    original_query: str
    normalized_query: str
    matched_text: str
    character_start: int
    character_end: int
    surrounding_context: str
    confidence: float
    occurrence_kind: OccurrenceKind
    page_number: int | None = None


@dataclass
class EvidenceGroup:
    group_id: str
    text: str
    occurrences: list[EntityOccurrence]
    entity_types_found: list[EntityType]
    start_position: int
    end_position: int
    page_number: int | None = None


@dataclass
class ContactEvidence:
    type: str
    value: str
    normalized_value: str
    source_url: str
    context: str
    page_number: int | None = None
    group_id: str = ""


@dataclass
class JointHolderEvidence:
    """One supplied joint-holder name checked against one record group.

    Co-occurrence is corroboration only; it never satisfies identity rules.
    """

    field: str
    input_name: str
    matched_text: str = ""
    classification: NameEquivalence = NameEquivalence.NONE
    same_record: bool = False
    group_id: str = ""
    page_number: int | None = None

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "input_name": self.input_name,
            "matched_text": self.matched_text,
            "classification": self.classification.value,
            "same_record": self.same_record,
            "group_id": self.group_id,
            "page_number": self.page_number,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JointHolderEvidence":
        return cls(
            field=data.get("field", ""),
            input_name=data.get("input_name", ""),
            matched_text=data.get("matched_text", ""),
            classification=NameEquivalence(data.get("classification") or "NONE"),
            same_record=bool(data.get("same_record")),
            group_id=data.get("group_id", ""),
            page_number=data.get("page_number"),
        )


@dataclass
class GroupEvaluation:
    group: EvidenceGroup
    level: SourceMatchLevel
    name_equivalence: NameEquivalence
    address_scores: dict[str, float | None]
    reasons: list[str] = field(default_factory=list)
    joint_holders: list[JointHolderEvidence] = field(default_factory=list)


@dataclass
class SourceInvestigationResult:
    source_url: str
    title: str
    level: SourceMatchLevel
    occurrences: list[EntityOccurrence]
    groups: list[EvidenceGroup]
    evaluations: list[GroupEvaluation]
    contacts: list[ContactEvidence]
    reasons: list[str] = field(default_factory=list)
    accepted_groups: list[EvidenceGroup] = field(default_factory=list)
    # Best classification of each supplied joint-holder name anywhere in the
    # document, independent of record grouping (field -> NameEquivalence).
    joint_holders_in_source: dict[str, NameEquivalence] = field(default_factory=dict)
