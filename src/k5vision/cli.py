"""Installed K5 Vision process entry point."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn

from k5vision import __version__

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
_LOG_LEVELS = ("critical", "error", "warning", "info", "debug", "trace")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="k5-vision",
        description="Run the installed K5 Vision control plane.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")
    serve = subparsers.add_parser("serve", help="start the local control-plane service")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--log-level", choices=_LOG_LEVELS, default="info")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the installed command without enabling any device action by itself."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    uvicorn.run(
        "k5vision.main:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        workers=1,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
