"""
console_errors.py — Console error capture and classification.

Visits each page discovered by the crawler and performs a focused analysis
of JavaScript console output. This is a separate pass from navigation
because we want a clean page load (no prior state) with a maximally
verbose console listener.

Severity classification:
  console.error()   → TestResult FAIL  (likely a real bug)
  console.warn()    → TestResult PASS  (warning, noted in metadata)
  console.log/info  → Ignored (too noisy)

Noise filtering:
  Many production sites generate "console errors" from:
  - Browser extensions injecting content
  - Third-party analytics scripts (Google Analytics, Hotjar, etc.)
  - favicon.ico 404s (not the app's fault)
  - React/Vue development mode warnings in prod (informational)
  - ResizeObserver loop exceeded (Chrome internal bug)

  The NOISE_PATTERNS list filters these out so the report only surfaces
  errors that are genuinely the application's responsibility.

  Design decision: The noise list is additive — if a pattern is in the
  list, it's always filtered. We don't try to detect "is this third-party?"
  dynamically because that requires cross-origin access we don't have.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Optional

from src.core.constants import EngineType, TestStatus
from src.core.logger import get_logger
from src.core.models import TestResult
from src.engines.ui.tests.page_load import (
    _capture_screenshot,
    _format_exc,
    _make_result,
    _url_label,
)

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Noise filter patterns
# ─────────────────────────────────────────────────────────────────────────────

_NOISE_PATTERNS: list[re.Pattern] = [
    # Browser / extension noise
    re.compile(r"chrome-extension://", re.I),
    re.compile(r"moz-extension://", re.I),
    re.compile(r"safari-extension://", re.I),

    # Favicon / static asset 404s (not app code)
    re.compile(r"favicon\.ico", re.I),
    re.compile(r"apple-touch-icon", re.I),

    # Known Chrome internal bugs
    re.compile(r"ResizeObserver loop limit exceeded", re.I),
    re.compile(r"ResizeObserver loop completed with undelivered notifications", re.I),

    # Third-party analytics / tracking scripts
    re.compile(r"(google-analytics|googletagmanager|hotjar|segment\.io"
               r"|mixpanel|amplitude|fullstory|heap\.io|intercom)", re.I),

    # Framework development-mode informational messages
    re.compile(r"Download the React DevTools", re.I),
    re.compile(r"You are running Vue in development mode", re.I),
    re.compile(r"\[HMR\]|\[WDS\]|\[webpack\]", re.I),         # Webpack dev server

    # Content Security Policy reports (informational, not bugs)
    re.compile(r"Content Security Policy", re.I),

    # Ad blocker interference
    re.compile(r"net::ERR_BLOCKED_BY_CLIENT", re.I),
    re.compile(r"ERR_BLOCKED_BY_ORB", re.I),

    # Stripe.js / payment SDK noise
    re.compile(r"stripe\.js", re.I),
]


def _is_noise(message_text: str, source_url: str = "") -> bool:
    """
    Return True if this console message should be filtered out.
    Checks both the message text and the source URL.
    """
    combined = f"{message_text} {source_url}"
    return any(pattern.search(combined) for pattern in _NOISE_PATTERNS)


# ─────────────────────────────────────────────────────────────────────────────
# Console message severity classification
# ─────────────────────────────────────────────────────────────────────────────

_SEVERITY_MAP = {
    "error":   "high",
    "warning": "medium",
    "warn":    "medium",
}


def _classify_console_message(msg_type: str) -> Optional[str]:
    """
    Return severity string ("high" / "medium") or None if this type
    should be ignored (info, log, debug, etc.).
    """
    return _SEVERITY_MAP.get(msg_type.lower())


# ─────────────────────────────────────────────────────────────────────────────
# Main test function
# ─────────────────────────────────────────────────────────────────────────────

async def run_console_error_tests(
    browser: object,
    session_id: str,
    urls: list[str],
    config: object,
    screenshot_dir: Path,
    storage_state: Optional[dict] = None,
):   # -> AsyncIterator[TestResult]
    """
    For each URL in `urls`, load the page and analyse its console output.
    Yields one TestResult per URL.

    TestResult status:
      PASS  → No filtered console errors
      FAIL  → One or more console.error() calls (after noise filtering)
      SKIP  → URL list was empty
      ERROR → Unexpected exception during testing

    Args:
        storage_state: Optional Playwright storage state from Phase 0 login.
                       When provided, each browser context is initialised
                       with it so pages are loaded as the authenticated user.
    """
    if not urls:
        log.debug("console_error_tests_skipped", reason="no_urls")
        return   # Empty generator

    log.info("console_error_tests_starting", url_count=len(urls))

    for url in urls:
        result = await _test_console_errors(
            browser=browser,
            session_id=session_id,
            url=url,
            config=config,
            screenshot_dir=screenshot_dir,
            storage_state=storage_state,
        )
        yield result

        # Rate limit: small delay between page loads
        await asyncio.sleep(config.rate_limit.request_delay_ms / 1000)


async def _test_console_errors(
    browser: object,
    session_id: str,
    url: str,
    config: object,
    screenshot_dir: Path,
    storage_state: Optional[dict] = None,
) -> TestResult:
    """
    Load a single page and collect + classify its console output.
    """
    raw_messages: list[dict] = []
    screenshot_path: Optional[str] = None

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

    # ── Console listener with source URL capture ──────────────────────────────
    def _on_console(msg: object) -> None:  # type: ignore[type-arg]
        location = {}
        if hasattr(msg, "location"):
            location = msg.location  # type: ignore[attr-defined]
        raw_messages.append({
            "type": msg.type,   # type: ignore[attr-defined]
            "text": msg.text,   # type: ignore[attr-defined]
            "url":  location.get("url", ""),
            "line": location.get("lineNumber", 0),
        })

    # ── Page error listener (uncaught JS exceptions) ──────────────────────────
    page_errors: list[str] = []

    def _on_page_error(err: object) -> None:  # type: ignore[type-arg]
        page_errors.append(str(err))

    page.on("console", _on_console)       # type: ignore[attr-defined]
    page.on("pageerror", _on_page_error)  # type: ignore[attr-defined]

    start = time.perf_counter()
    test_label = f"Console Errors: {_url_label(url)}"

    try:
        await asyncio.wait_for(
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=config.timeouts.page_load_ms,
            ),
            timeout=config.timeouts.page_load_ms / 1000 + 5,
        )

        # Brief wait for deferred scripts to fire (XHR callbacks, lazy imports)
        await asyncio.sleep(0.5)

        elapsed_ms = (time.perf_counter() - start) * 1000

        # ── Filter and classify console messages ──────────────────────────────
        filtered_errors: list[dict] = []
        filtered_warnings: list[dict] = []

        for msg in raw_messages:
            msg_text = msg.get("text", "")
            msg_src = msg.get("url", "")

            if _is_noise(msg_text, msg_src):
                continue

            severity = _classify_console_message(msg.get("type", ""))
            if severity == "high":
                filtered_errors.append({**msg, "severity": "high"})
            elif severity == "medium":
                filtered_warnings.append({**msg, "severity": "medium"})

        # Uncaught JS exceptions are always high severity
        for err_text in page_errors:
            if not _is_noise(err_text):
                filtered_errors.append({
                    "type": "pageerror",
                    "text": err_text,
                    "severity": "high",
                    "url": url,
                })

        all_notable = filtered_errors + filtered_warnings
        has_errors = len(filtered_errors) > 0

        # ── Build result ──────────────────────────────────────────────────────
        if has_errors:
            screenshot_path = await _capture_screenshot(
                page,
                screenshot_dir,
                f"console_errors_{_slug(url)}",
            )
            error_summary = _build_error_summary(filtered_errors)
            log.warning(
                "console_errors_found",
                url=url,
                error_count=len(filtered_errors),
                warning_count=len(filtered_warnings),
                first_error=filtered_errors[0].get("text", "")[:100],
            )
            return _make_result(
                session_id=session_id,
                test_name=test_label,
                test_url=url,
                status=TestStatus.FAIL,
                duration_ms=elapsed_ms,
                error_message=error_summary,
                screenshot_path=screenshot_path,
                console_logs=all_notable,
                metadata={
                    "error_count": len(filtered_errors),
                    "warning_count": len(filtered_warnings),
                    "page_error_count": len(page_errors),
                    "total_raw_messages": len(raw_messages),
                    "noise_filtered": len(raw_messages) - len(all_notable),
                },
            )

        log.info(
            "console_errors_clean",
            url=url,
            warnings=len(filtered_warnings),
            noise_filtered=len(raw_messages) - len(all_notable),
            duration_ms=f"{elapsed_ms:.0f}",
        )
        return _make_result(
            session_id=session_id,
            test_name=test_label,
            test_url=url,
            status=TestStatus.PASS,
            duration_ms=elapsed_ms,
            console_logs=filtered_warnings,   # Include warnings in PASS results
            metadata={
                "error_count": 0,
                "warning_count": len(filtered_warnings),
                "total_raw_messages": len(raw_messages),
                "noise_filtered": len(raw_messages) - len(all_notable),
            },
        )

    except asyncio.TimeoutError:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log.warning("console_test_timeout", url=url)
        return _make_result(
            session_id=session_id,
            test_name=test_label,
            test_url=url,
            status=TestStatus.TIMEOUT,
            duration_ms=elapsed_ms,
            error_message=f"Page load timeout during console error test",
        )

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log.error("console_test_error", url=url, error=str(exc))
        return _make_result(
            session_id=session_id,
            test_name=test_label,
            test_url=url,
            status=TestStatus.ERROR,
            duration_ms=elapsed_ms,
            error_message=str(exc),
            stack_trace=_format_exc(),
        )

    finally:
        try:
            await context.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_error_summary(errors: list[dict]) -> str:
    """
    Build a concise error summary string from a list of filtered errors.
    Used as the TestResult.error_message.
    """
    if not errors:
        return "No errors"

    count = len(errors)
    first = errors[0].get("text", "Unknown error")[:120]

    if count == 1:
        return f"1 console error: {first}"
    return f"{count} console errors. First: {first}"


def _slug(url: str) -> str:
    import re as _re
    from urllib.parse import urlparse as _parse
    path = _parse(url).path.strip("/").replace("/", "_") or "root"
    return _re.sub(r"[^a-zA-Z0-9_\-]", "", path)[:40]
