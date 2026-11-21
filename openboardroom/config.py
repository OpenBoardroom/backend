"""Environment-based configuration."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# SEC fair-access limit is 10 requests per second; the handbook caps us at or under it.
MAX_ALLOWED_RPS = 10.0
DEFAULT_RPS = "8"
DEFAULT_CACHE_DIR = "./cache"


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required but not set")
    return value


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
            database_url=_required(env, "OBR_DATABASE_URL"),
            edgar_user_agent=_required(env, "OBR_EDGAR_USER_AGENT"),
            edgar_max_rps=_parse_rps(env.get("OBR_EDGAR_MAX_RPS", DEFAULT_RPS)),
            # A blank value would become Path("."), caching filings at the repo root.
            cache_dir=Path(env.get("OBR_CACHE_DIR", "").strip() or DEFAULT_CACHE_DIR),
        )
