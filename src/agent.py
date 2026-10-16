from dataclasses import dataclass, field

from .matching import evaluate_candidate
from .models import Candidate, PersonRecord, ResearchResult
from .query_planner import QueryPlan, build_query_plan


@dataclass
class AgentConfig:
    max_queries_per_record: int = 5
    address_threshold: float = 95.0


@dataclass
class ResearchAgent:
    # Search is intentionally not implemented here yet.
    # This controller owns planning/evaluation/state; a browser/search tool
    # will be attached in the next phase.

    config: AgentConfig = field(default_factory=AgentConfig)

    def plan(self, person: PersonRecord) -> list[QueryPlan]:
        return build_query_plan(person)[:self.config.max_queries_per_record]

    def evaluate(self, person: PersonRecord, candidate: Candidate):
        return evaluate_candidate(
            person, candidate, threshold=self.config.address_threshold
        )

    def unresolved(self, person: PersonRecord, attempts):
        return ResearchResult(
            record_id=person.record_id,
            input_name=person.name,
            input_address1=person.address1,
            input_address2=person.address2,
            status="UNRESOLVED",
            attempts=attempts,
        )
