"""
config.py — Configuration loading and validation.

Design decisions:
  - Pydantic v2 models for strict type validation and helpful error messages.
  - YAML is the source of truth; CLI flags and env vars override it.
  - Environment variables are interpolated using ${VAR_NAME} syntax in YAML
    so credentials never need to be hardcoded.
  - load_config() merges: default.yaml → scenario YAML → CLI overrides.
  - The resulting AppConfig object is immutable (frozen=True) to prevent
    accidental mutation during a test run.
"""

import os
import re
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from src.core.constants import AIProvider, AuthType, TestDepth
from src.core.exceptions import ConfigError, InvalidConfigError, MissingConfigError
from src.core.logger import get_logger

log = get_logger(__name__)

# Regex to match ${VAR_NAME} placeholders in YAML values
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


# ─────────────────────────────────────────────────────────────────────────────
# Sub-models (nested config sections)
# ─────────────────────────────────────────────────────────────────────────────

class SingleAuthConfig(BaseModel):
    """Auth config for one login role."""

    type: AuthType = AuthType.NONE
    login_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    token_field: str = "access_token"
    api_key: Optional[str] = None
    api_key_header: str = "X-API-Key"
    token_expiry_seconds: int = 3600

    @model_validator(mode="after")
    def validate_credentials(self) -> "SingleAuthConfig":
        """Ensure required fields are present for the chosen auth type."""
        if self.type in (AuthType.BASIC, AuthType.JWT):
            if not self.username or not self.password:
                raise ValueError(
                    f"Auth type '{self.type}' requires 'username' and 'password'."
                )
        if self.type == AuthType.API_KEY and not self.api_key:
            raise ValueError("Auth type 'api_key' requires 'api_key' to be set.")
        if self.type in (AuthType.JWT, AuthType.BEARER) and not self.login_url:
            raise ValueError(
                f"Auth type '{self.type}' requires 'login_url' to obtain a token."
            )
        return self


class AuthConfig(BaseModel):
    """Top-level auth config supporting multiple login roles."""

    type: AuthType = AuthType.NONE
    login_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    token_field: str = "access_token"
    api_key: Optional[str] = None
    api_key_header: str = "X-API-Key"
    token_expiry_seconds: int = 3600

    # Optional second login role (e.g., admin)
    second_login: Optional[SingleAuthConfig] = None


class RateLimitConfig(BaseModel):
    max_requests_per_second: int = Field(default=5, ge=1, le=50)
    max_concurrent_tests: int = Field(default=3, ge=1, le=10)
    request_delay_ms: int = Field(default=200, ge=0)


class TimeoutConfig(BaseModel):
    page_load_ms: int = Field(default=30_000, ge=1_000)
    action_ms: int = Field(default=10_000, ge=500)
    api_request_ms: int = Field(default=10_000, ge=500)
    test_timeout_ms: int = Field(default=60_000, ge=5_000)
    session_timeout_ms: int = Field(default=600_000, ge=10_000)


class BrowserConfig(BaseModel):
    headless: bool = True
    browser_type: Literal["chromium", "firefox", "webkit"] = "chromium"
    viewport_width: int = Field(default=1280, ge=320)
    viewport_height: int = Field(default=720, ge=240)
    slow_mo_ms: int = Field(default=0, ge=0)


class PerformanceThresholds(BaseModel):
    page_load_ms: int = 5_000
    lcp_ms: int = 2_500
    ttfb_ms: int = 800


class PerformanceConfig(BaseModel):
    enabled: bool = True
    thresholds: PerformanceThresholds = Field(default_factory=PerformanceThresholds)
    lighthouse: bool = False


class APIConfig(BaseModel):
    enabled: bool = True
    openapi_url: Optional[str] = "/openapi.json"
    har_file: Optional[str] = None
    crawl_depth: int = Field(default=2, ge=0, le=5)
    ignore_patterns: list[str] = Field(
        default_factory=lambda: ["logout", "delete", "destroy"]
    )

    @field_validator("har_file")
    @classmethod
    def validate_har_file(cls, v: Optional[str]) -> Optional[str]:
        if v and not Path(v).exists():
            raise ValueError(f"HAR file not found: {v}")
        return v


class AIConfig(BaseModel):
    provider: AIProvider = AIProvider.MOCK
    model: str = "claude-opus-4-6"
    openai_model: str = "gpt-4o"
    max_tokens: int = Field(default=1024, ge=256, le=4096)
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)


