"""
auth.py — Browser-level login handler for UIEngine.

Implements Phase 0 of the UI test pipeline: authenticate the browser session
before any functional tests run. On success, captures Playwright's storage
state (cookies + localStorage) which is then injected into every subsequent
BrowserContext so all test modules run as the authenticated user.

Flow:
  1. Open a fresh BrowserContext (no prior cookies)
  2. Navigate to login_url (resolved against target base URL)
  3. Fill username + password fields using configurable CSS selectors
  4. Click the submit button
  5. Wait for network idle (or just DOM load if network_idle=False)
  6. Verify login succeeded via success_indicator:
       - If success_indicator looks like a CSS selector (starts with #, ., [,
         or a known tag) → assert the element is present in the DOM
       - Otherwise → assert the text appears somewhere on the page
  7. Call context.storage_state() to capture all cookies + localStorage
  8. Return a LoginResult (success=True, storage_state=<dict>)

Failure modes are handled gracefully:
  - Missing selector    → LoginFailedError with clear message
  - Wrong credentials   → LoginFailedError (success_indicator absent)
  - Timeout             → LoginFailedError with "timeout" hint
  - Network error       → LoginFailedError

Security note:
  Credentials are NEVER logged. Log statements may reference selector names
  and URLs but will never contain username/password values.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

from src.core.exceptions import LoginFailedError
from src.core.logger import get_logger

log = get_logger(__name__)

# CSS selector prefixes that distinguish selectors from plain text strings
_SELECTOR_PREFIXES = ("#", ".", "[", "input", "button", "a", "div", "span",
                      "form", "nav", "header", "main", "section", "article")


def _is_css_selector(indicator: str) -> bool:
    """
    Heuristic: if the success_indicator starts with a known CSS prefix treat it
    as a DOM selector; otherwise treat it as visible page text.
    """
    stripped = indicator.strip()
    return any(stripped.startswith(prefix) for prefix in _SELECTOR_PREFIXES)


def _resolve_login_url(base_url: str, login_path: str) -> str:
    """
    Combine base URL with login path.

    Examples:
      base="https://example.com", path="/login"   → "https://example.com/login"
      base="https://example.com", path="login"    → "https://example.com/login"
      base="https://example.com", path="https://…" → unchanged
    """
    if login_path.startswith(("http://", "https://")):
        return login_path
    # urljoin handles leading-slash vs relative paths correctly
    base = base_url.rstrip("/") + "/"
    path = login_path.lstrip("/")
    return urljoin(base, path)


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LoginResult:
    """
    Outcome of a login attempt.

    Attributes:
        success:        True if login completed and success_indicator was found.
        storage_state:  Playwright storage state dict (cookies + localStorage).
                        None if login failed.
        duration_ms:    Wall-clock time for the full login flow.
        error_message:  Human-readable failure reason; None on success.
        screenshot_path: Path to failure screenshot; None on success or if
                         screenshot capture itself failed.
    """
    success: bool
    storage_state: Optional[dict]
    duration_ms: float
    error_message: Optional[str] = None
    screenshot_path: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Login handler
# ─────────────────────────────────────────────────────────────────────────────

class LoginHandler:
    """
    Performs a browser-based login using Playwright and returns a LoginResult.

    Usage:
        handler = LoginHandler()
        result = await handler.perform_login(
            browser=browser,
            config=config,
            session_id=session.id,
            screenshot_dir=screenshot_dir,
        )
    """

    async def perform_login(
        self,
        browser: object,
        config: object,          # AppConfig
        session_id: str,
        screenshot_dir: Path,
    ) -> LoginResult:
        """
        Authenticate the browser session.

        Args:
            browser:        Playwright Browser instance (already launched).
            config:         AppConfig — reads config.ui_auth and config.browser.
            session_id:     Used to name the failure screenshot.
            screenshot_dir: Directory to write screenshots on failure.

        Returns:
            LoginResult with success=True and storage_state on success,
            or success=False and error_message on failure.
        """
        ui_auth = config.ui_auth  # type: ignore[attr-defined]
        browser_cfg = config.browser  # type: ignore[attr-defined]
        timeouts = config.timeouts  # type: ignore[attr-defined]

        login_url = _resolve_login_url(
            base_url=config.url,  # type: ignore[attr-defined]
            login_path=ui_auth.login_url,
        )

        log.info(
            "login_attempt_start",
            login_url=login_url,
            username_selector=ui_auth.selectors.username,
            password_selector=ui_auth.selectors.password,
            submit_selector=ui_auth.selectors.submit,
            success_indicator=ui_auth.success_indicator,
            # NOTE: credentials are intentionally NOT logged
        )

        t_start = time.perf_counter()
        context = None

        try:
            # ── Open a fresh, unauthenticated browser context ─────────────────
            context = await browser.new_context(  # type: ignore[attr-defined]
                viewport={
                    "width": browser_cfg.viewport_width,
                    "height": browser_cfg.viewport_height,
                },
                ignore_https_errors=True,
                java_script_enabled=True,
            )
            page = await context.new_page()

            # ── Navigate to login page ────────────────────────────────────────
            log.debug("login_navigating", url=login_url)
            await page.goto(
                login_url,
                wait_until="domcontentloaded",
                timeout=timeouts.page_load_ms,
            )

            # ── Fill username ─────────────────────────────────────────────────
            try:
                await page.wait_for_selector(
                    ui_auth.selectors.username,
                    timeout=timeouts.action_ms,
                    state="visible",
                )
            except Exception:
                raise LoginFailedError(
                    login_url=login_url,
                    status_code=None,
                ) from None

            # Fill without logging the value
            await page.fill(ui_auth.selectors.username, ui_auth.credentials.username)
            log.debug("login_username_filled", selector=ui_auth.selectors.username)

            # ── Fill password ─────────────────────────────────────────────────
            try:
                await page.wait_for_selector(
                    ui_auth.selectors.password,
                    timeout=timeouts.action_ms,
                    state="visible",
                )
            except Exception:
                raise LoginFailedError(
                    login_url=login_url,
                    status_code=None,
                ) from None

            await page.fill(ui_auth.selectors.password, ui_auth.credentials.password)
            log.debug("login_password_filled", selector=ui_auth.selectors.password)

            # ── Click submit ──────────────────────────────────────────────────
            try:
                await page.wait_for_selector(
                    ui_auth.selectors.submit,
                    timeout=timeouts.action_ms,
                    state="visible",
                )
            except Exception:
                raise LoginFailedError(
                    login_url=login_url,
                    status_code=None,
                ) from None

            log.debug("login_submitting", selector=ui_auth.selectors.submit)
            await page.click(ui_auth.selectors.submit)

            # ── Wait for post-login state ─────────────────────────────────────
            wait_timeout = ui_auth.post_login_wait.timeout_ms
            if ui_auth.post_login_wait.network_idle:
                try:
                    await page.wait_for_load_state(
                        "networkidle",
                        timeout=wait_timeout,
                    )
                except Exception:
                    # networkidle can time out on sites with polling; fall back
                    # to domcontentloaded which is almost always achievable
                    log.debug("login_networkidle_timeout_fallback")
                    await page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=wait_timeout,
                    )
            else:
                await page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=wait_timeout,
                )

            # ── Verify success ────────────────────────────────────────────────
            indicator = ui_auth.success_indicator
            verified = False

            if _is_css_selector(indicator):
                # Selector-based: check DOM presence
                try:
                    await page.wait_for_selector(
                        indicator,
                        timeout=timeouts.action_ms,
                        state="attached",
                    )
                    verified = True
                    log.debug("login_success_selector_found", selector=indicator)
                except Exception:
                    log.debug("login_success_selector_not_found", selector=indicator)
            else:
                # Text-based: scan visible page text
                page_text = await page.inner_text("body")
                verified = indicator.lower() in page_text.lower()
                log.debug(
                    "login_success_text_check",
                    indicator=indicator,
                    found=verified,
                )

            if not verified:
                raise LoginFailedError(
                    login_url=login_url,
                    status_code=None,
                )

            # ── Capture storage state ─────────────────────────────────────────
            storage_state = await context.storage_state()
            duration_ms = (time.perf_counter() - t_start) * 1000

            log.info(
                "login_success",
                login_url=login_url,
                duration_ms=round(duration_ms, 1),
                cookies=len(storage_state.get("cookies", [])),
            )

            return LoginResult(
                success=True,
                storage_state=storage_state,
                duration_ms=duration_ms,
            )

        except LoginFailedError as exc:
            duration_ms = (time.perf_counter() - t_start) * 1000
            screenshot_path = await self._capture_failure_screenshot(
                page=page if "page" in dir() else None,  # type: ignore[arg-type]
                session_id=session_id,
                screenshot_dir=screenshot_dir,
            )
            log.warning(
                "login_failed",
                login_url=login_url,
                error=str(exc),
                duration_ms=round(duration_ms, 1),
            )
            return LoginResult(
                success=False,
                storage_state=None,
                duration_ms=duration_ms,
                error_message=str(exc),
                screenshot_path=screenshot_path,
            )

        except Exception as exc:
            duration_ms = (time.perf_counter() - t_start) * 1000
            screenshot_path = await self._capture_failure_screenshot(
                page=page if "page" in dir() else None,  # type: ignore[arg-type]
                session_id=session_id,
                screenshot_dir=screenshot_dir,
            )
            error_msg = f"Unexpected error during login: {type(exc).__name__}: {exc}"
            log.error(
                "login_error",
                login_url=login_url,
                error_type=type(exc).__name__,
                # NOTE: exc may contain response bodies; log only type + message
                error=str(exc)[:200],
                duration_ms=round(duration_ms, 1),
            )
            return LoginResult(
                success=False,
                storage_state=None,
                duration_ms=duration_ms,
                error_message=error_msg,
                screenshot_path=screenshot_path,
            )

        finally:
            if context is not None:
                try:
                    await context.close()  # type: ignore[attr-defined]
                    log.debug("login_context_closed")
                except Exception:
                    pass

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    async def _capture_failure_screenshot(
        page: Optional[object],
        session_id: str,
        screenshot_dir: Path,
    ) -> Optional[str]:
        """
        Attempt to capture a screenshot of the current page state.
        Returns the file path string, or None if capture fails.
        """
        if page is None:
            return None
        try:
            screenshot_path = screenshot_dir / f"auth_failure_{session_id}.png"
            await page.screenshot(path=str(screenshot_path), full_page=False)  # type: ignore[attr-defined]
            log.debug("login_failure_screenshot_saved", path=str(screenshot_path))
            return str(screenshot_path)
        except Exception as exc:
            log.debug("login_failure_screenshot_error", error=str(exc))
            return None
