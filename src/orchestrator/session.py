"""
session.py — TestSession model and lifecycle state machine.

A TestSession is the top-level unit of work: one run of the QA bot
against one target URL.

State machine:
  ┌─────────┐   start()    ┌─────────┐   complete()  ┌───────────┐
  │ CREATED │ ──────────▶  │ RUNNING │ ────────────▶  │ COMPLETED │
  └─────────┘              └─────────┘                └───────────┘
                                │  fail()                    │
                                ▼                            │  (terminal)
                           ┌────────┐                        │
                           │ FAILED │◀───────────────────────┘
                           └────────┘
                                │
                           ┌────────────┐
                           │ CANCELLED  │  (future: via cancel())
                           └────────────┘

Design decisions:
  - asyncio.Lock guards all status transitions — safe for concurrent engine tasks
    that might call session.add_result() simultaneously.
  - Transitions are strict: COMPLETED → RUNNING is illegal. Any attempted
    invalid transition raises SessionError immediately.
  - `config_snapshot` stores a serialised copy of AppConfig so the session
    is self-contained and reports correctly even if config changes.
  - `results` and `bug_reports` are accumulated in-memory during the run;
    the storage layer persists them to DB after collection.
  - `transition_history` provides a full audit trail for debugging stuck sessions.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from src.core.constants import SessionStatus
from src.core.exceptions import SessionError, SessionTimeoutError
from src.core.logger import bind_session, get_logger
from src.core.models import BugReport, SessionSummary, TestResult

log = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Valid state transitions
# ─────────────────────────────────────────────────────────────────────────────

# Maps: current_status → set of allowed next statuses
_VALID_TRANSITIONS: dict[SessionStatus, set[SessionStatus]] = {
    SessionStatus.PENDING:   {SessionStatus.RUNNING},
    SessionStatus.RUNNING:   {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED},
    SessionStatus.COMPLETED: set(),   # Terminal — no further transitions
    SessionStatus.FAILED:    set(),   # Terminal
    SessionStatus.CANCELLED: set(),   # Terminal
}


# ─────────────────────────────────────────────────────────────────────────────
# TestSession
# ─────────────────────────────────────────────────────────────────────────────

class TestSession:
    """
    Represents one full QA run: from input URL to final report.

    Thread safety:
      All mutable operations (status transitions, result accumulation)
      are protected by `_lock` (asyncio.Lock). This prevents race conditions
      when multiple engine tasks call `add_result()` concurrently.

    Lifecycle:
      session = TestSession.create(config)
      await session.start()                # PENDING → RUNNING
      session.add_result(result)           # Accumulate
      await session.complete(summary)      # RUNNING → COMPLETED
      # or
      await session.fail(error_msg)        # RUNNING → FAILED
    """

    def __init__(
        self,
        session_id: str,
        url: str,
        config_snapshot: dict[str, Any],
        test_depth: str,
    ) -> None:
        # Identity
        self.id: str = session_id
        self.url: str = url
        self.config_snapshot: dict[str, Any] = config_snapshot
        self.test_depth: str = test_depth

        # State
        self._status: SessionStatus = SessionStatus.PENDING
        self._lock = asyncio.Lock()

        # Timing
        self.created_at: datetime = datetime.now(tz=timezone.utc)
        self.started_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None

        # Results accumulation
        self._results: list[TestResult] = []
        self._bug_reports: list[BugReport] = []
        self._summary: Optional[SessionSummary] = None

        # Audit trail: list of (from_status, to_status, timestamp, reason)
        self._transition_history: list[dict[str, Any]] = []

        # Optional failure message
        self.failure_reason: Optional[str] = None

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def create(cls, config: Any) -> "TestSession":
        """
        Factory: create a new session from an AppConfig.

        Serialises config to a snapshot dict so the session is self-contained.
        Binds session_id to the async logging context immediately.
        """
        session_id = str(uuid.uuid4())

        # Serialise config (Pydantic model → dict, drop secrets)
        snapshot = config.model_dump(mode="json", exclude={"auth"})
        snapshot["url"] = config.url   # Always include the URL

        session = cls(
            session_id=session_id,
            url=config.url,
            config_snapshot=snapshot,
            test_depth=config.test_depth.value,
        )

        # Bind to async logging context — all subsequent logs in this task
        # and its children will automatically include session_id=...
        bind_session(session_id)

        log.info(
            "session_created",
            session_id=session_id,
            url=config.url,
            depth=config.test_depth.value,
        )
        return session

    # ── Status property ───────────────────────────────────────────────────────

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def is_terminal(self) -> bool:
        """True if the session has reached a terminal state."""
        return self._status in (
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
        )

    @property
    def is_running(self) -> bool:
        return self._status == SessionStatus.RUNNING

    # ── Lifecycle transitions (async, lock-protected) ─────────────────────────

    async def start(self) -> None:
        """
        Transition PENDING → RUNNING.
        Call this before launching any engine.
        """
        async with self._lock:
            self._transition_to(SessionStatus.RUNNING)
            self.started_at = datetime.now(tz=timezone.utc)
            log.info("session_started", session_id=self.id, url=self.url)

    async def complete(self, summary: Optional[SessionSummary] = None) -> None:
        """
        Transition RUNNING → COMPLETED.
        Call this after all engines have finished and results are collected.
        """
        async with self._lock:
            self._transition_to(SessionStatus.COMPLETED)
            self.completed_at = datetime.now(tz=timezone.utc)
            self._summary = summary
            duration = self._duration_ms()
            log.info(
                "session_completed",
                session_id=self.id,
                url=self.url,
                total_tests=len(self._results),
                duration_ms=duration,
            )

    async def fail(self, reason: str) -> None:
        """
        Transition RUNNING → FAILED.
        Call this when an unrecoverable error prevents the run from finishing.
        """
        async with self._lock:
            self._transition_to(SessionStatus.FAILED)
            self.completed_at = datetime.now(tz=timezone.utc)
            self.failure_reason = reason
            log.error(
                "session_failed",
                session_id=self.id,
                url=self.url,
                reason=reason,
            )

    async def cancel(self) -> None:
        """
        Transition RUNNING → CANCELLED.
        Used when the user interrupts a run (Ctrl+C or API cancellation).
        """
        async with self._lock:
            if self._status != SessionStatus.RUNNING:
                return  # Silently ignore cancel on non-running sessions
            self._transition_to(SessionStatus.CANCELLED)
            self.completed_at = datetime.now(tz=timezone.utc)
            log.warning("session_cancelled", session_id=self.id, url=self.url)

    # ── Result accumulation (thread-safe) ─────────────────────────────────────

    async def add_result(self, result: TestResult) -> None:
        """
        Add a TestResult to this session.
        Safe to call concurrently from multiple engine tasks.
        """
        async with self._lock:
            self._results.append(result)
            log.debug(
                "result_added",
                test_name=result.test_name,
                status=result.status.value,
                duration_ms=result.duration_ms,
            )

    async def add_bug_report(self, bug: BugReport) -> None:
        """Add an AI-generated BugReport to this session."""
        async with self._lock:
            self._bug_reports.append(bug)
            log.info(
                "bug_reported",
                title=bug.description[:60],
                severity=bug.severity.value,
                category=bug.category.value,
                confidence=bug.confidence_score,
            )

    # ── Read-only accessors ───────────────────────────────────────────────────

    @property
    def results(self) -> list[TestResult]:
        """Snapshot of all accumulated TestResults (read-only copy)."""
        return list(self._results)

    @property
    def bug_reports(self) -> list[BugReport]:
        """Snapshot of all accumulated BugReports (read-only copy)."""
        return list(self._bug_reports)

    @property
    def summary(self) -> Optional[SessionSummary]:
        return self._summary

    @property
    def transition_history(self) -> list[dict[str, Any]]:
        return list(self._transition_history)

    def result_count(self) -> int:
        return len(self._results)

    def failed_results(self) -> list[TestResult]:
        """Convenience: all results with non-passing status."""
        return [r for r in self._results if r.failed]

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _transition_to(self, new_status: SessionStatus, reason: str = "") -> None:
        """
        Validate and perform a status transition.
        Must be called while holding `_lock`.
        Raises SessionError on invalid transitions.
        """
        allowed = _VALID_TRANSITIONS.get(self._status, set())
        if new_status not in allowed:
            raise SessionError(
                f"Invalid transition: {self._status.value} → {new_status.value}. "
                f"Allowed from {self._status.value}: "
                f"{[s.value for s in allowed] or 'none (terminal state)'}",
                context={
                    "session_id": self.id,
                    "from": self._status.value,
                    "to": new_status.value,
                },
            )

        # Record the transition in the audit trail
        self._transition_history.append({
            "from": self._status.value,
            "to": new_status.value,
            "at": datetime.now(tz=timezone.utc).isoformat(),
            "reason": reason,
        })

        self._status = new_status

    def _duration_ms(self) -> float:
        """Wall-clock duration since start, in milliseconds."""
        if not self.started_at:
            return 0.0
        end = self.completed_at or datetime.now(tz=timezone.utc)
        return (end - self.started_at).total_seconds() * 1000

    def __repr__(self) -> str:
        return (
            f"TestSession(id={self.id[:8]}…, url={self.url!r}, "
            f"status={self._status.value}, results={len(self._results)})"
        )
