from dataclasses import dataclass, field
from enum import Enum


class MatchStatus(str, Enum):
    REJECTED = "REJECTED"
    CANDIDATE = "CANDIDATE"
    STRONG = "STRONG"


class HolderType(str, Enum):
    PRIMARY = "PRIMARY"
    JOINT = "JOINT"
    UNKNOWN = "UNKNOWN"


@dataclass
class PersonRecord:
    record_id: str
    name: str
    address1: str = ""
    address2: str = ""
    country: str = ""
    scrip: str = ""
    address3: str = ""
    address4: str = ""
    pincode: str = ""
    joint1: str = ""
    joint2: str = ""


@dataclass
class Candidate:
    name: str
    address1: str = ""
    address2: str = ""
    country: str = ""
    scrip: str = ""
    address3: str = ""
    address4: str = ""
    mobile: str = ""
    email: str = ""
    source_url: str = ""
    source_title: str = ""
    evidence: str = ""
    holder_type: HolderType = HolderType.UNKNOWN


@dataclass
class MatchResult:
    status: MatchStatus
    name_exact: bool
    address1_similarity: float | None
    address2_similarity: float | None
    address_strong: bool
    reasons: list[str] = field(default_factory=list)
    address3_similarity: float | None = None
    address4_similarity: float | None = None

    def to_dict(self):
        return {
            "status": self.status.value,
            "name_exact": self.name_exact,
            "address1_similarity": self.address1_similarity,
            "address2_similarity": self.address2_similarity,
            "address3_similarity": self.address3_similarity,
            "address4_similarity": self.address4_similarity,
            "address_strong": self.address_strong,
            "reasons": self.reasons,
        }


@dataclass
class SearchAttempt:
    strategy: str
    query: str
    result_count: int = 0
    notes: str = ""


@dataclass
class ResearchResult:
    record_id: str
    input_name: str
    input_address1: str
    input_address2: str
    mobile: str = ""
    email: str = ""
    source_url: str = ""
    source_title: str = ""
    evidence: str = ""
    status: str = "UNRESOLVED"
    match_reason: str = ""
    attempts: list[SearchAttempt] = field(default_factory=list)
