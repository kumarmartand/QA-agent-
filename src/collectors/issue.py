"""
issue.py — Issue data model, grouping key logic, and severity pre-classification.

An Issue is NOT a raw TestResult. It is a deduplicated, human-readable
representation of ONE distinct problem that may have appeared on multiple
pages or in multiple test runs.

Transformation:
  N TestResults (same root cause) → 1 Issue with N occurrences

Grouping key design:
  The key must be:
    1. Stable across runs — same bug always maps to same key
    2. Collision-resistant — different bugs must not share a key
    3. URL-agnostic — the same 404 error on /login and /signup is one issue

  Algorithm:
    1. Extract first line of error_message (most signal, least noise)
    2. Replace URLs → <URL>, numbers ≥ 3 digits → <N>
    3. Prepend engine type to namespace by domain
    4. MD5-hash the result → 12-char hex key

Severity pre-classification:
  These rules run BEFORE the AI analysis layer. They provide immediate
  severity so the report is useful even when AI is disabled (mock mode).
  The AI layer can override these classifications.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.core.constants import BugCategory, EngineType, Severity, TestStatus
from src.core.models import TestResult

# ─────────────────────────────────────────────────────────────────────────────
# Issue grouping key
# ─────────────────────────────────────────────────────────────────────────────

# Patterns to normalise out of error messages before hashing
_URL_PATTERN = re.compile(r"https?://[^\s,)\]'\"]+")
_NUMBER_PATTERN = re.compile(r"\b\d{3,}\b")          # 3+ digit numbers
_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)
_HEX_PATTERN = re.compile(r"\b[0-9a-f]{16,}\b", re.I)  # Long hex strings (hashes, tokens)


def _normalise_error(raw: str) -> str:
    """
    Strip session-specific tokens from an error message to produce a
    stable representation suitable for hashing into a grouping key.
    """
    if not raw:
        return "no_error"
    # First line captures the error type; subsequent lines are stack detail
    first_line = raw.strip().split("\n")[0][:300]
    text = _TIMESTAMP_PATTERN.sub("<TS>", first_line)
    text = _UUID_PATTERN.sub("<UUID>", text)
    text = _HEX_PATTERN.sub("<HEX>", text)
    text = _URL_PATTERN.sub("<URL>", text)
    text = _NUMBER_PATTERN.sub("<N>", text)
    return text.lower().strip()


def make_issue_key(result: TestResult) -> str:
    """
    Produce a 12-character hex key that uniquely identifies the class of
    failure represented by this TestResult.

    Two TestResults that represent the same bug should produce the same key.
    Two unrelated bugs should (with high probability) produce different keys.
    """
    normalised = _normalise_error(result.error_message or "")
    # Include engine type so UI/API/Perf issues never accidentally merge
    raw = f"{result.engine.value}:{normalised}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


# ─────────────────────────────────────────────────────────────────────────────
# Severity pre-classification rules
# ─────────────────────────────────────────────────────────────────────────────

def pre_classify_severity(result: TestResult) -> Severity:
    """
    Assign a severity level using deterministic rules — no AI required.
    Evaluated from most to least severe so the first matching rule wins.

    Rules:
      CRITICAL → HTTP 5xx server errors (the app is broken for all users)
      HIGH     → Timeouts, unhandled exceptions, HTTP 401/403, multiple JS errors
      MEDIUM   → HTTP 404, single JS error, button click error, slow redirect
      LOW      → Slow load (above threshold but functional), warnings only
    """
    status = result.status
    meta = result.metadata
    http_status = meta.get("http_status")
    error_count = meta.get("error_count", 0) or meta.get("new_errors", 0)

    # ── CRITICAL: Server-side failures affect all users ───────────────────────
    if http_status and 500 <= http_status < 600:
        return Severity.CRITICAL

    # ── HIGH: App is partially broken or inaccessible ─────────────────────────
    if status == TestStatus.ERROR:
        return Severity.HIGH

    if status == TestStatus.TIMEOUT:
        return Severity.HIGH

    if http_status in (401, 403):
        # Auth failures: either a security issue or a broken login flow
        return Severity.HIGH

    if isinstance(error_count, int) and error_count >= 3:
        # Multiple JS errors on one page suggests systemic frontend breakage
        return Severity.HIGH

    # ── MEDIUM: Degraded but not completely broken ────────────────────────────
    if http_status == 404:
        return Severity.MEDIUM

    if http_status and 400 <= http_status < 500:
        return Severity.MEDIUM

    if isinstance(error_count, int) and error_count >= 1:
        return Severity.MEDIUM

    if status == TestStatus.FAIL and "Button Click" in result.test_name:
        return Severity.MEDIUM

    # ── LOW: Functional but below quality threshold ───────────────────────────
    if status == TestStatus.FAIL:
        return Severity.LOW

    # Shouldn't reach here for failing results, but safe default
    return Severity.LOW


def _infer_category(result: TestResult) -> BugCategory:
    """Infer bug category from the result's engine and metadata."""
    engine = result.engine
    meta = result.metadata
    test_name = result.test_name.lower()

    if engine == EngineType.API:
        return BugCategory.API
    if engine == EngineType.PERFORMANCE:
        return BugCategory.PERF

    # UI engine sub-classification
    http_status = meta.get("http_status")
    if http_status in (401, 403) or "auth" in test_name or "login" in test_name:
        return BugCategory.AUTH
    if "console" in test_name:
        return BugCategory.UI
    if "button" in test_name:
        return BugCategory.UI
    return BugCategory.UI


