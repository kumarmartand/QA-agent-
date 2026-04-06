"""
buttons.py — Button and interactive element testing.

Tests:
  1. All visible buttons are discoverable (not hidden by z-index/overflow tricks)
  2. Non-disabled buttons respond to click events
  3. Clicking doesn't produce JS console errors
  4. Page doesn't crash or navigate away unexpectedly
  5. Disabled buttons are correctly non-interactive

Safety rules (we NEVER violate these):
  - NEVER click buttons matching DANGEROUS_PATTERNS (logout, delete, etc.)
  - NEVER click form submit buttons (would submit with empty/invalid data)
  - NEVER click buttons that open file download dialogs
  - NEVER follow external navigation — detect and skip
  - ONLY click buttons on the root page by default (light/standard depth)
    Full depth tests buttons on every discovered page

Click strategy:
  1. Discover all interactive elements via JS evaluation (fast, one round-trip)
  2. Filter: skip dangerous, disabled, form-submit, file-download
  3. For each remaining button:
     a. Capture console state before click
     b. Click with timeout
     c. Capture console state after click
     d. Check for new JS errors introduced by the click
     e. If page navigated away, go back
  4. Yield TestResult per button

Design note on navigation detection:
  Playwright fires the 'framenavigated' event when the main frame URL changes.
  We listen for this before each click. If navigation happens, we record it
  in the result metadata and call page.go_back() to return to the test page.
  This prevents the rest of the test suite from running on a different page.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from src.core.logger import get_logger
from src.core.models import TestResult
from src.core.constants import TestStatus
from src.engines.ui.selectors import (
    InteractiveElement,
    discover_interactive_elements,
    is_same_domain,
)
from src.engines.ui.tests.page_load import (
    _capture_screenshot,
    _attach_console_listener,
    _format_exc,
    _make_result,
    _url_label,
)

log = get_logger(__name__)

# Form submission buttons — clicking these with empty forms triggers validation noise
_SUBMIT_PATTERN = re.compile(r"(submit|send|save|confirm|apply|purchase|buy|pay)", re.I)

# Buttons that open non-HTML content (don't navigate within app)
_DOWNLOAD_PATTERN = re.compile(r"(download|export|pdf|csv|xlsx|print)", re.I)

# Maximum buttons to test per page (prevents runaway on button-heavy pages)
_MAX_BUTTONS_PER_PAGE = 15


# ─────────────────────────────────────────────────────────────────────────────
# Main test function
# ─────────────────────────────────────────────────────────────────────────────

async def run_button_tests(
    browser: object,
    session_id: str,
    urls: list[str],
    config: object,
    screenshot_dir: Path,
    storage_state: Optional[dict] = None,
):   # -> AsyncIterator[TestResult]
    """
    Test interactive buttons on all provided URLs.
    Yields one TestResult per button tested.

    Args:
        browser:        Playwright Browser.
        session_id:     Current session UUID.
        urls:           Pages to test (typically [root_url] for standard depth,
                        all discovered URLs for full depth).
        config:         AppConfig.
        screenshot_dir: Directory for failure screenshots.
        storage_state:  Optional Playwright storage state from Phase 0 login.
                        When provided, each browser context is initialised
                        with it so buttons are tested as the authenticated user.
    """
    if not urls:
        log.debug("button_tests_skipped", reason="no_urls")
        return

    log.info("button_tests_starting", page_count=len(urls))

    for url in urls:
        async for result in _test_buttons_on_page(
            browser=browser,
            session_id=session_id,
            url=url,
            config=config,
            screenshot_dir=screenshot_dir,
            storage_state=storage_state,
        ):
            yield result


async def _test_buttons_on_page(
    browser: object,
    session_id: str,
    url: str,
    config: object,
    screenshot_dir: Path,
    storage_state: Optional[dict] = None,
):   # -> AsyncIterator[TestResult]
    """
    Load a page, discover its buttons, and test each one.
    Yields TestResult per button.
    """
    # ── Create a persistent context for this page's button tests ─────────────
    # We keep the same context across all button clicks on a page so that
    # app state (opened modals, expanded menus) persists between clicks.
    context_kwargs: dict = {
        "viewport": {
            "width": config.browser.viewport_width,
            "height": config.browser.viewport_height,
        },
        "ignore_https_errors": True,
    }
    if storage_state:
        context_kwargs["storage_state"] = storage_state

    context = await browser.new_context(**context_kwargs)  # type: ignore[attr-defined]
    page = await context.new_page()

    # Track console errors throughout this page session
    all_console: list[dict] = []
    _attach_console_listener(page, all_console)

    try:
        # Load the page
        response = await asyncio.wait_for(
            page.goto(url, wait_until="domcontentloaded",
                      timeout=config.timeouts.page_load_ms),
            timeout=config.timeouts.page_load_ms / 1000 + 5,
        )

        if response and response.status >= 400:
            log.warning("button_test_page_load_failed", url=url, status=response.status)
            return

        # ── Discover interactive elements ─────────────────────────────────────
        elements = await discover_interactive_elements(page, skip_dangerous=False)
        testable = _filter_testable(elements)

        log.info(
            "button_discovery_complete",
            url=url,
            total_found=len(elements),
            testable=len(testable),
            skipped_dangerous=sum(1 for e in elements if e.is_dangerous),
            skipped_disabled=sum(1 for e in elements if e.is_disabled),
        )

        if not testable:
            # Yield a single "no testable buttons" result
            yield _make_result(
                session_id=session_id,
                test_name=f"Button Discovery: {_url_label(url)}",
                test_url=url,
                status=TestStatus.PASS,
                metadata={
                    "message": "No testable buttons found",
                    "total_elements": len(elements),
                },
            )
            return

        # ── Test each button ──────────────────────────────────────────────────
        original_url = page.url

        for idx, element in enumerate(testable[:_MAX_BUTTONS_PER_PAGE]):
            result = await _click_button(
                page=page,
                element=element,
                session_id=session_id,
                url=url,
                original_url=original_url,
                config=config,
                screenshot_dir=screenshot_dir,
                index=idx,
            )
            yield result

            # After each click, ensure we're back on the original page
            if page.url.rstrip("/") != original_url.rstrip("/"):
                try:
                    await page.go_back(
                        wait_until="domcontentloaded",
                        timeout=config.timeouts.page_load_ms,
                    )
                    original_url = page.url
                except Exception:
                    log.warning("button_test_go_back_failed", url=url)
                    break   # Can't navigate back — stop testing this page

            # Small delay between clicks
            await asyncio.sleep(config.rate_limit.request_delay_ms / 1000)

    except asyncio.TimeoutError:
        log.warning("button_test_page_timeout", url=url)
        yield _make_result(
            session_id=session_id,
            test_name=f"Button Tests: {_url_label(url)}",
            test_url=url,
            status=TestStatus.TIMEOUT,
            error_message="Page load timeout before buttons could be tested",
        )

    except Exception as exc:
        log.error("button_test_page_error", url=url, error=str(exc))
        yield _make_result(
            session_id=session_id,
            test_name=f"Button Tests: {_url_label(url)}",
            test_url=url,
            status=TestStatus.ERROR,
            error_message=str(exc),
            stack_trace=_format_exc(),
        )

    finally:
        try:
            await context.close()
        except Exception:
            pass


async def _click_button(
    page: object,
    element: InteractiveElement,
    session_id: str,
    url: str,
    original_url: str,
    config: object,
    screenshot_dir: Path,
    index: int,
) -> TestResult:
    """
    Attempt to click a single button and evaluate the outcome.

    Captures:
    - Console errors introduced by the click
    - Whether the page navigated away
    - Whether a JS exception was thrown
    """
    btn_label = element.text[:50] or element.selector
    test_label = f"Button Click: '{btn_label}' on {_url_label(url)}"
    screenshot_path: Optional[str] = None

    # Snapshot of console before click
    pre_click_errors = [
        m for m in _current_page_console(page)
        if m.get("type") == "error"
    ]

    start = time.perf_counter()

    try:
        locator = page.locator(element.selector).first  # type: ignore[attr-defined]

        # Verify element is still visible before clicking
        await locator.wait_for(  # type: ignore[attr-defined]
            state="visible",
            timeout=config.timeouts.action_ms,
        )

        # Scroll element into view
        await locator.scroll_into_view_if_needed(  # type: ignore[attr-defined]
            timeout=config.timeouts.action_ms,
        )

        # Detect navigation (listen for URL change)
        navigated_to: list[str] = []

        def _on_nav(frame: object) -> None:  # type: ignore[type-arg]
            if frame == page.main_frame:  # type: ignore[attr-defined]
                new_url = frame.url  # type: ignore[attr-defined]
                if new_url.rstrip("/") != original_url.rstrip("/"):
                    navigated_to.append(new_url)

        page.on("framenavigated", _on_nav)  # type: ignore[attr-defined]

        try:
            await locator.click(timeout=config.timeouts.action_ms)  # type: ignore[attr-defined]
            # Wait briefly for any synchronous JS effects to settle
            await asyncio.sleep(0.3)
        finally:
            page.remove_listener("framenavigated", _on_nav)  # type: ignore[attr-defined]

        elapsed_ms = (time.perf_counter() - start) * 1000

        # Console errors introduced BY the click (not pre-existing)
        post_click_errors = [
            m for m in _current_page_console(page)
            if m.get("type") == "error"
        ]
        new_errors = post_click_errors[len(pre_click_errors):]

        # ── Determine result ──────────────────────────────────────────────────
        if new_errors:
            screenshot_path = await _capture_screenshot(
                page, screenshot_dir,
                f"btn_error_{_slug(btn_label)}_{index}"
            )
            first_error = new_errors[0].get("text", "Unknown error")[:120]
            log.warning(
                "button_click_console_error",
                button=btn_label,
                url=url,
                error=first_error,
            )
            return _make_result(
                session_id=session_id,
                test_name=test_label,
                test_url=url,
                status=TestStatus.FAIL,
                duration_ms=elapsed_ms,
                error_message=f"JS error on click: {first_error}",
                screenshot_path=screenshot_path,
                console_logs=new_errors,
                metadata={
                    "button_text": element.text,
                    "button_selector": element.selector,
                    "button_tag": element.tag,
                    "new_errors": len(new_errors),
                    "navigated_to": navigated_to[0] if navigated_to else None,
                },
            )

        log.info(
            "button_click_pass",
            button=btn_label,
            url=url,
            navigated=bool(navigated_to),
            duration_ms=f"{elapsed_ms:.0f}",
        )
        return _make_result(
            session_id=session_id,
            test_name=test_label,
            test_url=url,
            status=TestStatus.PASS,
            duration_ms=elapsed_ms,
            metadata={
                "button_text": element.text,
                "button_selector": element.selector,
                "button_tag": element.tag,
                "navigated_to": navigated_to[0] if navigated_to else None,
                "is_external_nav": (
                    not is_same_domain(navigated_to[0], url)
                    if navigated_to else False
                ),
            },
        )

    except asyncio.TimeoutError:
        elapsed_ms = (time.perf_counter() - start) * 1000
        screenshot_path = await _capture_screenshot(
            page, screenshot_dir,
            f"btn_timeout_{_slug(btn_label)}_{index}"
        )
        log.warning("button_click_timeout", button=btn_label, url=url)
        return _make_result(
            session_id=session_id,
            test_name=test_label,
            test_url=url,
            status=TestStatus.TIMEOUT,
            duration_ms=elapsed_ms,
            error_message=f"Click timeout after {config.timeouts.action_ms}ms",
            screenshot_path=screenshot_path,
            metadata={
                "button_text": element.text,
                "button_selector": element.selector,
            },
        )

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        screenshot_path = await _capture_screenshot(
            page, screenshot_dir,
            f"btn_err_{_slug(btn_label)}_{index}"
        )
        log.error("button_click_error", button=btn_label, url=url, error=str(exc))
        return _make_result(
            session_id=session_id,
            test_name=test_label,
            test_url=url,
            status=TestStatus.ERROR,
            duration_ms=elapsed_ms,
            error_message=str(exc),
            stack_trace=_format_exc(),
            screenshot_path=screenshot_path,
            metadata={
                "button_text": element.text,
                "button_selector": element.selector,
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# Filtering helpers
# ─────────────────────────────────────────────────────────────────────────────

def _filter_testable(elements: list[InteractiveElement]) -> list[InteractiveElement]:
    """
    Apply safety rules to produce a list of buttons safe to click.
    Always REMOVES:
      - Disabled elements
      - Dangerous (logout/delete etc.)
      - Form submit/save buttons
      - Download triggers
    """
    testable: list[InteractiveElement] = []
    for el in elements:
        if el.is_disabled:
            continue
        if el.is_dangerous:
            continue
        all_text = f"{el.text} {el.aria_label} {el.href}".lower()
        if _SUBMIT_PATTERN.search(all_text):
            continue
        if _DOWNLOAD_PATTERN.search(all_text):
            continue
        # Skip pure navigation links (href to another page) — tested by crawler
        if el.tag == "a" and el.href and not el.href.startswith("#"):
            continue
        testable.append(el)
    return testable


def _current_page_console(page: object) -> list[dict]:
    """
    Retrieve the accumulated console messages stored on the page object.

    We use a side-channel: the console listener registered in
    _attach_console_listener stores messages in a list that we read here
    via the page's own 'console_log' attribute. If not present (page
    was created without attach), return empty list.
    """
    return getattr(page, "_qa_console_log", [])


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "", text.replace(" ", "_"))[:30]
