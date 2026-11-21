"""Tests for the obr command-line entry point."""

import pytest

import openboardroom
from openboardroom.cli import main


def test_version_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    """`obr --version` prints the package version and exits 0."""
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == f"obr {openboardroom.__version__}\n"


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    """With no subcommand, `obr` prints usage and returns 0."""
    assert main([]) == 0
    assert "usage: obr" in capsys.readouterr().out
