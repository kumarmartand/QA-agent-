"""
models.py — Shared, immutable data models used across all layers.

Design principles:
  - All models are Pydantic v2 with `frozen=True` so a TestResult produced
    by an engine cannot be mutated by a collector or reporter.
  - UUIDs are used as IDs everywhere — safe for distributed use and avoids
    auto-increment races when writing to the DB asynchronously.
  - `metadata` is an open dict so engines can attach engine-specific data
    (e.g., HTTP status code for API results, selector used for UI results)
    without coupling the core model to engine internals.
  - All timestamps are UTC-aware datetimes.
  - Models provide `.to_dict()` for JSON serialisation and `.summary_line()`
    for human-readable console output.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field, computed_field

from src.core.constants import (
    BugCategory,
    EngineType,
    Severity,
    TestStatus,
)


def _utc_now() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(tz=timezone.utc)


def _new_uuid() -> str:
    """Generate a new UUID4 string."""
    return str(uuid.uuid4())


# ─────────────────────────────────────────────────────────────────────────────
# TestResult — one test case outcome
# ─────────────────────────────────────────────────────────────────────────────

class TestResult(BaseModel):
    """
    Immutable record of a single test case execution.

    Produced by engines, consumed by:
      - ResultCollector (aggregation + storage)
      - AIAnalyzer (bug classification input)
      - ReportingEngine (rendered in HTML/JSON)

    Design note: `screenshot_path` stores the filesystem path.
    The reporter is responsible for embedding it as base64 if needed.
    """

    model_config = {"frozen": True}

    # Identity
    id: str = Field(default_factory=_new_uuid)
    session_id: str                          # Links back to TestSession.id

    # What was tested
    engine: EngineType
    test_name: str                           # Human-readable: "Page Load: /login"
    test_url: str                            # Exact URL under test

    # Outcome
    status: TestStatus
    duration_ms: float = 0.0               # Wall-clock time for this test

    # Failure details (None on pass)
    error_message: Optional[str] = None
    stack_trace: Optional[str] = None

    # Artefacts
    screenshot_path: Optional[str] = None   # Absolute path or None
    console_logs: list[dict[str, Any]] = Field(default_factory=list)
    network_logs: list[dict[str, Any]] = Field(default_factory=list)

    # Open metadata for engine-specific data
    # e.g., {"http_status": 404, "selector_strategy": "aria-label"}
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Timestamp
    created_at: datetime = Field(default_factory=_utc_now)

    # ── Computed helpers ──────────────────────────────────────────────────────

    @computed_field
    @property
    def passed(self) -> bool:
        return self.status == TestStatus.PASS

    @computed_field
    @property
    def failed(self) -> bool:
        return self.status in (TestStatus.FAIL, TestStatus.ERROR, TestStatus.TIMEOUT)

    def summary_line(self) -> str:
        """One-line human-readable summary for console output."""
        icon = {"pass": "✅", "fail": "❌", "skip": "⏭️", "error": "💥", "timeout": "⏱️"}.get(
            self.status.value, "?"
        )
        return (
            f"{icon} [{self.engine.value.upper()}] {self.test_name} "
            f"({self.duration_ms:.0f}ms)"
            + (f" — {self.error_message}" if self.error_message else "")
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable dict (datetimes → ISO strings)."""
        return self.model_dump(mode="json")


# ─────────────────────────────────────────────────────────────────────────────
# EngineRunSummary — aggregate stats for one engine's run
# ─────────────────────────────────────────────────────────────────────────────

