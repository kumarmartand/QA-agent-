"""
engine.py — UIEngine: Playwright-based UI test engine.

This is the top-level orchestrator for all UI tests. It:
  1. Manages the Playwright browser lifecycle (one browser per session)
  2. Sequences all test modules: page load → navigation → console errors → buttons
  3. Streams TestResult objects as an AsyncIterator (yields immediately)
  4. Respects test_depth to skip expensive tests in light mode
  5. Handles browser crashes gracefully (setup/teardown via context manager)

Module execution order and rationale:
  ┌─────────────────────────────────────────────────────────────────┐
  │ 1. Page Load (root URL)                                         │
  │    ─ Always runs. If this fails, remaining steps are skipped.   │
  │    ─ Establishes baseline: "can we even reach this app?"        │
  ├─────────────────────────────────────────────────────────────────┤
  │ 2. Navigation Crawler (BFS)                                     │
  │    ─ Discovers all internal pages up to max_depth.              │
  │    ─ Tests each page loads (HTTP status + title).               │
  │    ─ Populates self._discovered_urls for steps 3 & 4.           │
  ├─────────────────────────────────────────────────────────────────┤
  │ 3. Console Error Analysis                                       │
  │    ─ Re-visits each discovered page.                            │
  │    ─ Dedicated pass captures JS errors from deferred scripts.   │
  │    ─ Skipped in "light" test depth.                             │
  ├─────────────────────────────────────────────────────────────────┤
  │ 4. Button Tests                                                 │
  │    ─ Tests interactive elements on discovered pages.            │
  │    ─ Only runs in "standard" or "full" test depth.              │
  │    ─ "full" tests all pages; others test root page only.        │
  └─────────────────────────────────────────────────────────────────┘

Browser context strategy:
  - One Browser instance (reused across all tests in the session)
  - Each test module creates its own BrowserContext for full isolation
  - Pages are closed after each test case
  - Browser is closed in teardown() — guaranteed even on failure
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Optional

from src.core.constants import EngineType, TestDepth, TestStatus
from src.core.exceptions import UIEngineError
from src.core.logger import bind_engine, get_logger
from src.core.models import TestResult
from src.engines.base import BaseEngine, EngineRegistry
from src.engines.ui.auth import LoginHandler
from src.engines.ui.tests.buttons import run_button_tests
from src.engines.ui.tests.console_errors import run_console_error_tests
from src.engines.ui.tests.navigation import NavigationCrawler
from src.engines.ui.tests.page_load import run_page_load_test

log = get_logger(__name__)


class UIEngine(BaseEngine):
    """
    Playwright-based UI test engine.

    Registered with EngineRegistry at module import time so the runner
    can instantiate it by type without importing this class directly.
    """

    engine_type = EngineType.UI

    def __init__(self, config: object) -> None:
        super().__init__(config)
        self._playwright: Optional[object] = None
        self._browser: Optional[object] = None
        self._screenshot_dir: Optional[Path] = None
        self._discovered_urls: list[str] = []
        self._auth_state: Optional[dict] = None  # Captured after Phase 0 login

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def setup(self) -> None:
        """
        Launch Playwright and the configured browser.

        Design note: We launch the browser once and share it across all test
        modules. Each module creates its own BrowserContext, providing
        isolation without the ~500ms overhead of full browser restarts.
        """
        if self._browser is not None:
            return   # Idempotent — already set up

        bind_engine(self.engine_type.value)

        try:
            # Import here so Playwright is optional at module level
            from playwright.async_api import async_playwright  # type: ignore[import]

            log.info(
                "browser_launching",
                browser_type=self.config.browser.browser_type,
                headless=self.config.browser.headless,
            )

            self._playwright = await async_playwright().start()

            # Select browser type from config
            browser_launcher = {
                "chromium": self._playwright.chromium,  # type: ignore[attr-defined]
                "firefox":  self._playwright.firefox,   # type: ignore[attr-defined]
                "webkit":   self._playwright.webkit,    # type: ignore[attr-defined]
            }.get(self.config.browser.browser_type, self._playwright.chromium)  # type: ignore[attr-defined]

            self._browser = await browser_launcher.launch(
                headless=self.config.browser.headless,
                slow_mo=self.config.browser.slow_mo_ms,
                args=[
                    "--no-sandbox",                      # Required in Docker/CI
                    "--disable-dev-shm-usage",           # Prevents OOM in Docker
                    "--disable-gpu",                     # Headless doesn't need GPU
                    "--disable-extensions",              # Prevents extension noise
                    "--disable-background-timer-throttling",
                ],
            )

            # Prepare screenshot directory
            self._screenshot_dir = (
                Path(self.config.output.dir)
                / "screenshots"
            )
            self._screenshot_dir.mkdir(parents=True, exist_ok=True)

            log.info(
                "browser_launched",
                browser=self.config.browser.browser_type,
                version=self._browser.version,  # type: ignore[attr-defined]
            )

        except Exception as exc:
            raise UIEngineError(
                f"Failed to launch {self.config.browser.browser_type}: {exc}",
                context={"browser": self.config.browser.browser_type},
            ) from exc

    async def teardown(self) -> None:
        """
        Close the browser and stop Playwright.
        Always called — even if execute() raised an exception.
        """
        if self._browser:
            try:
                await self._browser.close()  # type: ignore[attr-defined]
                log.info("browser_closed")
            except Exception as exc:
                log.warning("browser_close_error", error=str(exc))
            finally:
                self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()  # type: ignore[attr-defined]
            except Exception:
                pass
            finally:
                self._playwright = None

    # ── Core execute loop ─────────────────────────────────────────────────────

    async def execute(self, session: object) -> AsyncIterator[TestResult]:  # type: ignore[override]
        """
        Run all UI tests and yield results as they complete.

        The test_depth config controls which modules run:
          light    → page load + navigation only
          standard → + console errors + button tests (root page)
          full     → + button tests on ALL discovered pages
        """
        if self._browser is None:
            raise UIEngineError(
                "UIEngine.execute() called before setup(). "
                "Use `async with engine:` or call setup() first."
            )

        depth = getattr(self.config.test_depth, "value", str(self.config.test_depth))
        url = self.config.url

        log.info(
            "ui_engine_execute_start",
            url=url,
            depth=depth,
            headless=self.config.browser.headless,
        )

        # ── Phase 0: Browser authentication (optional) ────────────────────────
        if self.config.ui_auth.enabled:  # type: ignore[attr-defined]
            log.info(
                "phase_start",
                phase="authentication",
                login_url=self.config.ui_auth.login_url,  # type: ignore[attr-defined]
            )
            handler = LoginHandler()
            login_result = await handler.perform_login(
                browser=self._browser,
                config=self.config,
                session_id=session.id,  # type: ignore[attr-defined]
                screenshot_dir=self._screenshot_dir,
            )

            # Build a TestResult from the LoginResult so it flows through
            # the normal reporting pipeline
            auth_test = TestResult(
                session_id=session.id,  # type: ignore[attr-defined]
                engine=EngineType.UI,
                test_name="Authentication: Browser Login",
                test_url=url,
                status=TestStatus.PASS if login_result.success else TestStatus.FAIL,
                duration_ms=login_result.duration_ms,
                error_message=login_result.error_message,
                screenshot_path=login_result.screenshot_path,
                metadata={
                    "login_url": self.config.ui_auth.login_url,  # type: ignore[attr-defined]
                    "success_indicator": self.config.ui_auth.success_indicator,  # type: ignore[attr-defined]
                },
            )
            yield auth_test

            if not login_result.success:
                log.error(
                    "auth_failed_aborting_ui_tests",
                    login_url=self.config.ui_auth.login_url,  # type: ignore[attr-defined]
                    error=login_result.error_message,
                )
                return   # Abort — no point running tests without a session

            self._auth_state = login_result.storage_state
            log.info(
                "auth_complete_proceeding",
                cookies=len((self._auth_state or {}).get("cookies", [])),
            )

        # ── Phase 1: Root page load ───────────────────────────────────────────
        log.info("phase_start", phase="page_load", url=url)
        root_result = await run_page_load_test(
            browser=self._browser,
            session_id=session.id,  # type: ignore[attr-defined]
            url=url,
            config=self.config,
            screenshot_dir=self._screenshot_dir,
            test_label=f"Page Load: {url}",
            storage_state=self._auth_state,
        )
        yield root_result

        # If the root page itself is broken, there's nothing useful to crawl
        if root_result.status in (TestStatus.FAIL, TestStatus.ERROR, TestStatus.TIMEOUT):
            log.warning(
                "ui_engine_aborting_after_root_fail",
                url=url,
                status=root_result.status.value,
            )
            return

        # ── Phase 2: Navigation / BFS crawl ──────────────────────────────────
        log.info("phase_start", phase="navigation", url=url)
        crawler = NavigationCrawler(
            browser=self._browser,
            session_id=session.id,  # type: ignore[attr-defined]
            config=self.config,
            screenshot_dir=self._screenshot_dir,
            storage_state=self._auth_state,
        )

        async for result in crawler.crawl():
            yield result

        # Persist discovered URLs for downstream phases
        self._discovered_urls = crawler.discovered_urls
        log.info(
            "navigation_complete",
            discovered=len(self._discovered_urls),
        )

        # ── Phase 3: Console error analysis ──────────────────────────────────
        if depth not in ("light",):
            log.info(
                "phase_start",
                phase="console_errors",
                url_count=len(self._discovered_urls),
            )
            async for result in run_console_error_tests(
                browser=self._browser,
                session_id=session.id,  # type: ignore[attr-defined]
                urls=self._discovered_urls or [url],
                config=self.config,
                screenshot_dir=self._screenshot_dir,
                storage_state=self._auth_state,
            ):
                yield result

        # ── Phase 4: Button tests ─────────────────────────────────────────────
        if depth in ("standard", "full"):
            # standard → test only root page; full → test all discovered pages
            button_urls = (
                self._discovered_urls or [url]
                if depth == "full"
                else [url]
            )
            log.info(
                "phase_start",
                phase="button_tests",
                page_count=len(button_urls),
            )
            async for result in run_button_tests(
                browser=self._browser,
                session_id=session.id,  # type: ignore[attr-defined]
                urls=button_urls,
                config=self.config,
                screenshot_dir=self._screenshot_dir,
                storage_state=self._auth_state,
            ):
                yield result

        log.info("ui_engine_execute_complete", url=url, depth=depth)


# ─────────────────────────────────────────────────────────────────────────────
# Self-register with the engine registry
# ─────────────────────────────────────────────────────────────────────────────

EngineRegistry.register(EngineType.UI, UIEngine)
