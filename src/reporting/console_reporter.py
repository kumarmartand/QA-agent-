"""
console_reporter.py — Rich terminal output for the CLI.

Uses the `rich` library to produce clean, coloured, structured console
output without any external dependencies beyond the package itself.

Output sections (in order):
  1. Run header (URL, depth, session ID)
  2. Stats table (pass rate, durations, issue counts)
  3. Issues table (grouped, sorted by severity)
  4. Final verdict (PASS ✅ / FAIL ❌ with health score)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.text import Text
from rich.rule import Rule

from src.collectors.issue import Issue
from src.core.constants import Severity
from src.core.models import SessionSummary

console = Console(highlight=False)

# Severity → rich colour mapping
_SEVERITY_STYLE = {
    "critical": "bold red",
    "high":     "bold dark_orange",
    "medium":   "bold yellow",
    "low":      "bold green",
}
_STATUS_STYLE = {
    "pass":    "green",
    "fail":    "red",
    "error":   "bold red",
    "timeout": "magenta",
    "skip":    "dim",
}


def print_run_header(url: str, depth: str, session_id: str) -> None:
    """Print the run start banner."""
    console.print()
    console.print(Panel(
        f"[bold white]🤖 QA Bot — Automated Testing[/bold white]\n"
        f"[dim]Target:[/dim]  [cyan]{url}[/cyan]\n"
        f"[dim]Depth:[/dim]   [yellow]{depth.upper()}[/yellow]\n"
        f"[dim]Session:[/dim] [dim]{session_id[:8]}…[/dim]",
        border_style="bright_blue",
        expand=False,
    ))
    console.print()


def print_summary(summary: SessionSummary, issues: list[Issue]) -> None:
    """
    Print the full post-run summary: stats table + issues table + verdict.
    """
    _print_stats_table(summary)
    console.print()

    if issues:
        _print_issues_table(issues)
        console.print()

    _print_verdict(summary)


def print_result_live(engine: str, test_name: str, status: str, duration_ms: float) -> None:
    """
    Print a single result line as it streams in during the run.
    Called by the runner's consumer task for real-time feedback.
    """
    icon = {"pass": "✅", "fail": "❌", "error": "💥", "timeout": "⏱️", "skip": "⏭️"}.get(status, "?")
    style = _STATUS_STYLE.get(status, "white")
    console.print(
        f"  {icon} [{style}]{status.upper():7}[/{style}]  "
        f"[dim]{engine.upper():4}[/dim]  "
        f"{test_name[:65]}"
        f"  [dim]{duration_ms:.0f}ms[/dim]"
    )


def print_engine_start(engine: str) -> None:
    console.print(f"\n[bold bright_blue]▶ Starting {engine.upper()} engine…[/bold bright_blue]")


def print_error(message: str) -> None:
    console.print(f"[bold red]✗ {message}[/bold red]")


def print_info(message: str) -> None:
    console.print(f"[dim]ℹ  {message}[/dim]")


def print_report_paths(paths: dict[str, str]) -> None:
    """Print the locations of generated report files."""
    console.print()
    console.print(Rule("[bold]Reports Generated[/bold]"))
    for fmt, path in paths.items():
        icon = "🌐" if fmt == "html" else "📄"
        console.print(f"  {icon}  [{fmt.upper()}]  [cyan]{path}[/cyan]")
    console.print()


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _print_stats_table(summary: SessionSummary) -> None:
    """Print the run statistics in a compact table."""
    console.print(Rule("[bold]Run Summary[/bold]"))
    console.print()

    table = Table(box=box.SIMPLE_HEAD, show_header=True, expand=False)
    table.add_column("Metric", style="dim", no_wrap=True)
    table.add_column("Value", justify="right")

    pass_pct = f"{summary.pass_rate * 100:.1f}%"
    pass_style = "green" if summary.pass_rate >= 0.9 else "yellow" if summary.pass_rate >= 0.7 else "red"
    health_style = "green" if summary.health_score >= 80 else "yellow" if summary.health_score >= 50 else "red"
    duration_s = summary.overall_duration_ms / 1000

    table.add_row("Total Tests",      str(summary.total_tests))
    table.add_row("Passed",           f"[green]{summary.total_passed}[/green]")
    table.add_row("Failed",           f"[red]{summary.total_failed}[/red]")
    table.add_row("Pass Rate",        f"[{pass_style}]{pass_pct}[/{pass_style}]")
    table.add_row("Unique Issues",    str(summary.total_bugs))
    table.add_row("  ├ Critical",     f"[bold red]{summary.critical_bugs}[/bold red]")
    table.add_row("  └ High",         f"[dark_orange]{summary.high_bugs}[/dark_orange]")
    table.add_row("Duration",         f"{duration_s:.1f}s")
    table.add_row("Health Score",     f"[{health_style}]{summary.health_score}/100[/{health_style}]")

    console.print(table)


def _print_issues_table(issues: list[Issue]) -> None:
    """Print all issues as a rich table, sorted by severity."""
    console.print(Rule(f"[bold]Issues Found ({len(issues)})[/bold]"))
    console.print()

    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold dim",
        expand=True,
    )
    table.add_column("Sev",       width=8,  no_wrap=True)
    table.add_column("Cat",       width=8,  no_wrap=True)
    table.add_column("Title",     ratio=3)
    table.add_column("Pages",     width=6,  justify="right")
    table.add_column("Count",     width=6,  justify="right")
    table.add_column("Error",     ratio=2)

    for issue in issues:
        sev_style = _SEVERITY_STYLE.get(issue.severity.value, "white")
        table.add_row(
            Text(issue.severity.value.upper(), style=sev_style),
            Text(issue.category.value.upper(), style="dim"),
            issue.title[:70],
            str(len(issue.affected_pages)),
            str(issue.occurrences),
            Text(
                (issue.error_message or "")[:60],
                style="dim red" if issue.error_message else "dim",
            ),
        )

    console.print(table)


def _print_verdict(summary: SessionSummary) -> None:
    """Print the final PASS/FAIL verdict panel."""
    has_critical = summary.critical_bugs > 0
    has_high = summary.high_bugs > 0
    overall_pass = not has_critical and not has_high and summary.pass_rate >= 0.9

    if overall_pass:
        console.print(Panel(
            f"[bold green]✅ PASS — All critical checks passed[/bold green]\n"
            f"[dim]Health score: {summary.health_score}/100 · "
            f"{summary.total_passed}/{summary.total_tests} tests passed[/dim]",
            border_style="green",
            expand=False,
        ))
    else:
        reasons: list[str] = []
        if has_critical:
            reasons.append(f"{summary.critical_bugs} critical issue(s)")
        if has_high:
            reasons.append(f"{summary.high_bugs} high severity issue(s)")
        if summary.pass_rate < 0.9:
            reasons.append(f"pass rate {summary.pass_rate * 100:.1f}% < 90%")
        console.print(Panel(
            f"[bold red]❌ FAIL — {', '.join(reasons)}[/bold red]\n"
            f"[dim]Health score: {summary.health_score}/100 · "
            f"{summary.total_passed}/{summary.total_tests} tests passed[/dim]",
            border_style="red",
            expand=False,
        ))
    console.print()
