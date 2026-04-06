"""
exceptions.py — Domain-specific exception hierarchy.

Design principle: Every layer raises its own exception type.
The orchestrator catches them all, logs context, and stores
structured failure data rather than crashing the whole run.

Hierarchy:
  QABotError (base)
  ├── ConfigError
  ├── SessionError
  ├── EngineError
  │   ├── UIEngineError
  │   ├── APIEngineError
  │   └── PerformanceEngineError
  ├── AuthError
  ├── AIAnalysisError
  ├── ReportError
  └── StorageError
"""


class QABotError(Exception):
    """
    Base class for all QA Bot exceptions.
    Carries an optional context dict for structured logging.
    """

    def __init__(self, message: str, context: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict = context or {}

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.message!r}, context={self.context})"


# ── Configuration ─────────────────────────────────────────────────────────────

class ConfigError(QABotError):
    """Raised when config loading or validation fails."""


class MissingConfigError(ConfigError):
    """A required config key is absent."""


class InvalidConfigError(ConfigError):
    """A config value fails validation."""


# ── Session ───────────────────────────────────────────────────────────────────

class SessionError(QABotError):
    """Raised for test session lifecycle errors."""


class SessionTimeoutError(SessionError):
    """The full test session exceeded its wall-clock time limit."""


# ── Engines ───────────────────────────────────────────────────────────────────

class EngineError(QABotError):
    """Base class for all test engine errors."""


class UIEngineError(EngineError):
    """Raised by the Playwright UI engine."""


class PageLoadError(UIEngineError):
    """Page failed to load within the timeout."""

    def __init__(self, url: str, timeout_ms: int, original: Exception | None = None) -> None:
        super().__init__(
            f"Page '{url}' failed to load within {timeout_ms}ms",
            context={"url": url, "timeout_ms": timeout_ms},
        )
        self.original = original


class NavigationError(UIEngineError):
    """Navigation to a URL failed."""


class SelectorNotFoundError(UIEngineError):
    """
    An element could not be located after exhausting all selector strategies.
    This signals the self-healing selector engine to log and skip.
    """

    def __init__(self, strategies_tried: list[str], page_url: str) -> None:
        super().__init__(
            f"Element not found after trying: {strategies_tried}",
            context={"strategies": strategies_tried, "page_url": page_url},
        )
        self.strategies_tried = strategies_tried


class APIEngineError(EngineError):
    """Raised by the API test engine."""


class EndpointDiscoveryError(APIEngineError):
    """Could not discover any testable API endpoints."""


class PerformanceEngineError(EngineError):
    """Raised by the performance test engine."""


# ── Authentication ────────────────────────────────────────────────────────────

class AuthError(QABotError):
    """Raised when authentication setup or token acquisition fails."""


class LoginFailedError(AuthError):
    """Login attempt returned an unexpected response."""

    def __init__(self, login_url: str, status_code: int | None = None) -> None:
        super().__init__(
            f"Login failed at '{login_url}' (HTTP {status_code})",
            context={"login_url": login_url, "status_code": status_code},
        )


class TokenExpiredError(AuthError):
    """Cached auth token has expired and could not be refreshed."""


# ── AI Analysis ───────────────────────────────────────────────────────────────

class AIAnalysisError(QABotError):
    """Raised when the AI provider call fails or returns unparseable output."""


class AIResponseParseError(AIAnalysisError):
    """AI output did not match the required JSON schema."""

    def __init__(self, raw_response: str) -> None:
        super().__init__(
            "AI response could not be parsed into the required schema",
            context={"raw_response": raw_response[:500]},  # truncate for safety
        )
        self.raw_response = raw_response


class AIProviderUnavailableError(AIAnalysisError):
    """The configured AI provider API is unreachable."""


# ── Reporting ─────────────────────────────────────────────────────────────────

class ReportError(QABotError):
    """Raised when report generation fails."""


# ── Storage ───────────────────────────────────────────────────────────────────

class StorageError(QABotError):
    """Raised when database operations fail."""
