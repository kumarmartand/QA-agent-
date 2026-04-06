"""
base.py — Abstract BaseEngine interface contract.

Every test engine (UI, API, Performance) MUST implement this interface.
The orchestrator interacts exclusively through this contract — it never
touches engine internals directly, enabling engines to be swapped,
added, or mocked independently.

Interface contract:
  1. setup()       — acquire resources (browser, HTTP client, etc.)
  2. execute()     — run tests, YIELDING results as an async generator
                     (streaming — collector receives results as produced)
  3. teardown()    — release resources (always called, even on failure)
  4. run()         — convenience wrapper: setup → execute → teardown
                     Returns the full list of results for callers that
                     don't need streaming (e.g., tests, quick CLI runs).

Design decision — async generator for execute():
  Returning `AsyncIterator[TestResult]` rather than `list[TestResult]` lets
  the orchestrator stream results to the collector in real time. A long-running
  UI engine scanning 50 pages won't block the report from showing early results.
  Engines that are inherently batch can still `yield` at the end of each
  individual test case within their batch.

Design decision — context manager for lifecycle:
  `BaseEngine` implements `__aenter__`/`__aexit__` so engines can be used as:
      async with UIEngine(config) as engine:
          async for result in engine.execute(session):
              ...
  `teardown()` is guaranteed to run even if `execute()` raises.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Optional

from src.core.constants import EngineType, TestStatus
from src.core.exceptions import EngineError
from src.core.logger import bind_engine, get_logger
from src.core.models import TestResult

if TYPE_CHECKING:
    from src.core.config import AppConfig
    from src.orchestrator.session import TestSession

log = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Abstract base engine
# ─────────────────────────────────────────────────────────────────────────────

class BaseEngine(ABC):
    """
    Abstract base class all QA engines must subclass.

    Subclassing checklist:
      ✅ Set `engine_type` class variable
      ✅ Implement `setup()`
      ✅ Implement `execute()` as an async generator
      ✅ Implement `teardown()`
      ✅ Never swallow exceptions silently — let them propagate or wrap in EngineError
    """

    # Subclasses set this to identify themselves
    engine_type: EngineType

    def __init__(self, config: "AppConfig") -> None:
        self.config = config
        self._is_setup = False
        self._log = get_logger(self.__class__.__name__)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def setup(self) -> None:
        """
        Acquire and initialise all resources needed by this engine.
        Called once before any tests run.

        Examples:
          - UIEngine: launch Playwright browser, create context
          - APIEngine: initialise httpx.AsyncClient, acquire auth token
          - PerformanceEngine: set up timing instrumentation

        Must be idempotent (safe to call if already set up).
        Raise EngineError on unrecoverable setup failures.
        """
        ...

    @abstractmethod
    async def execute(self, session: "TestSession") -> AsyncIterator[TestResult]:
        """
        Run all tests and YIELD results as they complete.

        This is an async generator — use `yield` after each individual
        test completes. Do NOT accumulate all results and return at the end.

        The orchestrator wraps this in a retry decorator at the session level,
        but individual test retries are the engine's responsibility.

        Args:
            session: The active TestSession (read-only; use for session_id,
                     config snapshot, and status checks).

        Yields:
            TestResult for each completed test case.

        Raises:
            EngineError: For unrecoverable engine-level failures.
        """
        # This body is unreachable but satisfies the type checker for
        # async generator protocol. Subclasses must re-declare with `yield`.
        return
        yield  # noqa: unreachable — marks this as an async generator

    @abstractmethod
    async def teardown(self) -> None:
        """
        Release all resources acquired in setup().
        ALWAYS called — even if execute() raises an exception.

        Examples:
          - UIEngine: close browser context and browser
          - APIEngine: close httpx.AsyncClient
        """
        ...

    # ── Context manager support ───────────────────────────────────────────────

    async def __aenter__(self) -> "BaseEngine":
        await self.setup()
        self._is_setup = True
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        await self.teardown()
        self._is_setup = False
        return False   # Do not suppress exceptions

    # ── Convenience wrapper ───────────────────────────────────────────────────

    async def run(self, session: "TestSession") -> list[TestResult]:
        """
        Convenience method: setup → collect all results → teardown.

        Use this when you don't need streaming and just want the full list.
        The orchestrator uses `execute()` directly for streaming; this method
        is primarily for isolated tests and the CLI's quick-run mode.
        """
        bind_engine(self.engine_type.value)
        results: list[TestResult] = []

        try:
            await self.setup()
            self._is_setup = True
            async for result in self.execute(session):
                results.append(result)
        finally:
            await self.teardown()
            self._is_setup = False

        return results

    # ── Shared test helpers ───────────────────────────────────────────────────

    def _make_result(
        self,
        session: "TestSession",
        test_name: str,
        test_url: str,
        status: TestStatus,
        duration_ms: float = 0.0,
        error_message: Optional[str] = None,
        stack_trace: Optional[str] = None,
        screenshot_path: Optional[str] = None,
        console_logs: Optional[list[dict[str, Any]]] = None,
        network_logs: Optional[list[dict[str, Any]]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> TestResult:
        """
        Factory for TestResult — centralises field population so engine
        code only needs to provide the meaningful fields.
        """
        return TestResult(
            session_id=session.id,
            engine=self.engine_type,
            test_name=test_name,
            test_url=test_url,
            status=status,
            duration_ms=duration_ms,
            error_message=error_message,
            stack_trace=stack_trace,
            screenshot_path=screenshot_path,
            console_logs=console_logs or [],
            network_logs=network_logs or [],
            metadata=metadata or {},
        )

    def _timed(self) -> "_Timer":
        """Return a context manager that measures elapsed wall-clock time."""
        return _Timer()


# ─────────────────────────────────────────────────────────────────────────────
# Timer context manager
# ─────────────────────────────────────────────────────────────────────────────

class _Timer:
    """
    Lightweight synchronous timer context manager.

    Usage:
        with engine._timed() as t:
            await do_something()
        duration_ms = t.elapsed_ms
    """

    def __init__(self) -> None:
        self._start: float = 0.0
        self.elapsed_ms: float = 0.0

    def __enter__(self) -> "_Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_: Any) -> bool:
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Engine Registry
# ─────────────────────────────────────────────────────────────────────────────

class EngineRegistry:
    """
    Simple registry mapping EngineType → engine class.

    The orchestrator uses this to instantiate engines by type, enabling
    config-driven engine selection without importing engine classes directly.

    Usage:
        EngineRegistry.register(EngineType.UI, UIEngine)
        engine_cls = EngineRegistry.get(EngineType.UI)
        engine = engine_cls(config)
    """

    _registry: dict[EngineType, type[BaseEngine]] = {}

    @classmethod
    def register(cls, engine_type: EngineType, engine_cls: type[BaseEngine]) -> None:
        """Register an engine class for a given type."""
        cls._registry[engine_type] = engine_cls
        log.debug("engine_registered", engine_type=engine_type.value, cls=engine_cls.__name__)

    @classmethod
    def get(cls, engine_type: EngineType) -> type[BaseEngine]:
        """Retrieve a registered engine class. Raises KeyError if not found."""
        if engine_type not in cls._registry:
            raise KeyError(
                f"No engine registered for type '{engine_type.value}'. "
                f"Registered: {list(cls._registry.keys())}"
            )
        return cls._registry[engine_type]

    @classmethod
    def all_registered(cls) -> dict[EngineType, type[BaseEngine]]:
        """Return a copy of the full registry."""
        return cls._registry.copy()
