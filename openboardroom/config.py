"""Environment-based configuration."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from openboardroom.extract.llm import LlmSettings

# SEC fair-access limit is 10 requests per second; the handbook caps us at or under it.
MAX_ALLOWED_RPS = 10.0
DEFAULT_RPS = "8"
DEFAULT_CACHE_DIR = "./cache"
DEFAULT_EXTRACT_MODEL = "claude-opus-5-5"
DEFAULT_EXTRACT_EFFORT = "high"
DEFAULT_EXTRACT_MAX_TOKENS = "64000"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required but not set")
    return value


def database_url_from_env(env: Mapping[str, str] = os.environ) -> str:
    """Read the Postgres URL alone, for commands that never call EDGAR.

    Args:
        env: Variable mapping to read; defaults to the process environment.

    Returns:
        The value of `OBR_DATABASE_URL`, stripped.

    Raises:
        ConfigError: If `OBR_DATABASE_URL` is missing or blank.
    """
    return _required(env, "OBR_DATABASE_URL")


def cache_dir_from_env(env: Mapping[str, str] = os.environ) -> Path:
    """Read the cache directory alone, for commands that never call EDGAR.

    Args:
        env: Variable mapping to read; defaults to the process environment.

    Returns:
        `OBR_CACHE_DIR`, or `./cache` if it is unset or blank.
    """
    # A blank value would become Path("."), caching filings at the repo root.
    return Path(env.get("OBR_CACHE_DIR", "").strip() or DEFAULT_CACHE_DIR)


def _parse_rps(raw: str) -> float:
    name = "OBR_EDGAR_MAX_RPS"
    try:
        rps = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if math.isnan(rps) or rps <= 0:
        raise ConfigError(f"{name} must be greater than 0, got {raw!r}")
    if rps > MAX_ALLOWED_RPS:
        raise ConfigError(f"{name} must be at most {MAX_ALLOWED_RPS:g}, got {raw!r}")
    return rps


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the pipeline.

    Attributes:
        database_url: Postgres connection URL.
        edgar_user_agent: User-Agent sent to EDGAR (organization and contact).
        edgar_max_rps: Maximum EDGAR requests per second (at most 10).
        cache_dir: Directory where raw filings are cached.
    """

    database_url: str
    edgar_user_agent: str
    edgar_max_rps: float
    cache_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        """Build settings from environment variables.

        Args:
            env: Variable mapping to read; defaults to the process environment.

        Returns:
            The validated settings.

        Raises:
            ConfigError: If a required variable is missing or a value is invalid.
        """
        return cls(
            database_url=database_url_from_env(env),
            edgar_user_agent=_required(env, "OBR_EDGAR_USER_AGENT"),
            edgar_max_rps=_parse_rps(env.get("OBR_EDGAR_MAX_RPS", DEFAULT_RPS)),
            cache_dir=cache_dir_from_env(env),
        )


def extract_settings_from_env(env: Mapping[str, str] = os.environ) -> LlmSettings:
    """Read the extraction model settings.

    Blank values count as unset. Needs no database or EDGAR settings, and the
    API key is not read here: the Anthropic SDK reads `ANTHROPIC_API_KEY`.

    Args:
        env: Variable mapping to read; defaults to the process environment.

    Returns:
        The model, effort level, and output token ceiling.

    Raises:
        ConfigError: If the effort isn't one of low, medium, high, xhigh, max,
            or the token ceiling isn't a positive integer.
    """
    model = env.get("OBR_EXTRACT_MODEL", "").strip() or DEFAULT_EXTRACT_MODEL
    effort = env.get("OBR_EXTRACT_EFFORT", "").strip() or DEFAULT_EXTRACT_EFFORT
    if effort not in EFFORT_LEVELS:
        raise ConfigError(
            f"OBR_EXTRACT_EFFORT must be one of {', '.join(EFFORT_LEVELS)}, got {effort!r}"
        )
    raw = env.get("OBR_EXTRACT_MAX_TOKENS", "").strip() or DEFAULT_EXTRACT_MAX_TOKENS
    try:
        max_tokens = int(raw)
    except ValueError:
        raise ConfigError(f"OBR_EXTRACT_MAX_TOKENS must be an integer, got {raw!r}") from None
    if max_tokens <= 0:
        raise ConfigError(f"OBR_EXTRACT_MAX_TOKENS must be greater than 0, got {raw!r}")
    return LlmSettings(model=model, effort=effort, max_tokens=max_tokens)
