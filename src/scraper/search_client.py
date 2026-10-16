import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from ..tools.search import SearchResult


BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
IDENTIFYING_USER_AGENT = "shareholder-research/0.1 (deterministic research automation)"


class SearchStatus(str, Enum):
    OK = "OK"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class SearchPolicy:
    """Safe defaults for low-volume sequential search."""

    delay_seconds: float = 1.0
    max_retries: int = 2
    backoff_base_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 30.0
    max_consecutive_failures: int = 3
    max_consecutive_provider_failures: int = 10
    timeout_seconds: float = 20.0
    max_searches_per_person: int = 12
    max_concurrency: int = 1
    # Holder-name combination searches (at most 3 pairs + 1 triple) have their
    # own allowance so they never displace existing strategies.
    max_holder_combination_searches: int = 4
    # Name + address searches for each joint holder, counted per holder so the
    # primary holder's budget can never crowd them out.
    max_searches_per_additional_holder: int = 12

    def __post_init__(self) -> None:
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.backoff_base_seconds < 0:
            raise ValueError("backoff_base_seconds cannot be negative")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be at least 1")
        if self.max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be at least 1")
        if self.max_consecutive_provider_failures < 1:
            raise ValueError("max_consecutive_provider_failures must be at least 1")
        if self.max_searches_per_person < 1:
            raise ValueError("max_searches_per_person must be at least 1")
        if self.max_holder_combination_searches < 0:
            raise ValueError("max_holder_combination_searches cannot be negative")
        if self.max_searches_per_additional_holder < 0:
            raise ValueError("max_searches_per_additional_holder cannot be negative")
        if self.max_concurrency != 1:
            raise ValueError("Searches run one at a time; max_concurrency must be 1")


@dataclass
class SearchResponse:
    results: list[SearchResult]
    status: SearchStatus = SearchStatus.OK
    message: str = ""
    retryable: bool = False
    halted: bool = False
    # Optional Google AI Overview capture from the same rendered SERP (Playwright only).
    ai_overview: object | None = None


class SearchClient(Protocol):
    def search(self, query: str) -> SearchResponse:
        """Return structured results or a blocked/error state for one query."""


