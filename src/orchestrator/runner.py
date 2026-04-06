"""
runner.py — TestRunner: the central async orchestrator.

Responsibilities:
  1. Accept a list of engine instances and a TestSession.
  2. Run all engines concurrently (asyncio.gather) with a concurrency cap.
  3. Stream results from each engine to a ResultQueue as they arrive.
  4. Wrap each engine in a config-driven retry policy.
  5. Handle partial failures: one engine crashing must not abort others.
  6. Produce EngineRunSummary objects and attach a SessionSummary to the session.

Concurrency model:
  ┌────────────┐   ┌────────────┐   ┌─────────────────┐
  │ UI Engine  │   │ API Engine │   │ Perf Engine     │
  │ (task 1)   │   │ (task 2)   │   │ (task 3)        │
  └──────┬─────┘   └──────┬─────┘   └────────┬────────┘
         │ yield          │ yield             │ yield
         └────────────────┴───────────────────┘
                          │
                   asyncio.Queue[TestResult | None]
                          │
                   ResultConsumer (async task)
                          │
                   session.add_result() ──▶ Collector

Key design choices:
  - `asyncio.Semaphore(max_concurrent)` caps how many engines run in parallel.
    Even if 10 engines are registered, only N run at once.
  - `return_exceptions=True` in asyncio.gather means one engine's exception
    doesn't cancel the others.
  - The retry wrapper uses tenacity with a per-engine timeout enforced via
    `asyncio.wait_for`.
  - A sentinel value (None) is placed in the queue by each engine when it
    finishes, allowing the consumer to know when to stop.
  - The runner transitions the session to COMPLETED or FAILED based on
    whether any engine had a fatal error.
"""

from __future__ import annotations

import asyncio
import time
import traceback
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Optional

from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_fixed,
)

from src.core.config import AppConfig
from src.core.constants import EngineType, SessionStatus, TestStatus
from src.core.exceptions import EngineError, QABotError, SessionTimeoutError
from src.core.logger import get_logger
from src.core.models import EngineRunSummary, SessionSummary, TestResult
from src.engines.base import BaseEngine
from src.orchestrator.session import TestSession

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────────────

# Sentinel value placed in the queue when an engine is done
_QUEUE_SENTINEL = None
ResultQueue = asyncio.Queue  # Queue[Optional[TestResult]]

# Exceptions the retry system will attempt to recover from
# (network hiccups, browser crashes, transient timeouts)
_RETRYABLE_EXCEPTIONS = (
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
)


# ─────────────────────────────────────────────────────────────────────────────
# EngineTask — wraps one engine's execution lifecycle
# ─────────────────────────────────────────────────────────────────────────────

