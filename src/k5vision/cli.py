"""Installed K5 Vision process entry point."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from contextlib import nullcontext

import uvicorn

from k5vision import __version__
from k5vision.identity_state import IdentityStateError, identity_environment, load_identity_state
from k5vision.recovery_admin import DEFAULT_RECOVERY_USERNAME, setup_administrator
from k5vision.services.user_registry import UserRegistryConflictError, UserRegistryStorageError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
_LOG_LEVELS = ("critical", "error", "warning", "info", "debug", "trace")


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # Unsupported credential arguments must not be echoed into terminal logs.
        self.exit(2, "Invalid command arguments; use --help.\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
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
    serve.add_argument("--identity-dir", help="explicit durable local identity directory")
    serve.add_argument(
        "--operator",
        action="store_true",
        help="start the configured Stage-One operator application",
    )
    setup = subparsers.add_parser(
        "setup-admin", help="initialize a new durable local administrator"
    )
    setup.add_argument("--identity-dir", required=True, help="new private absolute directory")
    setup.add_argument("--username", default=DEFAULT_RECOVERY_USERNAME)
    subparsers.add_parser(
        "analytics-preflight", help="check selected analytics without starting the application"
    )
    return parser


def _analytics_preflight() -> int:
    """Expose only the fixed, camera-free configuration-admission outcome."""
    try:
        # Keep optional admission out of ordinary serving and administrator setup.
        # The loader checks installed package/model bytes without native creation.
        from k5vision.analytics_config import load_analytics_configuration

        enabled = load_analytics_configuration(os.environ) is not None
        status = "ready" if enabled else "disabled"
    except Exception:
        # Never disclose configuration values, paths, or dependency exceptions.
        enabled = False
        status = "refused"
    print(
        json.dumps(
            {"schema_version": "1", "analytics_enabled": enabled, "status": status},
            separators=(",", ":"),
        )
    )
    return 1 if status == "refused" else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the installed command without enabling any device action by itself."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "analytics-preflight":
        return _analytics_preflight()
    if args.command == "setup-admin":
        try:
            setup_administrator(args.identity_dir, username=args.username)
        except (
            ValueError,
            OSError,
            EOFError,
            Warning,
            UserRegistryConflictError,
            UserRegistryStorageError,
            IdentityStateError,
        ):
            print("Administrator setup refused or incomplete; existing state was not reset.")
            return 1
        except KeyboardInterrupt:
            print("Administrator setup cancelled; existing state was not reset.")
            return 130
        return 0
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    application = "k5vision.main:app"
    factory = False
    if args.operator:
        application = "k5vision.stage_one_app:create_stage_one_app"
        factory = True
    elif args.identity_dir is not None:
        application = "k5vision.main:create_app"
        factory = True

    try:
        scope = nullcontext()
        if args.identity_dir is not None:
            if args.host not in ("127.0.0.1", "::1", "localhost"):
                raise IdentityStateError("durable identity startup requires loopback")
            scope = identity_environment(load_identity_state(args.identity_dir), os.environ)
        with scope:
            uvicorn.run(
                application,
                factory=factory,
                host=args.host,
                port=args.port,
                log_level=args.log_level,
                workers=1,
            )
    except IdentityStateError:
        print("Durable identity startup refused; check the explicit local identity configuration.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
