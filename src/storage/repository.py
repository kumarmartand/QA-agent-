"""
repository.py — Data Access Layer (CRUD operations).

The repository pattern keeps SQL queries out of the business logic.
All database interactions happen here; the rest of the codebase never
imports SQLAlchemy directly (except models.py and database.py).

Design:
  - All methods are async
  - Bulk inserts where possible (avoid N+1 query patterns)
  - Methods return domain objects (TestResult, Issue) not ORM models
    → keeps the domain layer free from SQLAlchemy coupling
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.collectors.issue import Issue
from src.core.logger import get_logger
from src.core.models import TestResult
from src.orchestrator.session import TestSession
from src.storage.database import get_session
from src.storage.models import IssueModel, SessionModel, TestResultModel

log = get_logger(__name__)


class Repository:
    """
    Provides all database read/write operations for the QA bot.

    Instantiated with an output_dir so it knows where the SQLite file lives.
    """

    def __init__(self, output_dir: str = "outputs") -> None:
        self._output_dir = output_dir

    # ── Session CRUD ──────────────────────────────────────────────────────────

    async def save_session(self, session: TestSession) -> None:
        """Upsert a TestSession into the sessions table."""
        async with get_session(self._output_dir) as db:
            # Check if it already exists
            existing = await db.get(SessionModel, session.id)
            if existing:
                # Update status and timing
                existing.status = session.status.value
                existing.started_at = session.started_at
                existing.completed_at = session.completed_at
            else:
                db.add(SessionModel(
                    id=session.id,
                    url=session.url,
                    status=session.status.value,
                    test_depth=session.test_depth,
                    created_at=session.created_at,
                    started_at=session.started_at,
                    completed_at=session.completed_at,
                    config_snapshot=json.dumps(session.config_snapshot),
                ))
            log.debug("session_saved", session_id=session.id)

    async def update_session_stats(
        self,
        session_id: str,
        total_tests: int,
        total_passed: int,
        total_failed: int,
        total_issues: int,
        health_score: int,
        status: str,
        completed_at: Optional[datetime] = None,
    ) -> None:
        """Update denormalised stats columns after a run completes."""
        async with get_session(self._output_dir) as db:
            stmt = (
                update(SessionModel)
                .where(SessionModel.id == session_id)
                .values(
                    total_tests=total_tests,
                    total_passed=total_passed,
                    total_failed=total_failed,
                    total_issues=total_issues,
                    health_score=health_score,
                    status=status,
                    completed_at=completed_at,
                )
            )
            await db.execute(stmt)
            log.debug("session_stats_updated", session_id=session_id)

    # ── Test Results CRUD ─────────────────────────────────────────────────────

    async def save_results(self, results: list[TestResult]) -> None:
        """
        Bulk-insert TestResults.
        Uses a single transaction for all N inserts — much faster than
        N separate commits.
        """
        if not results:
            return

        async with get_session(self._output_dir) as db:
            models = [
                TestResultModel(
                    id=r.id,
                    session_id=r.session_id,
                    engine=r.engine.value,
                    test_name=r.test_name,
                    test_url=r.test_url,
                    status=r.status.value,
                    duration_ms=r.duration_ms,
                    error_message=r.error_message,
                    stack_trace=r.stack_trace,
                    screenshot_path=r.screenshot_path,
                    console_logs=json.dumps(r.console_logs),
                    metadata_json=json.dumps(r.metadata),
                    created_at=r.created_at,
                )
                for r in results
            ]
            db.add_all(models)
            log.info("results_saved", count=len(models))

    async def get_results_for_session(
        self, session_id: str
    ) -> list[TestResultModel]:
        """Retrieve all results for a session (for historical reporting)."""
        async with get_session(self._output_dir) as db:
            stmt = select(TestResultModel).where(
                TestResultModel.session_id == session_id
            )
            rows = await db.execute(stmt)
            return list(rows.scalars().all())

    # ── Issues CRUD ───────────────────────────────────────────────────────────

    async def save_issues(self, issues: list[Issue]) -> None:
        """Bulk-insert Issues (deduplicated by the collector)."""
        if not issues:
            return

        async with get_session(self._output_dir) as db:
            models = [
                IssueModel(
                    id=issue.id,
                    session_id=issue.session_id,
                    issue_key=issue.issue_key,
                    title=issue.title,
                    severity=issue.severity.value,
                    category=issue.category.value,
                    engine=issue.engine.value,
                    occurrences=issue.occurrences,
                    affected_pages=json.dumps(issue.affected_pages),
                    first_seen=issue.first_seen,
                    last_seen=issue.last_seen,
                    error_message=issue.error_message,
                    stack_trace=issue.stack_trace,
                    screenshots=json.dumps(issue.screenshots),
                    result_ids=json.dumps(issue.result_ids),
                    root_cause=issue.root_cause,
                    suggested_fix=issue.suggested_fix,
                    ai_confidence=issue.ai_confidence,
                )
                for issue in issues
            ]
            db.add_all(models)
            log.info("issues_saved", count=len(models))

    async def get_issues_for_session(
        self, session_id: str
    ) -> list[IssueModel]:
        """Retrieve all issues for a session."""
        async with get_session(self._output_dir) as db:
            stmt = (
                select(IssueModel)
                .where(IssueModel.session_id == session_id)
                .order_by(IssueModel.severity)
            )
            rows = await db.execute(stmt)
            return list(rows.scalars().all())

    # ── Historical queries ────────────────────────────────────────────────────

    async def list_sessions(self, limit: int = 20) -> list[SessionModel]:
        """List recent sessions, newest first."""
        async with get_session(self._output_dir) as db:
            stmt = (
                select(SessionModel)
                .order_by(SessionModel.created_at.desc())
                .limit(limit)
            )
            rows = await db.execute(stmt)
            return list(rows.scalars().all())
