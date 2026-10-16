"""Source-document investigation for already-fetched Google result pages/PDFs.

Performs no network I/O. The researcher fetches SourceDocuments, then calls
investigate() for record-level evidence before final exact-name acceptance.
"""

from .investigator import InvestigatorConfig, investigate, investigate_many
from .models import (
    ContactEvidence,
    EntityOccurrence,
    EntityType,
    EvidenceGroup,
    GroupEvaluation,
    JointHolderEvidence,
    NameEquivalence,
    OccurrenceKind,
    ShareholderTarget,
    SourceDocument,
    SourceInvestigationResult,
    SourceMatchLevel,
)
from .obituary import ObituaryLead, RelationshipMention, extract_obituary_leads

__all__ = [
    "ContactEvidence",
    "EntityOccurrence",
    "EntityType",
    "EvidenceGroup",
    "GroupEvaluation",
    "InvestigatorConfig",
    "JointHolderEvidence",
    "NameEquivalence",
    "ObituaryLead",
    "OccurrenceKind",
    "RelationshipMention",
    "extract_obituary_leads",
    "ShareholderTarget",
    "SourceDocument",
    "SourceInvestigationResult",
    "SourceMatchLevel",
    "investigate",
    "investigate_many",
]
