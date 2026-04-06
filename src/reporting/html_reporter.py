"""
html_reporter.py — Generates self-contained HTML reports using Jinja2.

Key design: the report is a SINGLE FILE.
  - Screenshots are embedded as base64 data URIs (no external image paths)
  - All CSS is inline in the template
  - No CDN or external JS dependencies

This makes the report portable: email it, attach it to a JIRA ticket,
or archive it — it always renders correctly with no broken images.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.collectors.issue import Issue
from src.core.logger import get_logger
from src.core.models import SessionSummary, TestResult

log = get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


class HTMLReporter:
    """
    Renders the QA report as a self-contained HTML file.

    Usage:
        reporter = HTMLReporter(output_dir="outputs/reports")
        path = await reporter.generate(
            session_id=...,
            summary=...,
            issues=...,
            results=...,
        )
        print(f"Report saved to: {path}")
    """

    def __init__(self, output_dir: str = "outputs/reports") -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # Jinja2 environment with auto-escaping for HTML safety
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "j2"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        # Custom filters
        self._env.filters["round"] = round

    async def generate(
        self,
        session_id: str,
        summary: SessionSummary,
        issues: list[Issue],
        results: list[TestResult],
        test_depth: str = "standard",
    ) -> str:
        """
        Render the full HTML report and write it to disk.

        Returns the absolute file path of the generated report.
        """
        template = self._env.get_template("report.html.j2")

        # Prepare issues with embedded screenshots
        enriched_issues = [
            self._enrich_issue(issue) for issue in issues
        ]

        # Prepare result rows for the timeline (no raw objects in template)
        result_rows = [self._result_to_row(r) for r in results]

        # Format duration
        duration_ms = summary.overall_duration_ms
        duration_formatted = _format_duration(duration_ms)

        generated_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        context = {
            "session_id": session_id,
            "summary": summary,
            "issues": enriched_issues,
            "results": result_rows,
            "test_depth": test_depth,
            "generated_at": generated_at,
            "duration_formatted": duration_formatted,
        }

        html = template.render(**context)

        # Write to disk
        filename = f"report_{session_id[:8]}_{_timestamp()}.html"
        report_path = self._output_dir / filename
        report_path.write_text(html, encoding="utf-8")

        log.info(
            "html_report_generated",
            path=str(report_path),
            size_kb=round(len(html.encode()) / 1024, 1),
            issues=len(issues),
            results=len(results),
        )
        return str(report_path)

    def _enrich_issue(self, issue: Issue) -> dict:
        """
        Convert an Issue to a template-ready dict with embedded screenshot.
        """
        d = issue.to_dict()

        # Embed primary screenshot as base64 data URI
        d["screenshot_b64"] = _embed_screenshot(issue.primary_screenshot)

        # Format timestamps for display
        d["first_seen"] = _fmt_dt(issue.first_seen)
        d["last_seen"] = _fmt_dt(issue.last_seen)

        return d

    def _result_to_row(self, r: TestResult) -> dict:
        """Flatten a TestResult to a simple dict for the timeline."""
        return {
            "status": r.status.value,
            "engine": r.engine.value,
            "test_name": r.test_name,
            "test_url": r.test_url,
            "error_message": r.error_message,
            "duration_ms": r.duration_ms,
            "created_at": _fmt_dt(r.created_at),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _embed_screenshot(path: Optional[str]) -> Optional[str]:
    """
    Read a screenshot PNG and return as a base64 data URI string.
    Returns None if the path is missing or the file can't be read.
    """
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{data}"
    except Exception as exc:
        log.warning("screenshot_embed_failed", path=path, error=str(exc))
        return None


def _format_duration(ms: float) -> str:
    """Human-friendly duration: 61000ms → '1m 1s'"""
    if ms < 1000:
        return f"{ms:.0f}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins}m {secs}s"


def _fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.strftime("%H:%M:%S")


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
