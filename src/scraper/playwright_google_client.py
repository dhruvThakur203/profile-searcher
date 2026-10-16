import asyncio
import logging
import random
from dataclasses import dataclass
from pathlib import Path

from ..tools.search import SearchResult
from .block_cache import GoogleBlockCache
from .google_config import WEBDRIVER_INIT_SCRIPT, GoogleBrowserConfig
from .ai_overview import (
    AI_OVERVIEW_EXTRACT_JS,
    STATUS_BLOCKED,
    STATUS_CAPTURE_FAILED,
    STATUS_NOT_PRESENT,
    AiOverviewCapture,
    capture_from_playwright_payload,
    parse_ai_overview_from_html,
)
from .google_result_parser import clean_google_results, parse_google_html
from .google_state import GooglePageState, detect_google_page
from .search_client import SearchResponse, SearchStatus
from .document_investigator.models import SourceDocument
from .source_fetcher import (
    FetchResult,
    extract_html_document,
    extract_pdf_document,
    log_fetch_result,
    log_pdf_result,
    looks_like_pdf,
    reject_empty_document,
)
from .timing import SearchTiming
from .urls import is_google_navigation_url, is_valid_external_source_url


logger = logging.getLogger("shareholder.google")

SEARCH_BOX_SELECTORS = (
    'textarea[name="q"]',
    'input[name="q"]',
    '[role="combobox"]',
)

RESULT_CONTAINER_SELECTORS = (
    "#search .g",
    "#rso .g",
    "div.MjjYud",
    "[data-sokoban-container]",
)

SERP_EXTRACT_JS = """
() => {
  const selectors = ["#search .g", "#rso .g", "div.MjjYud", "[data-sokoban-container]"];
  const nodes = [];
  for (const selector of selectors) {
    document.querySelectorAll(selector).forEach((node) => nodes.push(node));
  }
  const items = [];
  const seen = new Set();
  for (const node of nodes) {
    const titleNode = node.querySelector("h3");
    const link = titleNode ? titleNode.closest("a") : node.querySelector("a[href]");
    if (!link) continue;
    const href = link.getAttribute("href") || link.href || "";
    const title = titleNode ? (titleNode.innerText || "") : "";
    const snippetNode = node.querySelector(".VwiC3b, .IsZvec");
    const snippet = snippetNode ? (snippetNode.innerText || "") : "";
    const cardText = node.innerText || "";
    const key = title + "\\n" + href;
    if (!href || seen.has(key)) continue;
    seen.add(key);
    items.push({title, url: href, snippet, card_text: cardText});
  }
  return items;
}
"""


SEARCH_BOX_MAX_ATTEMPTS = 3


class SearchBoxNotFound(Exception):
    pass


class SearchBoxInteractionError(Exception):
    """The Google search box was found but could not be clicked safely."""


class ChromeLaunchError(RuntimeError):
    """Installed Chrome could not be launched. Chromium is never used as a fallback."""


def first_search_box_selector(available: list[str]) -> str | None:
    for selector in SEARCH_BOX_SELECTORS:
        if selector in available:
            return selector
    return None


def bbox_intersects_viewport(
    bbox: dict | None,
    viewport_width: int,
    viewport_height: int,
) -> bool:
    if not bbox:
        return False
    try:
        x = float(bbox["x"])
        y = float(bbox["y"])
        width = float(bbox["width"])
        height = float(bbox["height"])
    except (KeyError, TypeError, ValueError):
        return False
    if width <= 0 or height <= 0:
        return False
    return x + width > 0 and y + height > 0 and x < viewport_width and y < viewport_height


@dataclass
class PageSnapshot:
    url: str
    html: str
    title: str = ""


