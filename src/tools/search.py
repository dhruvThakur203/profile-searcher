from dataclasses import dataclass
from typing import Protocol


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    serp_index: int | None = None


class SearchTool(Protocol):
    def search(self, query: str) -> list[SearchResult]:
        """Return structured results for an exact query string."""


class FakeSearchTool:
    """Deterministic search stand-in. Matches the query string exactly."""

    def __init__(self) -> None:
        self._registry: dict[str, tuple[SearchResult, ...]] = {}

    def register(self, query: str, results: list[SearchResult]) -> None:
        self._registry[query] = tuple(results)

    def search(self, query: str) -> list[SearchResult]:
        return list(self._registry.get(query, ()))
