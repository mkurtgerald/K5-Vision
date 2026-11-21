"""Fixed-path, service-token bridge for the unattended Windows recorder.

Only the sealed native service host starts this module. The host supplies one
bounded request on private stdin; setup credentials never enter command lines,
environment variables or files. All profile, worker and listening selections
are derived here rather than accepted from a client request.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TextIO
from uuid import UUID

from k5vision import desktop_setup
from k5vision.windows_identity_security import _native_security

SERVICE_ACCOUNT = "NT SERVICE\\K5VisionRecorder"
_PROGRAM_DATA = UUID("62ab5d82-fdc1-4dc3-a9dd-070d1d495d97")
_COMMON_FIELDS = {"schema_version", "operation"}
_INITIALIZE_FIELDS = _COMMON_FIELDS | {"username", "password", "confirmation"}


@dataclass(frozen=True)
class _Layout:
    root: Path
    application: Path


def _require_service_token() -> None:
    """Compare the real TokenUser to the resolved, exact virtual account SID."""
    if os.name != "nt":
        raise ValueError
    native = _native_security()
    sid_size, domain_size, use = ctypes.c_uint32(68), ctypes.c_uint32(256), ctypes.c_uint32()
    sid, domain = ctypes.create_string_buffer(68), ctypes.create_unicode_buffer(256)
    if not native.security.LookupAccountNameW(
        None,
        SERVICE_ACCOUNT,
        sid,
        ctypes.byref(sid_size),
        domain,
        ctypes.byref(domain_size),
        ctypes.byref(use),
    ):
        raise ValueError
    if native.current_user() != native.sid(ctypes.addressof(sid)):
        raise ValueError


def is_recorder_account() -> bool:
    """Read-only predicate for the exact virtual recorder TokenUser.

    A service SID present only as a group grants nothing. Failed native lookup
    and unsupported platforms return false without exposing native details.
    """
    try:
        _require_service_token()
        return True
    except Exception:
        return False


def _program_data() -> Path:
    """Use the Windows known folder, never a caller-controlled environment path."""
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell.SHGetKnownFolderPath.restype = ctypes.c_int32
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    identifier = ctypes.create_string_buffer(_PROGRAM_DATA.bytes_le)
    result = ctypes.c_void_p()
    try:
        if shell.SHGetKnownFolderPath(identifier, 0, None, ctypes.byref(result)) != 0:
            raise ValueError
        if not result.value:
            raise ValueError
        path = Path(ctypes.wstring_at(result))
        if not path.is_absolute():
            raise ValueError
        return path
    finally:
        if result.value:
            ole.CoTaskMemFree(result)


def _layout() -> _Layout:
    _require_service_token()
    executable = Path(sys.executable)
    if executable.name.casefold() != "python.exe" or executable.parent.name != "runtime":
        raise ValueError
    return _Layout(_program_data() / "K5Vision", executable.parent.parent)


def _request(payload: bytes) -> dict[str, object]:
    if type(payload) is not bytes or not 1 <= len(payload) <= desktop_setup.MAX_REQUEST_BYTES:
        raise ValueError
    request = json.loads(
        payload.decode("utf-8", errors="strict"), object_pairs_hook=desktop_setup._unique_object
    )
    if (
        type(request) is not dict
        or type(request.get("schema_version")) is not int
        or request["schema_version"] != 1
        or request.get("operation") not in ("status", "initialize", "serve")
        or set(request)
        != (_INITIALIZE_FIELDS if request["operation"] == "initialize" else _COMMON_FIELDS)
    ):
        raise ValueError
    return request


def _fixed_request(request: dict[str, object], layout: _Layout) -> dict[str, object]:
    result = {**request, "identity_directory": str(layout.root / "identity")}
    if request["operation"] == "serve":
        result.update(
            recording_root=str(layout.root / "recordings"),
            media_executable=str(layout.application / "workers" / "mediamtx.exe"),
            gateway_executable=str(layout.application / "workers" / "caddy.exe"),
            port=8000,
            recording_limit_gb=20,
        )
    # Reuse all existing validation, including fixed absolute serve paths.
    return desktop_setup._request(json.dumps(result, ensure_ascii=True).encode("ascii"))


def main(
    argv: Sequence[str] | None = None,
    *,
    input_stream: BinaryIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    """One finite setup response, or serving until the service closes stdin."""
    arguments = sys.argv[1:] if argv is None else argv
    source = input_stream if input_stream is not None else getattr(sys.stdin, "buffer", None)
    destination = output_stream if output_stream is not None else sys.stdout
    result = desktop_setup._response("refused", "invalid_request")
    try:
        if arguments or source is None or source.isatty():
            raise ValueError
        request = _request(source.readline(desktop_setup.MAX_REQUEST_BYTES + 1))
        if request["operation"] != "serve" and source.read(1):
            raise ValueError
        try:
            layout = _layout()
        except Exception:
            result = desktop_setup._response("refused", "service_identity_unavailable")
        else:
            request = _fixed_request(request, layout)
            if request["operation"] == "serve":
                result = desktop_setup._serve(request, source, destination)
            else:
                result = desktop_setup.process_request(json.dumps(request).encode("utf-8"))
    except Exception:
        result = desktop_setup._response("refused", "invalid_request")
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
