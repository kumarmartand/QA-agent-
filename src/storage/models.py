"""
models.py — SQLAlchemy ORM models for persistent storage.

Design decisions:
  - SQLAlchemy 2.0 mapped_column() style (type-annotated, clean syntax)
  - JSON columns stored as TEXT (SQLite-compatible; PostgreSQL uses JSONB)
  - Separate tables: sessions / test_results / issues
  - No foreign key constraints by default (SQLite doesn't enforce them
    without PRAGMA; avoids cascade confusion in MVP)
  - All IDs are UUIDs (strings in SQLite) — no auto-increment races
  - `created_at` is always stored in UTC ISO format

Upgrading to PostgreSQL:
  Change DATABASE_URL from sqlite+aiosqlite:/// to postgresql+asyncpg://
  All models remain identical — SQLAlchemy handles dialect differences.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Text, Float, Integer, String, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Sessions
# ─────────────────────────────────────────────────────────────────────────────

class SessionModel(Base):
    """Persisted TestSession record."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    test_depth: Mapped[str] = mapped_column(String(20), nullable=False, default="standard")

    # Timing
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Stats (denormalised for fast dashboard queries)
    total_tests: Mapped[int] = mapped_column(Integer, default=0)
    total_passed: Mapped[int] = mapped_column(Integer, default=0)
    total_failed: Mapped[int] = mapped_column(Integer, default=0)
    total_issues: Mapped[int] = mapped_column(Integer, default=0)
    health_score: Mapped[int] = mapped_column(Integer, default=0)

    # Full config used for this run (JSON string)
    config_snapshot: Mapped[str] = mapped_column(Text, default="{}")

    def __repr__(self) -> str:
        return f"<SessionModel id={self.id[:8]} url={self.url} status={self.status}>"


# ─────────────────────────────────────────────────────────────────────────────
# Test Results
# ─────────────────────────────────────────────────────────────────────────────

class TestResultModel(Base):
    """Persisted TestResult record."""

    __tablename__ = "test_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    # What was tested
    engine: Mapped[str] = mapped_column(String(20), nullable=False)
    test_name: Mapped[str] = mapped_column(Text, nullable=False)
    test_url: Mapped[str] = mapped_column(Text, nullable=False)

    # Outcome
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)

    # Failure details (NULL for passing tests to save space)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stack_trace: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    screenshot_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # JSON arrays/objects stored as TEXT
    console_logs: Mapped[str] = mapped_column(Text, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    def __repr__(self) -> str:
        return f"<TestResultModel {self.test_name[:40]} status={self.status}>"


# ─────────────────────────────────────────────────────────────────────────────
# Issues
# ─────────────────────────────────────────────────────────────────────────────

class IssueModel(Base):
    """Persisted Issue record (deduplicated, grouped failures)."""

    __tablename__ = "issues"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    issue_key: Mapped[str] = mapped_column(String(12), nullable=False, index=True)

    # Classification
    title: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    engine: Mapped[str] = mapped_column(String(20), nullable=False)

    # Aggregates
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    affected_pages: Mapped[str] = mapped_column(Text, default="[]")  # JSON

    # Timing
    first_seen: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    # Details
    error_message: Mapped[str] = mapped_column(Text, default="")
    stack_trace: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    screenshots: Mapped[str] = mapped_column(Text, default="[]")     # JSON
    result_ids: Mapped[str] = mapped_column(Text, default="[]")      # JSON

    # AI analysis (populated in Phase 3)
    root_cause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    suggested_fix: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ai_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    def __repr__(self) -> str:
        return f"<IssueModel key={self.issue_key} severity={self.severity} occurrences={self.occurrences}>"
