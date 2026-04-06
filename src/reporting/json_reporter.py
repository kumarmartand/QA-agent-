"""
json_reporter.py — Structured JSON report for API consumption.

The JSON report is the machine-readable counterpart to the HTML report.
It exposes the full data model so downstream tools (JIRA integrations,
Slack bots, CI/CD pipelines) can parse and act on results programmatically.

Schema:
{
  "meta":    { session_id, generated_at, qa_bot_version },
  "summary": { total_tests, passed, failed, bugs, health_score, ... },
  "issues":  [ { id, title, severity, category, occurrences, affected_pages,
                 error_message, root_cause, suggested_fix, ... } ],
  "results": [ { id, engine, test_name, status, duration_ms, error_message,
                 screenshot_path, metadata } ],
  "engines": [ { engine, total, passed, failed, errors, pass_rate } ]
}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.collectors.issue import Issue
from src.core.logger import get_logger
from src.core.models import SessionSummary, TestResult

log = get_logger(__name__)


class JSONReporter:
    """
    Writes a structured JSON report to disk.

    Usage:
        reporter = JSONReporter(output_dir="outputs/reports")
        path = await reporter.generate(session_id, summary, issues, results)
    """

    def __init__(self, output_dir: str = "outputs/reports") -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    async def generate(
        self,
        session_id: str,
        summary: SessionSummary,
        issues: list[Issue],
        results: list[TestResult],
        test_depth: str = "standard",
    ) -> str:
        """
        Serialise all data to JSON and write to disk.
        Returns the absolute file path.
        """
        payload: dict[str, Any] = {
            "meta": {
                "session_id": session_id,
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "qa_bot_version": "0.1.0",
                "test_depth": test_depth,
            },
            "summary": {
                "target_url": summary.target_url,
                "total_tests": summary.total_tests,
                "total_passed": summary.total_passed,
                "total_failed": summary.total_failed,
                "total_errors": summary.total_errors,
                "total_bugs": summary.total_bugs,
                "critical_bugs": summary.critical_bugs,
                "high_bugs": summary.high_bugs,
                "pass_rate": round(summary.pass_rate, 4),
                "health_score": summary.health_score,
                "overall_duration_ms": round(summary.overall_duration_ms, 2),
                "started_at": summary.started_at.isoformat() if summary.started_at else None,
                "completed_at": summary.completed_at.isoformat() if summary.completed_at else None,
            },
            "issues": [issue.to_dict() for issue in issues],
            "results": [
                {
                    "id": r.id,
                    "engine": r.engine.value,
                    "test_name": r.test_name,
                    "test_url": r.test_url,
                    "status": r.status.value,
                    "duration_ms": round(r.duration_ms, 2),
                    "error_message": r.error_message,
                    "screenshot_path": r.screenshot_path,
                    "console_log_count": len(r.console_logs),
                    "metadata": r.metadata,
                    "created_at": r.created_at.isoformat(),
                }
                for r in results
            ],
            "engines": [
                {
                    "engine": s.engine.value,
                    "total": s.total,
                    "passed": s.passed,
                    "failed": s.failed,
                    "errors": s.errors,
                    "pass_rate": round(s.pass_rate, 4),
                    "total_duration_ms": round(s.total_duration_ms, 2),
                    "engine_error": s.engine_error,
                }
                for s in summary.engine_summaries
            ],
        }

        filename = f"report_{session_id[:8]}_{_timestamp()}.json"
        report_path = self._output_dir / filename
        report_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        log.info(
            "json_report_generated",
            path=str(report_path),
            size_kb=round(report_path.stat().st_size / 1024, 1),
        )
        return str(report_path)


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
