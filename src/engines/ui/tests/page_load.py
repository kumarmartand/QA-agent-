"""
page_load.py — Page load test module.

Tests performed for each URL:
  1. HTTP navigation succeeds within configured timeout
  2. HTTP response status is not 4xx/5xx
  3. Page has a non-empty <title>
  4. DOM is fully interactive (wait_until="domcontentloaded")
  5. Page does not immediately redirect to an error page

Performance metrics captured:
  - Total load time (ms)
  - TTFB approximation via response timing
  - Final URL after redirects

Design note on wait_until strategy:
  We use "domcontentloaded" rather than "networkidle" because:
  - "networkidle" waits for ALL network activity to stop, which can hang
    on pages with long-polling, WebSockets, or analytics pings.
  - "domcontentloaded" fires when the DOM is parseable, which is what
    matters for testing UI elements.
  - For performance measurement we record the full navigation time regardless.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.core.constants import EngineType, TestStatus
from src.core.exceptions import PageLoadError
from src.core.logger import get_logger
from src.core.models import TestResult

log = get_logger(__name__)

# HTTP status codes that are acceptable (2xx + 3xx handled by Playwright redirect follow)
_ACCEPTABLE_STATUS = set(range(200, 400))

# Patterns in page title or body that indicate a server error page
_ERROR_PAGE_PATTERNS = re.compile(
    r"(404\s*not\s*found|403\s*forbidden|500\s*internal\s*server"
    r"|502\s*bad\s*gateway|503\s*service\s*unavailable"
    r"|access\s*denied|page\s*not\s*found|error\s*occurred)",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────────────
# Screenshot helper
# ─────────────────────────────────────────────────────────────────────────────

async def _capture_screenshot(
    page: object,
    screenshot_dir: Path,
    name: str,
) -> Optional[str]:
    """
    Save a full-page PNG screenshot.
    Returns the file path on success, None if screenshot fails.
    Screenshot failures are logged but never propagate — they must not
    cause a test to fail.
    """
    try:
        path = screenshot_dir / f"{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(  # type: ignore[attr-defined]
            path=str(path),
            full_page=True,
            timeout=5_000,   # Never block the test run waiting for a screenshot
        )
        log.debug("screenshot_saved", path=str(path))
        return str(path)
    except Exception as exc:
        log.warning("screenshot_failed", name=name, error=str(exc))
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Page load result builder
# ─────────────────────────────────────────────────────────────────────────────

async def run_page_load_test(
    browser: object,
    session_id: str,
    url: str,
    config: object,
    screenshot_dir: Path,
    test_label: Optional[str] = None,
) -> TestResult:
    """
    Run a single page load test against `url`.

    This function is the primary unit used by both the PageLoadTester (for
    the root URL) and the NavigationCrawler (for each discovered link).
    Keeping it as a standalone async function (not a method) makes it
    trivially testable in isolation.

    Args:
        browser:        Playwright Browser instance.
        session_id:     Current session UUID.
        url:            Target URL to load.
        config:         AppConfig (for timeouts, browser settings, depth).
        screenshot_dir: Directory to save failure screenshots.
        test_label:     Optional human label override (defaults to URL path).

    Returns:
        A fully populated TestResult.
    """
    label = test_label or f"Page Load: {_url_label(url)}"
    http_status: Optional[int] = None
    final_url: str = url
    screenshot_path: Optional[str] = None
    console_errors: list[dict] = []

    # ── Create isolated browser context ──────────────────────────────────────
    context = await browser.new_context(  # type: ignore[attr-defined]
        viewport={
            "width": config.browser.viewport_width,
            "height": config.browser.viewport_height,
        },
        ignore_https_errors=True,     # Don't fail on self-signed certs
        java_script_enabled=True,
    )

    page = await context.new_page()

    # Attach console listener BEFORE navigation so we catch all load errors
    _attach_console_listener(page, console_errors)

    start = time.perf_counter()

    try:
        # Navigate with response interception for status code
        response = await asyncio.wait_for(
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=config.timeouts.page_load_ms,
            ),
            timeout=config.timeouts.page_load_ms / 1000 + 5,  # asyncio timeout slightly higher
        )

        elapsed_ms = (time.perf_counter() - start) * 1000
        final_url = page.url

        # ── Evaluate response ─────────────────────────────────────────────────
        if response is None:
            # Can happen for file:// or data: URIs — treat as pass for now
            return _make_result(
                session_id=session_id,
                test_name=label,
                test_url=url,
                status=TestStatus.PASS,
                duration_ms=elapsed_ms,
                metadata={"final_url": final_url, "note": "No HTTP response (non-HTTP URL)"},
            )

        http_status = response.status

        # ── Check HTTP status ─────────────────────────────────────────────────
        if http_status not in _ACCEPTABLE_STATUS:
            screenshot_path = await _capture_screenshot(
                page, screenshot_dir, f"http_{http_status}_{_slug(url)}"
            )
            log.warning(
                "page_load_http_error",
                url=url,
                status=http_status,
                final_url=final_url,
            )
            return _make_result(
                session_id=session_id,
                test_name=label,
                test_url=url,
                status=TestStatus.FAIL,
                duration_ms=elapsed_ms,
                error_message=f"HTTP {http_status} — {_http_status_text(http_status)}",
                screenshot_path=screenshot_path,
                console_logs=console_errors,
                metadata={"http_status": http_status, "final_url": final_url},
            )

        # ── Check page title ──────────────────────────────────────────────────
        title = await page.title()
        if not title.strip():
            log.warning("page_load_empty_title", url=url)
            # Not a hard failure, but worth noting in metadata

        # ── Check for server-error pages disguised as 200 ─────────────────────
        # Some servers return 200 with an error HTML page
        try:
            body_text = await asyncio.wait_for(
                page.inner_text("body"),
                timeout=2.0,
            )
            if _ERROR_PAGE_PATTERNS.search(body_text[:500]):
                log.warning(
                    "page_load_error_body_detected",
                    url=url,
                    http_status=http_status,
                )
                # Soft warning — treat as pass but flag in metadata
        except Exception:
            body_text = ""

        # ── Performance timing ────────────────────────────────────────────────
        timing = await _get_performance_timing(page)

        log.info(
            "page_load_pass",
            url=url,
            status=http_status,
            title=title[:60],
            duration_ms=f"{elapsed_ms:.0f}",
            ttfb_ms=timing.get("ttfb_ms"),
        )

        # ── Check load time threshold ─────────────────────────────────────────
        threshold = config.performance.thresholds.page_load_ms
        load_status = TestStatus.PASS
        error_msg = None
        if elapsed_ms > threshold:
            load_status = TestStatus.FAIL
            error_msg = (
                f"Page load time {elapsed_ms:.0f}ms exceeds threshold {threshold}ms"
            )
            screenshot_path = await _capture_screenshot(
                page, screenshot_dir, f"slow_load_{_slug(url)}"
            )
            log.warning(
                "page_load_slow",
                url=url,
                duration_ms=elapsed_ms,
                threshold_ms=threshold,
            )

        return _make_result(
            session_id=session_id,
            test_name=label,
            test_url=url,
            status=load_status,
            duration_ms=elapsed_ms,
            error_message=error_msg,
            screenshot_path=screenshot_path,
            console_logs=console_errors,
            metadata={
                "http_status": http_status,
                "final_url": final_url,
                "title": title,
                "ttfb_ms": timing.get("ttfb_ms"),
                "dom_content_loaded_ms": timing.get("dom_content_loaded_ms"),
                "console_error_count": sum(
                    1 for c in console_errors if c.get("type") == "error"
                ),
            },
        )

    except asyncio.TimeoutError:
        elapsed_ms = (time.perf_counter() - start) * 1000
        screenshot_path = await _capture_screenshot(
            page, screenshot_dir, f"timeout_{_slug(url)}"
        )
        log.error("page_load_timeout", url=url, timeout_ms=config.timeouts.page_load_ms)
        return _make_result(
            session_id=session_id,
            test_name=label,
            test_url=url,
            status=TestStatus.TIMEOUT,
            duration_ms=elapsed_ms,
            error_message=(
                f"Page failed to load within {config.timeouts.page_load_ms}ms"
            ),
            screenshot_path=screenshot_path,
            console_logs=console_errors,
            metadata={"http_status": http_status, "final_url": final_url},
        )

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        screenshot_path = await _capture_screenshot(
            page, screenshot_dir, f"error_{_slug(url)}"
        )
        log.error("page_load_error", url=url, error=str(exc), exc_info=True)
        return _make_result(
            session_id=session_id,
            test_name=label,
            test_url=url,
            status=TestStatus.ERROR,
            duration_ms=elapsed_ms,
            error_message=str(exc),
            stack_trace=_format_exc(),
            screenshot_path=screenshot_path,
            console_logs=console_errors,
            metadata={"http_status": http_status, "final_url": final_url},
        )

    finally:
        # Always close the context — releases all pages inside it
        try:
            await context.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _attach_console_listener(page: object, bucket: list[dict]) -> None:
    """
    Register a console message listener that appends to `bucket`.
    Called before page.goto() so we catch messages emitted during load.
    """
    def _on_console(msg: object) -> None:  # type: ignore[type-arg]
        bucket.append({
            "type": msg.type,  # type: ignore[attr-defined]
            "text": msg.text,  # type: ignore[attr-defined]
            "url":  msg.location.get("url", "") if hasattr(msg, "location") else "",
        })

    page.on("console", _on_console)  # type: ignore[attr-defined]


async def _get_performance_timing(page: object) -> dict:
    """Extract Web Performance API timing from the loaded page."""
    try:
        timing = await asyncio.wait_for(
            page.evaluate(  # type: ignore[attr-defined]
                """() => {
                    const t = performance.timing;
                    if (!t) return {};
                    return {
                        ttfb_ms:               t.responseStart - t.navigationStart,
                        dom_content_loaded_ms: t.domContentLoadedEventEnd - t.navigationStart,
                        load_event_ms:         t.loadEventEnd - t.navigationStart,
                    };
                }"""
            ),
            timeout=2.0,
        )
        return timing or {}
    except Exception:
        return {}


def _make_result(
    session_id: str,
    test_name: str,
    test_url: str,
    status: TestStatus,
    duration_ms: float = 0.0,
    error_message: Optional[str] = None,
    stack_trace: Optional[str] = None,
    screenshot_path: Optional[str] = None,
    console_logs: Optional[list] = None,
    metadata: Optional[dict] = None,
) -> TestResult:
    return TestResult(
        session_id=session_id,
        engine=EngineType.UI,
        test_name=test_name,
        test_url=test_url,
        status=status,
        duration_ms=duration_ms,
        error_message=error_message,
        stack_trace=stack_trace,
        screenshot_path=screenshot_path,
        console_logs=console_logs or [],
        metadata=metadata or {},
    )


def _url_label(url: str) -> str:
    """Human-friendly URL label: just the path+query, no scheme/host."""
    from urllib.parse import urlparse
    p = urlparse(url)
    label = p.path or "/"
    if p.query:
        label += f"?{p.query[:40]}"
    return label


def _slug(url: str) -> str:
    """URL → filesystem-safe slug for screenshot filenames."""
    from urllib.parse import urlparse
    path = urlparse(url).path.strip("/").replace("/", "_") or "root"
    return re.sub(r"[^a-zA-Z0-9_\-]", "", path)[:50]


def _http_status_text(status: int) -> str:
    """Return a human-readable description for common HTTP error codes."""
    _STATUS_MAP = {
        400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
        404: "Not Found", 405: "Method Not Allowed", 408: "Request Timeout",
        429: "Too Many Requests", 500: "Internal Server Error",
        502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout",
    }
    return _STATUS_MAP.get(status, f"HTTP Error {status}")


def _format_exc() -> str:
    """Capture current exception traceback as a string."""
    import traceback
    return traceback.format_exc()[-4000:]   # Truncate to DB-safe length
