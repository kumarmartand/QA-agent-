"""
cli.py — Command-line interface for QA Bot.

Commands:
  run    → Full test run against a URL
  report → Regenerate report from a saved session ID

Usage:
  python -m src.cli run --url https://example.com
  python -m src.cli run --url https://example.com --depth full --headless false
  python -m src.cli run --url https://example.com --scenario config/test_scenarios.yaml
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

# Load .env before anything else so ${VAR} config interpolation works
load_dotenv(dotenv_path=Path(".env"), override=False)

from src.core.config import load_config
from src.core.constants import TestDepth
from src.core.exceptions import QABotError
from src.core.logger import setup_logging, get_logger
from src.collectors.result_collector import ResultCollector
from src.orchestrator.session import TestSession
from src.orchestrator.runner import TestRunner
from src.reporting.console_reporter import (
    print_run_header,
    print_summary,
    print_report_paths,
    print_error,
    print_info,
)
from src.reporting.html_reporter import HTMLReporter
from src.reporting.json_reporter import JSONReporter
from src.storage.database import init_db, close_db
from src.storage.repository import Repository


log = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CLI group
# ─────────────────────────────────────────────────────────────────────────────

@click.group()
def cli() -> None:
    """🤖 QA Bot — AI-Powered Automated QA & Bug Reporting System"""
    pass


# ─────────────────────────────────────────────────────────────────────────────
# `run` command
# ─────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--url",       "-u", required=True,  help="Target URL to test")
@click.option("--depth",     "-d", default="standard",
              type=click.Choice(["light", "standard", "full"], case_sensitive=False),
              help="Test depth (light/standard/full)")
@click.option("--scenario",  "-s", default=None,   help="Path to test_scenarios.yaml")
@click.option("--headless/--no-headless", default=True, help="Run browser headless")
@click.option("--output",    "-o", default="outputs", help="Output directory")
@click.option("--no-db",     is_flag=True, default=False, help="Skip database persistence")
@click.option("--format",    "-f", multiple=True,
              type=click.Choice(["html", "json"], case_sensitive=False),
              default=["html", "json"], help="Report format(s) to generate")
def run(
    url: str,
    depth: str,
    scenario: str | None,
    headless: bool,
    output: str,
    no_db: bool,
    format: tuple[str, ...],
) -> None:
    """Run a full QA test suite against a URL and generate a report."""
    setup_logging(output_dir=output)

    overrides: dict = {
        "browser": {"headless": headless},
        "output":  {"dir": output, "formats": list(format)},
        "test_depth": depth,
    }

    try:
        config = load_config(
            url=url,
            scenario_file=scenario,
            overrides=overrides,
        )
    except QABotError as exc:
        print_error(str(exc))
        sys.exit(1)

    try:
        exit_code = asyncio.run(_run_async(config, no_db=no_db))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        click.echo("\n⚠️  Run cancelled by user.")
        sys.exit(130)


async def _run_async(config: object, no_db: bool = False) -> int:
    """
    Full async run pipeline:
      Config → Session → Runner (engines) → Collector → Reports → Exit code

    Returns 0 on pass, 1 on failure (for CI/CD integration).
    """
    # ── Import engines (side-effect: registers them with EngineRegistry) ──────
    from src.engines.ui.engine import UIEngine  # noqa: F401

    # ── Print header ──────────────────────────────────────────────────────────
    print_run_header(
        url=config.url,
        depth=config.test_depth.value,
        session_id="(starting…)",
    )

    # ── Storage setup ─────────────────────────────────────────────────────────
    repository = None
    if not no_db:
        try:
            await init_db(output_dir=config.output.dir)
            repository = Repository(output_dir=config.output.dir)
        except Exception as exc:
            print_info(f"Database unavailable ({exc}). Continuing without persistence.")

    # ── Session ───────────────────────────────────────────────────────────────
    session = TestSession.create(config)
    print_info(f"Session ID: {session.id}")

    if repository:
        await repository.save_session(session)

    # ── Collector ─────────────────────────────────────────────────────────────
    collector = ResultCollector(session_id=session.id)

    # ── Build runner ──────────────────────────────────────────────────────────
    runner = TestRunner(config)
    runner.register(UIEngine(config))

    # ── Execute ───────────────────────────────────────────────────────────────
    log.info("run_starting", url=config.url, session_id=session.id)

    try:
        # The runner transitions session PENDING → RUNNING internally
        summary = await runner.run(session)
    except Exception as exc:
        print_error(f"Fatal runner error: {exc}")
        log.critical("fatal_runner_error", error=str(exc), exc_info=True)
        return 1
    finally:
        await close_db()

    # ── Collect results from session into the collector ───────────────────────
    for result in session.results:
        await collector.ingest(result)

    # ── Persist ───────────────────────────────────────────────────────────────
    await collector.flush(repository)
    issues = collector.issues()

    # ── Build final session summary from collector ────────────────────────────
    final_summary = collector.build_summary(
        session_id=session.id,
        target_url=config.url,
        engine_summaries=summary.engine_summaries,
        started_at=session.started_at,
        completed_at=session.completed_at,
    )

    if repository:
        await repository.update_session_stats(
            session_id=session.id,
            total_tests=final_summary.total_tests,
            total_passed=final_summary.total_passed,
            total_failed=final_summary.total_failed,
            total_issues=final_summary.total_bugs,
            health_score=final_summary.health_score,
            status=session.status.value,
            completed_at=session.completed_at,
        )

    # ── Generate reports ──────────────────────────────────────────────────────
    report_paths: dict[str, str] = {}
    all_results = session.results
    formats = list(config.output.formats)

    if "html" in formats:
        try:
            html_reporter = HTMLReporter(
                output_dir=str(Path(config.output.dir) / "reports")
            )
            report_paths["html"] = await html_reporter.generate(
                session_id=session.id,
                summary=final_summary,
                issues=issues,
                results=all_results,
                test_depth=config.test_depth.value,
            )
        except Exception as exc:
            print_info(f"HTML report failed: {exc}")
            log.error("html_report_error", error=str(exc))

    if "json" in formats:
        try:
            json_reporter = JSONReporter(
                output_dir=str(Path(config.output.dir) / "reports")
            )
            report_paths["json"] = await json_reporter.generate(
                session_id=session.id,
                summary=final_summary,
                issues=issues,
                results=all_results,
                test_depth=config.test_depth.value,
            )
        except Exception as exc:
            print_info(f"JSON report failed: {exc}")
            log.error("json_report_error", error=str(exc))

    # ── Console output ────────────────────────────────────────────────────────
    print_summary(final_summary, issues)
    print_report_paths(report_paths)

    # ── Exit code for CI/CD ───────────────────────────────────────────────────
    # Exit 1 if there are any critical or high issues (breaks the build)
    if final_summary.critical_bugs > 0 or final_summary.high_bugs > 0:
        return 1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
