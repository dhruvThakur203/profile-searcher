"""Recovery after the Chrome window dies and retry of failed searches on rerun."""

import asyncio
from datetime import datetime, timezone

from src.models import PersonRecord
from src.query_planner import build_query_plan
from src.scraper.block_cache import GoogleBlockCache
from src.scraper.checkpoint import SqliteCheckpoint
from src.scraper.google_config import GoogleBrowserConfig
from src.scraper.playwright_google_client import PageSnapshot, PlaywrightGoogleSearchClient
from src.scraper.researcher import ShareholderResearcher
from src.scraper.search_client import SearchPolicy, SearchStatus, StaticSearchClient
from src.scraper.timing import SearchTiming
from tests.test_google_research import SERP_HTML
from tests.test_source_integration import MapSourceFetcher


async def _noop(_seconds):
    return None


class ClosableDriver:
    """Fake driver whose window can be closed like a real Chrome window."""

    def __init__(self):
        self.alive = False
        self.start_count = 0

    def is_alive(self):
        return self.alive

    async def start(self):
        self.alive = True
        self.start_count += 1

    async def stop(self):
        if not self.alive:
            raise RuntimeError("Target page, context or browser has been closed")
        self.alive = False

    async def ensure_home(self):
        return None

    async def submit_query(self, query, timing):
        return None

    async def settle_reading(self):
        return None

    async def snapshot(self):
        if not self.alive:
            raise RuntimeError("Page.content: Target page, context or browser has been closed")
        return PageSnapshot(url="https://www.google.com/search?q=x", html=SERP_HTML, title="r")

    async def content(self):
        return SERP_HTML

    async def dismiss_consent(self):
        return False

    async def evaluate_results(self):
        return []

    async def locator_results(self):
        return []

    async def save_diagnostics(self, directory, snapshot):
        return None


def _client(tmp_path, driver):
    return PlaywrightGoogleSearchClient(
        GoogleBrowserConfig(timing=SearchTiming.instant()),
        driver=driver,
        block_cache=GoogleBlockCache(
            tmp_path / "block.json",
            cooldown_seconds=1,
            clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        sleeper=_noop,
    )


def test_closed_browser_window_is_relaunched_and_search_succeeds(tmp_path):
    driver = ClosableDriver()
    client = _client(tmp_path, driver)

    async def scenario():
        first = await client.search("TEST PERSON")
        driver.alive = False  # user closed the window / Chrome crashed
        second = await client.search("TEST PERSON TWO")
        return first, second

    first, second = asyncio.run(scenario())
    assert first.status == SearchStatus.OK
    assert second.status == SearchStatus.OK
    assert driver.start_count == 2


def _record():
    return PersonRecord(
        record_id="12",
        name="TEST PERSON",
        address1="12 MAIN STREET SPRINGFIELD",
        address2="560001",
        country="India",
    )


def test_finished_row_retries_only_its_failed_searches(tmp_path):
    record = _record()
    plans = build_query_plan(record)
    store = SqliteCheckpoint(tmp_path / "cp.sqlite")
    flaky = StaticSearchClient()
    failed = [plans[1].query, plans[3].query]
    for query in failed:
        flaky.register(query, status=SearchStatus.ERROR, message="browser closed")
    first = ShareholderResearcher(
        flaky,
        SearchPolicy(delay_seconds=0),
        checkpoint=store,
        source_fetcher=MapSourceFetcher({}),
        use_result_text_when_unfetched=False,
    ).research_person(record)
    assert store.load(record.record_id).finished
    assert sum(entry.status == SearchStatus.ERROR for entry in first.log) == 2

    retry = StaticSearchClient()
    second = ShareholderResearcher(
        retry,
        SearchPolicy(delay_seconds=0),
        checkpoint=store,
        source_fetcher=MapSourceFetcher({}),
        use_result_text_when_unfetched=False,
    ).research_person(record)
    assert retry.calls == failed
    assert all(entry.status == SearchStatus.OK for entry in second.log)
    assert sorted(entry.strategy for entry in second.log) == sorted(
        entry.strategy for entry in first.log
    )
