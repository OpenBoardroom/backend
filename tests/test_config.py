"""Tests for environment configuration."""

from pathlib import Path

import pytest

from openboardroom.config import ConfigError, Settings, database_url_from_env

VALID = {
    "OBR_DATABASE_URL": "postgresql://u:p@localhost/db",
    "OBR_EDGAR_USER_AGENT": "Org Name contact@example.org",
}


def test_missing_user_agent_raises() -> None:
    with pytest.raises(ConfigError, match="OBR_EDGAR_USER_AGENT"):
        Settings.from_env({"OBR_DATABASE_URL": VALID["OBR_DATABASE_URL"]})


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_user_agent_counts_as_missing(value: str) -> None:
    with pytest.raises(ConfigError, match="OBR_EDGAR_USER_AGENT"):
        Settings.from_env({**VALID, "OBR_EDGAR_USER_AGENT": value})


def test_missing_database_url_raises() -> None:
    with pytest.raises(ConfigError, match="OBR_DATABASE_URL"):
        Settings.from_env({"OBR_EDGAR_USER_AGENT": VALID["OBR_EDGAR_USER_AGENT"]})


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_database_url_counts_as_missing(value: str) -> None:
    with pytest.raises(ConfigError, match="OBR_DATABASE_URL"):
        Settings.from_env({**VALID, "OBR_DATABASE_URL": value})


def test_rps_above_ten_raises() -> None:
    with pytest.raises(ConfigError, match="OBR_EDGAR_MAX_RPS"):
        Settings.from_env({**VALID, "OBR_EDGAR_MAX_RPS": "10.5"})


@pytest.mark.parametrize("value", ["fast", "0", "-1", "nan", "inf"])
def test_rps_not_positive_number_raises(value: str) -> None:
    with pytest.raises(ConfigError, match="OBR_EDGAR_MAX_RPS"):
        Settings.from_env({**VALID, "OBR_EDGAR_MAX_RPS": value})


def test_rps_ten_is_allowed() -> None:
    assert Settings.from_env({**VALID, "OBR_EDGAR_MAX_RPS": "10"}).edgar_max_rps == 10.0


def test_defaults() -> None:
    settings = Settings.from_env(VALID)
    assert settings.edgar_max_rps == 8.0
    assert settings.cache_dir == Path("cache")
    assert settings.database_url == VALID["OBR_DATABASE_URL"]
    assert settings.edgar_user_agent == VALID["OBR_EDGAR_USER_AGENT"]


def test_cache_dir_override() -> None:
    settings = Settings.from_env({**VALID, "OBR_CACHE_DIR": "/tmp/obr"})
    assert settings.cache_dir == Path("/tmp/obr")


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_cache_dir_uses_default(value: str) -> None:
    settings = Settings.from_env({**VALID, "OBR_CACHE_DIR": value})
    assert settings.cache_dir == Path("cache")


def test_from_env_reads_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in VALID.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("OBR_EDGAR_MAX_RPS", raising=False)
    monkeypatch.delenv("OBR_CACHE_DIR", raising=False)
    assert Settings.from_env().database_url == VALID["OBR_DATABASE_URL"]


def test_database_url_from_env_needs_only_database_url() -> None:
    assert database_url_from_env({"OBR_DATABASE_URL": " postgresql://x "}) == "postgresql://x"
    with pytest.raises(ConfigError, match="OBR_DATABASE_URL"):
        database_url_from_env({"OBR_DATABASE_URL": "  "})
