"""
result_collector.py — Streaming result ingestion, deduplication, and aggregation.

The ResultCollector sits between the TestRunner's output queue and the
storage/reporting layers. It is NOT just a pass-through — it actively:

  1. Classifies each failing result before storage
  2. Groups similar failures into Issue objects (deduplication)
  3. Counts occurrences and tracks affected pages per Issue
  4. Builds aggregate statistics as results stream in

Design: pull-based consumer
  The collector exposes `ingest(result)` which is called by the runner's
  consumer task. This is intentionally synchronous in signature — the
  runner calls `await collector.ingest(result)` and the collector does
  its grouping work (all in-memory, fast) before optionally persisting.

  Persistence is optional at ingest time. Bulk-persist after the run via
  `await collector.flush(repository)` to avoid per-result DB writes.

Thread safety:
  All state is protected behind asyncio.Lock. Multiple engine tasks
  streaming results concurrently (via the runner's queue consumer) is safe.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from src.collectors.issue import Issue, make_issue_key
from src.core.constants import EngineType, Severity, TestStatus
from src.core.logger import get_logger
from src.core.models import EngineRunSummary, SessionSummary, TestResult

log = get_logger(__name__)


class ResultCollector:
    """
    Stateful collector that processes TestResult objects as they stream
    in from the runner and produces deduplicated Issue objects.

    Usage:
        collector = ResultCollector(session_id="abc123")

        # Called by runner's queue consumer for each result:
        await collector.ingest(result)

        # After all engines finish:
        issues = collector.issues()
        summary = collector.build_summary(session, engine_summaries)
        await collector.flush(repository)
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._lock = asyncio.Lock()

        # All raw results (ordered by arrival)
        self._results: list[TestResult] = []

        # Issue registry: issue_key → Issue
        self._issues: dict[str, Issue] = {}

        # Per-engine result buckets for summary computation
        self._by_engine: dict[EngineType, list[TestResult]] = defaultdict(list)

        # Running counters (updated as results arrive — no re-scan needed)
        self._total = 0
        self._passed = 0
        self._failed = 0
        self._errors = 0
        self._timeouts = 0

        log.debug("collector_created", session_id=session_id)

    # ── Primary interface ─────────────────────────────────────────────────────

    async def ingest(self, result: TestResult) -> None:
        """
        Process one TestResult.

        - Updates running counters
        - If the result represents a failure, creates or updates an Issue
        - Thread-safe (asyncio.Lock)

        Called by the runner's queue consumer task.
        """
        async with self._lock:
            self._results.append(result)
            self._by_engine[result.engine].append(result)
            self._total += 1

            # Update counters
            if result.status == TestStatus.PASS:
                self._passed += 1
            elif result.status in (TestStatus.FAIL, TestStatus.ERROR, TestStatus.TIMEOUT):
                if result.status == TestStatus.TIMEOUT:
                    self._timeouts += 1
                elif result.status == TestStatus.ERROR:
                    self._errors += 1
                else:
                    self._failed += 1

                # Group into Issue
                self._process_failure(result)
            # SKIP counts but doesn't create an issue

            log.debug(
                "result_ingested",
                test=result.test_name,
                status=result.status.value,
                total=self._total,
                issues=len(self._issues),
            )

    def _process_failure(self, result: TestResult) -> None:
        """
        Create or update an Issue for a failing result.
        Must be called while holding self._lock.
        """
        key = make_issue_key(result)

        if key in self._issues:
            # Existing issue — absorb this as another occurrence
            self._issues[key].absorb(result)
            log.debug(
                "issue_occurrence_added",
                key=key,
                title=self._issues[key].title[:60],
                occurrences=self._issues[key].occurrences,
            )
        else:
            # New distinct failure — create a fresh Issue
            issue = Issue.from_result(result, key)
            self._issues[key] = issue
            log.info(
                "new_issue_detected",
                key=key,
                title=issue.title[:80],
                severity=issue.severity.value,
                url=result.test_url,
            )

    # ── Read-only accessors ───────────────────────────────────────────────────

    def results(self) -> list[TestResult]:
        """All ingested TestResults (arrival order)."""
        return list(self._results)

    def issues(self) -> list[Issue]:
        """
        All deduplicated Issues, sorted by severity (critical first).
        This is what the reporter renders.
        """
        return sorted(
            self._issues.values(),
            key=lambda i: i.severity_score,
            reverse=True,   # CRITICAL first
        )

    def issues_by_severity(self) -> dict[str, list[Issue]]:
        """Group issues by severity for the report dashboard."""
        groups: dict[str, list[Issue]] = {
            "critical": [],
            "high": [],
            "medium": [],
            "low": [],
        }
        for issue in self._issues.values():
            groups[issue.severity.value].append(issue)
        return groups

    def stats(self) -> dict:
        """Running statistics snapshot (no lock needed — atomic reads)."""
        return {
            "total": self._total,
            "passed": self._passed,
            "failed": self._failed,
            "errors": self._errors,
            "timeouts": self._timeouts,
            "issues": len(self._issues),
            "pass_rate": round(self._passed / self._total, 3) if self._total else 0.0,
        }

    def build_summary(
        self,
        session_id: str,
        target_url: str,
        engine_summaries: list[EngineRunSummary],
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
    ) -> SessionSummary:
        """Build the final SessionSummary from collected data."""
        all_issues = list(self._issues.values())
        return SessionSummary(
            session_id=session_id,
            target_url=target_url,
            total_tests=self._total,
            total_passed=self._passed,
            total_failed=self._failed + self._errors + self._timeouts,
            total_errors=self._errors,
            total_bugs=len(all_issues),
            critical_bugs=sum(1 for i in all_issues if i.severity == Severity.CRITICAL),
            high_bugs=sum(1 for i in all_issues if i.severity == Severity.HIGH),
            engine_summaries=engine_summaries,
            overall_duration_ms=sum(
                s.total_duration_ms for s in engine_summaries
            ),
            started_at=started_at,
            completed_at=completed_at or datetime.now(tz=timezone.utc),
        )

    # ── Storage persistence ───────────────────────────────────────────────────

    async def flush(self, repository: Optional[object] = None) -> None:
        """
        Persist all collected results and issues to the storage layer.

        This is a bulk operation called once after the run completes —
        not per-result — to avoid database write amplification.

        Args:
            repository: A Repository instance (from src.storage.repository).
                        If None (no DB configured), silently skips persistence.
        """
        if repository is None:
            log.debug("flush_skipped", reason="no_repository")
            return

        async with self._lock:
            results_snapshot = list(self._results)
            issues_snapshot = list(self._issues.values())

        try:
            await repository.save_results(results_snapshot)
            await repository.save_issues(issues_snapshot)
            log.info(
                "collector_flushed",
                results=len(results_snapshot),
                issues=len(issues_snapshot),
            )
        except Exception as exc:
            # Storage failure must NOT crash the reporting step
            log.error("flush_failed", error=str(exc), exc_info=True)

    def __repr__(self) -> str:
        return (
            f"ResultCollector(session={self.session_id[:8]}…, "
            f"results={self._total}, issues={len(self._issues)})"
        )
