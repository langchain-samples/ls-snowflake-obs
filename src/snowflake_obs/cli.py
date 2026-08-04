"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from snowflake_obs.bridge import BridgeOptions, run
from snowflake_obs.config import ConfigError, load_settings

DEFAULT_POLL_INTERVAL = 30


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snowflake-obs",
        description="Snowflake AI Observability → LangSmith OTEL bridge",
    )
    parser.add_argument(
        "--interval",
        type=_positive_int,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Poll interval in seconds (default: {DEFAULT_POLL_INTERVAL})",
    )
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Dump failed-batch payload/response to a private temp directory",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[bridge] Configuration error: {exc}", file=sys.stderr)
        return 2

    return run(
        settings,
        BridgeOptions(poll_interval=args.interval, once=args.once, debug=args.debug),
    )


if __name__ == "__main__":
    raise SystemExit(main())
