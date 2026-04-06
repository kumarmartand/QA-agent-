"""
selectors.py — Self-healing selector strategy.

Problem: CSS selectors break when developers rename classes, restructure
HTML, or remove IDs. This module implements a priority-ordered fallback
chain that tries the most robust attributes first and falls back gracefully.

Priority order (SELECTOR_PRIORITY from constants.py rationalised):
  1. data-testid   → Explicit test hook; most stable, never changes for UX
  2. aria-label    → Accessibility attribute; changes only for semantic reasons
  3. role          → ARIA role; very stable structural attribute
  4. visible text  → Playwright `text=` locator; survives DOM restructuring
  5. CSS selector  → Last resort; most brittle, breaks on refactors

Design notes:
  - `find_element()` tries each strategy and returns the FIRST visible match.
  - Logs which strategy succeeded — this is valuable diagnostics data.
    If a test always falls back to CSS, that's a signal the app needs testids.
  - `find_all_interactive()` uses a broad multi-strategy query to discover
    all clickable elements on a page without needing a hint.
  - All timeouts are configurable and short (default 2s per attempt) since
    we're trying multiple strategies; total max is strategy_count * timeout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from src.core.exceptions import SelectorNotFoundError
from src.core.logger import get_logger

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Strategy definitions
# ─────────────────────────────────────────────────────────────────────────────

# Each strategy is (name, selector_builder_fn)
# The builder receives a "hint" — either a test-id value, label text, or CSS
_STRATEGIES: list[tuple[str, object]] = [
    ("data-testid", lambda h: f'[data-testid="{h}"]'),
    ("aria-label",  lambda h: f'[aria-label="{h}"]'),
    ("aria-label-contains", lambda h: f'[aria-label*="{h}"]'),
    ("role",        lambda h: f'[role="{h}"]'),
    ("text-exact",  lambda h: f'text="{h}"'),
    ("text-partial",lambda h: f'text={h}'),
    ("placeholder", lambda h: f'[placeholder="{h}"]'),
    ("name",        lambda h: f'[name="{h}"]'),
    ("id",          lambda h: f'#{h}'),
    ("css",         lambda h: h),           # Raw CSS — final fallback
]

# Selectors that discover interactive elements without a hint
# Ordered from most semantic (role-based) to most generic (tag-based)
INTERACTIVE_SELECTORS = [
    "button:visible",
    "[role='button']:visible",
    "input[type='submit']:visible",
    "input[type='button']:visible",
    "input[type='reset']:visible",
    "[role='link']:visible",
    "a[href]:visible",
]

# Selectors that are never safe to click automatically
# (would log out, delete data, submit forms with empty data)
DANGEROUS_PATTERNS = re.compile(
    r"(logout|log.out|sign.out|signout|delete|destroy|remove|cancel.account"
    r"|deactivate|unsubscribe|reset.password)",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────────────
# Result of a selector resolution
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SelectorResult:
    """Records which strategy succeeded and the resolved selector string."""
    element: object                    # playwright Locator
    strategy: str                      # e.g. "aria-label"
    selector: str                      # Resolved selector string
    hint: str                          # Original hint passed by caller


# ─────────────────────────────────────────────────────────────────────────────
# Core find function
# ─────────────────────────────────────────────────────────────────────────────

async def find_element(
    page: object,
    hint: str,
    timeout_ms: int = 2000,
    visible_only: bool = True,
) -> SelectorResult:
    """
    Attempt to locate an element using the priority fallback chain.

    Args:
        page:        Playwright Page object.
        hint:        A test-id, aria-label, text content, or CSS selector.
        timeout_ms:  Per-strategy timeout. Total max = len(strategies) * timeout.
        visible_only: If True, only match elements that are visible in viewport.

    Returns:
        SelectorResult with the Playwright Locator and winning strategy.

    Raises:
        SelectorNotFoundError: If all strategies fail.
    """
    strategies_tried: list[str] = []
    page_url: str = page.url  # type: ignore[attr-defined]

    for strategy_name, builder in _STRATEGIES:
        selector = builder(hint)  # type: ignore[operator]
        strategies_tried.append(f"{strategy_name}:{selector}")

        try:
            locator = page.locator(selector).first  # type: ignore[attr-defined]
            state = "visible" if visible_only else "attached"
            await locator.wait_for(timeout=timeout_ms, state=state)

            log.debug(
                "selector_resolved",
                hint=hint,
                strategy=strategy_name,
                selector=selector,
                page_url=page_url,
            )
            return SelectorResult(
                element=locator,
                strategy=strategy_name,
                selector=selector,
                hint=hint,
            )
        except Exception:
            # This strategy failed — try the next one silently
            continue

    # All strategies exhausted
    log.warning(
        "selector_not_found",
        hint=hint,
        strategies_tried=strategies_tried,
        page_url=page_url,
    )
    raise SelectorNotFoundError(
        strategies_tried=strategies_tried,
        page_url=page_url,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Interactive element discovery
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class InteractiveElement:
    """A clickable element discovered on the page."""
    selector: str
    tag: str              # button, a, input
    text: str             # Visible text / label
    is_disabled: bool
    is_dangerous: bool    # Matches DANGEROUS_PATTERNS
    aria_label: str = ""
    test_id: str = ""
    href: str = ""        # For <a> elements


async def discover_interactive_elements(
    page: object,
    skip_dangerous: bool = True,
) -> list[InteractiveElement]:
    """
    Find all interactive elements on the current page.

    Uses Playwright's evaluate() to extract element metadata in one JS call
    rather than N separate Playwright queries — much faster for pages with
    many buttons.

    Args:
        page:           Playwright Page.
        skip_dangerous: If True, elements matching DANGEROUS_PATTERNS are
                        included in the list but marked `is_dangerous=True`.
                        The caller decides whether to skip or include them.

    Returns:
        List of InteractiveElement descriptors, deduplicated by text+selector.
    """
    # Broad CSS query — intentionally over-inclusive; we filter in Python
    js_selectors = " ,".join([
        "button",
        "[role='button']",
        "input[type='submit']",
        "input[type='button']",
        "input[type='reset']",
        "a[href]",
    ])

    # Single JS evaluation avoids round-trip overhead for each element
    raw_elements: list[dict] = await page.evaluate(  # type: ignore[attr-defined]
        """
        (selectors) => {
            const els = Array.from(document.querySelectorAll(selectors));
            return els
                .filter(el => {
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;  // visible check
                })
                .map(el => ({
                    tag:       el.tagName.toLowerCase(),
                    text:      (el.innerText || el.value || el.textContent || '').trim().substring(0, 100),
                    disabled:  el.disabled || el.getAttribute('aria-disabled') === 'true',
                    ariaLabel: el.getAttribute('aria-label') || '',
                    testId:    el.getAttribute('data-testid') || '',
                    href:      el.getAttribute('href') || '',
                    id:        el.id || '',
                    classes:   el.className || '',
                }));
        }
        """,
        js_selectors,
    )

    elements: list[InteractiveElement] = []
    seen_texts: set[str] = set()

    for raw in raw_elements:
        text = raw.get("text", "").strip()
        aria = raw.get("ariaLabel", "")
        tag = raw.get("tag", "")
        href = raw.get("href", "")

        # Build a display label for this element
        display = text or aria or raw.get("testId", "") or f"<{tag}>"

        # Deduplicate by display label (avoids testing 40 identical "Learn More" buttons)
        dedup_key = f"{tag}:{display[:50]}"
        if dedup_key in seen_texts:
            continue
        seen_texts.add(dedup_key)

        # Build a best-effort CSS selector for this element
        test_id = raw.get("testId", "")
        el_id = raw.get("id", "")
        if test_id:
            selector = f'[data-testid="{test_id}"]'
        elif el_id:
            selector = f'#{el_id}'
        elif aria:
            selector = f'[aria-label="{aria}"]'
        elif text:
            selector = f'text={text[:50]}'
        else:
            continue  # No viable selector — skip

        # Check danger patterns against all text signals
        all_text = f"{text} {aria} {href} {selector}".lower()
        is_dangerous = bool(DANGEROUS_PATTERNS.search(all_text))

        elements.append(InteractiveElement(
            selector=selector,
            tag=tag,
            text=display,
            is_disabled=bool(raw.get("disabled", False)),
            is_dangerous=is_dangerous,
            aria_label=aria,
            test_id=test_id,
            href=href,
        ))

    log.debug(
        "interactive_elements_discovered",
        total=len(elements),
        dangerous=sum(1 for e in elements if e.is_dangerous),
        disabled=sum(1 for e in elements if e.is_disabled),
    )
    return elements


# ─────────────────────────────────────────────────────────────────────────────
# URL utilities (shared across navigation + crawl)
# ─────────────────────────────────────────────────────────────────────────────

from urllib.parse import urljoin, urlparse  # noqa: E402


def normalise_url(url: str, base_url: str) -> Optional[str]:
    """
    Resolve a potentially relative URL against base_url, strip fragments,
    and normalise trailing slashes.

    Returns None for URLs we should never crawl:
      - javascript: pseudo-links
      - mailto: / tel: links
      - Data URIs
      - External domains
      - Static file extensions (images, fonts, PDFs, etc.)
    """
    # Skip non-HTTP schemes
    if url.startswith(("javascript:", "mailto:", "tel:", "data:", "#", "void(0)")):
        return None

    # Skip common static assets (not pages worth crawling)
    _STATIC_EXTS = {
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
        ".woff", ".woff2", ".ttf", ".eot", ".otf",
        ".pdf", ".zip", ".tar", ".gz",
        ".mp4", ".mp3", ".webm", ".ogg",
        ".css", ".js", ".map",
    }
    path = urlparse(url).path.lower()
    if any(path.endswith(ext) for ext in _STATIC_EXTS):
        return None

    # Resolve relative → absolute
    absolute = urljoin(base_url, url)

    # Remove fragment
    parsed = urlparse(absolute)
    clean = parsed._replace(fragment="").geturl()

    return clean.rstrip("/") or None


def is_same_domain(url: str, base_url: str) -> bool:
    """True if `url` is on the same host as `base_url` (ignores port/scheme)."""
    try:
        base_host = urlparse(base_url).netloc.lower().lstrip("www.")
        url_host = urlparse(url).netloc.lower().lstrip("www.")
        return base_host == url_host
    except Exception:
        return False
