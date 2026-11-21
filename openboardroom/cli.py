"""Command-line entry point for the `obr` tool."""

import argparse

from openboardroom import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser.

    Later tasks add subcommands by calling their own `register(subparsers)`
    functions on the object returned by `add_subparsers` here.

    Returns:
        The configured argument parser.
    """
    parser = argparse.ArgumentParser(prog="obr", description="OpenBoardroom backend tools.")
    parser.add_argument("--version", action="version", version=f"obr {__version__}")
    parser.add_subparsers(dest="command", metavar="COMMAND")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the `obr` command.

    Args:
        argv: Arguments to parse; defaults to `sys.argv[1:]`.

    Returns:
        The process exit code.
    """
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