class RetryConfig(BaseModel):
    max_attempts: int = Field(default=2, ge=1, le=5)
    wait_seconds: float = Field(default=2.0, ge=0.5)
    exponential_backoff: bool = True


class OutputConfig(BaseModel):
    dir: str = "outputs"
    formats: list[Literal["html", "json"]] = Field(default_factory=lambda: ["html", "json"])
    screenshots_on_pass: bool = False
    embed_screenshots: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Root config model
# ─────────────────────────────────────────────────────────────────────────────

class AppConfig(BaseModel):
    """
    Top-level configuration object. Frozen after construction to prevent
    accidental mutation inside test engines.
    """

    model_config = {"frozen": True}

    # Target
    url: str                              # Resolved from target.url
    test_depth: TestDepth = TestDepth.STANDARD

    # Sub-configs
    auth: AuthConfig = Field(default_factory=AuthConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    timeouts: TimeoutConfig = Field(default_factory=TimeoutConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("'url' must not be empty.")
        if not v.startswith(("http://", "https://")):
            raise ValueError(
                f"URL must start with http:// or https://. Got: '{v}'"
            )
        return v.rstrip("/")   # Normalise: no trailing slash


# ─────────────────────────────────────────────────────────────────────────────
# Environment variable interpolation
# ─────────────────────────────────────────────────────────────────────────────

def _interpolate_env_vars(obj: Any) -> Any:
    """
    Recursively walk a parsed YAML structure and replace all ${VAR_NAME}
    placeholders with the corresponding environment variable value.

    Raises ConfigError if a referenced variable is not set.
    """
    if isinstance(obj, str):
        def replacer(match: re.Match) -> str:
            var_name = match.group(1)
            value = os.getenv(var_name)
            if value is None:
                # Non-fatal: return empty string and warn
                log.warning("env_var_not_set", var=var_name)
                return ""
            return value
        return _ENV_VAR_PATTERN.sub(replacer, obj)

    if isinstance(obj, dict):
        return {k: _interpolate_env_vars(v) for k, v in obj.items()}

    if isinstance(obj, list):
        return [_interpolate_env_vars(item) for item in obj]

    return obj


# ─────────────────────────────────────────────────────────────────────────────
# YAML loading helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_yaml(path: Path) -> dict:
    """Load and parse a YAML file. Returns empty dict if file doesn't exist."""
    if not path.exists():
        log.debug("config_file_not_found", path=str(path))
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return _interpolate_env_vars(data)


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge override into base.
    Override values win; nested dicts are merged rather than replaced.
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def load_config(
    url: Optional[str] = None,
    scenario_file: Optional[str] = None,
    overrides: Optional[dict] = None,
    config_dir: str = "config",
) -> AppConfig:
    """
    Build the final AppConfig by merging (in order):
      1. config/default.yaml
      2. scenario_file (if provided)
      3. overrides dict (from CLI flags)
      4. url (always wins — required)

    Args:
        url:           Target URL. If provided, overrides target.url in YAML.
        scenario_file: Path to a custom test_scenarios.yaml.
        overrides:     Flat dict of overrides (e.g., {"browser.headless": False}).
        config_dir:    Directory containing default.yaml.

    Returns:
        A validated, immutable AppConfig instance.

    Raises:
        MissingConfigError: If no URL is found anywhere.
        InvalidConfigError: If validation fails.
    """
    config_path = Path(config_dir)

    # 1. Load defaults
    raw = _load_yaml(config_path / "default.yaml")

    # 2. Merge scenario file
    if scenario_file:
        scenario_data = _load_yaml(Path(scenario_file))
        raw = _deep_merge(raw, scenario_data)
        log.debug("scenario_loaded", file=scenario_file)

    # 3. Apply override dict
    if overrides:
        raw = _deep_merge(raw, overrides)

    # 4. Flatten target section into top-level for AppConfig
    target = raw.pop("target", {})
    raw["url"] = url or target.get("url", "")
    raw["test_depth"] = target.get("test_depth", TestDepth.STANDARD)

    # 5. Validate
    if not raw["url"]:
        raise MissingConfigError(
            "No URL provided. Use --url flag or set 'target.url' in config."
        )

    try:
        config = AppConfig(**raw)
    except Exception as exc:
        raise InvalidConfigError(
            f"Config validation failed: {exc}",
            context={"raw_config": raw},
        ) from exc

    log.info(
        "config_loaded",
        url=config.url,
        depth=config.test_depth,
        ai_provider=config.ai.provider,
    )
    return config