class PlaywrightGoogleSearchClient:
    """One headed Chrome page that types queries into the Google search box.

    A CAPTCHA or unusual-traffic page is recorded and searched again only
    after the cooldown. It is not solved.
    """

    def __init__(
        self,
        config: GoogleBrowserConfig | None = None,
        driver=None,
        block_cache: GoogleBlockCache | None = None,
        sleeper=None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config or GoogleBrowserConfig()
        self._driver = driver
        self._owns_driver = driver is None
        self.block_cache = block_cache or GoogleBlockCache(
            self.config.block_file(),
            self.config.timing.block_cooldown_seconds,
        )
        self._sleeper = sleeper or asyncio.sleep
        self._rng = rng or random.Random()
        self._lock: asyncio.Lock | None = None
        self._started = False
        self._completed_searches = 0
        self._search_attempts = 0

    async def start(self) -> None:
        if self._started:
            if self._driver_alive():
                return
            logger.info("[GOOGLE] Browser window was closed; relaunching Chrome")
            await self._discard_driver_session()
        if self._driver is None:
            self._driver = PlaywrightGoogleDriver(self.config, sleeper=self._sleeper, rng=self._rng)
            self._owns_driver = True
        logger.info("[GOOGLE] Starting browser")
        await self._driver.start()
        await self._driver.ensure_home()
        self._started = True

    async def stop(self) -> None:
        if self._driver is not None and self._started:
            await self._driver.stop()
        self._started = False

    def _driver_alive(self) -> bool:
        alive = getattr(self._driver, "is_alive", None)
        return True if alive is None else bool(alive())

    async def _discard_driver_session(self) -> None:
        try:
            await self._driver.stop()
        except Exception as exc:
            logger.info("[GOOGLE] Ignoring error while closing the dead browser: %s", exc)
        self._started = False

    async def search(self, query: str) -> SearchResponse:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            return await self._search_locked(query)

    async def open_source(
        self,
        url: str,
        title: str = "",
        serp_rank: int | None = None,
    ) -> FetchResult:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            try:
                await self.start()
            except Exception as exc:
                return FetchResult(
                    ok=False,
                    error_code="SOURCE_FETCH_ERROR",
                    error_message=f"Browser failed to start: {exc}",
                )
            if serp_rank is not None and serp_rank > 0:
                clicked = await self._driver.open_serp_result(serp_rank, title=title)
                if clicked.ok:
                    return clicked
            return await self._driver.open_url_document(url, title=title)

    async def _search_locked(self, query: str) -> SearchResponse:
        if self.block_cache.is_blocked():
            logger.info("[GOOGLE] Cooldown active")
            return SearchResponse(
                results=[],
                status=SearchStatus.BLOCKED,
                message=(
                    "Google cooldown is active "
                    f"({self.block_cache.reason() or 'blocked'}). "
                    f"Next search after {self.block_cache.blocked_until()}."
                ),
                halted=True,
            )
        try:
            await self.start()
        except Exception as exc:
            logger.info("[GOOGLE] Browser start failed: %s", exc)
            return SearchResponse(
                results=[],
                status=SearchStatus.ERROR,
                message=f"Browser failed to start: {exc}",
            )

        self._search_attempts += 1
        between = None
        if self._completed_searches > 0:
            between = self.config.timing.sample_between_searches(self._rng)
            await self._pause(between)
        pre_search = self.config.timing.sample_pre_search(self._rng)
        await self._pause(pre_search)
        self._log_timing(
            query_number=self._search_attempts,
            pre_search=pre_search,
            between_search=between,
            post_submit=None,
            reading=None,
            cooldown=None,
        )

        try:
            preview = await self._driver.snapshot()
        except Exception as exc:
            logger.info("[GOOGLE] Could not read the browser page: %s", exc)
            preview = None
            if not self._driver_alive():
                try:
                    await self.start()
                    preview = await self._driver.snapshot()
                except Exception as retry_exc:
                    exc = retry_exc
            if preview is None:
                return SearchResponse(
                    results=[],
                    status=SearchStatus.ERROR,
                    message=f"Google search failed: {exc}",
                )
        preview_state = detect_google_page(preview.url, preview.html, preview.title)
        if preview_state == GooglePageState.CONSENT:
            logger.info("[GOOGLE] Consent page detected")
            await self._driver.dismiss_consent()
        elif preview_state == GooglePageState.CAPTCHA:
            return await self._block("captcha", preview)

        logger.info("[GOOGLE] Searching: %s", query)
        try:
            await self._driver.submit_query(query, self.config.timing)
            post_submit = self.config.timing.sample_post_submit(self._rng)
            await self._pause(post_submit)
            snapshot = await self._driver.snapshot()
        except SearchBoxNotFound as exc:
            logger.info("[GOOGLE] Search box was not found")
            return SearchResponse(
                results=[],
                status=SearchStatus.ERROR,
                message=str(exc),
            )
        except SearchBoxInteractionError as exc:
            logger.info("[GOOGLE] Search box interaction failed: %s", exc)
            return SearchResponse(
                results=[],
                status=SearchStatus.ERROR,
                message=str(exc),
            )
        except Exception as exc:
            logger.info("[GOOGLE] Search failed: %s", exc)
            return SearchResponse(
                results=[],
                status=SearchStatus.ERROR,
                message=f"Google search failed: {exc}",
            )

        state = detect_google_page(snapshot.url, snapshot.html, snapshot.title)
        if state == GooglePageState.CONSENT:
            logger.info("[GOOGLE] Consent page detected")
            dismissed = await self._driver.dismiss_consent()
            if dismissed:
                post_submit = self.config.timing.sample_post_submit(self._rng)
                await self._pause(post_submit)
                snapshot = await self._driver.snapshot()
                state = detect_google_page(snapshot.url, snapshot.html, snapshot.title)
        if state == GooglePageState.CAPTCHA:
            return await self._block("captcha", snapshot)
        if state == GooglePageState.ERROR:
            return SearchResponse(
                results=[],
                status=SearchStatus.ERROR,
                message="Google returned an error page.",
            )

        reading = self.config.timing.sample_reading(self._rng)
        await self._driver.settle_reading()
        await self._pause(reading)
        self._log_timing(
            query_number=self._search_attempts,
            pre_search=pre_search,
            between_search=between,
            post_submit=post_submit,
            reading=reading,
            cooldown=None,
        )

        results = await self._collect_results()
        ai_overview = await self._capture_ai_overview()
        self._completed_searches += 1
        logger.info("[GOOGLE] Results found: %s", len(results))

        cooldown = None
        if self._completed_searches % self.config.timing.cooldown_after_searches == 0:
            cooldown = self.config.timing.sample_cooldown(self._rng)
            logger.info("[GOOGLE] Pausing after %s searches", self._completed_searches)
            await self._pause(cooldown)
            self._log_timing(
                query_number=self._search_attempts,
                pre_search=pre_search,
                between_search=between,
                post_submit=post_submit,
                reading=reading,
                cooldown=cooldown,
            )
        return SearchResponse(results=results, status=SearchStatus.OK, ai_overview=ai_overview)

    async def _capture_ai_overview(self) -> AiOverviewCapture:
        maybe_wait = getattr(self._driver, "maybe_wait_for_ai_overview", None)
        if maybe_wait is not None:
            await maybe_wait()
        try:
            payload = await self._driver.evaluate_ai_overview()
            capture = capture_from_playwright_payload(payload)
            if capture.status != STATUS_NOT_PRESENT:
                return capture
        except Exception as exc:
            logger.info("[GOOGLE] AI Overview JS capture failed: %s", exc)
        try:
            fresh = await self._driver.snapshot()
            if fresh.html:
                return parse_ai_overview_from_html(fresh.html)
        except Exception as exc:
            return AiOverviewCapture(
                status=STATUS_CAPTURE_FAILED,
                extraction_notes=f"HTML fallback failed: {exc}",
            )
        return AiOverviewCapture(
            status=STATUS_CAPTURE_FAILED,
            extraction_notes="No SERP snapshot available for AI Overview capture.",
        )

    async def _collect_results(self) -> list[SearchResult]:
        for reader in (
            self._driver.evaluate_results,
            self._driver.locator_results,
        ):
            try:
                items = await reader()
            except Exception:
                items = None
            cleaned = clean_google_results(items or [])
            if cleaned:
                return [item.as_search_result() for item in cleaned]
        try:
            html = await self._driver.content()
        except Exception:
            return []
        return [item.as_search_result() for item in parse_google_html(html)]

    async def _block(self, reason: str, snapshot: PageSnapshot) -> SearchResponse:
        logger.info("[GOOGLE] CAPTCHA/block detected")
        self.block_cache.record_block(reason)
        if self.config.debug:
            await self._driver.save_diagnostics(str(self.config.diagnostics_path()), snapshot)
        return SearchResponse(
            results=[],
            status=SearchStatus.BLOCKED,
            message=(
                "Google presented a CAPTCHA or block page. "
                f"Searching is paused until {self.block_cache.blocked_until()}."
            ),
            halted=True,
            ai_overview=AiOverviewCapture(
                status=STATUS_BLOCKED,
                extraction_notes="Search blocked before results could be captured.",
            ),
        )

    def _log_timing(
        self,
        *,
        query_number: int,
        pre_search: float | None,
        between_search: float | None,
        post_submit: float | None,
        reading: float | None,
        cooldown: float | None,
    ) -> None:
        profile = Path(self.config.profile_dir)
        if not profile.is_absolute():
            profile = Path.cwd() / profile
        search_number = (
            self._completed_searches if cooldown is not None else self._completed_searches + 1
        )
        parts = [
            f"search_number={search_number}",
            f"query_number={query_number}",
            f"profile={profile.resolve()}",
            f"channel={self.config.channel}",
            f"headless={self.config.headless}",
            f"typing_range={self.config.timing.typing_min_ms}-{self.config.timing.typing_max_ms}ms",
        ]
        if pre_search is not None:
            parts.append(f"pre_search={pre_search:.2f}s")
        if between_search is not None:
            parts.append(f"between_search={between_search:.2f}s")
        if post_submit is not None:
            parts.append(f"post_submit={post_submit:.2f}s")
        if reading is not None:
            parts.append(f"reading={reading:.2f}s")
        if cooldown is not None:
            parts.append(f"cooldown={cooldown:.2f}s")
        logger.info("[GOOGLE TIMING] %s", " ".join(parts))

    async def _pause(self, seconds: float) -> None:
        if seconds > 0:
            await self._sleeper(seconds)


class SyncPlaywrightGoogleSearchClient:
    """Synchronous facade so the existing researcher can call search(query)."""

    manages_browser = True

    def __init__(
        self,
        config: GoogleBrowserConfig | None = None,
        driver=None,
        block_cache: GoogleBlockCache | None = None,
        sleeper=None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config or GoogleBrowserConfig()
        self.block_cache = block_cache or GoogleBlockCache(
            self.config.block_file(),
            self.config.timing.block_cooldown_seconds,
        )
        self._client = PlaywrightGoogleSearchClient(
            self.config,
            driver=driver,
            block_cache=self.block_cache,
            sleeper=sleeper or asyncio.sleep,
            rng=rng,
        )
        self._loop = asyncio.new_event_loop()
        self._closed = False

    @property
    def circuit_open(self) -> bool:
        return self.block_cache.is_blocked()

    def search(self, query: str) -> SearchResponse:
        self._ensure_open()
        return self._loop.run_until_complete(self._client.search(query))

    def open_source(self, url: str, title: str = "", serp_rank: int | None = None) -> FetchResult:
        """Open a Google-discovered source in the headed Chrome session.

        Prefers clicking the current SERP result (follows /goto redirects) when
        serp_rank is provided. Falls back to a direct navigation in a new tab.
        """
        self._ensure_open()
        return self._loop.run_until_complete(
            self._client.open_source(url, title=title, serp_rank=serp_rank)
        )

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._loop.run_until_complete(self._client.stop())
        finally:
            self._loop.close()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("The Google browser client is already closed.")


class PlaywrightGoogleDriver:
    def __init__(
        self,
        config: GoogleBrowserConfig,
        sleeper=None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self._sleeper = sleeper or asyncio.sleep
        self._rng = rng or random.Random()
        self._playwright = None
        self._context = None
        self._page = None
        self.channel_used: str | None = None
        self.start_count = 0

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        profile = self.config.profile_path()
        profile.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        launch_kwargs = self.config.persistent_launch_kwargs()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                **launch_kwargs
            )
        except Exception as exc:
            raise ChromeLaunchError(
                "Installed Chrome (channel='chrome') could not be launched. "
                "Chromium fallback is disabled. Install Google Chrome or fix the "
                f"Chrome channel. Original error: {exc}"
            ) from exc
        self.channel_used = self.config.channel
        self.start_count += 1
        await self._context.add_init_script(WEBDRIVER_INIT_SCRIPT)
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        mode = "headless" if self.config.headless else "headed"
        logger.info("[GOOGLE] Launched %s Chrome (channel=%s)", mode, self.channel_used)

    async def stop(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
        finally:
            try:
                if self._playwright is not None:
                    await self._playwright.stop()
            finally:
                self._context = None
                self._page = None
                self._playwright = None

    def is_alive(self) -> bool:
        try:
            return self._page is not None and not self._page.is_closed()
        except Exception:
            return False

    async def ensure_home(self) -> None:
        if self._page is None:
            raise RuntimeError("Browser page is not open.")
        await self._page.goto(self.config.home_url, wait_until="domcontentloaded")

    async def submit_query(self, query: str, timing: SearchTiming) -> None:
        box, selector = await self._find_and_prepare_search_box(query)
        await box.fill("")
        await self._type_query(query, timing)
        await self._page.keyboard.press("Enter")
        logger.info("[GOOGLE SEARCHBOX] prepared selector=%s query=%s", selector, query)
        try:
            await self._page.wait_for_selector(
                "#search, #rso, #captcha-form, iframe[src*='recaptcha']",
                timeout=15000,
            )
        except Exception:
            logger.info("[GOOGLE] Timed out waiting for a results or block page")

    async def _find_and_prepare_search_box(self, query: str):
        """Locate a fresh visible search box, scroll it into view, and click it."""
        restored_home = False
        last_error: Exception | None = None
        for attempt in range(1, SEARCH_BOX_MAX_ATTEMPTS + 1):
            box, selector = await self._find_search_box()
            if box is None and not restored_home:
                logger.info(
                    "[GOOGLE SEARCHBOX] attempt=%s no search box; restoring Google home",
                    attempt,
                )
                await self.ensure_home()
                restored_home = True
                box, selector = await self._find_search_box()
            if box is None:
                last_error = SearchBoxNotFound(
                    "Google search box was not found. Selectors tried: "
                    + ", ".join(SEARCH_BOX_SELECTORS)
                )
                await self._log_searchbox_state(
                    query=query,
                    attempt=attempt,
                    selector=selector,
                    visible=False,
                    bbox=None,
                )
                continue
            try:
                prepared = await self._prepare_search_box_click(
                    box, selector=selector, query=query, attempt=attempt
                )
            except Exception as exc:
                last_error = exc
                await self._log_searchbox_state(
                    query=query,
                    attempt=attempt,
                    selector=selector,
                    visible=None,
                    bbox=None,
                    error=str(exc),
                )
                continue
            if prepared is None:
                last_error = SearchBoxInteractionError(
                    "Google search box was outside the viewport or not clickable."
                )
                if not restored_home and attempt < SEARCH_BOX_MAX_ATTEMPTS:
                    logger.info(
                        "[GOOGLE SEARCHBOX] attempt=%s click prep failed; restoring Google home",
                        attempt,
                    )
                    await self.ensure_home()
                    restored_home = True
                continue
            if attempt > 1:
                logger.info("[GOOGLE SEARCHBOX] recovered on attempt=%s", attempt)
            return prepared, selector

        if isinstance(last_error, SearchBoxNotFound):
            raise last_error
        raise SearchBoxInteractionError(
            "Google search box could not be clicked after "
            f"{SEARCH_BOX_MAX_ATTEMPTS} attempts. Query={query!r}. "
            f"Last error: {last_error}"
        )

    async def _prepare_search_box_click(
        self,
        box,
        *,
        selector: str | None,
        query: str,
        attempt: int,
    ):
        try:
            visible = await box.is_visible()
        except Exception:
            visible = False
        try:
            await box.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        await self._pause(self._rng.uniform(0.05, 0.25))
        try:
            bbox = await box.bounding_box()
        except Exception:
            bbox = None
        viewport = {
            "width": self.config.viewport_width,
            "height": self.config.viewport_height,
        }
        try:
            viewport = self._page.viewport_size or viewport
        except Exception:
            pass
        in_view = bbox_intersects_viewport(
            bbox, int(viewport["width"]), int(viewport["height"])
        )
        await self._log_searchbox_state(
            query=query,
            attempt=attempt,
            selector=selector,
            visible=visible,
            bbox=bbox,
            viewport=viewport,
            in_view=in_view,
        )
        if not visible or not in_view:
            try:
                await self._page.evaluate("window.scrollTo(0, 0)")
            except Exception:
                pass
            box, selector = await self._find_search_box()
            if box is None:
                return None
            try:
                await box.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            try:
                bbox = await box.bounding_box()
            except Exception:
                bbox = None
            in_view = bbox_intersects_viewport(
                bbox, int(viewport["width"]), int(viewport["height"])
            )
            try:
                visible = await box.is_visible()
            except Exception:
                visible = False
            if not visible or not in_view:
                return None
        await self._move_mouse_toward(box)
        try:
            await box.click(timeout=5000)
        except Exception as exc:
            # Element moved or was replaced; caller re-locates a fresh handle.
            logger.info(
                "[GOOGLE SEARCHBOX] click failed attempt=%s error=%s",
                attempt,
                exc,
            )
            return None
        return box

    async def _log_searchbox_state(
        self,
        *,
        query: str,
        attempt: int,
        selector: str | None,
        visible: bool | None,
        bbox: dict | None,
        viewport: dict | None = None,
        in_view: bool | None = None,
        error: str | None = None,
    ) -> None:
        if viewport is None:
            viewport = {
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            }
            try:
                viewport = self._page.viewport_size or viewport
            except Exception:
                pass
        url = ""
        title = ""
        try:
            url = self._page.url
            title = await self._page.title()
        except Exception:
            pass
        parts = [
            f"attempt={attempt}",
            f"selector={selector or '(none)'}",
            f"visible={visible}",
            f"bbox={bbox}",
            f"viewport={viewport.get('width')}x{viewport.get('height')}",
            f"in_view={in_view}",
            f"url={url}",
            f"title={title}",
            f"query={query}",
        ]
        if error:
            parts.append(f"error={error}")
        logger.info("[GOOGLE SEARCHBOX] %s", " ".join(parts))

    async def settle_reading(self) -> None:
        if self._page is None:
            return
        try:
            await self._page.mouse.wheel(0, self._rng.randint(120, 420))
            await self._pause(self._rng.uniform(0.1, 0.4))
            await self._page.mouse.wheel(0, -self._rng.randint(40, 160))
        except Exception:
            return

    async def snapshot(self) -> PageSnapshot:
        return PageSnapshot(
            url=self._page.url,
            html=await self._page.content(),
            title=await self._page.title(),
        )

    async def content(self) -> str:
        return await self._page.content()

    async def dismiss_consent(self) -> bool:
        labels = ("Accept all", "I agree", "Accept", "Reject all")
        for label in labels:
            locator = self._page.get_by_role("button", name=label)
            try:
                if await locator.count() == 0:
                    continue
                await locator.first.click(timeout=3000)
                await self._page.wait_for_load_state("domcontentloaded")
                return True
            except Exception:
                continue
        return False

    async def evaluate_results(self) -> list[dict]:
        items = await self._page.evaluate(SERP_EXTRACT_JS)
        return list(items or [])

    async def maybe_wait_for_ai_overview(self) -> None:
        if self._page is None:
            return
        selector = (
            '[data-attrid="SGCOP"], [data-container-id="aimfl"], '
            'div[jsname="ZLxsqf"], div[jsname="Ya7sxe"], #cnt div[jsname="ZLxsqf"]'
        )
        try:
            await self._page.wait_for_selector(selector, timeout=2500, state="visible")
        except Exception:
            pass

    async def evaluate_ai_overview(self) -> dict:
        if self._page is None:
            return {"present": False}
        try:
            result = await self._page.evaluate(AI_OVERVIEW_EXTRACT_JS)
            return dict(result or {})
        except Exception:
            return {"present": False}

    async def locator_results(self) -> list[dict]:
        items = []
        seen = set()
        for selector in RESULT_CONTAINER_SELECTORS:
            locator = self._page.locator(selector)
            try:
                count = min(await locator.count(), 10)
            except Exception:
                continue
            for index in range(count):
                card = locator.nth(index)
                try:
                    title = await card.locator("h3").first.inner_text(timeout=1000)
                except Exception:
                    title = ""
                try:
                    href = await card.locator("a[href]").first.get_attribute("href")
                except Exception:
                    href = ""
                snippet = ""
                for snippet_selector in (".VwiC3b", ".IsZvec"):
                    try:
                        snippet = await card.locator(snippet_selector).first.inner_text(timeout=500)
                        if snippet:
                            break
                    except Exception:
                        continue
                try:
                    card_text = await card.inner_text(timeout=1000)
                except Exception:
                    card_text = " ".join(part for part in (title, snippet) if part)
                key = (title, href or "")
                if not href or key in seen:
                    continue
                seen.add(key)
                items.append(
                    {
                        "title": title,
                        "url": href,
                        "snippet": snippet,
                        "card_text": card_text,
                    }
                )
        return items

    async def open_serp_result(self, rank: int, title: str = "") -> FetchResult:
        """Click the Nth SERP title link and capture the destination page text."""
        if self._page is None:
            return FetchResult(
                ok=False,
                error_code="SOURCE_FETCH_ERROR",
                error_message="Browser page is not open.",
            )
        serp_url = self._page.url
        link = await self._serp_result_link(rank)
        if link is None:
            return FetchResult(
                ok=False,
                error_code="SOURCE_FETCH_ERROR",
                error_message=f"SERP result rank {rank} was not clickable.",
            )
        try:
            await link.click(timeout=10000)
            await self._page.wait_for_load_state("domcontentloaded", timeout=20000)
            await self._pause(1.0)
        except Exception as exc:
            try:
                await self._page.goto(serp_url, wait_until="domcontentloaded")
            except Exception:
                pass
            return FetchResult(
                ok=False,
                error_code="SOURCE_FETCH_ERROR",
                error_message=f"Failed to open SERP result {rank}: {exc}",
            )

        try:
            opened = await self._document_from_opened_navigation(title=title)
        except Exception as exc:
            opened = FetchResult(
                ok=False,
                error_code="SOURCE_FETCH_ERROR",
                error_message=f"Failed to read opened source: {exc}",
            )

        try:
            await self._page.goto(serp_url, wait_until="domcontentloaded")
        except Exception as exc:
            logger.info("[SOURCE] Could not restore SERP after open: %s", exc)

        if not opened.ok or opened.document is None:
            return opened
        if is_google_navigation_url(opened.document.source_url):
            return FetchResult(
                ok=False,
                error_code="SOURCE_FETCH_ERROR",
                error_message="SERP click remained on a Google navigation URL.",
            )
        logger.info("[SOURCE] Opened SERP result %s -> %s", rank, opened.document.source_url)
        return opened

    async def open_url_document(self, url: str, title: str = "") -> FetchResult:
        cleaned = (url or "").strip()
        if not is_valid_external_source_url(cleaned):
            return FetchResult(
                ok=False,
                error_code="SOURCE_FETCH_ERROR",
                error_message=f"Rejected invalid or non-external source URL: {cleaned}",
            )
        if self._context is None:
            return FetchResult(
                ok=False,
                error_code="SOURCE_FETCH_ERROR",
                error_message="Browser context is not open.",
            )
        page = await self._context.new_page()
        try:
            response = await page.goto(cleaned, wait_until="domcontentloaded", timeout=20000)
            await self._pause(0.8)
            content_type = ""
            body = b""
            if response is not None:
                content_type = (response.headers or {}).get("content-type", "")
                try:
                    body = await response.body()
                except Exception:
                    body = b""
            final_url = page.url or cleaned
            if looks_like_pdf(final_url, content_type, body):
                if not body:
                    body = await self._download_bytes(final_url)
                return self._pdf_bytes_to_result(
                    body,
                    source_url=final_url,
                    title=title,
                    content_type=content_type,
                    request_url=cleaned,
                )
            document = await self._document_from_specific_page(page, title=title)
            empty = reject_empty_document(document, document_type_hint="html")
            if empty is not None:
                log_fetch_result(
                    url=cleaned,
                    final_url=final_url,
                    content_type=content_type,
                    document=document,
                    status=empty.error_code,
                    bytes_count=len(body) if body else None,
                )
                return empty
            log_fetch_result(
                url=cleaned,
                final_url=final_url,
                content_type=content_type,
                document=document,
                status="OK",
                bytes_count=len(body) if body else None,
            )
            logger.info("[SOURCE] Browser-fetched %s", document.source_url)
            return FetchResult(ok=True, document=document)
        except Exception as exc:
            code = "PDF_EXTRACTION_ERROR" if looks_like_pdf(url) else "SOURCE_FETCH_ERROR"
            message = str(exc)
            if "PDF" in message.upper() or "extract" in message.lower():
                code = "PDF_EXTRACTION_ERROR"
            log_fetch_result(url=cleaned, status=code)
            return FetchResult(ok=False, error_code=code, error_message=message)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _document_from_opened_navigation(self, title: str = "") -> FetchResult:
        """Build a SourceDocument from the current page, using PDF bytes when needed."""
        final_url = ""
        if self._page is not None:
            try:
                final_url = self._page.url or ""
            except Exception:
                final_url = ""

        body = b""
        content_type = ""
        if final_url and not is_google_navigation_url(final_url):
            body, content_type = await self._fetch_current_resource_bytes(final_url)

        if looks_like_pdf(final_url, content_type, body):
            if not body:
                body = await self._download_bytes(final_url)
            return self._pdf_bytes_to_result(
                body,
                source_url=final_url,
                title=title,
                content_type=content_type,
                request_url=final_url,
            )

        document = await self._document_from_page(title=title)
        empty = reject_empty_document(document, document_type_hint="html")
        if empty is not None:
            log_fetch_result(
                url=final_url,
                final_url=final_url,
                content_type=content_type,
                document=document,
                status=empty.error_code,
                bytes_count=len(body) if body else None,
            )
            return empty
        log_fetch_result(
            url=final_url,
            final_url=final_url,
            content_type=content_type,
            document=document,
            status="OK",
            bytes_count=len(body) if body else None,
        )
        return FetchResult(ok=True, document=document)

    async def _fetch_current_resource_bytes(self, url: str) -> tuple[bytes, str]:
        body, content_type = await self._download_bytes_with_headers(url)
        if not content_type and body and body.lstrip()[:5] == b"%PDF-":
            content_type = "application/pdf"
        return body, content_type

    async def _download_bytes(self, url: str) -> bytes:
        body, _ = await self._download_bytes_with_headers(url)
        return body

    async def _download_bytes_with_headers(self, url: str) -> tuple[bytes, str]:
        if not url or self._context is None:
            return b"", ""
        try:
            response = await self._context.request.get(url, timeout=20000)
            if response is None:
                return b"", ""
            headers = response.headers or {}
            content_type = headers.get("content-type", "") or headers.get("Content-Type", "")
            body = await response.body()
            return body or b"", content_type
        except Exception as exc:
            logger.info("[SOURCE] Byte download failed for %s: %s", url, exc)
            return b"", ""

    def _pdf_bytes_to_result(
        self,
        body: bytes,
        *,
        source_url: str,
        title: str = "",
        content_type: str = "",
        request_url: str = "",
    ) -> FetchResult:
        if not body:
            log_pdf_result(
                url=source_url,
                bytes_count=0,
                pages=None,
                text_chars=0,
                extraction_status="PDF_EXTRACTION_ERROR",
            )
            log_fetch_result(
                url=request_url or source_url,
                final_url=source_url,
                content_type=content_type or "application/pdf",
                status="PDF_EXTRACTION_ERROR",
                bytes_count=0,
            )
            return FetchResult(
                ok=False,
                error_code="PDF_EXTRACTION_ERROR",
                error_message="PDF body could not be downloaded.",
            )
        try:
            document = extract_pdf_document(body, source_url=source_url, title=title)
        except Exception as exc:
            code = getattr(exc, "code", None) or "PDF_EXTRACTION_ERROR"
            log_pdf_result(
                url=source_url,
                bytes_count=len(body),
                pages=None,
                text_chars=0,
                extraction_status=code,
            )
            log_fetch_result(
                url=request_url or source_url,
                final_url=source_url,
                content_type=content_type or "application/pdf",
                status=code,
                bytes_count=len(body),
            )
            return FetchResult(ok=False, error_code=code, error_message=str(exc))

        empty = reject_empty_document(document, document_type_hint="pdf")
        if empty is not None:
            log_pdf_result(
                url=source_url,
                bytes_count=len(body),
                pages=len(document.page_texts or []),
                text_chars=0,
                extraction_status="PDF_EXTRACTION_ERROR",
            )
            log_fetch_result(
                url=request_url or source_url,
                final_url=source_url,
                content_type=content_type or "application/pdf",
                document=document,
                status="PDF_EXTRACTION_ERROR",
                bytes_count=len(body),
            )
            return empty

        log_pdf_result(
            url=source_url,
            bytes_count=len(body),
            pages=len(document.page_texts or []),
            text_chars=len(document.text or ""),
            extraction_status="OK",
        )
        log_fetch_result(
            url=request_url or source_url,
            final_url=source_url,
            content_type=content_type or "application/pdf",
            document=document,
            status="OK",
            bytes_count=len(body),
        )
        logger.info("[SOURCE] Browser-fetched PDF %s", document.source_url)
        return FetchResult(ok=True, document=document)

    async def _serp_result_link(self, rank: int):
        selectors = (
            "#rso a:has(h3)",
            "#search a:has(h3)",
            "div.MjjYud a:has(h3)",
            "div.g a:has(h3)",
        )
        for selector in selectors:
            locator = self._page.locator(selector)
            try:
                count = await locator.count()
            except Exception:
                continue
            if count >= rank:
                return locator.nth(rank - 1)
        return None

    async def _document_from_page(self, title: str = ""):
        return await self._document_from_specific_page(self._page, title=title)

    async def _document_from_specific_page(self, page, title: str = ""):
        final_url = page.url or ""
        page_title = ""
        try:
            page_title = await page.title()
        except Exception:
            page_title = title
        try:
            visible = await page.inner_text("body", timeout=5000)
        except Exception:
            visible = ""
        if visible.strip():
            return SourceDocument(
                source_url=final_url,
                title=(page_title or title or final_url).strip(),
                document_type="html",
                text=visible,
                metadata={"fetch_mode": "browser_visible_text"},
            )
        html = await page.content()
        return extract_html_document(
            html.encode("utf-8", errors="replace"),
            source_url=final_url,
            title=page_title or title,
        )

    async def save_diagnostics(self, directory: str, snapshot: PageSnapshot) -> None:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        html_path = target / "google_block.html"
        html_path.write_text(snapshot.html, encoding="utf-8")
        logger.info("[GOOGLE] Saved block page HTML to %s", html_path)
        if self._page is None:
            return
        try:
            image_path = target / "google_block.png"
            await self._page.screenshot(path=str(image_path), full_page=False)
            logger.info("[GOOGLE] Saved block page screenshot to %s", image_path)
        except Exception as exc:
            logger.info("[GOOGLE] Screenshot was not saved: %s", exc)

    async def _type_query(self, query: str, timing: SearchTiming) -> None:
        for character in query:
            delay_ms = timing.sample_typing_delay_ms(self._rng)
            await self._page.keyboard.type(character)
            if delay_ms > 0:
                await self._pause(delay_ms / 1000.0)
            pause = timing.sample_typing_pause(self._rng)
            if pause:
                await self._pause(pause)

    async def _move_mouse_toward(self, box) -> None:
        try:
            bounds = await box.bounding_box()
        except Exception:
            return
        if not bounds:
            return
        try:
            await self._page.mouse.move(
                self._rng.uniform(40, 220),
                self._rng.uniform(40, 180),
            )
            await self._pause(self._rng.uniform(0.05, 0.2))
            await self._page.mouse.move(
                bounds["x"] + bounds["width"] * self._rng.uniform(0.3, 0.7),
                bounds["y"] + bounds["height"] * self._rng.uniform(0.3, 0.7),
                steps=self._rng.randint(8, 18),
            )
        except Exception:
            return

    async def _find_search_box(self):
        """Return a fresh (element, selector) pair. Prefer a currently visible box."""
        fallback = (None, None)
        for selector in SEARCH_BOX_SELECTORS:
            try:
                boxes = await self._page.query_selector_all(selector)
            except Exception:
                boxes = []
            for box in boxes:
                if fallback[0] is None:
                    fallback = (box, selector)
                try:
                    if await box.is_visible():
                        return box, selector
                except Exception:
                    continue
        return fallback

    async def _pause(self, seconds: float) -> None:
        if seconds > 0:
            await self._sleeper(seconds)