class HttpTransport(Protocol):
    def __call__(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> tuple[int, bytes]:
        """Perform one GET and return the status code and body."""


class UrllibSession:
    """Reusable opener for sequential requests to one official API."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener()

    def __call__(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


class StaticSearchClient:
    """Exact-query stand-in for tests and offline runs. No network access."""

    def __init__(self) -> None:
        self._registry: dict[str, SearchResponse] = {}
        self._overview_registry: dict[str, object] = {}
        self.calls: list[str] = []

    def register(
        self,
        query: str,
        results: list[SearchResult] | None = None,
        *,
        status: SearchStatus = SearchStatus.OK,
        message: str = "",
        retryable: bool = False,
        ai_overview: object | None = None,
    ) -> None:
        self._registry[query] = SearchResponse(
            results=list(results or []),
            status=status,
            message=message,
            retryable=retryable,
            ai_overview=ai_overview,
        )
        if ai_overview is not None:
            self._overview_registry[query] = ai_overview

    def register_overview(self, query: str, ai_overview: object) -> None:
        self._overview_registry[query] = ai_overview
        registered = self._registry.get(query)
        if registered is not None:
            self._registry[query] = SearchResponse(
                results=list(registered.results),
                status=registered.status,
                message=registered.message,
                retryable=registered.retryable,
                ai_overview=ai_overview,
            )

    def search(self, query: str) -> SearchResponse:
        self.calls.append(query)
        registered = self._registry.get(query)
        if registered is None:
            overview = self._overview_registry.get(query)
            return SearchResponse(results=[], status=SearchStatus.OK, ai_overview=overview)
        return SearchResponse(
            results=list(registered.results),
            status=registered.status,
            message=registered.message,
            retryable=registered.retryable,
            ai_overview=registered.ai_overview,
        )


class PoliteSearchClient:
    """Rate limit, retry, cache, and circuit-breaker around any search client."""

    def __init__(
        self,
        client: SearchClient,
        policy: SearchPolicy | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._client = client
        self.policy = policy or SearchPolicy()
        self._sleep = sleeper or time.sleep
        self._cache: dict[str, SearchResponse] = {}
        self._consecutive_provider_failures = 0

    @property
    def circuit_open(self) -> bool:
        return (
            self._consecutive_provider_failures
            >= self.policy.max_consecutive_provider_failures
        )

    def search(self, query: str) -> SearchResponse:
        cached = self._cache.get(query)
        if cached is not None:
            return _clone_response(cached)

        if self.circuit_open:
            return SearchResponse(
                results=[],
                status=SearchStatus.BLOCKED,
                message=(
                    "Stopped requesting the search provider after repeated "
                    "blocking or errors."
                ),
                halted=True,
            )

        self._pause(self.policy.delay_seconds)
        attempt = 0
        while True:
            response = self._client.search(query)
            if response.status == SearchStatus.OK:
                self._consecutive_provider_failures = 0
                self._cache[query] = response
                return _clone_response(response)

            can_retry = (
                response.retryable
                and response.status == SearchStatus.ERROR
                and attempt < self.policy.max_retries
            )
            if can_retry:
                wait = self.policy.backoff_base_seconds * (
                    self.policy.backoff_multiplier ** attempt
                )
                self._pause(min(wait, self.policy.max_backoff_seconds))
                attempt += 1
                continue

            self._consecutive_provider_failures += 1
            self._cache[query] = response
            return _clone_response(response)

    def _pause(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep(seconds)


class BraveSearchClient:
    """Official Brave Search API client.

    Calls only the configured API endpoint. A block page, captcha response,
    or access denial becomes BLOCKED. This client does not retry, change
    its identity, or fetch result pages.
    """

    def __init__(
        self,
        api_key: str,
        endpoint: str = BRAVE_SEARCH_ENDPOINT,
        transport: HttpTransport | None = None,
        timeout: float = 20.0,
        results_per_query: int = 10,
    ) -> None:
        self.api_key = api_key.strip()
        self.endpoint = endpoint
        self.timeout = timeout
        self.results_per_query = results_per_query
        self._transport = transport or UrllibSession()

    def search(self, query: str) -> SearchResponse:
        if not self.api_key:
            return SearchResponse(
                results=[],
                status=SearchStatus.ERROR,
                message="Brave Search API key is not configured. No request was sent.",
            )

        separator = "&" if "?" in self.endpoint else "?"
        url = (
            f"{self.endpoint}{separator}"
            + urllib.parse.urlencode(
                {"q": query, "count": str(self.results_per_query)}
            )
        )
        headers = {
            "Accept": "application/json",
            "User-Agent": IDENTIFYING_USER_AGENT,
            "X-Subscription-Token": self.api_key,
        }
        try:
            status, body = self._transport(url, headers, self.timeout)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            return SearchResponse(
                results=[],
                status=SearchStatus.ERROR,
                message=f"Temporary network error: {exc}",
                retryable=True,
            )
        return self._interpret(status, body)

    def _interpret(self, status: int, body: bytes) -> SearchResponse:
        if status in {401, 403, 451}:
            return SearchResponse(
                results=[],
                status=SearchStatus.BLOCKED,
                message=f"Search provider blocked automated access (HTTP {status}).",
            )
        if status == 429 or status == 408 or 500 <= status <= 599:
            return SearchResponse(
                results=[],
                status=SearchStatus.ERROR,
                message=f"Temporary search provider error (HTTP {status}).",
                retryable=True,
            )
        if status != 200:
            return SearchResponse(
                results=[],
                status=SearchStatus.ERROR,
                message=f"Search provider returned HTTP {status}.",
            )
        return self._parse_ok_body(body)

    def _parse_ok_body(self, body: bytes) -> SearchResponse:
        text = body.decode("utf-8", errors="replace")
        stripped = text.lstrip().lower()
        if stripped.startswith("<") or stripped.startswith("<!doctype"):
            return SearchResponse(
                results=[],
                status=SearchStatus.BLOCKED,
                message="Search provider returned a block page instead of search results.",
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            lowered = text.lower()
            if "captcha" in lowered or "unusual traffic" in lowered:
                return SearchResponse(
                    results=[],
                    status=SearchStatus.BLOCKED,
                    message="Search provider returned a block page instead of search results.",
                )
            return SearchResponse(
                results=[],
                status=SearchStatus.ERROR,
                message="Search provider returned an unreadable response.",
            )
        if not isinstance(payload, dict):
            return SearchResponse(
                results=[],
                status=SearchStatus.ERROR,
                message="Search provider returned an unexpected response.",
            )
        web = payload.get("web") or {}
        items = web.get("results") if isinstance(web, dict) else None
        if not isinstance(items, list):
            return SearchResponse(results=[], status=SearchStatus.OK)
        results = []
        for item in items[: self.results_per_query]:
            if not isinstance(item, dict):
                continue
            snippet_parts = [str(item.get("description") or "")]
            extras = item.get("extra_snippets") or []
            if isinstance(extras, list):
                snippet_parts.extend(str(part) for part in extras if part)
            results.append(
                SearchResult(
                    title=str(item.get("title") or ""),
                    url=str(item.get("url") or ""),
                    snippet="\n".join(part for part in snippet_parts if part),
                )
            )
        return SearchResponse(results=results, status=SearchStatus.OK)


def _clone_response(response: SearchResponse) -> SearchResponse:
    return SearchResponse(
        results=list(response.results),
        status=response.status,
        message=response.message,
        retryable=response.retryable,
        halted=response.halted,
        ai_overview=response.ai_overview,
    )