# ─────────────────────────────────────────────────────────────────────────────
# Issue dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Issue:
    """
    A deduplicated, grouped representation of a recurring failure.

    Created by ResultCollector when it encounters a failing TestResult.
    If an existing Issue has the same grouping key, the new result is
    merged into it (occurrence count + new URL added to affected_pages).

    The `representative_result` is the first TestResult that created this
    issue. It's used by the reporter to show a screenshot and stack trace.
    """

    # Identity
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    issue_key: str = ""          # 12-char MD5 for dedup

    # Classification
    title: str = ""
    severity: Severity = Severity.MEDIUM
    category: BugCategory = BugCategory.UI
    engine: EngineType = EngineType.UI

    # Occurrence tracking
    occurrences: int = 0
    affected_pages: list[str] = field(default_factory=list)
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None

    # Artifacts from the representative (first) occurrence
    error_message: str = ""
    stack_trace: Optional[str] = None
    screenshots: list[str] = field(default_factory=list)

    # IDs of all contributing TestResults (for cross-referencing in reports)
    result_ids: list[str] = field(default_factory=list)

    # Pre-AI analysis fields (populated by AIAnalyzer in Phase 3)
    root_cause: Optional[str] = None
    suggested_fix: Optional[str] = None
    ai_confidence: float = 0.0

    def absorb(self, result: TestResult) -> None:
        """
        Merge a new TestResult into this Issue (same grouping key found).
        Updates occurrence count, affected pages, and timing.
        Keeps the representative artifacts from the first occurrence.
        """
        self.occurrences += 1
        self.result_ids.append(result.id)

        # Track unique affected pages
        if result.test_url not in self.affected_pages:
            self.affected_pages.append(result.test_url)

        # Time window
        ts = result.created_at
        if self.first_seen is None or ts < self.first_seen:
            self.first_seen = ts
        if self.last_seen is None or ts > self.last_seen:
            self.last_seen = ts

        # Collect screenshots (keep max 3 to avoid bloat)
        if result.screenshot_path and result.screenshot_path not in self.screenshots:
            if len(self.screenshots) < 3:
                self.screenshots.append(result.screenshot_path)

    @classmethod
    def from_result(cls, result: TestResult, key: str) -> "Issue":
        """
        Create a brand-new Issue from the first failing TestResult that
        produced `key`. Subsequent occurrences call `.absorb()`.
        """
        severity = pre_classify_severity(result)
        category = _infer_category(result)

        # Title: human-readable description of the failure
        title = _build_title(result)

        issue = cls(
            session_id=result.session_id,
            issue_key=key,
            title=title,
            severity=severity,
            category=category,
            engine=result.engine,
            error_message=result.error_message or "",
            stack_trace=result.stack_trace,
        )
        issue.absorb(result)
        return issue

    @property
    def primary_screenshot(self) -> Optional[str]:
        """First available screenshot path."""
        return self.screenshots[0] if self.screenshots else None

    @property
    def severity_score(self) -> int:
        """Numeric sort key (higher = more urgent)."""
        return self.severity.priority_score

    def to_dict(self) -> dict:
        """JSON-serialisable dict for the JSON report and storage."""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "issue_key": self.issue_key,
            "title": self.title,
            "severity": self.severity.value,
            "category": self.category.value,
            "engine": self.engine.value,
            "occurrences": self.occurrences,
            "affected_pages": self.affected_pages,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "error_message": self.error_message,
            "screenshots": self.screenshots,
            "result_ids": self.result_ids,
            "root_cause": self.root_cause,
            "suggested_fix": self.suggested_fix,
            "ai_confidence": self.ai_confidence,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Title builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_title(result: TestResult) -> str:
    """
    Build a concise, human-readable issue title from a TestResult.

    Priority:
      1. HTTP status description  (e.g., "HTTP 404 Not Found on /checkout")
      2. First line of error_message, truncated to 80 chars
      3. Test name as fallback
    """
    meta = result.metadata
    http_status = meta.get("http_status")
    test_url = result.test_url or ""
    # Shorten URL to just the path for readability
    from urllib.parse import urlparse
    path = urlparse(test_url).path or "/"

    # HTTP status-based titles (most descriptive)
    _STATUS_TITLES = {
        400: f"Bad Request on {path}",
        401: f"Unauthorised — authentication required on {path}",
        403: f"Access Forbidden on {path}",
        404: f"Page Not Found: {path}",
        500: f"Internal Server Error on {path}",
        502: f"Bad Gateway on {path}",
        503: f"Service Unavailable on {path}",
    }
    if http_status and http_status in _STATUS_TITLES:
        return _STATUS_TITLES[http_status]

    if http_status and http_status >= 400:
        return f"HTTP {http_status} error on {path}"

    # Timeout
    if result.status == TestStatus.TIMEOUT:
        return f"Timeout loading {path}"

    # Error message first line
    if result.error_message:
        first_line = result.error_message.split("\n")[0].strip()
        # Strip common JS prefixes to keep titles short
        first_line = re.sub(r"^(Uncaught |Error: |TypeError: )", "", first_line)
        if len(first_line) > 80:
            first_line = first_line[:77] + "…"
        return first_line

    # Fallback: derive from test name
    return result.test_name
