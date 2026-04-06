"""
navigation.py — BFS navigation crawler.

Crawls the target site starting from the root URL, staying within the
same domain, and testing that every discovered page loads successfully.

BFS over DFS rationale:
  BFS tests shallower (more important) pages first. If a time limit is
  hit or the run is cancelled, we've tested the most critical pages.
  DFS would exhaust one branch before testing top-level pages.

Depth-based page limits:
  light    → max 5 pages,  depth 1
  standard → max 20 pages, depth 2
  full     → max 50 pages, depth 3

Same-domain enforcement:
  Every discovered URL is normalised and checked against the base URL's
  netloc. External links, CDN assets, and mailto/tel links are silently
  skipped. The ignore_patterns config list provides additional URL-level
  exclusions (e.g., "logout", "delete").

Console capture during crawl:
  Each page gets a console listener attached BEFORE navigation. This is
  the most efficient approach: instead of two page loads per URL (one to
  crawl, one to capture console), we do one load and capture both the
  navigation result AND console output simultaneously.

Link extraction:
  Uses a single JS evaluation to extract all <a href> values at once,
  which is ~10x faster than querying them individually via Playwright's
  Python API over the CDP protocol.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from src.core.constants import EngineType, TestStatus
from src.core.logger import get_logger
from src.core.models import TestResult
from src.engines.ui.selectors import is_same_domain, normalise_url
from src.engines.ui.tests.page_load import (
    _attach_console_listener,
    _capture_screenshot,
    _format_exc,
    _http_status_text,
    _make_result,
    _url_label,
)

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Depth / page limits per test depth
# ─────────────────────────────────────────────────────────────────────────────

_DEPTH_LIMITS = {
    "light":    (1, 5),    # (max_depth, max_pages)
    "standard": (2, 20),
    "full":     (3, 50),
}


# ─────────────────────────────────────────────────────────────────────────────
# NavigationCrawler
# ─────────────────────────────────────────────────────────────────────────────

class NavigationCrawler:
    """
    BFS crawler that discovers links and tests each page.

    Yields (TestResult, discovered_url_or_None) tuples so the engine can
    both stream results to the collector AND track discovered URLs for
    subsequent test modules (console errors, buttons).

    Usage (in UIEngine.execute):
        crawler = NavigationCrawler(browser, session_id, config, screenshot_dir)
        async for result in crawler.crawl():
            yield result
        discovered = crawler.discovered_urls   # used by later modules
    """

    def __init__(
        self,
        browser: object,
        session_id: str,
        config: object,
        screenshot_dir: Path,
        storage_state: Optional[dict] = None,
    ) -> None:
        self.browser = browser
        self.session_id = session_id
        self.config = config
        self.screenshot_dir = screenshot_dir
        self._storage_state = storage_state  # Injected into every new_context()

        depth_str = getattr(config.test_depth, "value", str(config.test_depth))
        self._max_depth, self._max_pages = _DEPTH_LIMITS.get(depth_str, (2, 20))
        self._base_url: str = config.url
        self._visited: set[str] = set()
        self._discovered_urls: list[str] = []   # Pages successfully crawled

    @property
    def discovered_urls(self) -> list[str]:
        """All URLs successfully navigated during the crawl."""
        return list(self._discovered_urls)

    async def crawl(self):   # -> AsyncIterator[TestResult]
        """
        Execute the BFS crawl and yield a TestResult for each page visited.

        The root URL is always tested first. Subsequent pages are drawn from
        the BFS queue in breadth order.
        """
        base_url = self._base_url

        # BFS queue items: (url, depth)
        queue: deque[tuple[str, int]] = deque()
        queue.append((base_url, 0))
        self._visited.add(base_url)
        pages_tested = 0

        log.info(
            "crawl_started",
            base_url=base_url,
            max_depth=self._max_depth,
            max_pages=self._max_pages,
        )

        while queue and pages_tested < self._max_pages:
            url, depth = queue.popleft()
            pages_tested += 1

            log.debug(
                "crawl_visiting",
                url=url,
                depth=depth,
                queue_size=len(queue),
                tested=pages_tested,
            )

            # Test this page and discover its links
            result, child_links = await self._test_page(url, depth)
            yield result

            if result.status == TestStatus.PASS:
                self._discovered_urls.append(url)

            # Enqueue child links if we haven't hit max depth
            if depth < self._max_depth and pages_tested < self._max_pages:
                for link in child_links:
                    normalised = normalise_url(link, base_url)
                    if (
                        normalised
                        and normalised not in self._visited
                        and is_same_domain(normalised, base_url)
                        and not self._is_ignored(normalised)
                    ):
                        self._visited.add(normalised)
                        queue.append((normalised, depth + 1))

        log.info(
            "crawl_completed",
            pages_tested=pages_tested,
            discovered=len(self._discovered_urls),
            queued_but_skipped=len(queue),
        )

    async def _test_page(
        self,
        url: str,
        depth: int,
    ) -> tuple[TestResult, list[str]]:
        """
        Navigate to a URL, run basic checks, capture console output,
        and extract all outbound links.

        Returns (TestResult, list_of_raw_href_values).
        """
        console_logs: list[dict] = []
        links: list[str] = []
        http_status: Optional[int] = None
        screenshot_path: Optional[str] = None

        context_kwargs: dict = {
            "viewport": {
                "width": self.config.browser.viewport_width,
                "height": self.config.browser.viewport_height,
            },
            "ignore_https_errors": True,
        }
        if self._storage_state:
            context_kwargs["storage_state"] = self._storage_state

        context = await self.browser.new_context(**context_kwargs)  # type: ignore[attr-defined]
        page = await context.new_page()

        # Console listener BEFORE navigation
        _attach_console_listener(page, console_logs)

        start = time.perf_counter()
        test_label = f"Navigation: {_url_label(url)}"

        try:
            response = await asyncio.wait_for(
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.config.timeouts.page_load_ms,
                ),
                timeout=self.config.timeouts.page_load_ms / 1000 + 5,
            )

            elapsed_ms = (time.perf_counter() - start) * 1000
            final_url = page.url
            http_status = response.status if response else None

            # ── Check HTTP status ─────────────────────────────────────────────
            if http_status and http_status >= 400:
                screenshot_path = await _capture_screenshot(
                    page,
                    self.screenshot_dir,
                    f"nav_http{http_status}_{self._slug(url)}",
                )
                result = _make_result(
                    session_id=self.session_id,
                    test_name=test_label,
                    test_url=url,
                    status=TestStatus.FAIL,
                    duration_ms=elapsed_ms,
                    error_message=f"HTTP {http_status} — {_http_status_text(http_status)}",
                    screenshot_path=screenshot_path,
                    console_logs=console_logs,
                    metadata={
                        "http_status": http_status,
                        "final_url": final_url,
                        "depth": depth,
                    },
                )
                return result, []

            # ── Extract links from DOM ────────────────────────────────────────
            links = await self._extract_links(page)

            # ── Check for redirect loops ──────────────────────────────────────
            if final_url.rstrip("/") != url.rstrip("/"):
                log.debug("redirect_detected", from_url=url, to_url=final_url)

            result = _make_result(
                session_id=self.session_id,
                test_name=test_label,
                test_url=url,
                status=TestStatus.PASS,
                duration_ms=elapsed_ms,
                console_logs=console_logs,
                metadata={
                    "http_status": http_status,
                    "final_url": final_url,
                    "links_found": len(links),
                    "depth": depth,
                    "console_errors": sum(
                        1 for c in console_logs if c.get("type") == "error"
                    ),
                },
            )
            log.info(
                "navigation_pass",
                url=url,
                status=http_status,
                links=len(links),
                depth=depth,
                duration_ms=f"{elapsed_ms:.0f}",
            )
            return result, links

        except asyncio.TimeoutError:
            elapsed_ms = (time.perf_counter() - start) * 1000
            screenshot_path = await _capture_screenshot(
                page, self.screenshot_dir, f"nav_timeout_{self._slug(url)}"
            )
            log.warning("navigation_timeout", url=url, depth=depth)
            result = _make_result(
                session_id=self.session_id,
                test_name=test_label,
                test_url=url,
                status=TestStatus.TIMEOUT,
                duration_ms=elapsed_ms,
                error_message=f"Navigation timeout after {self.config.timeouts.page_load_ms}ms",
                screenshot_path=screenshot_path,
                console_logs=console_logs,
                metadata={"depth": depth, "http_status": http_status},
            )
            return result, []

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            screenshot_path = await _capture_screenshot(
                page, self.screenshot_dir, f"nav_error_{self._slug(url)}"
            )
            log.error("navigation_error", url=url, depth=depth, error=str(exc))
            result = _make_result(
                session_id=self.session_id,
                test_name=test_label,
                test_url=url,
                status=TestStatus.ERROR,
                duration_ms=elapsed_ms,
                error_message=str(exc),
                stack_trace=_format_exc(),
                screenshot_path=screenshot_path,
                console_logs=console_logs,
                metadata={"depth": depth, "http_status": http_status},
            )
            return result, []

        finally:
            try:
                await context.close()
            except Exception:
                pass

    async def _extract_links(self, page: object) -> list[str]:
        """
        Extract all <a href> values from the current page in one JS call.
        Filters out empty hrefs and obvious non-page links (anchors, etc.).
        """
        try:
            hrefs: list[str] = await asyncio.wait_for(
                page.evaluate(  # type: ignore[attr-defined]
                    """() => Array.from(document.querySelectorAll('a[href]'))
                               .map(a => a.getAttribute('href'))
                               .filter(h => h && h.length > 0)"""
                ),
                timeout=3.0,
            )
            return hrefs or []
        except Exception as exc:
            log.warning("link_extraction_failed", error=str(exc))
            return []

    def _is_ignored(self, url: str) -> bool:
        """Check if url matches any configured ignore patterns."""
        ignore_patterns = getattr(self.config.api, "ignore_patterns", [])
        url_lower = url.lower()
        return any(pat.lower() in url_lower for pat in ignore_patterns)

    @staticmethod
    def _slug(url: str) -> str:
        import re
        path = urlparse(url).path.strip("/").replace("/", "_") or "root"
        return re.sub(r"[^a-zA-Z0-9_\-]", "", path)[:40]
