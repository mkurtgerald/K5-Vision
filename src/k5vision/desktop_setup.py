"""Bounded, pipe-only account setup for the native desktop supervisor.

The graphical owner supplies credentials only through the child's redirected
standard input. No credential argument, environment value, file, or exception
text is accepted or returned by this interface. Existing identity admission and
exclusive creation remain authoritative; a partial installation is never reset.
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import BinaryIO, TextIO

from k5vision.domain.users import UserCreate, UserRole
from k5vision.identity_state import (
    initialize_identity_state,
    load_identity_state,
    validate_new_identity_directory,
)

MAX_REQUEST_BYTES = 32768
_COMMON_FIELDS = {"schema_version", "operation", "identity_directory"}
_INITIALIZE_FIELDS = _COMMON_FIELDS | {"username", "password", "confirmation"}
_SERVE_FIELDS = _COMMON_FIELDS | {
    "recording_root",
    "media_executable",
    "gateway_executable",
    "port",
    "recording_limit_gb",
}


class _InvalidRequest(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidRequest()
        result[key] = value
    return result


def _response(status: str, reason: str) -> dict[str, object]:
    return {"schema_version": 1, "status": status, "reason": reason}


def _request(payload: bytes) -> dict[str, object]:
    if type(payload) is not bytes or not 1 <= len(payload) <= MAX_REQUEST_BYTES:
        raise _InvalidRequest()
    request = json.loads(payload.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object)
    if (
        type(request) is not dict
        or type(request.get("schema_version")) is not int
        or request["schema_version"] != 1
        or request.get("operation") not in ("status", "initialize", "serve")
    ):
        raise _InvalidRequest()
    expected = {
        "initialize": _INITIALIZE_FIELDS,
        "status": _COMMON_FIELDS,
        "serve": _SERVE_FIELDS,
    }[request["operation"]]
    if set(request) != expected:
        raise _InvalidRequest()
    directory = request["identity_directory"]
    if type(directory) is not str or not 1 <= len(directory) <= 4096 or "\0" in directory:
        raise _InvalidRequest()
    directory.encode("utf-8", errors="strict")
    if request["operation"] == "serve":
        for field in (
            "identity_directory",
            "recording_root",
            "media_executable",
            "gateway_executable",
        ):
            path = request[field]
            if (
                type(path) is not str
                or not 1 <= len(path) <= 4096
                or "\0" in path
                or not Path(path).is_absolute()
            ):
                raise _InvalidRequest()
            path.encode("utf-8", errors="strict")
        for field, maximum in (("port", 65535), ("recording_limit_gb", 4096)):
            if type(request[field]) is not int or not 1 <= request[field] <= maximum:
                raise _InvalidRequest()
    return request


def process_request(payload: bytes) -> dict[str, object]:
    """Consume one bounded request and return only a finite public outcome."""
    try:
        request = _request(payload)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return _response("refused", "invalid_request")

    directory = request["identity_directory"]
    if request["operation"] == "serve":
        # This operation needs the pipe lifetime and is handled only by main.
        return _response("refused", "invalid_request")
    if request["operation"] == "status":
        try:
            load_identity_state(directory)
            return _response("ready", "existing_identity")
        except Exception:
            try:
                validate_new_identity_directory(directory)
                return _response("new", "account_required")
            except Exception:
                return _response("refused", "unsafe_state")

    username = request["username"]
    try:
        if type(username) is not str:
            raise ValueError()
        account = UserCreate(
            username=username,
            display_name="Recovery administrator",
            role=UserRole.ADMINISTRATOR,
        )
        if account.username != username:
            raise ValueError()
    except (ValueError, TypeError):
        return _response("refused", "invalid_username")

    password, confirmation = request["password"], request["confirmation"]
    try:
        if (
            type(password) is not str
            or type(confirmation) is not str
            or not 12 <= len(password) <= 256
            or not 12 <= len(confirmation) <= 256
        ):
            raise ValueError()
        password_bytes = password.encode("utf-8", errors="strict")
        confirmation_bytes = confirmation.encode("utf-8", errors="strict")
    except (ValueError, UnicodeError):
        return _response("refused", "invalid_password")
    if not secrets.compare_digest(password_bytes, confirmation_bytes):
        return _response("refused", "password_mismatch")

    try:
        # Recheck custody immediately before the existing exclusive creator.
        validate_new_identity_directory(directory)
    except Exception:
        return _response("refused", "unsafe_state")
    try:
        initialize_identity_state(directory, username=username, password=password)
        return _response("ready", "account_initialized")
    except Exception:
        # Preserve every partial artifact for diagnosis. Never echo its contents
        # or a dependency error, and never retry by deleting existing state.
        return _response("refused", "setup_failed")


class _DiscardOutput:
    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        # Uvicorn's default formatter probes the redirected stdout stream when
        # deciding whether to color its output, even when logs are suppressed.
        return False


def _watch_control_pipe(source: BinaryIO, stop: threading.Event) -> None:
    try:
        # EOF, an explicit stop, or invalid control input all end owner lifetime.
        # Read no further data and never log a supplied control value.
        source.readline(17)
    except Exception:
        pass
    finally:
        stop.set()


def _serve(
    request: dict[str, object], source: BinaryIO, destination: TextIO | None
) -> dict[str, object]:
    try:
        # Validate the existing account before creating any owned native worker.
        load_identity_state(request["identity_directory"])
    except Exception:
        return _response("refused", "unsafe_state")
    stop = threading.Event()

    def ready():
        if destination is None:
            raise ValueError()
        destination.write('{"schema_version":1,"status":"running"}\n')
        destination.flush()

    thread = threading.Thread(
        target=_watch_control_pipe,
        args=(source, stop),
        daemon=True,
        name="operator-desktop-control-pipe",
    )
    try:
        thread.start()
        sink = _DiscardOutput()
        # Ordinary console prints and dependency logs do not become GUI errors.
        # The supervisor receives only the fixed completion envelope below.
        with redirect_stdout(sink), redirect_stderr(sink):
            from k5vision.operator_product import run_operator_product

            run_operator_product(
                identity_directory=Path(request["identity_directory"]),
                recording_root=Path(request["recording_root"]),
                media_executable=Path(request["media_executable"]),
                gateway_executable=Path(request["gateway_executable"]),
                port=request["port"],
                recording_limit_gb=request["recording_limit_gb"],
                open_browser=False,
                shutdown_requested=stop.is_set,
                ready_callback=ready,
            )
        return _response("stopped", "operator_stopped")
    except Exception:
        return _response("refused", "startup_failed")
    finally:
        stop.set()


def main(
    argv: Sequence[str] | None = None,
    *,
    input_stream: BinaryIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    """Read a JSON line from a pipe; serving ends on the next line or EOF."""
    arguments = sys.argv[1:] if argv is None else argv
    source = input_stream if input_stream is not None else getattr(sys.stdin, "buffer", None)
    destination = output_stream if output_stream is not None else sys.stdout
    result = _response("refused", "invalid_request")
    try:
        if arguments or source is None or source.isatty():
            raise _InvalidRequest()
        payload = source.readline(MAX_REQUEST_BYTES + 1)
        request = _request(payload)
        if request["operation"] == "serve":
            result = _serve(request, source, destination)
        elif source.read(1):
            result = _response("refused", "invalid_request")
        else:
            result = process_request(payload)
    except Exception:
        result = _response("refused", "invalid_request")
    try:
        if destination is None:
            return 1
        destination.write(json.dumps(result, separators=(",", ":")) + "\n")
        destination.flush()
    except Exception:
        return 1
    return 0 if result["status"] in ("ready", "new", "stopped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