class EngineRunSummary(BaseModel):
    """
    Summary produced after an engine finishes all its tests.
    Used by the orchestrator to build the session-level summary.
    """

    model_config = {"frozen": True}

    engine: EngineType
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    total_duration_ms: float = 0.0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # Whether the engine itself crashed (distinct from individual test failures)
    engine_error: Optional[str] = None

    @computed_field
    @property
    def pass_rate(self) -> float:
        """0.0–1.0 pass rate. Returns 0.0 if no tests ran."""
        if self.total == 0:
            return 0.0
        return self.passed / self.total

    @computed_field
    @property
    def success(self) -> bool:
        """True if the engine itself didn't crash and all tests passed."""
        return self.engine_error is None and self.failed == 0 and self.errors == 0

    @classmethod
    def from_results(
        cls,
        engine: EngineType,
        results: list[TestResult],
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        engine_error: Optional[str] = None,
    ) -> "EngineRunSummary":
        """Build a summary from a list of TestResult objects."""
        return cls(
            engine=engine,
            total=len(results),
            passed=sum(1 for r in results if r.status == TestStatus.PASS),
            failed=sum(1 for r in results if r.status == TestStatus.FAIL),
            skipped=sum(1 for r in results if r.status == TestStatus.SKIP),
            errors=sum(1 for r in results if r.status in (TestStatus.ERROR, TestStatus.TIMEOUT)),
            total_duration_ms=sum(r.duration_ms for r in results),
            started_at=started_at,
            completed_at=completed_at,
            engine_error=engine_error,
        )


# ─────────────────────────────────────────────────────────────────────────────
# BugReport — AI-analysed, classified bug
# ─────────────────────────────────────────────────────────────────────────────

class BugReport(BaseModel):
    """
    Structured bug produced by the AI Analysis Layer.

    This is the STRICT schema all AI providers must output.
    Validated by Pydantic before storage — if the AI returns garbage,
    we catch it here rather than storing corrupt data.

    JSON schema (enforced):
    {
      "severity":           "low|medium|high|critical",
      "category":           "ui|api|perf|auth|security",
      "root_cause":         "string",
      "description":        "string",
      "steps_to_reproduce": ["string"],
      "suggested_fix":      "string",
      "confidence_score":   0.0–1.0
    }
    """

    model_config = {"frozen": True}

    # Identity
    id: str = Field(default_factory=_new_uuid)
    session_id: str
    result_id: str                           # The TestResult that triggered this

    # AI classification (strict schema)
    severity: Severity
    category: BugCategory
    root_cause: str = Field(min_length=1)
    description: str = Field(min_length=1)
    steps_to_reproduce: list[str] = Field(min_length=1)
    suggested_fix: str = Field(min_length=1)
    confidence_score: float = Field(ge=0.0, le=1.0)

    # Which AI provider produced this
    ai_provider: str = "mock"
    ai_model: str = ""

    created_at: datetime = Field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ─────────────────────────────────────────────────────────────────────────────
# SessionSummary — top-level aggregate across all engines
# ─────────────────────────────────────────────────────────────────────────────

class SessionSummary(BaseModel):
    """
    Final summary attached to a completed TestSession.
    Rendered in the report header and console output.
    """

    model_config = {"frozen": True}

    session_id: str
    target_url: str
    total_tests: int = 0
    total_passed: int = 0
    total_failed: int = 0
    total_errors: int = 0
    total_bugs: int = 0
    critical_bugs: int = 0
    high_bugs: int = 0
    engine_summaries: list[EngineRunSummary] = Field(default_factory=list)
    overall_duration_ms: float = 0.0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @computed_field
    @property
    def pass_rate(self) -> float:
        if self.total_tests == 0:
            return 0.0
        return self.total_passed / self.total_tests

    @computed_field
    @property
    def health_score(self) -> int:
        """
        0–100 score combining pass rate and bug severity.
        Critical bugs have an outsized penalty.
        """
        base = self.pass_rate * 100
        penalty = (self.critical_bugs * 25) + (self.high_bugs * 10)
        return max(0, int(base - penalty))

    @classmethod
    def from_engine_summaries(
        cls,
        session_id: str,
        target_url: str,
        engine_summaries: list[EngineRunSummary],
        bug_reports: list[BugReport],
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
    ) -> "SessionSummary":
        return cls(
            session_id=session_id,
            target_url=target_url,
            total_tests=sum(s.total for s in engine_summaries),
            total_passed=sum(s.passed for s in engine_summaries),
            total_failed=sum(s.failed for s in engine_summaries),
            total_errors=sum(s.errors for s in engine_summaries),
            total_bugs=len(bug_reports),
            critical_bugs=sum(1 for b in bug_reports if b.severity == Severity.CRITICAL),
            high_bugs=sum(1 for b in bug_reports if b.severity == Severity.HIGH),
            engine_summaries=engine_summaries,
            overall_duration_ms=sum(s.total_duration_ms for s in engine_summaries),
            started_at=started_at,
            completed_at=completed_at,
        )
