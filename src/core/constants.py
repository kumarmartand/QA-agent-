"""
constants.py — All enums and shared constant values.

Design decision: Using Python Enum (not plain strings) so that:
  - IDEs give autocomplete
  - Typos are caught at import time
  - Values are serialised consistently to lowercase strings in JSON/DB
"""

from enum import Enum


# ─────────────────────────────────────────────────────────────────────────────
# Test Result Status
# ─────────────────────────────────────────────────────────────────────────────

class TestStatus(str, Enum):
    """Outcome of a single test case execution."""
    PASS    = "pass"
    FAIL    = "fail"
    SKIP    = "skip"
    ERROR   = "error"     # Unexpected exception (not a test assertion failure)
    TIMEOUT = "timeout"


# ─────────────────────────────────────────────────────────────────────────────
# Engine Types
# ─────────────────────────────────────────────────────────────────────────────

class EngineType(str, Enum):
    """Which test engine produced the result."""
    UI          = "ui"
    API         = "api"
    PERFORMANCE = "performance"


# ─────────────────────────────────────────────────────────────────────────────
# Bug Severity — matches the strict AI output schema
# ─────────────────────────────────────────────────────────────────────────────

class Severity(str, Enum):
    """Bug severity classification used by the AI analysis layer."""
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"

    @property
    def priority_score(self) -> int:
        """Numeric priority for sorting (higher = more urgent)."""
        return {"low": 1, "medium": 2, "high": 3, "critical": 4}[self.value]

    @property
    def emoji(self) -> str:
        """For report rendering."""
        return {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}[self.value]


# ─────────────────────────────────────────────────────────────────────────────
# Bug Category — matches the strict AI output schema
# ─────────────────────────────────────────────────────────────────────────────

class BugCategory(str, Enum):
    """Domain classification of a detected bug."""
    UI       = "ui"
    API      = "api"
    PERF     = "perf"
    AUTH     = "auth"
    SECURITY = "security"


# ─────────────────────────────────────────────────────────────────────────────
# Auth Types
# ─────────────────────────────────────────────────────────────────────────────

class AuthType(str, Enum):
    NONE    = "none"
    BASIC   = "basic"
    JWT     = "jwt"
    API_KEY = "api_key"
    BEARER  = "bearer"


# ─────────────────────────────────────────────────────────────────────────────
# Test Depth — controls how many tests are run
# ─────────────────────────────────────────────────────────────────────────────

class TestDepth(str, Enum):
    """
    light    → Page load + basic nav only. Fast (~30s)
    standard → + buttons, forms, console errors. Medium (~2min)
    full     → + auth flows, API scan, performance. Thorough (~5min+)
    """
    LIGHT    = "light"
    STANDARD = "standard"
    FULL     = "full"


# ─────────────────────────────────────────────────────────────────────────────
# Session Status
# ─────────────────────────────────────────────────────────────────────────────

class SessionStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    CANCELLED = "cancelled"


# ─────────────────────────────────────────────────────────────────────────────
# AI Provider
# ─────────────────────────────────────────────────────────────────────────────

class AIProvider(str, Enum):
    OPENAI = "openai"
    CLAUDE = "claude"
    MOCK   = "mock"     # Offline mode — no API key required


# ─────────────────────────────────────────────────────────────────────────────
# Report Formats
# ─────────────────────────────────────────────────────────────────────────────

class ReportFormat(str, Enum):
    HTML = "html"
    JSON = "json"


# ─────────────────────────────────────────────────────────────────────────────
# Shared constants
# ─────────────────────────────────────────────────────────────────────────────

# Selector fallback order for self-healing (tried left to right)
SELECTOR_PRIORITY = [
    "data-testid",
    "aria-label",
    "role",
    "text",
    "css",
]

# Console message types that count as errors
CONSOLE_ERROR_TYPES = {"error", "warning"}

# HTTP status codes considered "healthy"
HEALTHY_STATUS_CODES = set(range(200, 400))

# Max characters kept from a stack trace in the DB (prevent bloat)
MAX_STACK_TRACE_LENGTH = 4000

# Screenshot JPEG quality (0-100)
SCREENSHOT_QUALITY = 85