class EngineTask:
    """
    Wraps a single engine's full execution lifecycle including:
      - Setup / teardown (via context manager)
      - Retry with config-driven policy
      - Per-test timeout enforcement
      - Streaming results to a shared queue
      - Producing an EngineRunSummary on completion

    The orchestrator creates one EngineTask per engine and runs them
    all concurrently via asyncio.gather.
    """

    def __init__(
        self,
        engine: BaseEngine,
        session: TestSession,
        result_queue: ResultQueue,
        config: AppConfig,
        semaphore: asyncio.Semaphore,
    ) -> None:
        self.engine = engine
        self.session = session
        self.result_queue = result_queue
        self.config = config
        self.semaphore = semaphore
        self._results: list[TestResult] = []
        self._started_at: Optional[datetime] = None
        self._completed_at: Optional[datetime] = None

    async def run(self) -> EngineRunSummary:
        """
        Execute the engine with retries and produce a summary.

        Returns EngineRunSummary regardless of outcome — never raises
        so that asyncio.gather(return_exceptions=True) isn't needed
        for partial failure protection at the result level.
        """
        engine_name = self.engine.engine_type.value
        engine_error: Optional[str] = None

        async with self.semaphore:
            log.info("engine_task_starting", engine=engine_name)
            self._started_at = datetime.now(tz=timezone.utc)

            try:
                await self._run_with_retry()
            except Exception as exc:
                # The engine failed after all retries — record the error
                # but do NOT propagate. Partial failures are normal.
                engine_error = f"{type(exc).__name__}: {exc}"
                log.error(
                    "engine_task_failed",
                    engine=engine_name,
                    error=engine_error,
                    exc_info=True,
                )
            finally:
                self._completed_at = datetime.now(tz=timezone.utc)
                # Signal this engine is done by placing a sentinel in the queue
                await self.result_queue.put(_QUEUE_SENTINEL)
                log.info(
                    "engine_task_finished",
                    engine=engine_name,
                    results=len(self._results),
                    error=engine_error or "none",
                )

        return EngineRunSummary.from_results(
            engine=self.engine.engine_type,
            results=self._results,
            started_at=self._started_at,
            completed_at=self._completed_at,
            engine_error=engine_error,
        )

    async def _run_with_retry(self) -> None:
        """
        Run the engine, retrying on transient failures.

        Uses tenacity for the retry loop. The full engine (setup + execute
        + teardown) is retried, not individual test cases.

        Design note: Retrying the full engine means we get a fresh browser
        context / HTTP client on each attempt, which resolves most transient
        failures (browser crash, stale auth token, etc.).
        """
        retry_cfg = self.config.retry
        engine_timeout_s = self.config.timeouts.session_timeout_ms / 1000

        if retry_cfg.exponential_backoff:
            wait_strategy = wait_exponential(
                multiplier=retry_cfg.wait_seconds,
                min=retry_cfg.wait_seconds,
                max=retry_cfg.wait_seconds * 10,
            )
        else:
            wait_strategy = wait_fixed(retry_cfg.wait_seconds)

        # Build tenacity retry decorator dynamically from config
        retry_decorator = retry(
            stop=stop_after_attempt(retry_cfg.max_attempts),
            wait=wait_strategy,
            retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
            reraise=True,
            before_sleep=lambda retry_state: log.warning(
                "engine_retrying",
                engine=self.engine.engine_type.value,
                attempt=retry_state.attempt_number,
                error=str(retry_state.outcome.exception()),
            ),
        )

        @retry_decorator
        async def _attempt() -> None:
            async with asyncio.timeout(engine_timeout_s):
                await self._execute_engine()

        try:
            await _attempt()
        except RetryError as exc:
            # tenacity exhausted all attempts
            last_exc = exc.last_attempt.exception()
            raise EngineError(
                f"Engine '{self.engine.engine_type.value}' failed after "
                f"{retry_cfg.max_attempts} attempts",
                context={"last_error": str(last_exc)},
            ) from last_exc
        except asyncio.TimeoutError:
            raise SessionTimeoutError(
                f"Engine '{self.engine.engine_type.value}' exceeded "
                f"session timeout of {engine_timeout_s}s"
            )

    async def _execute_engine(self) -> None:
        """
        Run setup → stream execute() → teardown.
        Streams each yielded TestResult to the shared queue immediately.
        """
        engine_name = self.engine.engine_type.value

        try:
            await self.engine.setup()

            # Stream results as the engine produces them
            async for result in self.engine.execute(self.session):
                self._results.append(result)
                await self.result_queue.put(result)
                log.debug(
                    "result_streamed",
                    engine=engine_name,
                    test=result.test_name,
                    status=result.status.value,
                )

        finally:
            # teardown is ALWAYS called, even if execute() raises
            try:
                await self.engine.teardown()
            except Exception as teardown_exc:
                log.error(
                    "engine_teardown_error",
                    engine=engine_name,
                    error=str(teardown_exc),
                )


# ─────────────────────────────────────────────────────────────────────────────
# ResultConsumer — drains the shared queue into the session
# ─────────────────────────────────────────────────────────────────────────────

async def _consume_results(
    result_queue: ResultQueue,
    session: TestSession,
    engine_count: int,
) -> None:
    """
    Async consumer task: drains the result queue and calls session.add_result()
    for each item until all `engine_count` engines signal completion.

    Uses a sentinel-counting approach: each engine places None in the queue
    when done. When we see `engine_count` sentinels, we stop.

    Running this as a separate task ensures the queue never backs up even
    if add_result() has I/O work to do (e.g., DB writes in future phases).
    """
    sentinels_received = 0

    while sentinels_received < engine_count:
        item = await result_queue.get()

        if item is _QUEUE_SENTINEL:
            sentinels_received += 1
            log.debug(
                "consumer_engine_done",
                sentinels=sentinels_received,
                total_engines=engine_count,
            )
            result_queue.task_done()
            continue

        # It's a real TestResult — add to session
        await session.add_result(item)
        result_queue.task_done()

    log.debug("consumer_finished", total_results=session.result_count())


# ─────────────────────────────────────────────────────────────────────────────
# TestRunner — the public orchestration interface
# ─────────────────────────────────────────────────────────────────────────────

class TestRunner:
    """
    Coordinates all registered engines for a TestSession.

    Usage:
        runner = TestRunner(config)
        runner.register(ui_engine)
        runner.register(api_engine)
        summary = await runner.run(session)

    The runner:
      1. Transitions the session to RUNNING
      2. Launches all engines concurrently (limited by max_concurrent_tests)
      3. Streams results to the session via a shared queue + consumer task
      4. Collects EngineRunSummary from each engine
      5. Builds and attaches SessionSummary to the session
      6. Transitions the session to COMPLETED or FAILED
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._engines: list[BaseEngine] = []
        self._semaphore = asyncio.Semaphore(
            config.rate_limit.max_concurrent_tests
        )

    def register(self, engine: BaseEngine) -> "TestRunner":
        """
        Register an engine. Returns self for chaining:
            runner.register(ui_engine).register(api_engine)
        """
        self._engines.append(engine)
        log.debug(
            "engine_registered_with_runner",
            engine=engine.engine_type.value,
            total=len(self._engines),
        )
        return self

    def register_many(self, *engines: BaseEngine) -> "TestRunner":
        """Register multiple engines at once."""
        for engine in engines:
            self.register(engine)
        return self

    async def run(self, session: TestSession) -> SessionSummary:
        """
        Execute all registered engines and return the final SessionSummary.

        This is the primary public method. It handles the full session
        lifecycle from PENDING → RUNNING → COMPLETED/FAILED.

        Args:
            session: A TestSession in PENDING state.

        Returns:
            SessionSummary with aggregated results.

        Raises:
            SessionError: If the session is not in PENDING state.
            ValueError: If no engines are registered.
        """
        if not self._engines:
            raise ValueError("No engines registered. Call runner.register() first.")

        if session.status != SessionStatus.PENDING:
            from src.core.exceptions import SessionError
            raise SessionError(
                f"Cannot run session in state '{session.status.value}'. "
                "Session must be PENDING."
            )

        # ── Transition to RUNNING ─────────────────────────────────────────────
        await session.start()

        run_started = time.perf_counter()
        engine_summaries: list[EngineRunSummary] = []
        fatal_error: Optional[str] = None

        log.info(
            "runner_starting",
            session_id=session.id,
            url=session.url,
            engines=[e.engine_type.value for e in self._engines],
            max_concurrent=self.config.rate_limit.max_concurrent_tests,
        )

        try:
            engine_summaries = await self._run_engines(session)
        except Exception as exc:
            fatal_error = f"{type(exc).__name__}: {exc}"
            log.critical(
                "runner_fatal_error",
                session_id=session.id,
                error=fatal_error,
                exc_info=True,
            )

        # ── Build summary regardless of outcome ───────────────────────────────
        summary = SessionSummary.from_engine_summaries(
            session_id=session.id,
            target_url=session.url,
            engine_summaries=engine_summaries,
            bug_reports=session.bug_reports,
            started_at=session.started_at,
            completed_at=datetime.now(tz=timezone.utc),
        )

        # ── Transition to terminal state ──────────────────────────────────────
        if fatal_error:
            await session.fail(fatal_error)
        else:
            await session.complete(summary)

        total_ms = (time.perf_counter() - run_started) * 1000
        log.info(
            "runner_finished",
            session_id=session.id,
            status=session.status.value,
            total_tests=summary.total_tests,
            passed=summary.total_passed,
            failed=summary.total_failed,
            bugs=summary.total_bugs,
            duration_ms=f"{total_ms:.0f}",
            health_score=summary.health_score,
        )

        return summary

    async def _run_engines(self, session: TestSession) -> list[EngineRunSummary]:
        """
        The core concurrent execution logic.

        Creates:
          - One EngineTask per engine
          - One shared ResultQueue (bounded to prevent unbounded memory use)
          - One consumer task that drains the queue into the session

        All engine tasks and the consumer run concurrently via asyncio.gather.
        The consumer finishes only after ALL engines signal completion.
        """
        # Bounded queue: holds up to 500 results before producers back-pressure
        result_queue: ResultQueue = asyncio.Queue(maxsize=500)
        engine_count = len(self._engines)

        # Create all engine task wrappers
        engine_tasks = [
            EngineTask(
                engine=engine,
                session=session,
                result_queue=result_queue,
                config=self.config,
                semaphore=self._semaphore,
            )
            for engine in self._engines
        ]

        # Launch consumer + all engine tasks concurrently
        # return_exceptions=True: engine failures don't cancel other tasks
        results = await asyncio.gather(
            # Consumer task (no summary returned)
            _consume_results(result_queue, session, engine_count),
            # All engine tasks
            *[task.run() for task in engine_tasks],
            return_exceptions=True,
        )

        # results[0] is consumer output (None); results[1:] are EngineRunSummary or exceptions
        engine_results = results[1:]
        summaries: list[EngineRunSummary] = []

        for engine, result in zip(self._engines, engine_results):
            if isinstance(result, BaseException):
                # Engine task itself raised (shouldn't happen — EngineTask catches internally)
                # Create a failed summary as a fallback
                log.error(
                    "engine_task_unexpected_exception",
                    engine=engine.engine_type.value,
                    error=str(result),
                )
                summaries.append(
                    EngineRunSummary(
                        engine=engine.engine_type,
                        engine_error=f"Unexpected: {result}",
                    )
                )
            else:
                summaries.append(result)

        return summaries

    # ── Diagnostic helpers ────────────────────────────────────────────────────

    def registered_engines(self) -> list[str]:
        """Return names of all registered engines."""
        return [e.engine_type.value for e in self._engines]

    def __repr__(self) -> str:
        return (
            f"TestRunner(engines={self.registered_engines()}, "
            f"max_concurrent={self.config.rate_limit.max_concurrent_tests})"
        )
